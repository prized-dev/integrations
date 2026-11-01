// Shared pieces of the Prized herdr plugin: config, mapping state, herdr context, the `prized` and
// `herdr` CLIs, and packing a worktree. No dependencies beyond Node and the system's git and tar.
//
// Secrets: PRIZED_API_KEY and any forwarded agent key stay in the environment of the processes that
// need them. Nothing here prints, logs or passes one on a command line.

import { spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const PLUGIN_ID = "prized.sandbox";
export const INSTALL_URL = "https://prized.dev/docs/quickstart?via=herdr";
// `prized sandbox cp` moves one file of up to 100 MB.
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

export class PluginError extends Error {
  constructor(message, hint = "") {
    super(message);
    this.hint = hint;
  }
}

// ---------------------------------------------------------------- config

const AGENTS = {
  claude: { command: "claude", herdr: "claude", keys: ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"] },
  codex: { command: "codex", herdr: "codex", keys: ["OPENAI_API_KEY"] },
  shell: { command: "", herdr: "", keys: [] },
};

export const DEFAULT_CONFIG = Object.freeze({
  agent: "claude",
  command: null,
  template: "prized-agent-v1",
  vcpu: 2,
  memoryGb: 4,
  autoPause: "30m",
  maxRuntime: null,
  onClose: "pause",
  box: null,
  forwardEnv: null,
});

export function configDir(env = process.env) {
  return env.HERDR_PLUGIN_CONFIG_DIR || path.join(os.homedir(), ".config", "herdr-prized");
}

export function stateDir(env = process.env) {
  return env.HERDR_PLUGIN_STATE_DIR || path.join(os.homedir(), ".local", "state", "herdr-prized");
}

export function loadConfig(env = process.env) {
  const file = path.join(configDir(env), "config.json");
  let raw = {};
  if (fs.existsSync(file)) {
    try {
      raw = JSON.parse(fs.readFileSync(file, "utf8"));
    } catch (error) {
      throw new PluginError(`${file} is not valid JSON: ${error.message}`);
    }
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new PluginError(`${file} must hold a JSON object`);
  }
  const config = { ...DEFAULT_CONFIG, ...raw };
  if (!AGENTS[config.agent] && !config.command) {
    throw new PluginError(`agent "${config.agent}" is not one of claude, codex or shell`, `or set "command" in ${file}`);
  }
  if (!["pause", "keep"].includes(config.onClose)) throw new PluginError(`onClose must be "pause" or "keep"`);
  if (config.forwardEnv !== null && !(Array.isArray(config.forwardEnv) && config.forwardEnv.every(isEnvName))) {
    throw new PluginError("forwardEnv must be a list of environment variable names");
  }
  for (const key of ["vcpu", "memoryGb"]) {
    if (!(Number.isInteger(config[key]) && config[key] > 0)) throw new PluginError(`${key} must be a positive whole number`);
  }
  return config;
}

export function isEnvName(name) {
  return typeof name === "string" && /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(name);
}

// The command the session runs ("" for a plain shell) and the herdr agent hint for the pane.
export function agentOf(config) {
  if (config.command) return { command: String(config.command), herdr: "", keys: [] };
  return AGENTS[config.agent];
}

// Names of the variables to copy into a sandbox: forwardEnv as given, or else the agent's own key
// variables. Only names that are set are kept; values are read later, only to send them on stdin.
export function forwardNames(config, env = process.env) {
  const names = config.forwardEnv ?? agentOf(config).keys;
  return names.filter((name) => isEnvName(name) && typeof env[name] === "string" && env[name] !== "");
}

// ---------------------------------------------------------------- state

function statePath(env) {
  return path.join(stateDir(env), "mappings.json");
}

export function readState(env = process.env) {
  const file = statePath(env);
  if (!fs.existsSync(file)) return { version: 1, mappings: {} };
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    if (parsed && typeof parsed.mappings === "object") return parsed;
  } catch {
    // fall through: a broken file is set aside, never silently reused
  }
  fs.renameSync(file, `${file}.broken-${Date.now()}`);
  return { version: 1, mappings: {} };
}

function sleepMs(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

// Read-modify-write the state under a lock directory, so the pane and the close hook never lose
// each other's writes.
export function updateState(fn, env = process.env) {
  const dir = stateDir(env);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const lock = path.join(dir, "mappings.lock");
  const deadline = Date.now() + 10_000;
  for (;;) {
    try {
      fs.mkdirSync(lock);
      break;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      try {
        if (Date.now() - fs.statSync(lock).mtimeMs > 30_000) fs.rmdirSync(lock);
      } catch {
        // another process removed it first
      }
      if (Date.now() > deadline) throw new PluginError(`state is locked: remove ${lock} if no herdr-prized command is running`);
      sleepMs(50);
    }
  }
  try {
    const state = readState(env);
    const result = fn(state);
    const file = statePath(env);
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
    fs.renameSync(tmp, file);
    return result;
  } finally {
    fs.rmdirSync(lock);
  }
}

export function patchMapping(id, patch, env = process.env) {
  return updateState((state) => {
    const current = state.mappings[id];
    if (!current) throw new PluginError(`no mapping ${id}`);
    state.mappings[id] = { ...current, ...patch, updatedAt: new Date().toISOString() };
    return state.mappings[id];
  }, env);
}

export function newId() {
  return crypto.randomBytes(4).toString("hex");
}

// ---------------------------------------------------------------- herdr context

export function pluginContext(env = process.env) {
  const raw = env.HERDR_PLUGIN_CONTEXT_JSON;
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

export function contextCwd(context, fallback = process.cwd()) {
  return context.worktree?.checkout_path || context.focused_pane_cwd || context.workspace_cwd || fallback;
}

// The Git worktree that holds the invoking pane's directory.
export function worktreeRoot(cwd) {
  const result = spawnSync("git", ["-C", cwd, "rev-parse", "--show-toplevel"], { encoding: "utf8" });
  if (result.status !== 0) {
    throw new PluginError(`${cwd} is not inside a Git worktree`, "open the pane in a Git repository, then run the action again");
  }
  return fs.realpathSync(result.stdout.trim());
}

// The mapping an action means: the one attached to the focused pane, else the newest for this
// worktree.
export function findMapping(state, context, root) {
  const all = Object.values(state.mappings);
  const pane = context.focused_pane_id;
  const byPane = pane && all.find((m) => m.paneId === pane);
  if (byPane) return byPane;
  const byRoot = all.filter((m) => m.root === root).sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)));
  return byRoot[0] ?? null;
}

