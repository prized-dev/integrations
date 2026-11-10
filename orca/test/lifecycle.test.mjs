// The whole life of a workspace, driven the way Orca drives a plugin recipe (test/orca-harness.mjs):
// setup, create, Orca's ssh connection, sleep, a refused reconnect, wake, a certificate renewal, an
// idle pause, delete, and the ways a create or a sleep can go wrong. The Prized API is a local fake
// (test/fake-api.mjs); the prized CLI is a fake signed in with a login (test/fakes/prized); ssh and
// sshd are the real OpenSSH, with a sshd set up like a sandbox guest. Every path has a space in it.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { after, before, describe, test } from "node:test";
import { API_KEY, startFakeApi } from "./fake-api.mjs";
import { machineLabel } from "../src/commands.mjs";
import { PKG, allText, haveSshKit, makeRepo, makeSshdKit, runAdapter, sh, tempDir, testEnv } from "./helpers.mjs";
import { checkoutModeFailure, orcaSsh, orcaSshExit, parseRecipe, parseResult, payload, resumeIntegrityError, runPhase } from "./orca-harness.mjs";

const kit = haveSshKit();
const skip = kit ? false : "needs ssh, ssh-keygen, setpriv and sshd (/usr/sbin/sshd)";

function certBefore(home, id) {
  const cert = path.join(home, ".config", "prized", "sandboxes", id, "cert.pub");
  const out = spawnSync("ssh-keygen", ["-L", "-f", cert], { encoding: "utf8" }).stdout;
  return /Valid: from \S+ to (\S+)/.exec(out)?.[1];
}

