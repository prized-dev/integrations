import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const PLUGIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FAKES = path.join(PLUGIN, "test", "fakes");

// Obvious fakes: the tests check that neither value ever shows up in a log, an argv or the output.
export const FAKE_PRIZED_KEY = `dcp_${"0".repeat(48)}`;
export const FAKE_AGENT_KEY = "sk-ant-test-FAKE-agent-key-do-not-print";

export function git(cwd, ...args) {
  const r = spawnSync("git", ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", ...args], {
    cwd,
    encoding: "utf8",
  });
  if (r.status !== 0) throw new Error(`git ${args.join(" ")}: ${r.stderr}`);
  return r.stdout;
}

const worlds = [];
process.on("exit", () => {
  for (const dir of worlds) fs.rmSync(dir, { recursive: true, force: true });
});

// A temp world: a Git worktree, herdr's config and state directories, and the fakes' homes.
export function world({ config } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "herdr-prized-test-"));
  worlds.push(dir);
  const repo = path.join(dir, "my repo");
  fs.mkdirSync(path.join(repo, "src"), { recursive: true });
  git(dir, "init", "-q", repo);
  fs.writeFileSync(path.join(repo, "README.md"), "hello\n");
  fs.writeFileSync(path.join(repo, "src", "app.js"), "export const a = 1;\n");
  fs.writeFileSync(path.join(repo, ".gitignore"), "dist/\n");
  fs.writeFileSync(path.join(repo, "gone.txt"), "soon deleted\n");
  git(repo, "add", "-A");
  git(repo, "commit", "-qm", "init");
  fs.rmSync(path.join(repo, "gone.txt"));
  fs.writeFileSync(path.join(repo, "notes.txt"), "untracked but not ignored\n");
  fs.mkdirSync(path.join(repo, "dist"));
  fs.writeFileSync(path.join(repo, "dist", "bundle.js"), "ignored\n");
  fs.writeFileSync(path.join(repo, ".env"), `ANTHROPIC_API_KEY=${FAKE_AGENT_KEY}\n`);
  fs.writeFileSync(path.join(repo, ".env.example"), "ANTHROPIC_API_KEY=\n");
  fs.writeFileSync(path.join(repo, "server.pem"), "-----BEGIN PRIVATE KEY-----\n");
  const w = {
    dir,
    repo: fs.realpathSync(repo),
    home: path.join(dir, "home"),
    configDir: path.join(dir, "config"),
    stateDir: path.join(dir, "state"),
    prizedDir: path.join(dir, "prized"),
    herdrLog: path.join(dir, "herdr.log"),
    sshLog: path.join(dir, "ssh.log"),
  };
  for (const d of [w.home, w.configDir, w.stateDir, w.prizedDir]) fs.mkdirSync(d, { recursive: true });
  fs.writeFileSync(w.herdrLog, "");
  if (config) fs.writeFileSync(path.join(w.configDir, "config.json"), JSON.stringify(config));
  return w;
}

export function baseEnv(w, extra = {}) {
  return {
    PATH: process.env.PATH,
    HOME: w.home,
    HERDR_BIN_PATH: path.join(FAKES, "herdr"),
    PRIZED_BIN: path.join(FAKES, "prized"),
    PRIZED_HERDR_SSH: path.join(FAKES, "ssh"),
    FAKE_PRIZED_DIR: w.prizedDir,
    FAKE_HERDR_LOG: w.herdrLog,
    FAKE_SSH_LOG: w.sshLog,
    HERDR_PLUGIN_CONFIG_DIR: w.configDir,
    HERDR_PLUGIN_STATE_DIR: w.stateDir,
    PRIZED_API_KEY: FAKE_PRIZED_KEY,
    ...extra,
  };
}

export function run(w, script, { env = {}, input = "" } = {}) {
  const r = spawnSync(process.execPath, [path.join(PLUGIN, "src", script)], {
    cwd: PLUGIN,
    env: baseEnv(w, env),
    input,
    encoding: "utf8",
    timeout: 60_000,
  });
  return { status: r.status, out: `${r.stdout}${r.stderr}` };
}

export function action(w, id, context, env = {}) {
  return run(w, "action.mjs", {
    env: { HERDR_PLUGIN_ACTION_ID: id, HERDR_PLUGIN_CONTEXT_JSON: JSON.stringify(context), ...env },
  });
}

export function state(w) {
  return JSON.parse(fs.readFileSync(path.join(w.stateDir, "mappings.json"), "utf8"));
}

export function prizedCalls(w) {
  const file = path.join(w.prizedDir, "log.jsonl");
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

export function herdrCalls(w) {
  return fs.readFileSync(w.herdrLog, "utf8").trim().split("\n").filter(Boolean);
}

export function db(w) {
  return JSON.parse(fs.readFileSync(path.join(w.prizedDir, "db.json"), "utf8"));
}

export function sandboxHome(w, id) {
  return path.join(w.prizedDir, "sbx", id, "home");
}

export function boxHome(w, name) {
  return path.join(w.prizedDir, "box", name, "home");
}

// Every file the test world wrote that a secret could leak into.
export function everythingWritten(w, ...outputs) {
  const texts = [...outputs];
  for (const f of [path.join(w.prizedDir, "log.jsonl"), w.herdrLog, path.join(w.stateDir, "mappings.json")]) {
    if (fs.existsSync(f)) texts.push(fs.readFileSync(f, "utf8"));
  }
  return texts.join("\n");
}
