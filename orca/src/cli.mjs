// The local programs the plugin drives: the prized CLI (with the user's login, for shells), ssh
// (to check the exact connection Orca will dial), and git.
//
// PRIZED_API_KEY is taken out of every child's environment: the CLI opens shells with the login only,
// and nothing else the plugin runs needs the key.

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { INSTALL_CLI_URL, UserError, redact, userSshConfig } from "./util.mjs";

export function childEnv(env = process.env) {
  const out = { ...env };
  delete out.PRIZED_API_KEY;
  delete out.PRIZED_API_KEY_ORIGIN;
  return out;
}

// The prized CLI: the path setup recorded, PRIZED_BIN, or the PATH and the places the installer uses.
export function findPrized(config = {}, env = process.env) {
  if (env.PRIZED_BIN) return env.PRIZED_BIN;
  if (config.prizedBin && fs.existsSync(config.prizedBin)) return config.prizedBin;
  const dirs = [
    ...String(env.PATH ?? "").split(path.delimiter),
    path.join(os.homedir(), ".local", "bin"),
    path.join(os.homedir(), ".prized", "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
  ];
  for (const dir of dirs) {
    if (!dir) continue;
    const candidate = path.join(dir, "prized");
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      // keep looking
    }
  }
  return null;
}

export function requirePrized(config, env = process.env) {
  const bin = findPrized(config, env);
  if (!bin) throw new UserError("the prized CLI is not installed (or not where Orca can find it)", `install it: ${INSTALL_CLI_URL}, then run setup again`);
  return bin;
}

// `prized sandbox ssh` arrived in 0.21.9.
export const MIN_CLI = [0, 21, 9];

export function prizedVersion(bin, env = process.env) {
  const r = spawnSync(bin, ["--version"], { env: childEnv(env), encoding: "utf8", timeout: 30_000 });
  const m = /prized\s+v?(\d+)\.(\d+)\.(\d+)/.exec(`${r.stdout ?? ""}${r.stderr ?? ""}`);
  return m ? m.slice(1, 4).map(Number) : null;
}

// A build from source (0.0.x) passes: it is newer than any release it was built after.
export function versionOk(v, min = MIN_CLI) {
  if (!v) return false;
  if (v[0] === 0 && v[1] === 0) return true;
  for (let i = 0; i < 3; i++) if (v[i] !== min[i]) return v[i] > min[i];
  return true;
}

export function runPrized(bin, args, { timeoutMs = 120_000, env = process.env } = {}) {
  const result = spawnSync(bin, args, {
    env: childEnv(env),
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: timeoutMs,
    maxBuffer: 16 * 1024 * 1024,
  });
  if (result.error?.code === "ENOENT") throw new UserError(`the prized CLI is not at ${bin}`, `install it: ${INSTALL_CLI_URL}`);
  if (result.error && result.error.code !== "ETIMEDOUT") throw result.error;
  return { status: result.status ?? (result.error ? 124 : 1), stdout: result.stdout ?? "", stderr: redact(result.stderr ?? "") };
}

// The CLI's answer when its sign-in is an API key (`prized login --token`), not a browser login:
// 403 ssh_requires_login for a Full access key, 403 key_access_denied for a Custom one.
const KEY_SIGN_IN = /not an API key|ssh_requires_login|API key is limited|may not use sandboxes|key_access_denied/i;
const KEY_SIGN_IN_HINT =
  "run `prized login` and sign in in the browser: the CLI is signed in with an API key (prized login --token), " +
  "and a shell in a sandbox opens only with a browser sign-in. PRIZED_API_KEY in your profile stays as it is";

// Which sign-in the CLI would open a sandbox shell with, without making or waking anything: ask for
// a shell in a sandbox that doesn't exist. Prized checks the credential before it looks the sandbox
// up, so a browser login gets "no sandbox …" (exit 5), and an API key or no sign-in is refused.
export const PROBE_SANDBOX = "sbx_0000000000000000";
export function shellSignIn(bin, opts = {}) {
  const r = runPrized(bin, ["sandbox", "ssh", PROBE_SANDBOX, "--", "true"], { timeoutMs: 45_000, ...opts });
  const said = `${r.stderr}\n${r.stdout}`;
  const detail = said.trim().split("\n").filter(Boolean).slice(-3).join(" / ");
  if (KEY_SIGN_IN.test(said)) return { ok: false, kind: "api-key", detail };
  if (r.status === 8) return { ok: false, kind: "signed-out", detail };
  if (r.status === 5 && /no sandbox/i.test(said)) return { ok: true, kind: "login", detail };
  return { ok: null, kind: "unknown", detail: detail || `exit ${r.status}` };
}