describe("a workspace on a Prized sandbox", { skip, concurrency: false }, () => {
  let root, api, env, repo, recipe, context, created, id, home, ssh, controlDir;
  const started = [];
  const instanceSandboxes = (instance) => [...api.sandboxes.values()].filter((s) => s.labels?.["orca-instance"] === instance);
  const state = () => JSON.parse(fs.readFileSync(path.join(home, ".prized-orca", "state.json"), "utf8")).sandboxes;
  const phase = (mode, extra = {}) =>
    runPhase({
      command: recipe[mode],
      mode,
      context: extra.context ?? context,
      recipe,
      baseEnv: extra.env ?? env,
      stdin: mode === "create" ? undefined : payload(mode, recipe, extra.context ?? context, extra.result ?? created),
      cancelAfterMs: extra.cancelAfterMs,
    });

  before(async () => {
    root = tempDir("prized orca test-");
    // a sandbox's home is /home/prized: no space there, unlike everything on "this machine"
    api = await startFakeApi(tempDir("prized-orca-sandboxes-"));
    repo = makeRepo(path.join(root, "repo"));
    env = testEnv(root, {
      PRIZED_API_KEY: API_KEY,
      PRIZED_ORCA_API_URL: api.apiUrl,
      FAKE_API: api.base,
      FAKE_SSHD_DIR: makeSshdKit(path.join(root, "sshd")),
      NODE_OPTIONS: `--require ${JSON.stringify(path.join(PKG, "test", "spy.cjs"))}`,
      SPY_LOG: path.join(root, "spy.log"),
    });
    home = env.HOME;
    // Orca's own control path has no space (ssh would read -o ControlPath=... as two words). ssh
    // refuses one of 108 bytes or more and %C is 40 of them, so under a long TMPDIR (a CI runner's)
    // the socket goes in /tmp.
    controlDir = fs.mkdtempSync(path.join(api.root.length <= 60 ? api.root : "/tmp", "cm-"));
    ssh = { configFile: env.PRIZED_ORCA_SSH_CONFIG, controlPath: path.join(controlDir, "%C"), env };
    recipe = parseRecipe(fs.readFileSync(path.join(PKG, "vm-recipe.json"), "utf8"));
    context = {
      instanceId: "orca-7d1f0c2a-55aa-4b1e-9f00-123456789abc",
      recipeId: recipe.id,
      projectId: "proj-1",
      workspaceId: "ws-1",
      workspaceName: "fix login bug",
      repoPath: repo.dir,
      // An SSH address through an alias in ~/.ssh/config (setup's test writes "Host other" there): the
      // sandbox gets https://example.com/acme/app.git. Orca passes no credentials; the plugin strips
      // any regardless.
      repoUrl: "git@other:acme/app.git",
      branch: "ada/fix-login",
      ref: "origin/main",
      expectedRefHead: repo.first,
      orcaVersion: "1.4.245",
    };
  });

  after(async () => {
    if (created) await orcaSshExit(created.connection.target, ssh);
    for (const pid of started) {
      try {
        process.kill(pid);
      } catch {
        // already gone
      }
    }
    await api?.close();
    if (controlDir) fs.rmSync(controlDir, { recursive: true, force: true });
    if (api) fs.rmSync(api.root, { recursive: true, force: true });
    if (root) fs.rmSync(root, { recursive: true, force: true });
  });

  test("setup installs the adapter and adds one Include line to ~/.ssh/config", async () => {
    fs.writeFileSync(env.PRIZED_ORCA_SSH_CONFIG, "Host other\n    HostName example.com\n", { mode: 0o644 });
    const r = await runAdapter(["setup", "--yes"], { env });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout, "");
    assert.match(r.stderr, /prized CLI: .* 0\.21\.9 \(signed in with prized login\)/);
    assert.ok(fs.existsSync(path.join(home, ".prized-orca", "bin", "prized-orca")));
    const cfg = fs.readFileSync(env.PRIZED_ORCA_SSH_CONFIG, "utf8");
    assert.ok(cfg.startsWith(`# Added by the Prized Orca plugin (prized-orca setup): Orca workspaces on Prized sandboxes.\nInclude "${home}/.prized-orca/ssh_config"\n\nHost other\n`));
    assert.equal(fs.statSync(env.PRIZED_ORCA_SSH_CONFIG).mode & 0o777, 0o644);
    const again = await runAdapter(["setup", "--yes"], { env });
    assert.equal(again.status, 0, again.stderr);
    assert.equal(fs.readFileSync(env.PRIZED_ORCA_SSH_CONFIG, "utf8"), cfg, "a second setup changes nothing");
    const conf = JSON.parse(fs.readFileSync(path.join(home, ".prized-orca", "config.json"), "utf8"));
    assert.equal(conf.prizedBin, env.PRIZED_BIN);
    const old = await runAdapter(["setup", "--yes"], { env: { ...env, FAKE_CLI_VERSION: "0.21.8" } });
    assert.notEqual(old.status, 0);
    assert.match(old.stderr, /needs 0\.21\.9 or later/);
    // `prized login --token`: lists sandboxes fine, but Prized refuses it a shell
    for (const signin of ["api-key", "limited"]) {
      const keyed = await runAdapter(["setup", "--yes"], { env: { ...env, FAKE_CLI_SIGNIN: signin } });
      assert.notEqual(keyed.status, 0, signin);
      assert.match(keyed.stderr, /the prized CLI is signed in with an API key, so it can't open a shell in a sandbox/);
      assert.match(keyed.stderr, /run `prized login` and sign in in the browser/);
    }
  });

  test("create: a sandbox with the exact commit checked out, and a result Orca accepts", async () => {
    const r = await phase("create");
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
    assert.deepEqual(sbx.labels, { via: "orca", "orca-host": machineLabel(), "orca-instance": context.instanceId, "orca-workspace": "fix login bug" });
    assert.match(machineLabel(), /^[0-9a-f]{12}$/);
    assert.equal(connection.projectRoot, `${sbx.home}/work/repo`);

    const g = (...a) => spawnSync("git", ["-C", connection.projectRoot, ...a], { encoding: "utf8" }).stdout.trim();
    assert.equal(g("rev-parse", "HEAD"), repo.first, "the pinned commit, not the branch tip");
    assert.equal(g("branch", "--show-current"), "ada/fix-login");
    assert.equal(g("remote", "get-url", "origin"), "https://example.com/acme/app.git");
    assert.equal(g("config", "user.email"), "ada@example.com");
    assert.equal(g("for-each-ref", "refs/prized-orca"), "", "no temporary refs left in the sandbox");
    assert.equal(fs.existsSync(path.join(sbx.home, ".prized-orca", "repo.bundle")), false);
    assert.equal(spawnSync("git", ["-C", repo.dir, "for-each-ref", "refs/prized-orca"], { encoding: "utf8" }).stdout.trim(), "", "nor in the user's repo");
    assert.deepEqual(fs.readdirSync(env.TMPDIR), [], "the bundle's temporary directory is gone");

    const include = fs.readFileSync(path.join(home, ".prized-orca", "ssh_config"), "utf8");
    assert.ok(include.includes(`Match originalhost ${id} exec "'${home}/.prized-orca/bin/prized-orca' cert-hook ${id}"\nHost ${id}\n    ProxyCommand '${home}/.prized-orca/bin/prized-orca' proxy ${id}\nInclude "${home}/.config/prized/sandboxes/${id}/ssh_config"`));
  });

  test("Orca's ssh connection reaches the checkout, as the sandbox user", async () => {
    const t = created.connection.target;
    const r = await orcaSsh(t, `git -C '${created.connection.projectRoot}' rev-parse HEAD`, ssh);
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout.trim(), repo.first);
    const bg = await orcaSsh(t, "nohup sleep 600 >/dev/null 2>&1 & echo $!", ssh);
    assert.equal(bg.status, 0, bg.stderr);
    started.push(Number(bg.stdout.trim()));
  });

  test("sleep: suspend pauses the sandbox, and a reconnect is refused instead of waking it", async () => {
    const r = await phase("suspend");
    assert.equal(r.exitCode, 0, r.stderr);
    assert.equal(r.stdout, "");
    assert.equal(api.sandboxes.get(id).state, "paused");
    await orcaSshExit(created.connection.target, ssh); // Orca disconnects a suspended workspace
    const resumesBefore = api.sandboxes.get(id).resumes;
    const dial = await orcaSsh(created.connection.target, "true", ssh);
    assert.notEqual(dial.status, 0);
    // with ControlPersist, ssh sends the ProxyCommand's stderr nowhere; without it the reason shows
    const plain = await orcaSsh(created.connection.target, "true", { ...ssh, controlPath: null });
    assert.notEqual(plain.status, 0);
    assert.match(plain.stderr, /asleep in Orca/);
    assert.match(fs.readFileSync(path.join(home, ".prized-orca", "lifecycle.log"), "utf8"), new RegExp(`"cmd":"proxy","sandbox":"${id}","refused":"asleep"`));
    assert.equal(api.sandboxes.get(id).state, "paused", "still paused");
    assert.equal(api.sandboxes.get(id).resumes, resumesBefore);
    const again = await phase("suspend");
    assert.equal(again.exitCode, 0, "suspending a paused sandbox is fine");
  });

  test("wake: resume restarts it and prints a result that passes Orca's integrity check", async () => {
    // a short certificate this time, so the next connection has to renew it
    const r = await phase("resume", { env: { ...env, FAKE_CERT_VALIDITY: "+2m" } });
    assert.equal(r.exitCode, 0, r.stderr);
    const { result } = parseResult(r.stdout);
    assert.equal(resumeIntegrityError(created, result), null);
    assert.deepEqual(result, created);
    assert.equal(api.sandboxes.get(id).state, "running");
  });

  test("the cert hook renews a certificate about to end before ssh loads it", async () => {
    const short = certBefore(home, id);
    const r = await orcaSsh(created.connection.target, `git -C '${created.connection.projectRoot}' log -1 --format=%s`, ssh);
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout.trim(), "first");
    const renewed = certBefore(home, id);
    assert.ok(renewed > short, `renewed ${renewed} after ${short}`);
    assert.equal(r.stderr.includes("prized-orca"), false, "the hook is quiet");
    // and a fresh certificate is left alone (ssh runs the hook on every dial, and Orca's ssh -G too)
    const shells = () => fs.readFileSync(env.FAKE_CLI_LOG, "utf8").split("\n").filter((l) => l.startsWith('["sandbox","ssh"')).length;
    const before = shells();
    await orcaSshExit(created.connection.target, ssh);
    const again = await orcaSsh(created.connection.target, "true", ssh);
    assert.equal(again.status, 0, again.stderr);
    assert.equal(shells(), before, "no renewal while the certificate is fresh");
  });

  test("an idle pause (not Orca's sleep) is undone by the next connection", async () => {
    await orcaSshExit(created.connection.target, ssh); // the laptop slept; the connection is gone
    api.sandboxes.get(id).state = "paused";
    const r = await orcaSsh(created.connection.target, "echo back", ssh);
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout.trim(), "back");
    assert.equal(api.sandboxes.get(id).state, "running");
  });

  test("a sleep that can't reach Prized leaves the workspace as it was", async () => {
    const noKey = { ...env, FAKE_LOGIN_KEY: "" };
    delete noKey.PRIZED_API_KEY;
    const r = await phase("suspend", { env: noKey });
    assert.notEqual(r.exitCode, 0);
    assert.match(r.stderr, /PRIZED_API_KEY is not set/);
    assert.equal(api.sandboxes.get(id).state, "running");
    assert.equal(state()[id].asleep, false);
    await orcaSshExit(created.connection.target, ssh);
    const dial = await orcaSsh(created.connection.target, "echo still", ssh);
    assert.equal(dial.status, 0, dial.stderr);
  });

  test("a sleep that races a reconnect's wake pauses again until it stays paused", async () => {
    await orcaSshExit(created.connection.target, ssh);
    const pauses = api.sandboxes.get(id).pauses;
    api.opts.flipAfterPause = true;
    const r = await phase("suspend");
    assert.equal(r.exitCode, 0, r.stderr);
    assert.equal(api.sandboxes.get(id).state, "paused");
    assert.equal(api.sandboxes.get(id).pauses, pauses + 2);
  });

  test("pull brings the workspace's branch here to push from here, and puts a sleeping sandbox back to sleep", async () => {
    const root = created.connection.projectRoot;
    const sbx = api.sandboxes.get(id);
    assert.equal(sbx.state, "paused", "the workspace is asleep in Orca (the test before)");
    const genv = { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: env.GIT_CONFIG_GLOBAL };
    const inSandbox = (...a) => sh("git", ["-C", root, ...a], { env: genv });
    const here = (...a) => sh("git", ["-C", repo.dir, ...a], { env: genv });
    for (const f of ["login.js", "notes.md"]) {
      fs.writeFileSync(path.join(root, f), `${f}\n`);
      inSandbox("add", f);
      inSandbox("commit", "-q", "-m", `add ${f}`);
    }
    fs.writeFileSync(path.join(root, "scratch.txt"), "not committed\n");
    const head = inSandbox("rev-parse", "HEAD");
    const { resumes, pauses } = sbx;

    const r = await runAdapter(["pull"], { env, cwd: repo.dir });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout, "");
    assert.match(r.stderr, new RegExp(`pulled ada/fix-login from ${id} into .* as the branch ada/fix-login \\([0-9a-f]{12}, 2 commits since the workspace started\\)`));
    assert.match(r.stderr, /push it from here with your own git sign-in: git -C '.*' push origin 'ada\/fix-login'$/m);
    assert.match(r.stderr, /1 uncommitted change in the workspace stayed there/);
    assert.equal(here("rev-parse", "refs/heads/ada/fix-login"), head);
    assert.equal(fs.existsSync(path.join(root, ".git", "prized-orca-pull.bundle")), false, "the bundle is gone from the sandbox");
    assert.equal(sbx.state, "paused", "asleep before, asleep after");
    assert.equal(sbx.resumes, resumes + 1);
    assert.ok(sbx.pauses > pauses);
    assert.equal(state()[id].asleep, true);
    assert.match(fs.readFileSync(path.join(home, ".prized-orca", "lifecycle.log"), "utf8"), new RegExp(`"cmd":"pull","sandbox":"${id}","commits":2,"ok":true`));

    // from here it pushes with this machine's own git sign-in (a bare repository stands in for GitHub)
    const bare = path.join(api.root, "origin.git");
    sh("git", ["init", "-q", "--bare", bare], { env: genv });
    here("push", "-q", bare, "ada/fix-login");
    assert.equal(sh("git", ["-C", bare, "rev-parse", "refs/heads/ada/fix-login"], { env: genv }), head);

    // pulling again is a no-op fast-forward; a local branch that moved apart is left alone
    const again = await runAdapter(["pull", id], { env });
    assert.equal(again.status, 0, again.stderr);
    const apartCommit = here("commit-tree", `${repo.first}^{tree}`, "-p", repo.first, "-m", "made here");
    here("update-ref", "refs/heads/ada/fix-login", apartCommit);
    const apart = await runAdapter(["pull", id], { env });
    assert.notEqual(apart.status, 0);
    assert.match(apart.stderr, /non-fast-forward/);
    assert.match(apart.stderr, /pull into a new branch: prized-orca pull sbx_\S+ --as 'ada\/fix-login-sandbox'/);
    assert.equal(here("rev-parse", "refs/heads/ada/fix-login"), apartCommit, "left alone");
    const as = await runAdapter(["pull", id, "--as", "ada/from-sandbox"], { env });
    assert.equal(as.status, 0, as.stderr);
    assert.equal(here("rev-parse", "refs/heads/ada/from-sandbox"), head);
    assert.match(as.stderr, /push origin 'ada\/from-sandbox'/);
    assert.equal(sbx.state, "paused");
    const bad = await runAdapter(["pull", id, "--force"], { env });
    assert.notEqual(bad.status, 0);
    assert.match(bad.stderr, /unknown option --force/);

    // two workspaces: the one whose project this folder is, or a list to name one from
    const file = path.join(home, ".prized-orca", "state.json");
    const saved = fs.readFileSync(file, "utf8");
    const two = JSON.parse(saved);
    two.sandboxes.sbx_00000000000000bb = { repoPath: api.root, projectRoot: "/home/prized/work/other", workspaceName: "other one", asleep: false };
    fs.writeFileSync(file, JSON.stringify(two));
    const which = await runAdapter(["pull"], { env, cwd: env.TMPDIR });
    assert.notEqual(which.status, 0);
    assert.match(which.stderr, new RegExp(`which workspace\\? .*${id}.*sbx_00000000000000bb \\("other one"`));
    const fromRepo = await runAdapter(["pull", "--as", "ada/from-repo-folder"], { env, cwd: repo.dir });
    assert.equal(fromRepo.status, 0, fromRepo.stderr);
    assert.equal(here("rev-parse", "refs/heads/ada/from-repo-folder"), head);
    fs.writeFileSync(file, saved);

    // an idle pause (not Orca's sleep) is left running after the copy: it pauses itself again
    const woke = await phase("resume");
    assert.equal(woke.exitCode, 0, woke.stderr);
    sbx.state = "paused";
    const idle = await runAdapter(["pull", id, "--as", "ada/idle"], { env });
    assert.equal(idle.status, 0, idle.stderr);
    assert.equal(sbx.state, "running");
    assert.doesNotMatch(idle.stderr, /pausing/);
    const unknown = await runAdapter(["pull", "sbx_00000000000000aa"], { env });
    assert.notEqual(unknown.status, 0);
    assert.match(unknown.stderr, /backs no Orca workspace on this machine/);
  });

  test("destroy saves the workspace's work here, then deletes the sandbox and its ssh block, and is safe to repeat", async () => {
    const root = created.connection.projectRoot;
    const genv = { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: env.GIT_CONFIG_GLOBAL };
    const wsHead = sh("git", ["-C", root, "rev-parse", "HEAD"], { env: genv });
    const r = await phase("destroy");
    assert.equal(r.exitCode, 0, r.stderr);
    // the pull test copied the two commits here already; scratch.txt, uncommitted, is all that's new
    const saved = `refs/heads/prized-orca/saved/${id}/uncommitted`;
    const here = (...a) => sh("git", ["-C", repo.dir, ...a], { env: genv });
    assert.match(r.stderr, new RegExp(`saved what ${id} had that this machine didn't, as branches in .*: prized-orca/saved/${id}/uncommitted$`, "m"));
    assert.equal(here("rev-parse", `${saved}^`), wsHead, "the uncommitted changes ride on the workspace's last commit");
    assert.equal(here("show", `${saved}:scratch.txt`), "not committed");
    assert.equal(here("for-each-ref", `refs/heads/prized-orca/saved/${id}/ada`), "", "the branch, already here, is not saved again");
    assert.match(fs.readFileSync(path.join(home, ".prized-orca", "lifecycle.log"), "utf8"), new RegExp(`"cmd":"destroy","sandbox":"${id}","saved":\\["prized-orca/saved/${id}/uncommitted"\\]`));
    assert.equal(api.sandboxes.get(id).state, "deleted");
    assert.doesNotMatch(fs.readFileSync(path.join(home, ".prized-orca", "ssh_config"), "utf8"), new RegExp(id));
    assert.deepEqual(state(), {});
    const again = await phase("destroy");
    assert.equal(again.exitCode, 0, again.stderr);
  });

  test("Remove copies nothing when there is nothing new, and keeps the sandbox when the copy fails", async () => {
    const genv = { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: env.GIT_CONFIG_GLOBAL };
    const here = (...a) => spawnSync("git", ["-C", repo.dir, ...a], { encoding: "utf8", env: genv });
    const make = async (instanceId, extra = {}) => {
      const ctx = { ...context, instanceId, ...extra };
      const c = await phase("create", { context: ctx });
      assert.equal(c.exitCode, 0, c.stderr);
      const result = parseResult(c.stdout).result;
      return { ctx, result, sid: result.userData.resourceId };
    };

    // nothing new here (its one commit was pulled already): nothing downloaded, no saved branch
    const quiet = await make("orca-save-1");
    const qroot = quiet.result.connection.projectRoot;
    fs.writeFileSync(path.join(qroot, "pulled.txt"), "pulled\n");
    sh("git", ["-C", qroot, "add", "pulled.txt"], { env: genv });
    sh("git", ["-C", qroot, "commit", "-q", "-m", "pulled"], { env: genv });
    const pulled = await runAdapter(["pull", quiet.sid, "--as", "ada/quiet"], { env });
    assert.equal(pulled.status, 0, pulled.stderr);
    const before = api.requests.length;
    const d1 = await phase("destroy", { context: quiet.ctx, result: quiet.result });
    assert.equal(d1.exitCode, 0, d1.stderr);
    assert.match(d1.stderr, /nothing to save/);
    assert.equal(api.requests.slice(before).filter((q) => q.path.endsWith("/files/download")).length, 0);
    assert.notEqual(here("rev-parse", "--verify", "-q", `refs/heads/prized-orca/saved/${quiet.sid}`).status, 0);
    assert.equal(api.sandboxes.get(quiet.sid).state, "deleted");

    // a commit, then Orca puts it to sleep, then Remove: the copy fails, so the sandbox stays
    const busy = await make("orca-save-2");
    const root = busy.result.connection.projectRoot;
    fs.writeFileSync(path.join(root, "late.txt"), "late\n");
    sh("git", ["-C", root, "add", "late.txt"], { env: genv });
    sh("git", ["-C", root, "commit", "-q", "-m", "late"], { env: genv });
    const busyHead = sh("git", ["-C", root, "rev-parse", "HEAD"], { env: genv });
    const slept = await phase("suspend", { context: busy.ctx, result: busy.result });
    assert.equal(slept.exitCode, 0, slept.stderr);
    api.opts.failExec = /git bundle create/;
    try {
      const failed = await phase("destroy", { context: busy.ctx, result: busy.result });
      assert.notEqual(failed.exitCode, 0);
      assert.match(failed.stderr, new RegExp(`could not copy the workspace's work from ${busy.sid} to this machine, so the sandbox was not deleted`));
      assert.match(failed.stderr, new RegExp(`prized sandbox rm ${busy.sid}`));
      assert.equal(api.sandboxes.get(busy.sid).state, "paused", "kept, and asleep again");
      assert.ok(state()[busy.sid], "still a workspace here, so pull still reaches it");
    } finally {
      api.opts.failExec = null;
    }
    const d2 = await phase("destroy", { context: busy.ctx, result: busy.result });
    assert.equal(d2.exitCode, 0, d2.stderr);
    assert.match(d2.stderr, new RegExp(`: prized-orca/saved/${busy.sid}/ada/fix-login$`, "m"));
    assert.equal(here("rev-parse", `refs/heads/prized-orca/saved/${busy.sid}/ada/fix-login`).stdout.trim(), busyHead);
    assert.equal(api.sandboxes.get(busy.sid).state, "deleted");
    assert.equal(state()[busy.sid], undefined);

    // another branch, a stash, an uncommitted change and a second worktree (as Orca makes for a new
    // workspace on this host), with only a partial record of the workspace here (the state was
    // lost, then a sleep wrote what it knows): Orca's payload and environment fill in the rest
    const lost = await make("orca-save-3");
    const lroot = lost.result.connection.projectRoot;
    const inSandbox = (...a) => sh("git", ["-C", lroot, ...a], { env: genv });
    fs.writeFileSync(path.join(lroot, "stashed.txt"), "stashed\n");
    inSandbox("stash", "push", "-q", "-u");
    const side = inSandbox("commit-tree", `${repo.first}^{tree}`, "-p", repo.first, "-m", "side work");
    inSandbox("update-ref", "refs/heads/side", side);
    fs.writeFileSync(path.join(lroot, "README.md"), "# app, edited\n");
    const wt = `${lroot}-second`;
    inSandbox("worktree", "add", "-q", "-b", "second", wt);
    fs.writeFileSync(path.join(wt, "second.txt"), "second\n");
    const file = path.join(home, ".prized-orca", "state.json");
    const st = JSON.parse(fs.readFileSync(file, "utf8"));
    st.sandboxes[lost.sid] = { asleep: false };
    fs.writeFileSync(file, JSON.stringify(st));
    const d3 = await phase("destroy", { context: lost.ctx, result: lost.result });
    assert.equal(d3.exitCode, 0, d3.stderr);
    const p3 = `refs/heads/prized-orca/saved/${lost.sid}`;
    assert.equal(here("rev-parse", `${p3}/side`).stdout.trim(), side);
    assert.equal(here("show", `${p3}/stash^3:stashed.txt`).stdout, "stashed\n", "the stash, untracked file included");
    assert.equal(here("show", `${p3}/uncommitted:README.md`).stdout, "# app, edited\n");
    assert.equal(here("show", `${p3}/uncommitted-repo-second:second.txt`).stdout, "second\n", "the second worktree's change");
    assert.equal(api.sandboxes.get(lost.sid).state, "deleted");

    // the start commit is here but on no branch (fetched to FETCH_HEAD only), and the workspace is
    // still at it with an uncommitted change: git leaves that branch out of the bundle, so it is
    // saved from the commit this repository has
    const dangling = sh("git", ["-C", repo.dir, "commit-tree", `${repo.first}^{tree}`, "-p", repo.first, "-m", "fetched, on no branch"], { env: genv });
    const dang = await make("orca-save-5", { expectedRefHead: dangling });
    fs.writeFileSync(path.join(dang.result.connection.projectRoot, "late.txt"), "late\n");
    const d5 = await phase("destroy", { context: dang.ctx, result: dang.result });
    assert.equal(d5.exitCode, 0, d5.stderr);
    const p5 = `refs/heads/prized-orca/saved/${dang.sid}`;
    assert.equal(here("rev-parse", `${p5}/ada/fix-login`).stdout.trim(), dangling);
    assert.equal(here("show", `${p5}/uncommitted:late.txt`).stdout, "late\n");

    // a sandbox in error may still have its disk: it is kept, and the user is told how to delete it
    const broken = await make("orca-save-4");
    api.sandboxes.get(broken.sid).state = "error";
    const d4 = await phase("destroy", { context: broken.ctx, result: broken.result });
    assert.notEqual(d4.exitCode, 0);
    assert.match(d4.stderr, new RegExp(`${broken.sid} is in error .* it was not deleted`));
    assert.match(d4.stderr, new RegExp(`prized sandbox rm ${broken.sid}`));
    assert.equal(api.sandboxes.get(broken.sid).state, "error");
    api.sandboxes.get(broken.sid).state = "deleted"; // the user runs prized sandbox rm
    const retry = await phase("destroy", { context: broken.ctx, result: broken.result });
    assert.equal(retry.exitCode, 0, retry.stderr);
    assert.equal(state()[broken.sid], undefined);
  });

  test("a CLI signed in with an API key stops the create before anything is made", async () => {
    const before = api.requests.length;
    const r = await phase("create", { context: { ...context, instanceId: "orca-keyed-1" }, env: { ...env, FAKE_CLI_SIGNIN: "api-key" } });
    assert.notEqual(r.exitCode, 0);
    assert.equal(r.stdout, "");
    assert.match(r.stderr, /signed in with an API key, so it can't open a shell in a sandbox/);
    assert.equal(instanceSandboxes("orca-keyed-1").length, 0);
    assert.equal(api.requests.slice(before).filter((q) => q.method === "POST" && q.path === "/api/v1/sandboxes").length, 0, "no create was sent");
  });

  test("a create that fails deletes the sandbox it made and prints no result", async () => {
    api.opts.failExec = /git fetch --quiet/;
    try {
      const ctx = { ...context, instanceId: "orca-fail-1" };
      const r = await phase("create", { context: ctx });
      assert.notEqual(r.exitCode, 0);
      assert.equal(r.stdout, "");
      assert.match(r.stderr, /checkout in the sandbox failed/);
      assert.deepEqual(instanceSandboxes("orca-fail-1").map((s) => s.state), ["deleted"]);
      assert.deepEqual(state(), {});
      assert.deepEqual(fs.readdirSync(env.TMPDIR), []);
    } finally {
      api.opts.failExec = null;
    }
  });

  test("Orca's Cancel while the create is unanswered still deletes what it made", async () => {
    api.opts.holdCreateMs = 4_000;
    try {
      const r = await phase("create", { context: { ...context, instanceId: "orca-cancel-1" }, cancelAfterMs: 1_000 });
      assert.notEqual(r.exitCode, 0);
      assert.equal(r.stdout, "");
      assert.match(r.stderr, /deleting any sandbox labelled orca-instance=orca-cancel-1/);
      assert.deepEqual(instanceSandboxes("orca-cancel-1").map((s) => s.state), ["deleted"]);
      assert.deepEqual(fs.readdirSync(env.TMPDIR), []);
    } finally {
      api.opts.holdCreateMs = 0;
    }
  });

  test("Orca's Cancel in the middle of opening the shell deletes the sandbox before exiting", async () => {
    const r = await phase("create", { context: { ...context, instanceId: "orca-cancel-2" }, env: { ...env, FAKE_SSH_DELAY_MS: "6000" }, cancelAfterMs: 2_500 });
    assert.notEqual(r.exitCode, 0);
    const made = instanceSandboxes("orca-cancel-2");
    assert.equal(made.length, 1);
    assert.equal(made[0].state, "deleted");
    assert.match(r.stderr, new RegExp(`deleted ${made[0].id}`), "the delete was answered before the exit");
    assert.deepEqual(state(), {});
    // The signal reaches the prized CLI too, and this create is blocked on it: it still ends as a
    // cancel, with one line in the log, not as a failed shell.
    assert.doesNotMatch(r.stderr, /could not open a shell/);
    const log = fs.readFileSync(path.join(home, ".prized-orca", "lifecycle.log"), "utf8");
    const lines = log.split("\n").filter((l) => l.includes('"instance":"orca-cancel-2"') && !l.includes('"start":true') && !l.includes('"ms":'));
    assert.deepEqual(lines.map((l) => JSON.parse(l)).map(({ t, pid, ...rest }) => rest), [
      { cmd: "create", sandbox: made[0].id, instance: "orca-cancel-2" },
      { cmd: "create", ok: false, cancelled: "SIGTERM", sandbox: made[0].id, instance: "orca-cancel-2" },
    ]);
    assert.doesNotMatch(log, /could not open a shell/);
  });

  test("a definite refusal makes nothing, so nothing is deleted and the exit is quick", async () => {
    api.opts.refuseCreate = { status: 409, body: { error: "insufficient_balance", message: "Top up to start a sandbox." } };
    try {
      const deletes = api.requests.filter((q) => q.method === "DELETE").length;
      const t = Date.now();
      const r = await phase("create", { context: { ...context, instanceId: "orca-refused-1" } });
      assert.notEqual(r.exitCode, 0);
      assert.match(r.stderr, /Top up to start a sandbox/);
      assert.doesNotMatch(r.stderr, /deleting any sandbox|prized sandbox rm/);
      assert.equal(api.requests.filter((q) => q.method === "DELETE").length, deletes);
      assert.ok(Date.now() - t < 2_000, `took ${Date.now() - t} ms`);
      assert.deepEqual(fs.readdirSync(env.TMPDIR), []);
    } finally {
      api.opts.refuseCreate = null;
    }
  });

  test("a create whose answer is lost is asked again, and makes one sandbox", async () => {
    api.opts.dropCreates = 1;
    const ctx = { ...context, instanceId: "orca-lost-1" };
    const r = await phase("create", { context: ctx });
    assert.equal(r.exitCode, 0, r.stderr);
    const { result } = parseResult(r.stdout);
    const made = instanceSandboxes("orca-lost-1");
    assert.deepEqual(made.map((s) => s.id), [result.userData.resourceId]);
    const d = await phase("destroy", { context: ctx, result });
    assert.equal(d.exitCode, 0, d.stderr);
  });

  test("on the free tier, a refused 7-day run time falls back to the limit the refusal names", async () => {
    api.opts.maxRuntimeLimit = 60;
    try {
      // as `orca vm recipe doctor --provision` runs it: no pinned source, so the local HEAD and origin
      const ctx = { ...context, instanceId: "orca-free-1", repoUrl: "", branch: "", ref: "", expectedRefHead: "" };
      const r = await phase("create", { context: ctx });
      assert.equal(r.exitCode, 0, r.stderr);
      const { result } = parseResult(r.stdout);
      const sbx = api.sandboxes.get(result.userData.resourceId);
      assert.equal(sbx.maxRuntimeMin, 60);
      assert.match(r.stderr, /run time was refused/);
      const g = (...a) => spawnSync("git", ["-C", result.connection.projectRoot, ...a], { encoding: "utf8" }).stdout.trim();
      assert.equal(g("rev-parse", "HEAD"), repo.head);
      assert.equal(g("remote", "get-url", "origin"), "https://example.com/acme/app.git", "the credentials in the local origin stay local");
      assert.equal(g("branch", "--show-current"), "orca-free1");
      const d = await phase("destroy", { context: ctx, result });
      assert.equal(d.exitCode, 0, d.stderr);
    } finally {
      api.opts.maxRuntimeLimit = null;
    }
  });

  test("Orca started from the Dock: the key comes from the login shell", async () => {
    const dockEnv = { ...env, FAKE_LOGIN_KEY: API_KEY };
    delete dockEnv.PRIZED_API_KEY;
    const ctx = { ...context, instanceId: "orca-dock-1" };
    const r = await phase("create", { context: ctx, env: dockEnv });
    assert.equal(r.exitCode, 0, r.stderr);
    const { result } = parseResult(r.stdout);
    const d = await phase("destroy", { context: ctx, env: dockEnv, result });
    assert.equal(d.exitCode, 0, d.stderr);
    const none = { ...env, FAKE_LOGIN_KEY: "" };
    delete none.PRIZED_API_KEY;
    const missing = await phase("create", { context: { ...context, instanceId: "orca-dock-2" }, env: none });
    assert.notEqual(missing.exitCode, 0);
    assert.match(missing.stderr, /PRIZED_API_KEY is not set/);
  });

  test("doctor names a sandbox the plugin made that backs no workspace", async () => {
    const ok = await runAdapter(["doctor"], { env });
    assert.equal(ok.status, 0, ok.stderr);
    assert.match(ok.stderr, /ok   prized login \(opens shells\): signed in/);
    const keyed = await runAdapter(["doctor"], { env: { ...env, FAKE_CLI_SIGNIN: "limited" } });
    assert.equal(keyed.status, 1);
    assert.match(keyed.stderr, /FAIL prized login \(opens shells\): signed in with an API key, which can't open a shell: run prized login/);
    const old = new Date(Date.now() - 3_600_000).toISOString();
    const add = (id, labels, createdAt = old) => api.sandboxes.set(id, { id, state: "paused", labels, home: api.root, createdAt });
    add("sbx_00000000000000fd", { via: "orca", "orca-host": "0123456789ab", "orca-workspace": "a teammate's" });
    add("sbx_00000000000000fe", { via: "orca", "orca-host": machineLabel() }, new Date().toISOString());
    const others = await runAdapter(["doctor"], { env });
    assert.equal(others.status, 0, "another machine's sandbox, and one still being made here, are not strays");
    add("sbx_00000000000000ff", { via: "orca", "orca-host": machineLabel(), "orca-workspace": "old one" });
    const r = await runAdapter(["doctor"], { env });
    assert.equal(r.status, 1);
    assert.match(r.stderr, /FAIL no stray sandboxes from this machine: sbx_00000000000000ff \(paused, workspace "old one"\)/);
    for (const s of ["fd", "fe", "ff"]) api.sandboxes.get(`sbx_00000000000000${s}`).state = "deleted";
  });

  test("the key went only to the API, in a header: never to a process, a file or a log", async () => {
    const records = fs.readFileSync(env.SPY_LOG, "utf8").trim().split("\n").map((l) => JSON.parse(l));
    const spawns = records.filter((r) => r.kind === "spawn");
    const fetches = records.filter((r) => r.kind === "fetch");
    assert.ok(spawns.length > 50 && fetches.length > 30, `${spawns.length} spawns, ${fetches.length} requests seen`);
    for (const s of spawns) {
      assert.equal(s.argvHasKey, false, `the key is on the command line of ${s.file}`);
      assert.equal(s.envHasKey, false, `${s.file} was started with the key in its environment`);
    }
    for (const f of fetches) {
      assert.equal(f.bodyHasKey, false, `the key is in the body sent to ${f.url}`);
      if (f.authHasKey) assert.ok(f.url.startsWith(`${api.base}/`), `the key went to ${f.url}`);
    }
    assert.ok(fetches.some((f) => f.authHasKey), "the plugin did send it to the API");
    const cliLog = fs.readFileSync(env.FAKE_CLI_LOG, "utf8");
    assert.doesNotMatch(cliLog, /KEY-LEAK|dcp_/);
    for (const [file, text] of [...allText(home), ...allText(api.root)]) {
      assert.equal(text.includes(API_KEY), false, `the key is in ${file}`);
      assert.equal(text.includes("ghp_secret123"), false, `the git credential is in ${file}`);
    }
    assert.ok(fs.readFileSync(path.join(home, ".prized-orca", "lifecycle.log"), "utf8").includes('"cmd":"create"'));
  });

  test("uninstall takes the Include line out and leaves the rest of ~/.ssh/config", async () => {
    const r = await runAdapter(["uninstall"], { env });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(fs.readFileSync(env.PRIZED_ORCA_SSH_CONFIG, "utf8"), "Host other\n    HostName example.com\n");
    assert.equal(fs.existsSync(path.join(home, ".prized-orca", "bin", "prized-orca")), false);
  });
});
