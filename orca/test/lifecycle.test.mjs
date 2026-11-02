// The whole life of a workspace, driven the way Orca drives a plugin recipe (test/orca-harness.mjs):
// setup, create, Orca's ssh connection, sleep, a refused reconnect, wake, a certificate renewal, an
// idle pause, delete. The Prized API is a local fake (test/fake-api.mjs); the prized CLI is a fake
// signed in with a login (test/fakes/prized); ssh and sshd are the real OpenSSH, with a sshd set up
// like a sandbox guest.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { after, before, describe, test } from "node:test";
import { API_KEY, startFakeApi } from "./fake-api.mjs";
import { PKG, allText, haveSshKit, makeRepo, makeSshdKit, runAdapter, tempDir, testEnv } from "./helpers.mjs";
import { checkoutModeFailure, orcaSsh, parseRecipe, parseResult, payload, resumeIntegrityError, runPhase } from "./orca-harness.mjs";

const kit = haveSshKit();
const skip = kit ? false : "needs ssh, ssh-keygen, setpriv and sshd (/usr/sbin/sshd)";

function certBefore(home, id) {
  const cert = path.join(home, ".config", "prized", "sandboxes", id, "cert.pub");
  const out = spawnSync("ssh-keygen", ["-L", "-f", cert], { encoding: "utf8" }).stdout;
  return /Valid: from \S+ to (\S+)/.exec(out)?.[1];
}