export function shellSignInError(signIn) {
  if (signIn.kind === "api-key") return new UserError("the prized CLI is signed in with an API key, so it can't open a shell in a sandbox", KEY_SIGN_IN_HINT);
  return new UserError("the prized CLI is not signed in", "run `prized login` (sandbox shells open with your browser sign-in), then try again");
}

export function prizedFailure(what, result) {
  const said = String(result.stderr || result.stdout || "").trim().split("\n").slice(-6).join("\n");
  if (KEY_SIGN_IN.test(said)) return new UserError(`${what}: the prized CLI is signed in with an API key${said ? `\n${said}` : ""}`, KEY_SIGN_IN_HINT);
  if (result.status === 8) {
    return new UserError(`${what}: the prized CLI is not signed in${said ? `\n${said}` : ""}`, "run `prized login` in a terminal (shells open with your login, not an API key)");
  }
  if (result.status === 124) return new UserError(`${what}: timed out`);
  return new UserError(`${what} (prized exit ${result.status})${said ? `:\n${said}` : ""}`);
}

// Open a shell once: this mints the sandbox's certificate and writes its ssh files. A paused sandbox
// is resumed first, by the CLI itself.
export function openShell(bin, id, opts = {}) {
  const result = runPrized(bin, ["sandbox", "ssh", id, "--", "true"], { timeoutMs: 180_000, ...opts });
  if (result.status !== 0) throw prizedFailure(`could not open a shell in ${id}`, result);
}

// The sandbox's state as the user's login sees it ("gone" once deleted, null when unknown).
export function cliSandboxState(bin, id, opts = {}) {
  const result = runPrized(bin, ["sandbox", "get", id, "--json"], { timeoutMs: 30_000, ...opts });
  if (result.status === 5) return "gone";
  if (result.status !== 0) return null;
  try {
    const parsed = JSON.parse(result.stdout);
    return parsed?.sandbox?.state ?? parsed?.state ?? null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- ssh

export function sshBin(env = process.env) {
  return env.PRIZED_ORCA_SSH_BIN || "ssh";
}

// ssh as Orca runs it for a sandbox: system OpenSSH with our ProxyCommand on the command line, the
// rest from ~/.ssh/config (our include). With PRIZED_ORCA_SSH_CONFIG set (tests), that file is read
// instead of the real one.
export function sshArgs(target, command, env = process.env) {
  const args = [];
  if (env.PRIZED_ORCA_SSH_CONFIG) args.push("-F", userSshConfig(env));
  args.push(
    "-o", "BatchMode=yes",
    "-T",
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
    "-o", "ConnectTimeout=30",
    "-o", `ProxyCommand=${target.proxyCommand}`,
    "--", `${target.username}@${target.host}`,
  );
  if (command) args.push(command);
  return args;
}

// Run one command over the exact connection Orca will dial. Resolves with ssh's exit and output.
export function sshRun(target, command, { env = process.env, timeoutMs = 120_000 } = {}) {
  return new Promise((resolve) => {
    const child = spawn(sshBin(env), sshArgs(target, command, env), { env: childEnv(env), stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    const timer = setTimeout(() => child.kill("SIGTERM"), timeoutMs);
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ status: 127, stdout, stderr: `${stderr}${error.message}` });
    });
    child.on("close", (status) => {
      clearTimeout(timer);
      resolve({ status: status ?? 1, stdout, stderr: redact(stderr) });
    });
  });
}

// ---------------------------------------------------------------- git

export function git(args, opts = {}) {
  const result = spawnSync("git", args, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: 64 * 1024 * 1024,
    ...opts,
    env: { ...childEnv(opts.env ?? process.env), GIT_TERMINAL_PROMPT: "0" },
  });
  if (result.error) throw result.error;
  return result;
}

export function gitOk(args, opts = {}) {
  const result = git(args, opts);
  if (result.status !== 0) throw new UserError(`git ${args.join(" ")} failed: ${redact((result.stderr || result.stdout || "").trim())}`);
  return result.stdout.trim();
}
