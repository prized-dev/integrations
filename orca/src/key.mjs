// Finding the user's PRIZED_API_KEY.
//
// Orca runs recipe commands with its own environment. Started from a terminal, that has the shell's
// exports; started from the Dock or a launcher, it has none of them. So: the environment first, and
// else the user's login shell, asked for that one variable the way Orca itself reads PATH from it.
// The key is kept in memory only: never written, logged, or passed on a command line.

import { spawnSync } from "node:child_process";
import { UserError, rememberSecret } from "./util.mjs";

const START = "__PRIZED_ORCA_KEY_START__";
const END = "__PRIZED_ORCA_KEY_END__";
const PROBE_TIMEOUT_MS = 10_000;

// dcp_ and 48 hex characters today; a little looser, so a future key shape still gets through.
const KEY_SHAPE = /^dcp_[A-Za-z0-9]{16,256}$/;

export function checkKeyShape(key, where) {
  if (!KEY_SHAPE.test(key)) {
    throw new UserError(
      `PRIZED_API_KEY ${where} is not a Prized API key (they start with dcp_)`,
      "make one at https://prized.dev/dashboard/workspace/api-keys?via=orca",
    );
  }
  return key;
}

// The login shell's PRIZED_API_KEY, or "" when it has none or can't be asked. Only stdout is read,
// and only between the markers, so a chatty profile can't get in the way.
export function keyFromLoginShell(env = process.env, spawn = spawnSync) {
  if (process.platform === "win32") return "";
  const shell = env.SHELL || (process.platform === "darwin" ? "/bin/zsh" : "/bin/bash");
  const script = `printf '%s' '${START}'; printenv PRIZED_API_KEY; printf '%s' '${END}'`;
  const childEnv = { ...env, ORCA_SHELL_PATH_PROBE: "1", PRIZED_ORCA_KEY_PROBE: "1" };
  delete childEnv.PRIZED_API_KEY;
  const result = spawn(shell, ["-ilc", script], {
    env: childEnv,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: PROBE_TIMEOUT_MS,
  });
  const out = String(result.stdout ?? "");
  const start = out.lastIndexOf(START);
  const end = out.lastIndexOf(END);
  if (start === -1 || end === -1 || end < start) return "";
  return out.slice(start + START.length, end).trim();
}

export function resolveKey(env = process.env, spawn = spawnSync) {
  const fromEnv = String(env.PRIZED_API_KEY ?? "").trim();
  if (fromEnv) {
    rememberSecret(fromEnv);
    return { key: checkKeyShape(fromEnv, "in Orca's environment"), source: "environment" };
  }
  const fromShell = keyFromLoginShell(env, spawn);
  if (fromShell) {
    rememberSecret(fromShell);
    return { key: checkKeyShape(fromShell, "in your login shell"), source: "login shell" };
  }
  throw new UserError(
    "PRIZED_API_KEY is not set, in Orca's environment or in your login shell",
    "add `export PRIZED_API_KEY=dcp_…` to your shell profile (~/.zprofile or ~/.bash_profile), then restart Orca; " +
      "make a key at https://prized.dev/dashboard/workspace/api-keys?via=orca",
  );
}
