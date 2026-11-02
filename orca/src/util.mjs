// Shared pieces: where the plugin keeps its files, errors, quoting and the lifecycle log.
//
// Secrets: PRIZED_API_KEY lives in memory only. It is never written to disk, printed, logged or put
// on a command line; error text passes through redact() before anyone sees it.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const VERSION = "0.1.0";
export const DOCS_URL = "https://prized.dev/docs/orca?via=orca";
export const INSTALL_CLI_URL = "https://prized.dev/docs/quickstart?via=orca";
export const SETUP_COMMAND = `npx -y github:prized-dev/orca-prized-plugin#v${VERSION} setup`;

export class UserError extends Error {
  constructor(message, hint = "") {
    super(message);
    this.name = "UserError";
    this.hint = hint;
  }
}

// ---------------------------------------------------------------- paths

// The plugin's own directory: config, state, the managed ssh include, the installed adapter.
export function appHome(env = process.env) {
  return env.PRIZED_ORCA_HOME || path.join(os.homedir(), ".prized-orca");
}

export const paths = (env = process.env) => {
  const home = appHome(env);
  return {
    home,
    config: path.join(home, "config.json"),
    state: path.join(home, "state.json"),
    lock: path.join(home, "state.lock"),
    sshInclude: path.join(home, "ssh_config"),
    log: path.join(home, "lifecycle.log"),
    app: path.join(home, "app"),
    bin: path.join(home, "bin", "prized-orca"),
  };
};

// The ssh config OpenSSH reads: the one in the passwd entry's home, which is where ssh looks even
// when HOME points elsewhere. PRIZED_ORCA_SSH_CONFIG names another file (tests), and ssh is then
// run with -F so it reads that one.
export function userSshConfig(env = process.env) {
  if (env.PRIZED_ORCA_SSH_CONFIG) return env.PRIZED_ORCA_SSH_CONFIG;
  let home;
  try {
    home = os.userInfo().homedir;
  } catch {
    home = os.homedir();
  }
  return path.join(home, ".ssh", "config");
}

// The prized CLI's per-sandbox ssh files (`prized sandbox ssh` writes them): the config directory is
// ~/.config/prized, or ~/.config/doppel on an install from before the rename.
export function cliSandboxDirs(id, env = process.env) {
  const home = os.homedir();
  return ["prized", "doppel"].map((family) => path.join(home, ".config", family, "sandboxes", id));
}

// ---------------------------------------------------------------- checks and quoting

const SANDBOX_ID = /^sbx_[A-Za-z0-9]{1,64}$/;

export function isSandboxId(id) {
  return typeof id === "string" && SANDBOX_ID.test(id);
}

export function sq(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

// A path that goes into ssh_config, a ProxyCommand and a Match exec line: those are read by three
// parsers (OpenSSH's, sh and ours), so anything any of them would treat specially is refused.
export function checkPlainPath(p, what) {
  if (!path.isAbsolute(p) || /["'\\\r\n%$`*?[\]]/.test(p)) {
    throw new UserError(
      `${what} (${p}) holds a character ssh_config can't carry (a quote, backslash, %, $, glob or line break)`,
      "the plugin lives in ~/.prized-orca, so it can't run from a home directory whose path has one of these",
    );
  }
  return p;
}

// ---------------------------------------------------------------- secrets

const KEYS = new Set();

export function rememberSecret(value) {
  if (value) KEYS.add(value);
}

export function redact(text) {
  let out = String(text ?? "");
  for (const key of KEYS) out = out.split(key).join("[key]");
  return out.replace(/dcp_[A-Za-z0-9]{8,}/g, "dcp_[key]");
}

// ---------------------------------------------------------------- logging

// stderr only: stdout carries the recipe result Orca parses, and nothing else.
export function say(...parts) {
  process.stderr.write(`[prized-orca] ${redact(parts.join(" "))}\n`);
}

// One JSON line per lifecycle call in lifecycle.log, for seeing what Orca ran. Ids, timings and
// outcomes only.
export function trace(fields, env = process.env) {
  try {
    const p = paths(env);
    fs.mkdirSync(p.home, { recursive: true, mode: 0o700 });
    const line = JSON.stringify({ t: new Date().toISOString(), pid: process.pid, ...fields });
    fs.appendFileSync(p.log, `${redact(line)}\n`, { mode: 0o600 });
  } catch {
    // the log is a convenience, never a reason to fail
  }
}

export function writeFileAtomic(file, text, mode = 0o600) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, text, { mode });
  fs.chmodSync(tmp, mode);
  fs.renameSync(tmp, file);
}

export function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return fallback;
    throw new UserError(`${file} is not valid JSON: ${error.message}`);
  }
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}