export function herdrBin(env = process.env) {
  return env.HERDR_BIN_PATH || "herdr";
}

export function herdr(args, options = {}) {
  return spawnSync(herdrBin(options.env), args, { encoding: "utf8", stdio: options.stdio ?? "pipe", env: options.env ?? process.env });
}

export function openPane(entrypoint, context, { placement, env = {}, cwd, focus = true } = {}) {
  const args = ["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", entrypoint];
  if (placement) args.push("--placement", placement);
  if (placement === "split" && context.focused_pane_id) args.push("--target-pane", context.focused_pane_id, "--direction", "right");
  if (cwd) args.push("--cwd", cwd);
  for (const [key, value] of Object.entries(env)) {
    if (!isEnvName(key) || /[\n\0]/.test(String(value))) throw new PluginError(`bad pane variable ${key}`);
    args.push("--env", `${key}=${value}`);
  }
  args.push(focus ? "--focus" : "--no-focus");
  const result = herdr(args);
  if (result.status !== 0) throw new PluginError(`herdr could not open the ${entrypoint} pane: ${(result.stderr || result.stdout || "").trim()}`);
  return result;
}

export function notify(title, body) {
  herdr(["notification", "show", title, "--body", body]);
}

// ---------------------------------------------------------------- prized CLI

export function prizedBin(env = process.env) {
  return env.PRIZED_BIN || "prized";
}

