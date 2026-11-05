import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { cleanupArchive, listWorktree, packWorktree, sessionScript, skipReason, sq, startSessionScript } from "../src/lib.mjs";
import { world } from "./helpers.mjs";

test("lists tracked and untracked files, minus ignored, deleted and credential files", () => {
  const w = world();
  const { files, skipped } = listWorktree(w.repo);
  assert.deepEqual(files.sort(), [".env.example", ".gitignore", "README.md", "notes.txt", "src/app.js"]);
  assert.deepEqual(skipped.map((s) => s.path).sort(), [".env", "server.pem"]);
});

test("credential names are skipped wherever they sit", () => {
  for (const p of [".env", "api/.env.production", "certs/tls.key", "a/.ssh/config", "id_ed25519", ".npmrc", "infra/terraform.tfstate", ".aws/credentials"]) {
    assert.notEqual(skipReason(p), "", p);
  }
  for (const p of [".env.example", "src/env.ts", "keys.md", "docs/credentials.md", "README.md"]) {
    assert.equal(skipReason(p), "", p);
  }
});

test("the archive holds exactly the listed files, names with spaces included", () => {
  const w = world();
  fs.writeFileSync(path.join(w.repo, "with space.txt"), "x\n");
  const { files } = listWorktree(w.repo);
  const { archive, size } = packWorktree(w.repo, files);
  try {
    assert.ok(size > 0);
    const listed = spawnSync("tar", ["-tzf", archive], { encoding: "utf8" }).stdout.trim().split("\n").sort();
    assert.deepEqual(listed, [...files].sort());
    assert.ok(listed.includes("with space.txt"));
  } finally {
    cleanupArchive(archive);
  }
  assert.equal(fs.existsSync(archive), false);
});

test("a submodule is reported, not copied", () => {
  const w = world();
  const sub = path.join(w.dir, "sub");
  spawnSync("git", ["init", "-q", sub]);
  spawnSync("git", ["-C", sub, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "s"]);
  const r = spawnSync("git", ["-C", w.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "vendor/sub"], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  const { files, skipped } = listWorktree(w.repo);
  assert.ok(!files.includes("vendor/sub"));
  assert.ok(skipped.some((s) => s.path === "vendor/sub" && s.reason === "submodule"));
});

test("shell quoting survives quotes in values", () => {
  const out = spawnSync("sh", ["-c", `printf %s ${sq("it's a \"test\" $HOME")}`], { encoding: "utf8" }).stdout;
  assert.equal(out, `it's a "test" $HOME`);
});

test("the session runs the agent, then stays in a shell", () => {
  assert.match(sessionScript("claude"), /claude; exec bash -l$/);
  assert.match(sessionScript(""), /; exec bash -l$/);
  assert.match(sessionScript("claude"), /\.herdr-prized\.env/);
});

test("a sandbox session finds or installs tmux and shares its socket with ssh; a box session does neither", () => {
  const sandbox = startSessionScript('"/home/prized/work/r"', "herdr-1", "claude", "sandbox");
  assert.match(sandbox, /export TMUX_TMPDIR="\$HOME\/\.tmux"/);
  assert.match(sandbox, /if ! command -v tmux .*\n[\s\S]*apt-get install --download-only -y -qq --no-install-recommends tmux/);
  assert.match(sandbox, /dpkg -x/);
  const box = startSessionScript('"$HOME/herdr/r-1"', "herdr-1", "claude", "box");
  assert.doesNotMatch(box, /TMUX_TMPDIR|apt-get|bashrc/);
  for (const script of [sandbox, box]) {
    const r = spawnSync("bash", ["-n"], { input: script, encoding: "utf8" });
    assert.equal(r.status, 0, r.stderr);
  }
});
