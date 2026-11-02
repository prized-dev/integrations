// The ssh side. A sandbox shell opens with the user's own `prized login`: `prized sandbox ssh ID`
// mints a short-lived certificate and writes the files ssh needs for that sandbox (its ssh_config,
// cert.pub and known_hosts) under ~/.config/prized/sandboxes/ID. Orca dials `USER@ID` with system
// ssh, which reads ~/.ssh/config, so the plugin keeps one include file there with a block per
// sandbox:
//
//   Match originalhost ID exec "'…/prized-orca' cert-hook ID"   renew the certificate before ssh loads it
//   Host ID
//       ProxyCommand '…/prized-orca' proxy ID                    refuse while asleep, wake an idle pause
//   Include "…/.config/prized/sandboxes/ID/ssh_config"         everything else, as the CLI wrote it
//
// ~/.ssh/config gets one line, at the top: Include "~/.prized-orca/ssh_config" (setup adds it, with
// the user's OK, and uninstall takes it out).

import fs from "node:fs";
import path from "node:path";
import { UserError, checkPlainPath, isSandboxId, paths, sq, userSshConfig, writeFileAtomic } from "./util.mjs";
import { readState, withLock } from "./state.mjs";

export const INCLUDE_MARK = "# Added by the Prized Orca plugin (prized-orca setup): Orca workspaces on Prized sandboxes.";

export function proxyCommandFor(adapter, id) {
  if (!isSandboxId(id)) throw new UserError(`not a sandbox id: ${id}`);
  return `${sq(checkPlainPath(adapter, "the adapter path"))} proxy ${id}`;
}

export function certHookFor(adapter, id) {
  if (!isSandboxId(id)) throw new UserError(`not a sandbox id: ${id}`);
  return `${sq(checkPlainPath(adapter, "the adapter path"))} cert-hook ${id}`;
}

export function renderInclude(state, adapter) {
  const lines = [
    "# Managed by the Prized Orca plugin (prized-orca): rewritten whole on every create, resume and destroy.",
    "# One block per Prized sandbox an Orca workspace runs on. `prized-orca uninstall` removes it.",
  ];
  for (const id of Object.keys(state.sandboxes).sort()) {
    const entry = state.sandboxes[id];
    if (!isSandboxId(id) || !entry?.cliConfig) continue;
    lines.push(
      "",
      `Match originalhost ${id} exec "${certHookFor(adapter, id)}"`,
      `Host ${id}`,
      `    ProxyCommand ${proxyCommandFor(adapter, id)}`,
      `Include "${checkPlainPath(entry.cliConfig, "the prized CLI's ssh_config")}"`,
    );
  }
  return `${lines.join("\n")}\n`;
}

// Rewrite the include file from the state, under the state lock so two writers can't interleave.
export function writeInclude(adapter, env = process.env) {
  const p = paths(env);
  return withLock(p.lock, () => {
    const text = renderInclude(readState(env), adapter);
    writeFileAtomic(p.sshInclude, text);
    return text;
  });
}

// ---------------------------------------------------------------- ~/.ssh/config

export function includeLine(env = process.env) {
  return `Include "${checkPlainPath(paths(env).sshInclude, "the plugin's ssh include")}"`;
}

export function hasUserInclude(env = process.env) {
  let text;
  try {
    text = fs.readFileSync(userSshConfig(env), "utf8");
  } catch {
    return false;
  }
  const want = includeLine(env);
  return text.split(/\r?\n/).some((line) => line.trim() === want);
}

// Put the Include line first: OpenSSH applies the first value it reads for an option, and an
// Include after a Host block would only apply inside that block.
export function addUserInclude(env = process.env) {
  if (hasUserInclude(env)) return "already there";
  const file = userSshConfig(env);
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  let old = "";
  let mode = 0o600;
  try {
    old = fs.readFileSync(file, "utf8");
    mode = fs.statSync(file).mode & 0o777;
    const backup = `${file}.prized-orca.bak`;
    if (!fs.existsSync(backup)) fs.writeFileSync(backup, old, { mode });
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  writeFileAtomic(file, `${INCLUDE_MARK}\n${includeLine(env)}\n\n${old}`, mode);
  return `added to ${file}`;
}

export function removeUserInclude(env = process.env) {
  const file = userSshConfig(env);
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return "no ~/.ssh/config";
  }
  const want = includeLine(env);
  const lines = text.split("\n");
  const kept = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].replace(/\r$/, "");
    if (line.trim() === INCLUDE_MARK || line.trim() === want) {
      // also drop the blank line addUserInclude put after it
      if (line.trim() === want && lines[i + 1] === "") i++;
      continue;
    }
    kept.push(lines[i]);
  }
  if (kept.length === lines.length) return "not there";
  writeFileAtomic(file, kept.join("\n"), fs.statSync(file).mode & 0o777);
  return `removed from ${file}`;
}

// ---------------------------------------------------------------- the prized CLI's files

// The first directory holding the CLI's ssh_config for this sandbox.
export function findCliConfig(id, dirs) {
  for (const dir of dirs) {
    const file = path.join(dir, "ssh_config");
    if (fs.existsSync(file)) return file;
  }
  return null;
}

// The options of the CLI's per-sandbox ssh_config the plugin needs. ssh_config doubles a literal %.
export function parseCliConfig(text) {
  const out = {};
  for (const raw of String(text).split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const m = /^(\S+)\s+(.*)$/.exec(line);
    if (!m) continue;
    const key = m[1].toLowerCase();
    const value = m[2].trim();
    if (key === "user") out.user ??= value;
    else if (key === "proxycommand") out.proxyCommand ??= value.replace(/%%/g, "%");
    else if (key === "certificatefile") out.certificateFile ??= value.replace(/%%/g, "%");
    else if (key === "hostkeyalias") out.hostKeyAlias ??= value;
  }
  return out;
}

export function readCliConfig(file) {
  const cfg = parseCliConfig(fs.readFileSync(file, "utf8"));
  if (!cfg.proxyCommand || !cfg.user) throw new UserError(`${file} has no ProxyCommand or User: is the prized CLI current?`);
  return cfg;
}

// ---------------------------------------------------------------- certificates

function reader(buf) {
  let at = 0;
  const need = (n) => {
    if (at + n > buf.length) throw new Error("short certificate");
  };
  return {
    u32() {
      need(4);
      const v = buf.readUInt32BE(at);
      at += 4;
      return v;
    },
    u64() {
      need(8);
      const v = buf.readBigUInt64BE(at);
      at += 8;
      return v;
    },
    bytes() {
      const n = this.u32();
      need(n);
      const v = buf.subarray(at, at + n);
      at += n;
      return v;
    },
  };
}

// When an ed25519 user certificate line (as `prized sandbox ssh` writes cert.pub) stops being valid,
// or null when the line can't be read.
export function certValidBefore(line) {
  try {
    const [type, b64] = String(line).trim().split(/\s+/);
    if (type !== "ssh-ed25519-cert-v01@openssh.com" || !b64) return null;
    const r = reader(Buffer.from(b64, "base64"));
    if (r.bytes().toString() !== type) return null;
    r.bytes(); // nonce
    r.bytes(); // public key
    r.u64(); // serial
    r.u32(); // type
    r.bytes(); // key id
    r.bytes(); // principals
    r.u64(); // valid after
    const before = r.u64();
    if (before >= BigInt(Number.MAX_SAFE_INTEGER)) return new Date(8.64e15);
    return new Date(Number(before) * 1000);
  } catch {
    return null;
  }
}
