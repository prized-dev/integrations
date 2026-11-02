#!/usr/bin/env node
// A stand-in for the prized CLI, signed in with a login (never an API key), for the commands the
// plugin runs: sandbox ssh ID -- CMD, sandbox get ID --json, sandbox resume ID, sandbox ls --json.
// `sandbox ssh` writes the same files the real CLI writes (internal/sshid/sandbox.go): a user
// certificate from a test CA, a known_hosts trusting the test host CA for box:<uuid>, and an
// ssh_config whose ProxyCommand reaches a local sshd (test/fakes/bridge.mjs). Then it runs the real
// ssh with that config, as the CLI does.
//
// It logs its arguments only (FAKE_CLI_LOG), and refuses to run if PRIZED_API_KEY reached it.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const env = process.env;
if (env.FAKE_CLI_LOG) fs.appendFileSync(env.FAKE_CLI_LOG, `${JSON.stringify(args)}\n`);
if (env.PRIZED_API_KEY) {
  process.stderr.write("fake prized: PRIZED_API_KEY reached the CLI\n");
  if (env.FAKE_CLI_LOG) fs.appendFileSync(env.FAKE_CLI_LOG, "KEY-LEAK\n");
  process.exit(99);
}
if (env.FAKE_NOT_SIGNED_IN === "1") {
  process.stderr.write("not signed in\n");
  process.exit(8);
}
// FAKE_LIVE_API (test/live.mjs): the real API, with FAKE_LOGIN_BEARER standing in for a login.
const LIVE = Boolean(env.FAKE_LIVE_API);
const API = LIVE ? env.FAKE_LIVE_API : `${env.FAKE_API}/api/v1`;
const auth = { Authorization: `Bearer ${LIVE ? env.FAKE_LOGIN_BEARER : "fake-login-token"}` };

async function api(method, p) {
  const r = await fetch(`${API}${p}`, { method, headers: auth });
  const text = await r.text();
  return { status: r.status, body: text ? JSON.parse(text) : null };
}

async function main() {
  if (args[0] === "--version") return console.log(`prized   ${env.FAKE_CLI_VERSION || "0.21.9"} (fake)`), 0;
  if (args[0] !== "sandbox") return 2;
  const [, sub, id] = args;
  if (sub === "ls") return console.log(JSON.stringify({ sandboxes: [] })), 0;
  if (sub === "get") {
    const r = await api("GET", `/sandboxes/${id}`);
    if (r.status === 404) return process.stderr.write(`no sandbox ${id}\n`), 5;
    console.log(JSON.stringify(r.body));
    return 0;
  }
  if (sub === "resume") {
    const r = await api("POST", `/sandboxes/${id}/resume?wait=running`);
    if (r.status !== 200) return process.stderr.write(`resume failed ${r.status}\n`), 1;
    console.log(id);
    return 0;
  }
  if (sub === "ssh") {
    if (env.FAKE_SSH_DELAY_MS) await new Promise((resolve) => setTimeout(resolve, Number(env.FAKE_SSH_DELAY_MS)));
    let r = await api("GET", `/sandboxes/${id}`);
    if (r.status === 404) return process.stderr.write(`no sandbox ${id}\n`), 5;
    if (r.body.sandbox.state === "paused") await api("POST", `/sandboxes/${id}/resume?wait=running`);
    const kit = env.FAKE_SSHD_DIR;
    const conf = path.join(os.homedir(), ".config", "prized");
    const dir = path.join(conf, "sandboxes", id);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const idKey = path.join(conf, "id_prized");
    if (!fs.existsSync(idKey)) spawnSync("ssh-keygen", ["-q", "-t", "ed25519", "-N", "", "-C", "fake", "-f", idKey]);
    const uuid = fs.readFileSync(path.join(kit, "uuid"), "utf8").trim();
    const forced = fs.readFileSync(path.join(kit, "forced-command"), "utf8").trim();
    const validity = env.FAKE_CERT_VALIDITY || "+15m";
    const s = spawnSync("ssh-keygen", ["-q", "-s", path.join(kit, "user_ca"), "-I", id, "-n", `box:${uuid}`, "-V", `-5m:${validity}`,
      "-O", "clear", "-O", "permit-pty", "-O", `force-command=${forced}`, `${idKey}.pub`], { encoding: "utf8" });
    if (s.status !== 0) return process.stderr.write(s.stderr), 1;
    fs.renameSync(`${idKey}-cert.pub`, path.join(dir, "cert.pub"));
    fs.writeFileSync(path.join(dir, "known_hosts"), `@cert-authority box:${uuid} ${fs.readFileSync(path.join(kit, "host_ca.pub"), "utf8").trim()}\n`, { mode: 0o600 });
    const bridge = `"${process.execPath}" "${path.join(here, "bridge.mjs")}" ${id}`;
    // internal/sshid stanzaPath: a path holding a space, tab or ' is double-quoted.
    const word = (p) => (/[ \t']/.test(p) ? `"${p}"` : p);
    const config = [
      "# Managed by `prized sandbox ssh`: rewritten on every connect.",
      `Host ${id}`,
      `    HostName ${id}`,
      `    User ${os.userInfo().username}`,
      `    HostKeyAlias box:${uuid}`,
      `    ProxyCommand ${bridge.replace(/%/g, "%%")}`,
      `    IdentityFile ${word(idKey)}`,
      `    CertificateFile ${word(path.join(dir, "cert.pub"))}`,
      "    IdentitiesOnly yes",
      `    UserKnownHostsFile ${word(path.join(dir, "known_hosts"))}`,
      "    StrictHostKeyChecking yes",
      "    UpdateHostKeys no",
      "    ForwardAgent no",
      "    ForwardX11 no",
      "    ClearAllForwardings yes",
      "    PermitLocalCommand no",
      "    ControlMaster no",
      "    ControlPath none",
      "    ServerAliveInterval 15",
      "    ServerAliveCountMax 4",
      "",
    ].join("\n");
    fs.writeFileSync(path.join(dir, "ssh_config"), config, { mode: 0o600 });
    // Live: the shell itself is test/fakes/ssh-via-api, which needs no files beyond these.
    if (LIVE) return 0;
    const dash = args.indexOf("--");
    const cmd = dash >= 0 ? args.slice(dash + 1) : [];
    const ssh = spawnSync("ssh", ["-F", path.join(dir, "ssh_config"), "-o", "BatchMode=yes", "-T", id, "--", ...cmd], { stdio: ["ignore", "inherit", "inherit"] });
    return ssh.status ?? 1;
  }
  return 2;
}

main().then((code) => process.exit(code ?? 0), (error) => {
  process.stderr.write(`fake prized: ${error.message}\n`);
  process.exit(1);
});