// Run `prized ARGS`. stdout is captured; stderr is captured too unless `stderr: "inherit"` lets the
// CLI's own progress lines show in the pane. `input` goes to stdin (a Buffer or string).
export function prized(args, { input, stderr = "pipe", stdout = "pipe", env = process.env } = {}) {
  const result = spawnSync(prizedBin(env), args, {
    input,
    env,
    stdio: [input === undefined ? "ignore" : "pipe", stdout, stderr],
    maxBuffer: 256 * 1024 * 1024,
  });
  if (result.error?.code === "ENOENT") {
    throw new PluginError("the prized CLI is not installed or not on herdr's PATH", `install it: ${INSTALL_URL}`);
  }
  if (result.error) throw result.error;
  return result;
}

// Run `prized ARGS` and fail with the CLI's own message on a non-zero exit.
export function prizedOk(args, options = {}) {
  const result = prized(args, options);
  if (result.status !== 0) throw prizedFailure(args, result);
  return result;
}

export function prizedFailure(args, result) {
  const said = String(result.stderr ?? "").trim().split("\n").slice(-6).join("\n");
  const what = `prized ${args.slice(0, 2).join(" ")} failed (exit ${result.status})`;
  if (result.status === 8) {
    return new PluginError(`${what}: not signed in${said ? `\n${said}` : ""}`, "run `prized login` in a terminal, or set PRIZED_API_KEY where herdr starts");
  }
  return new PluginError(said ? `${what}:\n${said}` : what);
}

export function sandboxState(id, env = process.env) {
  const result = prized(["sandbox", "get", id, "--json"], { env });
  if (result.status === 5) return "deleted";
  if (result.status !== 0) throw prizedFailure(["sandbox", "get"], result);
  try {
    return JSON.parse(String(result.stdout)).sandbox?.state ?? "unknown";
  } catch {
    return "unknown";
  }
}

// ---------------------------------------------------------------- shell quoting and remote scripts

// Commits the plugin makes itself: its own identity, never signed, no hooks.
const GIT_ID = "-c user.name=herdr -c user.email=herdr@prized.invalid -c commit.gpgsign=false";

export function sq(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

export function safeName(name) {
  const cleaned = String(name).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+/, "").slice(0, 40);
  return cleaned || "worktree";
}

// The shell the tmux session runs: load the forwarded keys if a file holds them, run the agent, then
// stay in a login shell when it exits so the session never closes under you.
export function sessionScript(agentCommand) {
  const load = 'f="$HOME/.herdr-prized.env"; if [ -f "$f" ]; then set -a; . "$f"; set +a; fi';
  return agentCommand ? `${load}; ${agentCommand}; exec bash -l` : `${load}; exec bash -l`;
}

// Unpack the worktree archive into DIR and record it as the base later diffs are taken against. The
// archive is a file on the sandbox or "-" (stdin) on a box. POSIX sh: a box runs it under sh -c.
export function unpackScript(dir, archive) {
  const fromStdin = archive === "-";
  return [
    "set -e",
    `D=${dir}`,
    'mkdir -p "$D"',
    `tar xzf ${fromStdin ? "-" : sq(archive)} -C "$D" --warning=no-unknown-keyword`,
    fromStdin ? "" : `rm -f ${sq(archive)}`,
    'cd "$D"',
    "[ -d .git ] || git -c init.defaultBranch=main init -q",
    "git add -A",
    `git ${GIT_ID} commit -q --allow-empty --no-verify -m 'herdr: the worktree as copied'`,
    "git update-ref refs/herdr/base HEAD",
  ]
    .filter(Boolean)
    .join("\n");
}

export function startSessionScript(dir, session, agentCommand) {
  return [
    `D=${dir}`,
    `if ! tmux has-session -t ${sq(`=${session}`)} 2>/dev/null; then`,
    `  tmux new-session -d -s ${sq(session)} -c "$D" bash -lc ${sq(sessionScript(agentCommand))}`,
    "fi",
    `tmux set-option -t ${sq(session)} status off >/dev/null`,
  ].join("\n");
}

