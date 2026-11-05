#!/usr/bin/env node
// Live check against the real Prized API: one sandbox through Orca's whole lifecycle, driven by the
// Orca stand-in (test/orca-harness.mjs) running the plugin's real create, suspend, resume and
// destroy. It bills your workspace a few cents and deletes the sandbox at the end.
//
//   PRIZED_ORCA_LIVE=1 PRIZED_API_KEY=dcp_… node test/live.mjs
//
// The shell half runs through stand-ins (test/fakes/prized in live mode, test/fakes/ssh-via-api),
// with your key standing in for a `prized login`; lifecycle.test.mjs checks the ssh half against a
// real sshd. What it checks here, on a real sandbox: the create body is accepted, the repository
// lands at the pinned commit with an SSH origin turned into HTTPS, a process started before sleep is
// the same process after wake, the result after wake passes Orca's integrity check, a commit made in
// the workspace comes back to this machine with `pull` while it sleeps (and pushes from here), and
// destroy (Remove, with the workspace asleep) first saves its uncommitted change here as the branch
// prized-orca/saved/<id>/uncommitted, then leaves a 404.
//
// The key goes to the plugin as PRIZED_API_KEY and to the stand-ins as FAKE_LOGIN_BEARER; nothing
// here prints it.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { FAKES, PKG, makeRepo, makeSshdKit, runAdapter, sh as hostSh, tempDir, testEnv } from "./helpers.mjs";
import { parseRecipe, parseResult, payload, resumeIntegrityError, runPhase } from "./orca-harness.mjs";