describe("a workspace on a Prized sandbox", { skip, concurrency: false }, () => {
  let root, api, env, repo, recipe, context, created, id, home;
  const started = [];

  before(async () => {
    root = tempDir();
    api = await startFakeApi(root);
    repo = makeRepo(path.join(root, "repo"));
    env = testEnv(root, {
      PRIZED_API_KEY: API_KEY,
      PRIZED_ORCA_API_URL: api.apiUrl,
      FAKE_API: api.base,
      FAKE_SSHD_DIR: makeSshdKit(path.join(root, "sshd")),
    });
    home = env.HOME;
    recipe = parseRecipe(fs.readFileSync(path.join(PKG, "vm-recipe.json"), "utf8"));
    context = {
      instanceId: "orca-7d1f0c2a-55aa-4b1e-9f00-123456789abc",
      recipeId: recipe.id,
      projectId: "proj-1",
      workspaceId: "ws-1",
      workspaceName: "fix login bug",
      repoPath: repo.dir,
      // Orca strips credentials from the URL it passes; the plugin strips them again regardless.
      repoUrl: "https://example.com/acme/app.git",
      branch: "ada/fix-login",
      ref: "origin/main",
      expectedRefHead: repo.first,
      orcaVersion: "1.4.245",
    };
  });

  after(async () => {
    for (const pid of started) {
      try {
        process.kill(pid);
      } catch {
        // already gone
      }
    }
    await api?.close();
    if (root) fs.rmSync(root, { recursive: true, force: true });
  });

  test("setup installs the adapter and adds one Include line to ~/.ssh/config", async () => {
    fs.writeFileSync(env.PRIZED_ORCA_SSH_CONFIG, "Host other\n    HostName example.com\n", { mode: 0o644 });
    const r = await runAdapter(["setup", "--yes"], { env });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout, "");
    assert.ok(fs.existsSync(path.join(home, ".prized-orca", "bin", "prized-orca")));
    const cfg = fs.readFileSync(env.PRIZED_ORCA_SSH_CONFIG, "utf8");
    assert.match(cfg, new RegExp(`^# Added by the Prized Orca plugin.*\\nInclude "${home}/.prized-orca/ssh_config"\\n\\nHost other\\n`));
    assert.equal(fs.statSync(env.PRIZED_ORCA_SSH_CONFIG).mode & 0o777, 0o644);
    const again = await runAdapter(["setup", "--yes"], { env });
    assert.equal(again.status, 0, again.stderr);
    assert.equal(fs.readFileSync(env.PRIZED_ORCA_SSH_CONFIG, "utf8"), cfg, "a second setup changes nothing");
    const conf = JSON.parse(fs.readFileSync(path.join(home, ".prized-orca", "config.json"), "utf8"));
    assert.equal(conf.prizedBin, env.PRIZED_BIN);
  });

  test("create: a sandbox with the exact commit checked out, and a result Orca accepts", async () => {
    const r = await runPhase({ command: recipe.create, mode: "create", context, recipe, baseEnv: env });
    assert.equal(r.exitCode, 0, r.stderr);
    assert.equal(r.stdout.trim().split("\n").length, 1, "stdout is the result alone");
    const { result, connection } = parseResult(r.stdout);
    assert.equal(checkoutModeFailure(recipe, result), null);
    created = result;
    id = result.userData.resourceId;
    assert.match(id, /^sbx_/);
    assert.equal(connection.target.host, id);
    assert.equal(connection.target.port, 22);
    assert.equal(connection.target.username, process.env.USER || spawnSync("id", ["-un"], { encoding: "utf8" }).stdout.trim());
    assert.equal(connection.target.proxyCommand, `'${home}/.prized-orca/bin/prized-orca' proxy ${id}`);
    assert.equal(connection.target.identityFile, undefined);

    const sbx = api.sandboxes.get(id);
    assert.equal(sbx.template, "prized-agent-v1");
    assert.deepEqual(sbx.resources, { vcpu: 2, memoryMb: 4096 });
    assert.equal(sbx.autoPauseMin, 30);
    assert.equal(sbx.maxRuntimeMin, 10080);
    assert.deepEqual(sbx.labels, { via: "orca", "orca-instance": context.instanceId, "orca-workspace": "fix login bug" });
    assert.equal(connection.projectRoot, `${sbx.home}/work/repo`);

    const g = (...a) => spawnSync("git", ["-C", connection.projectRoot, ...a], { encoding: "utf8" }).stdout.trim();
    assert.equal(g("rev-parse", "HEAD"), repo.first, "the pinned commit, not the branch tip");
    assert.equal(g("branch", "--show-current"), "ada/fix-login");
    assert.equal(g("remote", "get-url", "origin"), "https://example.com/acme/app.git");
    assert.equal(g("config", "user.email"), "ada@example.com");
    assert.equal(g("for-each-ref", "refs/prized-orca"), "", "no temporary refs left in the sandbox");
    assert.equal(fs.existsSync(path.join(sbx.home, ".prized-orca", "repo.bundle")), false);
    assert.equal(spawnSync("git", ["-C", repo.dir, "for-each-ref", "refs/prized-orca"], { encoding: "utf8" }).stdout.trim(), "", "nor in the user's repo");

    const include = fs.readFileSync(path.join(home, ".prized-orca", "ssh_config"), "utf8");
    assert.match(include, new RegExp(`Match originalhost ${id} exec "'.*prized-orca' cert-hook ${id}"`));
    assert.match(include, new RegExp(`Host ${id}\\n    ProxyCommand '.*prized-orca' proxy ${id}\\nInclude ".*/.config/prized/sandboxes/${id}/ssh_config"`));
  });

  test("Orca's ssh connection reaches the checkout, as the sandbox user", async () => {
    const t = created.connection.target;
    const r = await orcaSsh(t, `git -C '${created.connection.projectRoot}' rev-parse HEAD`, { configFile: env.PRIZED_ORCA_SSH_CONFIG, env });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout.trim(), repo.first);
    const bg = await orcaSsh(t, "nohup sleep 600 >/dev/null 2>&1 & echo $!", { configFile: env.PRIZED_ORCA_SSH_CONFIG, env });
    assert.equal(bg.status, 0, bg.stderr);
    started.push(Number(bg.stdout.trim()));
  });

  test("sleep: suspend pauses the sandbox, and a reconnect is refused instead of waking it", async () => {
    const r = await runPhase({ command: recipe.suspend, mode: "suspend", context, recipe, baseEnv: env, stdin: payload("suspend", recipe, context, created) });
    assert.equal(r.exitCode, 0, r.stderr);
    assert.equal(r.stdout, "");
    assert.equal(api.sandboxes.get(id).state, "paused");
    const resumesBefore = api.sandboxes.get(id).resumes;
    const dial = await orcaSsh(created.connection.target, "true", { configFile: env.PRIZED_ORCA_SSH_CONFIG, env });
    assert.notEqual(dial.status, 0);
    assert.match(dial.stderr, /asleep in Orca/);
    assert.equal(api.sandboxes.get(id).state, "paused", "still paused");
    assert.equal(api.sandboxes.get(id).resumes, resumesBefore);
    const again = await runPhase({ command: recipe.suspend, mode: "suspend", context, recipe, baseEnv: env, stdin: payload("suspend", recipe, context, created) });
    assert.equal(again.exitCode, 0, "suspending a paused sandbox is fine");
  });

  test("wake: resume restarts it and prints a result that passes Orca's integrity check", async () => {
    // a short certificate this time, so the next connection has to renew it
    const r = await runPhase({
      command: recipe.resume,
      mode: "resume",
      context,
      recipe,
      baseEnv: { ...env, FAKE_CERT_VALIDITY: "+2m" },
      stdin: payload("resume", recipe, context, created),
    });
    assert.equal(r.exitCode, 0, r.stderr);
    const { result } = parseResult(r.stdout);
    assert.equal(resumeIntegrityError(created, result), null);
    assert.deepEqual(result, created);
    assert.equal(api.sandboxes.get(id).state, "running");
  });

  test("the cert hook renews a certificate about to end before ssh loads it", async () => {
    const short = certBefore(home, id);
    const r = await orcaSsh(created.connection.target, `git -C '${created.connection.projectRoot}' log -1 --format=%s`, { configFile: env.PRIZED_ORCA_SSH_CONFIG, env });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout.trim(), "first");
    const renewed = certBefore(home, id);
    assert.ok(renewed > short, `renewed ${renewed} after ${short}`);
    assert.equal(r.stderr.includes("prized-orca"), false, "the hook is quiet");
  });

  test("an idle pause (not Orca's sleep) is undone by the next connection", async () => {
    api.sandboxes.get(id).state = "paused";
    const r = await orcaSsh(created.connection.target, "echo back", { configFile: env.PRIZED_ORCA_SSH_CONFIG, env });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout.trim(), "back");
    assert.equal(api.sandboxes.get(id).state, "running");
  });

  test("destroy deletes the sandbox and its ssh block, and is safe to repeat", async () => {
    const r = await runPhase({ command: recipe.destroy, mode: "destroy", context, recipe, baseEnv: env, stdin: payload("destroy", recipe, context, created) });
    assert.equal(r.exitCode, 0, r.stderr);
    assert.equal(api.sandboxes.get(id).state, "deleted");
    assert.doesNotMatch(fs.readFileSync(path.join(home, ".prized-orca", "ssh_config"), "utf8"), new RegExp(id));
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(home, ".prized-orca", "state.json"), "utf8")).sandboxes, {});
    const again = await runPhase({ command: recipe.destroy, mode: "destroy", context, recipe, baseEnv: env, stdin: payload("destroy", recipe, context, created) });
    assert.equal(again.exitCode, 0, again.stderr);
  });

  test("the key went only to the API, never to the CLI, a file, a log or a command line", async () => {
    assert.ok(api.requests.length > 5);
    for (const req of api.requests) {
      if (req.path.startsWith("/api/v1/sandboxes/") && req.who === "login") continue; // the CLI's own calls
      if (req.who === "key") assert.ok(req.path.startsWith("/api/v1/") || req.path.startsWith("/edge/"));
    }
    const cliLog = fs.readFileSync(env.FAKE_CLI_LOG, "utf8");
    assert.doesNotMatch(cliLog, /KEY-LEAK/);
    assert.doesNotMatch(cliLog, /dcp_/);
    for (const [file, text] of [...allText(home), ...allText(path.join(root, "sandboxes"))]) {
      assert.equal(text.includes(API_KEY), false, `the key is in ${file}`);
      assert.equal(text.includes("ghp_secret123"), false, `the git credential is in ${file}`);
    }
    assert.ok(fs.readFileSync(path.join(home, ".prized-orca", "lifecycle.log"), "utf8").includes('"cmd":"create"'));
  });

  test("a create that fails deletes the sandbox it made and prints no result", async () => {
    api.opts.failExec = /git fetch --quiet/;
    try {
      const r = await runPhase({ command: recipe.create, mode: "create", context: { ...context, instanceId: "orca-fail-1" }, recipe, baseEnv: env });
      assert.notEqual(r.exitCode, 0);
      assert.equal(r.stdout, "");
      assert.match(r.stderr, /checkout in the sandbox failed/);
      const made = [...api.sandboxes.values()].filter((s) => s.labels["orca-instance"] === "orca-fail-1");
      assert.equal(made.length, 1);
      assert.equal(made[0].state, "deleted");
      assert.deepEqual(JSON.parse(fs.readFileSync(path.join(home, ".prized-orca", "state.json"), "utf8")).sandboxes, {});
    } finally {
      api.opts.failExec = null;
    }
  });

  test("on the free tier, a refused 7-day run time falls back to the limit the refusal names", async () => {
    api.opts.maxRuntimeLimit = 60;
    try {
      // as `orca vm recipe doctor --provision` runs it: no pinned source, so the local HEAD and origin
      const ctx = { ...context, instanceId: "orca-free-1", repoUrl: "", branch: "", ref: "", expectedRefHead: "" };
      const r = await runPhase({ command: recipe.create, mode: "create", context: ctx, recipe, baseEnv: env });
      assert.equal(r.exitCode, 0, r.stderr);
      const { result } = parseResult(r.stdout);
      const sbx = api.sandboxes.get(result.userData.resourceId);
      assert.equal(sbx.maxRuntimeMin, 60);
      assert.match(r.stderr, /run time was refused/);
      const g = (...a) => spawnSync("git", ["-C", result.connection.projectRoot, ...a], { encoding: "utf8" }).stdout.trim();
      assert.equal(g("rev-parse", "HEAD"), repo.head);
      assert.equal(g("remote", "get-url", "origin"), "https://example.com/acme/app.git", "the credentials in the local origin stay local");
      assert.equal(g("branch", "--show-current"), "orca-free1");
      const d = await runPhase({ command: recipe.destroy, mode: "destroy", context: ctx, recipe, baseEnv: env, stdin: payload("destroy", recipe, ctx, result) });
      assert.equal(d.exitCode, 0, d.stderr);
    } finally {
      api.opts.maxRuntimeLimit = null;
    }
  });

  test("Orca started from the Dock: the key comes from the login shell", async () => {
    const dockEnv = { ...env, FAKE_LOGIN_KEY: API_KEY };
    delete dockEnv.PRIZED_API_KEY;
    const ctx = { ...context, instanceId: "orca-dock-1" };
    const r = await runPhase({ command: recipe.create, mode: "create", context: ctx, recipe, baseEnv: dockEnv });
    assert.equal(r.exitCode, 0, r.stderr);
    const { result } = parseResult(r.stdout);
    const d = await runPhase({ command: recipe.destroy, mode: "destroy", context: ctx, recipe, baseEnv: dockEnv, stdin: payload("destroy", recipe, ctx, result) });
    assert.equal(d.exitCode, 0, d.stderr);
    const none = { ...env, FAKE_LOGIN_KEY: "" };
    delete none.PRIZED_API_KEY;
    const missing = await runPhase({ command: recipe.create, mode: "create", context: { ...context, instanceId: "orca-dock-2" }, recipe, baseEnv: none });
    assert.notEqual(missing.exitCode, 0);
    assert.match(missing.stderr, /PRIZED_API_KEY is not set/);
  });

  test("uninstall takes the Include line out and leaves the rest of ~/.ssh/config", async () => {
    const r = await runAdapter(["uninstall"], { env });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(fs.readFileSync(env.PRIZED_ORCA_SSH_CONFIG, "utf8"), "Host other\n    HostName example.com\n");
    assert.equal(fs.existsSync(path.join(home, ".prized-orca", "bin", "prized-orca")), false);
  });
});