// Stage everything the agent changed, then diff it against the base: committed or not, new files
// included. Prints the tree it diffed on the first line, then the patch (or writes the patch to
// `out`, a remote path).
export function diffScript(dir, out = null) {
  return [
    "set -e",
    `cd ${dir}`,
    "git add -A",
    "T=$(git write-tree)",
    'echo "$T"',
    `git diff --binary --no-color --no-ext-diff refs/herdr/base "$T"${out ? ` > ${sq(out)}` : ""}`,
  ].join("\n");
}

// After the patch applied locally: the base moves to the tree that patch was taken from.
export function advanceBaseScript(dir, tree) {
  return [
    "set -e",
    `cd ${dir}`,
    `C=$(git ${GIT_ID} commit-tree ${sq(tree)} -p refs/herdr/base -m 'herdr: applied locally')`,
    'git update-ref refs/herdr/base "$C"',
  ].join("\n");
}

// The remote directory, as a shell word that expands on the remote side.
export function remoteDir(mapping) {
  return mapping.kind === "box" ? `"$HOME/herdr/${mapping.dirName}"` : `"/home/prized/work/${mapping.dirName}"`;
}

// ---------------------------------------------------------------- worktree packing

// Never copied: Git internals and files that usually hold credentials.
const SKIP_DIRS = new Set([".git", ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker"]);
const SECRET_FILE = [
  /^\.env$/,
  /^\.env\.(?!example$|sample$|template$|dist$).+$/,
  /\.(pem|key|p12|pfx|jks|keystore|kdbx)$/i,
  /^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$/,
  /^\.(npmrc|pypirc|netrc|git-credentials|pgpass)$/,
  /\.tfstate(\.backup)?$/,
  /^credentials(\.json)?$/,
  /^service-account.*\.json$/i,
  /^\.envrc$/,
];

export function skipReason(relPath) {
  const parts = relPath.split("/");
  if (parts.some((part) => SKIP_DIRS.has(part))) return "credentials or Git directory";
  const base = parts[parts.length - 1];
  if (SECRET_FILE.some((re) => re.test(base))) return "may hold a credential";
  return "";
}

// Files to copy: tracked and untracked, minus what .gitignore ignores, what no longer exists,
// submodules, and likely credentials.
export function listWorktree(root) {
  const result = spawnSync("git", ["-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"], {
    encoding: "buffer",
    maxBuffer: 512 * 1024 * 1024,
  });
  if (result.status !== 0) throw new PluginError(`git ls-files failed in ${root}: ${String(result.stderr).trim()}`);
  const seen = new Set();
  const files = [];
  const skipped = [];
  let bytes = 0;
  for (const rel of String(result.stdout).split("\0")) {
    if (!rel || seen.has(rel)) continue;
    seen.add(rel);
    const reason = skipReason(rel);
    if (reason) {
      skipped.push({ path: rel, reason });
      continue;
    }
    let stat;
    try {
      stat = fs.lstatSync(path.join(root, rel));
    } catch {
      continue; // deleted in the worktree, not yet staged
    }
    if (stat.isDirectory()) {
      skipped.push({ path: rel, reason: "submodule" });
      continue;
    }
    if (!stat.isFile() && !stat.isSymbolicLink()) continue;
    files.push(rel);
    if (stat.isFile()) bytes += stat.size;
  }
  return { files, skipped, bytes };
}

// A gzip tarball of the listed files, in a private temp directory. Returns its path and size; the
// caller removes it with cleanupArchive.
export function packWorktree(root, files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "herdr-prized-"));
  const list = path.join(dir, "files");
  const archive = path.join(dir, "worktree.tgz");
  fs.writeFileSync(list, files.map((f) => `${f}\0`).join(""));
  const result = spawnSync("tar", ["-czf", archive, "--no-recursion", "-C", root, "--null", "-T", list], {
    encoding: "utf8",
    env: { ...process.env, COPYFILE_DISABLE: "1" },
  });
  // GNU tar exits 1 when a file changed while it was read: the copy is still whole.
  if (result.status !== 0 && !(result.status === 1 && fs.existsSync(archive))) {
    cleanupArchive(archive);
    throw new PluginError(`tar failed: ${String(result.stderr).trim()}`);
  }
  const size = fs.statSync(archive).size;
  if (size > MAX_UPLOAD_BYTES) {
    cleanupArchive(archive);
    throw new PluginError(
      `the worktree packs to ${formatBytes(size)}, over the 100 MB a copy can carry`,
      "add the large files to .gitignore, or start the agent on a box",
    );
  }
  return { archive, size };
}