if (process.env.PRIZED_ORCA_LIVE !== "1" || !process.env.PRIZED_API_KEY) {
  console.error("live check: set PRIZED_ORCA_LIVE=1 and PRIZED_API_KEY (it creates one billed sandbox)");
  process.exit(2);
}
const KEY = process.env.PRIZED_API_KEY;
const API = "https://api.prized.dev/api/v1";
const auth = { Authorization: `Bearer ${KEY}` };
const t0 = Date.now();
const log = (...a) => console.error(`[live +${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);

async function get(id) {
  const r = await fetch(`${API}/sandboxes/${id}`, { headers: auth });
  return { status: r.status, sandbox: r.status === 200 ? (await r.json()).sandbox : null };
}

async function sh(sandbox, command) {
  const r = await fetch(`${sandbox.toolboxBase}/process/execute`, {
    method: "POST",
    headers: { ...auth, "Content-Type": "application/json", "Prized-Auto-Resume": "false" },
    body: JSON.stringify({ command, timeout: 60 }),
  });
  const out = await r.json();
  return { status: r.status, ...out };
}

const root = tempDir("prized-orca-live-");
const repo = makeRepo(path.join(root, "repo"));
const env = testEnv(root, {
  PRIZED_API_KEY: KEY,
  FAKE_LIVE_API: API,
  FAKE_LOGIN_BEARER: KEY,
  FAKE_SSHD_DIR: makeSshdKit(path.join(root, "sshd")),
  PRIZED_ORCA_SSH_BIN: path.join(FAKES, "ssh-via-api"),
});
const recipe = parseRecipe(fs.readFileSync(path.join(PKG, "vm-recipe.json"), "utf8"));
const instance = `orca-live-${Date.now().toString(36)}`;
const context = {
  instanceId: instance,
  recipeId: recipe.id,
  projectId: "live",
  workspaceId: "live",
  workspaceName: "prized-orca live check",
  repoPath: repo.dir,
  // an SSH origin, as many laptops have: the sandbox has no SSH key, so it gets HTTPS
  repoUrl: "git@github.com:acme/app.git",
  branch: "live/check",
  ref: "origin/main",
  expectedRefHead: repo.first,
  orcaVersion: "1.4.245",
};
let id = null;
let created = null;
const report = {};

try {
  const s = await runAdapter(["setup", "--yes", "--vcpu", "1", "--memory-gb", "2", "--auto-pause-min", "10", "--max-runtime-min", "30"], { env });
  assert.equal(s.status, 0, s.stderr);

  let r = await runPhase({ command: recipe.create, mode: "create", context, recipe, baseEnv: env });
  process.stderr.write(r.stderr);
  assert.equal(r.exitCode, 0, "create failed");
  created = parseResult(r.stdout).result;
  id = created.userData.resourceId;
  log("created", id);
  let { sandbox } = await get(id);
  report.created = { id, state: sandbox.state, template: sandbox.template, templateBuild: sandbox.templateBuild, resources: sandbox.resources, autoPauseMin: sandbox.autoPauseMin, maxRuntimeMin: sandbox.maxRuntimeMin, labels: sandbox.labels, toolboxHost: new URL(sandbox.toolboxBase).host };
  const projectRoot = created.connection.projectRoot;
  const head = await sh(sandbox, `git -C '${projectRoot}' rev-parse HEAD; git -C '${projectRoot}' branch --show-current; git -C '${projectRoot}' remote get-url origin`);
  report.checkout = head.stdout.trim().split("\n");
  assert.deepEqual(report.checkout, [repo.first, "live/check", "https://github.com/acme/app.git"]);

  // What Orca's SSH mode needs on the host, as this template has it.
  const probe = await sh(
    sandbox,
    "set +e; uname -m; id -un; echo HOME=$HOME SHELL=$SHELL; git --version; for c in sshd tmux node claude codex setpriv; do printf '%s=' $c; command -v $c || echo missing; done; " +
      "ls /usr/lib/openssh/sftp-server 2>&1; grep -i -E '^\\s*subsystem' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/* 2>/dev/null; ldd --version 2>&1 | head -1; df -h \"$HOME\" | tail -1",
  );
  report.template = probe.stdout.trim().split("\n");

  const bg = await sh(sandbox, "nohup sleep 3000 >/dev/null 2>&1 & echo $!; sleep 1");
  const pid = bg.stdout.trim().split("\n")[0];
  const lstart = (await sh(sandbox, `ps -o lstart= -p ${pid}`)).stdout.trim();
  report.process = { pid, lstart };

  let t = Date.now();
  r = await runPhase({ command: recipe.suspend, mode: "suspend", context, recipe, baseEnv: env, stdin: payload("suspend", recipe, context, created) });
  process.stderr.write(r.stderr);
  assert.equal(r.exitCode, 0, "suspend failed");
  report.suspendMs = Date.now() - t;
  ({ sandbox } = await get(id));
  assert.equal(sandbox.state, "paused");
  log("paused");

  t = Date.now();
  r = await runPhase({ command: recipe.resume, mode: "resume", context, recipe, baseEnv: env, stdin: payload("resume", recipe, context, created) });
  process.stderr.write(r.stderr);
  assert.equal(r.exitCode, 0, "resume failed");
  report.resumeMs = Date.now() - t;
  const resumed = parseResult(r.stdout).result;
  assert.equal(resumeIntegrityError(created, resumed), null);
  ({ sandbox } = await get(id));
  assert.equal(sandbox.state, "running");
  const after = (await sh(sandbox, `ps -o lstart= -p ${pid}`)).stdout.trim();
  report.processAfterWake = { pid, lstart: after, same: after === lstart && after !== "" };
  assert.equal(report.processAfterWake.same, true, "the process did not survive the pause");
  log("resumed; same process", pid);

  // A commit made in the workspace, and one change left uncommitted; then Orca puts it to sleep.
  const made = await sh(sandbox, `cd '${projectRoot}' && echo fixed > login.js && git add login.js && git commit -q -m 'fix login' && echo wip > scratch.txt && git rev-parse HEAD`);
  assert.equal(made.exitCode, 0, made.stderr);
  const wsHead = made.stdout.trim().split("\n").pop();
  r = await runPhase({ command: recipe.suspend, mode: "suspend", context, recipe, baseEnv: env, stdin: payload("suspend", recipe, context, created) });
  assert.equal(r.exitCode, 0, "second suspend failed");
  t = Date.now();
  const pulled = await runAdapter(["pull", id], { env });
  process.stderr.write(pulled.stderr);
  assert.equal(pulled.status, 0, "pull failed");
  const genv = { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: env.GIT_CONFIG_GLOBAL };
  const localHead = hostSh("git", ["-C", repo.dir, "rev-parse", "refs/heads/live/check"], { env: genv });
  ({ sandbox } = await get(id));
  const bare = path.join(root, "origin.git");
  hostSh("git", ["init", "-q", "--bare", bare], { env: genv });
  hostSh("git", ["-C", repo.dir, "push", "-q", bare, "live/check"], { env: genv });
  report.pull = {
    ms: Date.now() - t,
    workspaceHead: wsHead,
    localHead,
    same: localHead === wsHead,
    stateAfter: sandbox.state,
    pushedHead: hostSh("git", ["-C", bare, "rev-parse", "refs/heads/live/check"], { env: genv }),
    said: pulled.stderr.trim().split("\n").filter((l) => /pulled|uncommitted|push it/.test(l)),
  };
  assert.equal(report.pull.same, true, "pull did not bring the workspace's commit");
  assert.equal(report.pull.stateAfter, "paused", "pull did not put the sleeping sandbox back to sleep");
  assert.equal(report.pull.pushedHead, wsHead);
  log("pulled", wsHead.slice(0, 12), "while asleep; paused again");
} catch (error) {
  report.error = error.message;
  log("FAILED:", error.message);
} finally {
  if (id) {
    const d = await runPhase({ command: recipe.destroy, mode: "destroy", context, recipe, baseEnv: env, stdin: payload("destroy", recipe, context, created ?? { userData: { resourceId: id } }) });
    process.stderr.write(d.stderr);
    report.destroyExit = d.exitCode;
    if (report.pull) {
      // the pulled commit is here already; the uncommitted scratch.txt is saved on top of it
      const genv = { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: env.GIT_CONFIG_GLOBAL };
      const git = (...a) => spawnSync("git", ["-C", repo.dir, ...a], { encoding: "utf8", env: genv }).stdout.trim();
      const saved = `refs/heads/prized-orca/saved/${id}/uncommitted`;
      report.saved = { parent: git("rev-parse", `${saved}^`), scratch: git("show", `${saved}:scratch.txt`), said: d.stderr.trim().split("\n").filter((l) => /saved|nothing to save/.test(l)) };
      report.saved.ok = report.saved.parent === report.pull.workspaceHead && report.saved.scratch === "wip";
      if (!report.saved.ok && !report.error) report.error = "destroy did not save the uncommitted change";
    }
    let gone = null;
    for (let i = 0; i < 40; i++) {
      gone = await get(id);
      if (gone.status === 404) break;
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
    report.afterDestroy = { status: gone?.status, state: gone?.sandbox?.state ?? null };
    log("after destroy: GET", gone?.status);
  }
  fs.rmSync(root, { recursive: true, force: true });
  report.seconds = Math.round((Date.now() - t0) / 1000);
  console.log(JSON.stringify(report, null, 2));
  process.exitCode = report.error || report.afterDestroy?.status !== 404 ? 1 : 0;
}