export function cleanupArchive(archive) {
  fs.rmSync(path.dirname(archive), { recursive: true, force: true });
}

export function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// The forwarded keys as a shell file, sent on stdin and never on a command line.
export function envFile(names, env = process.env) {
  return names.map((name) => `${name}=${sq(env[name])}\n`).join("");
}

// ---------------------------------------------------------------- terminal prompts

// One box from `prized box ls --json`'s list: the only one, or the one the person picks.
export async function pickBox(boxes) {
  const usable = boxes.filter((b) => !["terminated", "terminating", "deleted"].includes(b.observedState));
  if (!usable.length) throw new PluginError("you have no Prized boxes", "create one with `prized box new --wait`");
  if (usable.length === 1) return usable[0].hostname;
  say("Which box?");
  usable.forEach((b, i) => say(`  ${i + 1}. ${b.hostname}  ${b.observedState}  ${b.tier ?? ""}  ${b.region ?? ""}`));
  const answer = (await readLine(`Box [1-${usable.length}, Enter for 1]: `)).trim();
  const n = answer === "" ? 1 : Number(answer);
  if (!Number.isInteger(n) || n < 1 || n > usable.length) throw new PluginError(`no box ${answer}`);
  return usable[n - 1].hostname;
}


// Answers come from the terminal; in tests and pipes, one line is one answer. Input read past the
// answer's newline is kept for the next question.
let inputBuffer = "";
let inputEnded = false;

function nextLine() {
  return new Promise((resolve) => {
    let onData;
    let onEnd;
    const cleanup = () => {
      process.stdin.off("data", onData);
      process.stdin.off("end", onEnd);
      process.stdin.pause();
    };
    const take = () => {
      const nl = inputBuffer.indexOf("\n");
      if (nl < 0 && !inputEnded) return false;
      const end = nl < 0 ? inputBuffer.length : nl;
      const line = inputBuffer.slice(0, end).replace(/\r$/, "");
      inputBuffer = inputBuffer.slice(end + 1);
      cleanup();
      resolve(line);
      return true;
    };
    onData = (d) => {
      inputBuffer += String(d);
      take();
    };
    onEnd = () => {
      inputEnded = true;
      take();
    };
    if (take()) return;
    process.stdin.on("data", onData);
    process.stdin.on("end", onEnd);
    process.stdin.resume();
  });
}

export async function readKey() {
  if (!process.stdin.isTTY) return (await nextLine()).slice(0, 1);
  return new Promise((resolve) => {
    process.stdin.setRawMode(true);
    process.stdin.resume();
    process.stdin.once("data", (d) => {
      process.stdin.setRawMode(false);
      process.stdin.pause();
      resolve(String(d).slice(0, 1));
    });
  });
}

export async function readLine(prompt) {
  process.stdout.write(prompt);
  return nextLine();
}

export function say(line = "") {
  process.stdout.write(`${line}\n`);
}

export function explain(error) {
  if (error instanceof PluginError) return error.hint ? `${error.message}\n  ${error.hint}` : error.message;
  return error?.stack || String(error);
}
