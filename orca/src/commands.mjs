// The adapter's commands. Orca runs create, suspend, resume and destroy (vm-recipe.json); ssh runs
// cert-hook and proxy (the include file); people run setup, doctor, list, pull and uninstall.

import { spawn, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import { PrizedApi, PrizedApiError, apiBase, canWriteSandboxes } from "./api.mjs";
import {
  MIN_CLI,
  childEnv,
  cliSandboxState,
  findPrized,
  git,
  openShell,
  prizedVersion,
  requirePrized,
  runPrized,
  shellSignIn,
  shellSignInError,
  sshHostname,
  sshRun,
  versionOk,
} from "./cli.mjs";
import { resolveKey, keyFromLoginShell } from "./key.mjs";
import {
  checkoutScript,
  ensureCommit,
  gitIdentity,
  makeBundle,
  parsePayload,
  SAVE_NS,
  pullScript,
  readStdin,
  recipeResult,
  sandboxRemoteUrl,
  saveBundleScript,
  saveListScript,
  sourceFromOrca,
} from "./recipe.mjs";
import {
  addUserInclude,
  certValidBefore,
  findCliConfig,
  hasUserInclude,
  proxyCommandFor,
  readCliConfig,
  removeUserInclude,
  writeInclude,
} from "./sshconf.mjs";
import { dropEntry, getEntry, putEntry, readState, withLock } from "./state.mjs";
import {
  DOCS_URL,
  SETUP_COMMAND,
  UserError,
  VERSION,
  checkPlainPath,
  cliSandboxDirs,
  isSandboxId,
  paths,
  readJson,
  say,
  sq,
  trace,
  userSshConfig,
  writeFileAtomic,
} from "./util.mjs";

// `prized sandbox cp` and the edge's upload take files up to 100 MB.
const MAX_BUNDLE_BYTES = 100 * 1024 * 1024;
// Renew a sandbox certificate (15 minutes by default) once it has less than this left.
const CERT_RENEW_MS = 5 * 60 * 1000;
const READY = "prized-orca-ready";

export const DEFAULT_CONFIG = Object.freeze({
  template: "prized-agent-v1",
  vcpu: 2,
  memoryGb: 4,
  diskGb: null,
  autoPauseMin: 30,
  maxRuntimeMin: 10080,
});

export function loadConfig(env = process.env) {
  const raw = readJson(paths(env).config, {});
  const config = { ...DEFAULT_CONFIG, ...raw };
  for (const key of ["vcpu", "memoryGb"]) {
    if (!(Number.isInteger(config[key]) && config[key] > 0)) throw new UserError(`${key} in ${paths(env).config} must be a positive whole number`);
  }
  for (const key of ["diskGb", "autoPauseMin", "maxRuntimeMin"]) {
    if (config[key] !== null && !(Number.isInteger(config[key]) && config[key] >= 0)) {
      throw new UserError(`${key} in ${paths(env).config} must be a whole number, or null`);
    }
  }
  if (typeof config.template !== "string" || !/^[a-z0-9][a-z0-9._-]{0,63}$/.test(config.template)) {
    throw new UserError(`template in ${paths(env).config} is not a template name`);
  }
  return config;
}

// The fixed path Orca's recipe and the ssh include call. It never changes between create and
// resume, which Orca checks.
export function adapterPath(env = process.env) {
  return checkPlainPath(paths(env).bin, "the plugin's install path");
}

function requireSetup(env) {
  if (!fs.existsSync(paths(env).bin)) throw new UserError("the Prized Orca plugin is not set up on this machine", `run: ${SETUP_COMMAND}`);
  if (!hasUserInclude(env)) {
    throw new UserError(`${userSshConfig(env)} does not include the plugin's ssh settings`, `run setup again: ${SETUP_COMMAND}`);
  }
}

function writeResult(result) {
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

function api(env) {
  const { key, source } = resolveKey(env);
  return { client: new PrizedApi(key, { base: apiBase(env) }), source };
}

// ---------------------------------------------------------------- create

function sandboxBody(config, labels, maxRuntimeMin) {
  return {
    template: config.template,
    resources: { vcpu: config.vcpu, memoryMb: config.memoryGb * 1024, ...(config.diskGb ? { diskGb: config.diskGb } : {}) },
    labels,
    ...(config.autoPauseMin ? { autoPauseMin: config.autoPauseMin } : {}),
    ...(maxRuntimeMin ? { maxRuntimeMin } : {}),
  };
}

// No answer, or one that says nothing about the outcome: the request may have been carried out.
function isTransient(error) {
  return error instanceof PrizedApiError && (error.status === 0 || error.status >= 500 || error.code === "idempotency_in_progress");
}

// A run time past what the workspace allows (the free tier's 60 minutes, or a plan's maximum) is
// refused rather than capped: ask again with the maximum the refusal names, or the default.
export function runtimeRefusal(error) {
  if (!(error instanceof PrizedApiError) || ![400, 402, 403, 409, 422, 429].includes(error.status)) return null;
  const b = error.body ?? {};
  const about = b.field === "maxRuntimeMin" || b.reason === "max_runtime" || b.tier === "free" || /run ?time|maxRuntime/i.test(error.message);
  if (!about) return null;
  return { limit: Number.isInteger(b.limit) && b.limit > 0 ? b.limit : null };
}

async function createSandbox(client, config, labels, instance) {
  const want = config.maxRuntimeMin || null;
  try {
    return await client.create(sandboxBody(config, labels, want), instance);
  } catch (error) {
    const refusal = want ? runtimeRefusal(error) : null;
    if (!refusal) throw error;
    say(`a ${want}-minute run time was refused (${error.code}); using ${refusal.limit ? `${refusal.limit} minutes` : "the workspace default"}`);
    return client.create(sandboxBody(config, labels, refusal.limit), `${instance}-runtime-${refusal.limit ?? "default"}`);
  }
}

// Which machine made a sandbox, so doctor only calls this machine's leftovers strays: a hash of the
// host name and user, never the names themselves.
export function machineLabel() {
  return createHash("sha256").update(`${os.hostname()}\0${process.getuid?.() ?? os.userInfo().username}`).digest("hex").slice(0, 12);
}

function labelValue(value, max) {
  return String(value ?? "").replace(/[\r\n]/g, " ").slice(0, max);
}

export async function create(env = process.env) {
  requireSetup(env);
  if (env.ORCA_RECIPE_RESULT_SCHEMA_VERSION !== "2") {
    throw new UserError(
      `this recipe needs Orca's provisioned-root results (schema 2); Orca asked for ${env.ORCA_RECIPE_RESULT_SCHEMA_VERSION || "none"}`,
      "update Orca to 1.4.245 or later",
    );
  }
  const config = loadConfig(env);
  const prized = requirePrized(config, env);
  const adapter = adapterPath(env);
  const { client } = api(env);
  // Before anything is made: a CLI that can't open a shell would fail this create at its last step.
  const signIn = shellSignIn(prized, { env });
  if (signIn.ok === false) throw shellSignInError(signIn);
  const src = sourceFromOrca(env);
  if (!src.fromOrca) say(`no pinned commit from Orca (a doctor run?): using ${src.head.slice(0, 12)} on ${src.branch}`);

  const instance = env.ORCA_VM_INSTANCE_ID || `orca-${randomUUID()}`;
  const labels = {
    via: "orca",
    "orca-host": machineLabel(),
    "orca-instance": labelValue(instance, 256),
    ...(env.ORCA_WORKSPACE_NAME ? { "orca-workspace": labelValue(env.ORCA_WORKSPACE_NAME, 256) } : {}),
  };

  // Everything a failed or cancelled create has to undo. Orca cancels with SIGTERM to the whole
  // process group and SIGKILL 5 seconds later, so the handlers go in before anything is made, the
  // error path and a signal share one cleanup, and once it starts the main flow stops at its next
  // step (live()).
  // made.answer: "pending" while the create request is out, "ok", "refused" (a definite no: nothing
  // was made) or "unknown" (no answer: it may have been made).
  const made = { bundleDir: null, answer: null, creating: null, id: null };
  let cleaning = null;
  const live = () => {
    if (cleaning) throw new UserError("cancelled");
  };
  const cleanup = (why) =>
    (cleaning ??= (async () => {
      if (!made.id && made.answer === "pending") {
        // The answer may still come with the id; wait for it briefly.
        let timer;
        await Promise.race([made.creating.catch(() => null), new Promise((resolve) => (timer = setTimeout(resolve, 2_500)))]);
        clearTimeout(timer);
      }
      try {
        if (made.id) {
          say(`create failed (${why}); deleting ${made.id}…`);
          await client.remove(made.id);
          say(`deleted ${made.id}`);
        } else if (made.answer === "pending" || made.answer === "unknown") {
          // No answer, so the sandbox may or may not exist: delete by this create's own labels.
          say(`create failed (${why}); deleting any sandbox labelled orca-instance=${labels["orca-instance"]}…`);
          const n = await client.removeByLabels({ via: "orca", "orca-instance": labels["orca-instance"] });
          say(n ? `deleted ${n}` : "none was made");
        }
      } catch (error) {
        say(
          `could not delete it: ${error.message}. Delete it with: prized sandbox rm --label orca-instance=${labels["orca-instance"]} ` +
            "(or at https://prized.dev/dashboard/sandboxes?via=orca)",
        );
      }
      if (made.id) {
        try {
          dropEntry(made.id, env);
          writeInclude(adapter, env);
        } catch {
          // the state is a record; the sandbox is what matters
        }
      }
      if (made.bundleDir) fs.rmSync(made.bundleDir, { recursive: true, force: true });
    })());
  // The exit below skips the caller's end-of-command trace, so a cancelled create logs its own.
  const onSignal = (signal) => {
    cleanup(signal).finally(() => {
      trace({ cmd: "create", ok: false, cancelled: signal, sandbox: made.id, instance }, env);
      process.exit(130);
    });
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);

  try {
    ensureCommit(src);
    const identity = gitIdentity(src.repoPath);
    const bundle = makeBundle(src);
    made.bundleDir = bundle.dir;
    say(`bundled ${src.head.slice(0, 12)} (${(bundle.bytes / 1e6).toFixed(1)} MB)`);
    if (bundle.bytes > MAX_BUNDLE_BYTES) {
      throw new UserError(`the repository's history is ${(bundle.bytes / 1e6).toFixed(0)} MB; a sandbox takes an upload of up to 100 MB`);
    }
    live();

    say(`creating a sandbox (${config.template}, ${config.vcpu} vCPU, ${config.memoryGb} GB)…`);
    made.answer = "pending";
    made.creating = createSandbox(client, config, labels, instance).then(
      (sandbox) => {
        made.answer = "ok";
        if (isSandboxId(sandbox?.id)) made.id = sandbox.id;
        return sandbox;
      },
      (error) => {
        made.answer = isTransient(error) ? "unknown" : "refused";
        throw error;
      },
    );
    let sandbox = await made.creating;
    if (!isSandboxId(sandbox?.id)) throw new UserError("Prized answered a create without a sandbox id");
    const id = sandbox.id;
    trace({ cmd: "create", sandbox: id, instance }, env);
    live();

    if (sandbox.state !== "running") sandbox = await client.waitFor(id, ["running"]);
    live();
    const home = (await client.exec(sandbox, 'printf %s "$HOME"', 60)).stdout.trim();
    live();
    if (!home.startsWith("/") || /[\s'"\\]/.test(home)) throw new UserError(`the sandbox's home directory is ${JSON.stringify(home)}`);
    const root = `${home}/work/${src.repoName}`;
    const bundlePath = `${home}/.prized-orca/repo.bundle`;
    const mk = await client.exec(sandbox, `mkdir -p ${sq(path.posix.dirname(bundlePath))}`, 60);
    if (mk.exitCode !== 0) throw new UserError(`mkdir in the sandbox failed: ${mk.stderr.trim()}`);
    say("uploading the repository…");
    await client.upload(sandbox, bundlePath, fs.readFileSync(bundle.file));
    live();
    const run = await client.exec(
      sandbox,
      checkoutScript({
        root,
        bundlePath,
        bundleRef: bundle.ref,
        head: src.head,
        branch: src.branch,
        repoUrl: sandboxRemoteUrl(src.repoUrl, (host) => sshHostname(host, env)),
        identity,
      }),
      240,
    );
    if (run.exitCode !== 0) throw new UserError(`the checkout in the sandbox failed (exit ${run.exitCode}): ${run.stderr.trim()}`);
    const got = run.stdout.trim().split("\n").pop();
    if (got !== src.head) throw new UserError(`the checkout landed on ${got}, not ${src.head}`);
    live();

    putEntry(
      id,
      {
        instanceId: instance,
        workspaceId: env.ORCA_WORKSPACE_ID || null,
        workspaceName: env.ORCA_WORKSPACE_NAME || null,
        repoPath: src.repoPath,
        projectRoot: root,
        baseCommit: src.head,
        asleep: false,
      },
      env,
    );
    say("opening a shell with your prized login…");
    live();
    openShell(prized, id, { env });
    live();
    const cliConfig = findCliConfig(id, cliSandboxDirs(id, env));
    if (!cliConfig) throw new UserError(`the prized CLI opened a shell but wrote no ssh_config for ${id}: is it current? (prized update)`);
    const cli = readCliConfig(cliConfig);
    putEntry(id, { cliConfig, user: cli.user }, env);
    writeInclude(adapter, env);

    const result = recipeResult({ id, user: cli.user, projectRoot: root, proxyCommand: proxyCommandFor(adapter, id) });
    await checkDial(result, root, env);
    live();
    writeResult(result);
    say(`${id} is ready: ${root}`);
  } catch (error) {
    await cleanup(error.message);
    throw error;
  } finally {
    if (made.bundleDir) fs.rmSync(made.bundleDir, { recursive: true, force: true });
    process.removeListener("SIGINT", onSignal);
    process.removeListener("SIGTERM", onSignal);
  }
}

// Dial exactly what Orca will dial, and check the checkout is there.
async function checkDial(result, root, env) {
  const target = result.connection.target;
  const r = await sshRun(target, `test -d ${sq(`${root}/.git`)} && echo ${READY}`, { env });
  if (r.status !== 0 || !r.stdout.includes(READY)) {
    throw new UserError(`ssh ${target.username}@${target.host} did not reach the checkout (exit ${r.status}): ${r.stderr.trim().split("\n").slice(-4).join(" / ")}`);
  }
}

// ---------------------------------------------------------------- suspend, resume, destroy

export async function suspend(env = process.env) {
  const p = parsePayload(await readStdin());
  // The key first: a suspend that can't reach Prized must change nothing here.
  const { client } = api(env);
  const wasAsleep = Boolean(getEntry(p.id, env)?.asleep);
  // Asleep before the pause: from then on ssh's hooks and proxy leave the sandbox alone.
  putEntry(p.id, { asleep: true }, env);
  try {
    const outcome = await pauseFirmly(client, p.id);
    say(outcome === "gone" ? `${p.id} is already gone (its run time ended, or it was deleted)` : `paused ${p.id}: its memory, processes and files are kept`);
  } catch (error) {
    // Orca offers no wake for a workspace whose suspend failed, so leave it usable as it was.
    putEntry(p.id, { asleep: wasAsleep }, env);
    throw error;
  }
}

// Pause, then look again: a reconnect that was already waking the sandbox when Orca put it to
// sleep may have resumed it, so pause until it stays paused.
export async function pauseFirmly(client, id, timeoutMs = 180_000) {
  const deadline = Date.now() + timeoutMs;
  let posts = 0;
  while (Date.now() < deadline) {
    const sandbox = await client.get(id);
    if (!sandbox || ["deleting", "deleted"].includes(sandbox.state)) return "gone";
    if (sandbox.state === "paused") return "paused";
    if (sandbox.state === "error") throw new UserError(`${id} failed: ${sandbox.errorReason ?? "no reason given"}`);
    if (sandbox.state === "pausing") {
      await client.sleep(2_000);
      continue;
    }
    if (posts++ >= 3) throw new UserError(`${id} keeps waking up after a pause`);
    try {
      await client.pause(id);
    } catch (error) {
      if (error?.status === 404 || error?.status === 410) return "gone";
      throw error;
    }
  }
  throw new UserError(`${id} did not pause in ${Math.round(timeoutMs / 1000)}s`);
}

export async function resume(env = process.env) {
  requireSetup(env);
  const p = parsePayload(await readStdin());
  const config = loadConfig(env);
  const prized = requirePrized(config, env);
  const adapter = adapterPath(env);
  const known = getEntry(p.id, env) ?? {};
  const projectRoot = known.projectRoot || p.projectRoot;
  if (!projectRoot) throw new UserError(`no checkout is recorded for ${p.id}`);
  putEntry(p.id, { asleep: false, projectRoot, ...(known.user || !p.user ? {} : { user: p.user }) }, env);

  const { client } = api(env);
  const sandbox = await client.get(p.id);
  if (!sandbox || ["deleting", "deleted"].includes(sandbox.state)) {
    throw new UserError(`${p.id} is gone: its run time ended, or it was deleted`, "delete this workspace in Orca and create a new one");
  }
  if (["paused", "pausing"].includes(sandbox.state)) {
    say(`resuming ${p.id}…`);
    await client.resume(p.id);
  } else if (sandbox.state !== "running") {
    await client.waitFor(p.id, ["running"]);
  }
  openShell(prized, p.id, { env });
  const cliConfig = findCliConfig(p.id, cliSandboxDirs(p.id, env));
  if (!cliConfig) throw new UserError(`the prized CLI wrote no ssh_config for ${p.id}`);
  const cli = readCliConfig(cliConfig);
  const user = getEntry(p.id, env)?.user || cli.user;
  putEntry(p.id, { cliConfig, user }, env);
  writeInclude(adapter, env);
  const result = recipeResult({ id: p.id, user, projectRoot, proxyCommand: proxyCommandFor(adapter, p.id) });
  await checkDial(result, projectRoot, env);
  writeResult(result);
  say(`${p.id} is running again`);
}

// Remove Project from Orca. The workspace's work comes back to this machine first (saveBeforeRemove);
// if that fails, the sandbox stays and Orca reports the failure, so nothing is lost.
export async function destroy(env = process.env) {
  const p = parsePayload(await readStdin());
  const { client } = api(env);
  // No record here (a state file set aside, an uninstall --force): Orca's payload and environment
  // still name the checkout and the project.
  // A partial record (suspend or resume after the state was lost) is filled in field by field.
  const known = getEntry(p.id, env) ?? {};
  const entry = { ...known, repoPath: known.repoPath || env.ORCA_REPO_PATH || process.cwd(), projectRoot: known.projectRoot || p.projectRoot || null };
  if (entry.projectRoot) await saveBeforeRemove(client, p.id, entry, env);
  else say(`no checkout is known for ${p.id}, so nothing could be saved from it`);
  const deleted = await client.remove(p.id);
  say(deleted ? `deleting ${p.id}` : `${p.id} was already gone`);
  dropEntry(p.id, env);
  if (fs.existsSync(paths(env).bin)) writeInclude(adapterPath(env), env);
}

export const savedPrefix = (id) => `prized-orca/saved/${id}`;

// Where each saved ref goes in the project's repository: branches under their own names; the stash,
// and each worktree's uncommitted changes and detached HEAD, under names that clash with no branch
// of that sandbox (nor with one as a directory: `stash` beside `stash/x`).
function savedNames(id, refs, worktrees) {
  const taken = refs.filter((r) => r.startsWith("refs/heads/")).map((r) => r.slice("refs/heads/".length));
  const clash = (n) => taken.some((t) => t === n || t.startsWith(`${n}/`) || n.startsWith(`${t}/`));
  const free = (name) => {
    let n = name;
    for (let i = 2; clash(n); i++) n = `${name}-${i}`;
    taken.push(n);
    return n;
  };
  const label = (i) => {
    if (i === "0") return "";
    const base = path.basename(worktrees[i] ?? `worktree-${i}`).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "").replace(/\.\.+/g, ".").replace(/\.lock$/, "") || `worktree-${i}`;
    return `-${base}`;
  };
  const special = (r) => {
    if (r === "refs/stash") return "stash";
    const m = new RegExp(`^${SAVE_NS}/(uncommitted|detached)/(\\d+)$`).exec(r);
    if (!m) throw new UserError(`the sandbox listed an unexpected ref: ${r}`);
    return `${m[1] === "detached" ? "detached-head" : "uncommitted"}${label(m[2])}`;
  };
  return Object.fromEntries(refs.map((r) => [r, `refs/heads/${savedPrefix(id)}/${r.startsWith("refs/heads/") ? r.slice("refs/heads/".length) : free(special(r))}`]));
}

// Copy what the workspace has that this machine doesn't into the project's repository here, as
// branches under prized-orca/saved/<id>/: every branch, the latest stash, a detached HEAD, and the
// uncommitted changes as one commit on top of HEAD. Only what this repository lacks is bundled and
// downloaded; nothing new, nothing copied. A sandbox already gone or failed has nothing left to
// save. Any other failure throws, so the caller keeps the sandbox.
async function saveBeforeRemove(client, sid, entry, env) {
  let sandbox = await client.get(sid);
  if (!sandbox || ["deleting", "deleted"].includes(sandbox.state)) return;
  if (sandbox.state === "error") {
    // Its disk may still be there (a failed wake can leave it so): keep it rather than guess.
    throw new UserError(
      `${sid} is in error (${sandbox.errorReason ?? "no reason given"}), so its work can't be copied, and it was not deleted`,
      `to delete it as it is: prized sandbox rm ${sid}, then remove the workspace in Orca again`,
    );
  }
  let resumed = false;
  let cancelled = false;
  let pausing = null;
  const sleepAgain = () =>
    (pausing ??= resumed && getEntry(sid, env)?.asleep ? pauseFirmly(client, sid).catch((error) => say(`could not pause ${sid} again: ${error.message}`)) : Promise.resolve());
  // Orca's Stop cleanup: SIGTERM, then SIGKILL 5 seconds later. Nothing is deleted then.
  const onSignal = (signal) => {
    cancelled = true;
    sleepAgain().finally(() => {
      trace({ cmd: "destroy", sandbox: sid, ok: false, cancelled: signal }, env);
      process.exit(130);
    });
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "prized-orca-save-"));
  try {
    const repo = entry.repoPath && fs.existsSync(entry.repoPath) ? realpath(entry.repoPath) : null;
    const top = repo ? git(["rev-parse", "--show-toplevel"], { cwd: repo, env }) : null;
    if (!repo || top.status !== 0 || realpath(top.stdout.trim()) !== repo) throw new UserError(`the project's repository is not at ${entry.repoPath ?? "a recorded path"} any more`);
    if (!entry.projectRoot) throw new UserError("no checkout is recorded for it");
    const here = (args) => git(args, { cwd: repo, env });
    if (sandbox.state === "pausing") sandbox = await client.waitFor(sid, ["paused", "running"]);
    if (sandbox.state === "paused") {
      say(`resuming ${sid} to save its work before deleting it…`);
      resumed = true;
      sandbox = await client.resume(sid);
    } else if (sandbox.state !== "running") {
      sandbox = await client.waitFor(sid, ["running"]);
    }

    const listed = await client.exec(sandbox, saveListScript({ root: entry.projectRoot }), 240);
    if (listed.exitCode !== 0) throw new UserError(`reading the workspace's checkout failed (exit ${listed.exitCode}): ${plain(listed.stderr)}`);
    const lines = listed.stdout.split("\n");
    const refs = lines.filter((l) => l.startsWith("ref ")).map((l) => {
      const [, commit, ...name] = l.split(" ");
      return { commit, name: name.join(" ") };
    });
    const worktrees = Object.fromEntries(lines.filter((l) => l.startsWith("worktree ")).map((l) => /^worktree (\d+) (.*)$/.exec(l)?.slice(1) ?? []));
    const nested = Number(/^nested (\d+)$/m.exec(listed.stdout)?.[1] ?? 0);
    const end = /^end (\d+)$/m.exec(listed.stdout)?.[1];
    // the whole list, or nothing: a cut-off answer would leave refs out
    if (!refs.length || String(refs.length) !== end || refs.some((r) => !COMMIT_ID.test(r.commit) || !r.name.startsWith("refs/"))) {
      throw new UserError(`the sandbox answered ${JSON.stringify(plain(listed.stdout).slice(-300))}`);
    }
    // What this repository already has (a commit some ref here contains) is not copied again.
    const known = (c) => here(["cat-file", "-e", `${c}^{commit}`]).status === 0 && here(["for-each-ref", "--count=1", "--contains", c]).stdout.trim() !== "";
    const want = refs.filter((r) => !known(r.commit));
    if (nested) say(`note: ${nested} nested repositor${nested === 1 ? "y or submodule" : "ies or submodules"} in ${sid}: only the commit each one is at is saved, not its own work`);
    if (!want.length) {
      if (cancelled) throw new UserError("cancelled");
      say(`${sid} has nothing this machine doesn't: nothing to save`);
      trace({ cmd: "destroy", sandbox: sid, saved: null }, env);
      return;
    }
    const base = COMMIT_ID.test(entry.baseCommit ?? "") ? [entry.baseCommit] : [];
    const have = [...new Set(refs.filter((r) => !want.includes(r)).map((r) => r.commit))];
    const names = savedNames(sid, refs.map((r) => r.name), worktrees);
    const bundlePath = `${entry.projectRoot}/.git/prized-orca-save.bundle`;
    const file = path.join(dir, "save.bundle");
    const local = (c) => here(["cat-file", "-e", `${c}^{commit}`]).status === 0;
    let landed = false;
    // Without what this repository has: the start commit and the refs it already holds. If the
    // start commit turns out to be missing here, again without it.
    for (const not of base.length ? [[...base, ...have], have] : [have]) {
      const made = await client.exec(sandbox, saveBundleScript({ root: entry.projectRoot, bundlePath, refs: want.map((r) => r.name), not }), 240);
      if (made.exitCode !== 0) throw new UserError(`bundling the workspace's work failed (exit ${made.exitCode}): ${plain(made.stderr)}`);
      if (made.stdout.trim().split("\n").pop() === "empty") {
        // every commit is in what `not` names; fine if this repository has them all
        if (!want.every((w) => local(w.commit))) continue;
        for (const w of want) {
          const u = here(["update-ref", names[w.name], w.commit]);
          if (u.status !== 0) throw new UserError(`git could not save ${names[w.name]} in ${repo}: ${u.stderr.trim()}`);
        }
        landed = true;
        break;
      }
      try {
        fs.writeFileSync(file, await client.download(sandbox, bundlePath));
      } finally {
        await client.exec(sandbox, `rm -f ${sq(bundlePath)}`, 60).catch(() => {});
      }
      if (here(["bundle", "verify", "-q", file]).status !== 0) continue;
      // git leaves out of a bundle a ref whose commit `not` already covers: that one must be here
      const listedHeads = new Set(here(["bundle", "list-heads", file]).stdout.split("\n").map((l) => l.split(" ").slice(1).join(" ")));
      const inBundle = want.filter((w) => listedHeads.has(w.name));
      const outside = want.filter((w) => !listedHeads.has(w.name));
      if (!outside.every((w) => local(w.commit))) continue;
      if (inBundle.length) {
        const r = here(["fetch", "--no-tags", "--no-recurse-submodules", file, ...inBundle.map((w) => `+${w.name}:${names[w.name]}`)]);
        if (r.status !== 0) throw new UserError(`git could not save it in ${repo}: ${(r.stderr || r.stdout).trim()}`);
      }
      for (const w of outside) {
        const u = here(["update-ref", names[w.name], w.commit]);
        if (u.status !== 0) throw new UserError(`git could not save ${names[w.name]} in ${repo}: ${u.stderr.trim()}`);
      }
      landed = true;
      break;
    }
    if (!landed) throw new UserError(`the bundle from ${sid} does not fit ${repo}`);
    for (const w of want) {
      if (here(["rev-parse", "--verify", "-q", names[w.name]]).stdout.trim() !== w.commit) throw new UserError(`${names[w.name]} did not land in ${repo}`);
    }
    const saved = want.map((w) => names[w.name].slice("refs/heads/".length));
    say(`saved what ${sid} had that this machine didn't, as branches in ${repo}: ${saved.join(", ")}`);
    trace({ cmd: "destroy", sandbox: sid, saved }, env);
    if (cancelled) throw new UserError("cancelled");
  } catch (error) {
    await sleepAgain();
    throw new UserError(
      `could not copy the workspace's work from ${sid} to this machine, so the sandbox was not deleted: ${error.message}`,
      `copy or push its work yourself, then remove the workspace in Orca again (Retry cleanup); or delete the sandbox as it is: prized sandbox rm ${sid}`,
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    process.removeListener("SIGINT", onSignal);
    process.removeListener("SIGTERM", onSignal);
  }
}

// ---------------------------------------------------------------- pull

// The workspace to pull from: the one named, the only one, or the one whose project is the git
// repository the command runs in.
function pickWorkspace(id, env) {
  const all = readState(env).sandboxes;
  if (id) {
    if (!isSandboxId(id)) throw new UserError(`not a sandbox id: ${id}`);
    if (!all[id]) throw new UserError(`${id} backs no Orca workspace on this machine`, "prized-orca list shows the ones that do");
    return [id, all[id]];
  }
  const entries = Object.entries(all);
  if (entries.length === 1) return entries[0];
  if (!entries.length) throw new UserError("no Orca workspace on this machine runs on a Prized sandbox");
  const top = git(["rev-parse", "--show-toplevel"], { cwd: process.cwd(), env });
  const here = top.status === 0 ? realpath(top.stdout.trim()) : null;
  const mine = here ? entries.filter(([, e]) => e.repoPath && realpath(e.repoPath) === here) : [];
  if (mine.length === 1) return mine[0];
  const names = (mine.length ? mine : entries).map(([sid, e]) => `${sid} (${e.workspaceName ? `"${e.workspaceName}", ` : ""}${e.repoPath ?? "?"})`);
  throw new UserError(`which workspace? ${names.join(", ")}`, "name it: prized-orca pull ID");
}

function realpath(p) {
  try {
    return fs.realpathSync(p);
  } catch {
    return p;
  }
}

// pull's copy: a bundle made in the running sandbox (pullScript), downloaded through the API,
// checked against the project's repository here, and fetched fast-forward only into the branch
// `target` (default: the workspace's branch name).
async function copyFromSandbox(client, sandbox, sid, entry, env, { target = null } = {}) {
  const base = COMMIT_ID.test(entry.baseCommit ?? "") ? entry.baseCommit : null;
  const bundlePath = `${entry.projectRoot}/.git/prized-orca-pull.bundle`;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "prized-orca-pull-"));
  const here = (args) => git(args, { cwd: entry.repoPath, env });
  try {
    // From the start commit, which this repository has; the whole branch if it doesn't.
    for (const since of base ? [base, null] : [null]) {
      const run = await client.exec(sandbox, pullScript({ root: entry.projectRoot, bundlePath, base: since }), 240);
      if (run.exitCode !== 0) throw new UserError(`reading the workspace's branch failed (exit ${run.exitCode}): ${plain(run.stderr)}`);
      const [branch, head, dirty, count] = run.stdout.trim().split("\n").slice(-4);
      const named = branch && here(["check-ref-format", "--branch", branch]).status === 0;
      if (!named || !COMMIT_ID.test(head ?? "") || !Number.isInteger(Number(count))) throw new UserError(`the sandbox answered ${JSON.stringify(plain(run.stdout).slice(-300))}`);
      const got = { branch, head, dirty: Number(dirty), commits: Number(count), since, target: target ?? branch, copied: false };
      if (got.commits === 0) return got;
      const file = path.join(dir, "pull.bundle");
      try {
        fs.writeFileSync(file, await client.download(sandbox, bundlePath));
      } finally {
        await client.exec(sandbox, `rm -f ${sq(bundlePath)}`, 60).catch(() => {});
      }
      if (here(["bundle", "verify", "-q", file]).status !== 0) {
        if (since) continue;
        throw new UserError(`the bundle from ${sid} does not fit ${entry.repoPath}`);
      }
      const r = here(["fetch", "--no-tags", "--no-recurse-submodules", file, `refs/heads/${branch}:refs/heads/${got.target}`]);
      if (r.status !== 0) {
        throw new UserError(
          `git could not put ${branch} from ${sid} into the branch ${got.target} here: ${(r.stderr || r.stdout).trim()}`,
          `if ${got.target} has commits the workspace's branch doesn't, or is checked out here, pull into a new branch: prized-orca pull ${sid} --as ${sq(`${branch}-sandbox`)}`,
        );
      }
      got.copied = true;
      return got;
    }
    throw new UserError(`the bundle from ${sid} does not fit ${entry.repoPath}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

const COMMIT_ID = /^[0-9a-f]{40}([0-9a-f]{24})?$/;

const PULL_USAGE = "usage: prized-orca pull [ID] [--as BRANCH]";

// What the sandbox printed, safe for a terminal: no control characters.
function plain(text) {
  return String(text).replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g, "?").trim();
}

// Copy the workspace's branch from its sandbox into the project's own repository on this machine, so
// it can be pushed from here with the user's own git sign-in: no git credential goes into the
// sandbox. Only commits travel: a bundle of what the workspace added since it started, made in the
// sandbox and downloaded through the API. The local branch is created, or fast-forwarded; one that
// has moved apart, or is checked out here, is left alone (--as names another branch). A workspace
// Orca put to sleep is resumed for the copy and paused again.
export async function pull(argv, env = process.env) {
  if (argv.includes("--help") || argv.includes("-h")) {
    say(PULL_USAGE);
    return;
  }
  const flags = parseFlags(argv);
  for (const k of Object.keys(flags)) if (k !== "as") throw new UserError(`unknown option --${k}`, PULL_USAGE);
  const ids = argv.filter((a, i) => !a.startsWith("-") && argv[i - 1] !== "--as");
  if (ids.length > 1) throw new UserError(`one workspace at a time: ${ids.join(" ")}`, PULL_USAGE);
  const [sid, entry] = pickWorkspace(ids[0], env);
  if (!entry.repoPath || !fs.existsSync(entry.repoPath)) throw new UserError(`the project's repository for ${sid} is not at ${entry.repoPath ?? "a recorded path"} any more`);
  if (!entry.projectRoot) throw new UserError(`no checkout is recorded for ${sid}`);
  if (flags.as !== undefined && (typeof flags.as !== "string" || git(["check-ref-format", "--branch", flags.as], { cwd: entry.repoPath, env }).status !== 0)) {
    throw new UserError("--as needs a branch name", PULL_USAGE);
  }
  const { client } = api(env);

  // Put back to sleep only what Orca has asleep, and only while it still does: a wake in Orca
  // meanwhile, or an idle pause (which comes back by itself), is left running.
  let resumed = false;
  let sleeping = null;
  const sleepAgain = () =>
    (sleeping ??= (async () => {
      if (!resumed || !getEntry(sid, env)?.asleep) return;
      say(`pausing ${sid} again…`);
      await pauseFirmly(client, sid).catch((error) => say(`could not pause it again: ${error.message}`));
    })());
  const onSignal = (signal) => {
    sleepAgain().finally(() => {
      trace({ cmd: "pull", sandbox: sid, ok: false, cancelled: signal }, env);
      process.exit(130);
    });
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);
  try {
    let sandbox = await client.get(sid);
    if (!sandbox || ["deleting", "deleted"].includes(sandbox.state)) throw new UserError(`${sid} is gone: its run time ended, or it was deleted`);
    if (["paused", "pausing"].includes(sandbox.state)) {
      say(`${sid} is paused; resuming it to read the checkout…`);
      resumed = Boolean(entry.asleep);
      sandbox = await client.resume(sid);
    } else if (sandbox.state !== "running") {
      sandbox = await client.waitFor(sid, ["running"]);
    }

    const got = await copyFromSandbox(client, sandbox, sid, entry, env, { target: typeof flags.as === "string" ? flags.as : null });
    const { branch, head, dirty, commits, since, target } = got;
    if (got.copied) {
      const counted = `${commits} commit${commits === 1 ? "" : "s"}${since ? " since the workspace started" : ""}`;
      say(`pulled ${branch} from ${sid} into ${entry.repoPath} as the branch ${target} (${head.slice(0, 12)}, ${counted})`);
      say(`push it from here with your own git sign-in: git -C ${sq(entry.repoPath)} push origin ${sq(target)}`);
    } else {
      say(`${branch} in ${sid} has no commits since the workspace started: nothing to pull`);
    }
    if (dirty > 0) say(`note: ${dirty} uncommitted change${dirty === 1 ? "" : "s"} in the workspace stayed there: commit ${dirty === 1 ? "it" : "them"} in the workspace, then pull again`);
    trace({ cmd: "pull", sandbox: sid, commits, ok: true }, env);
  } finally {
    await sleepAgain();
    process.removeListener("SIGINT", onSignal);
    process.removeListener("SIGTERM", onSignal);
  }
}

// ---------------------------------------------------------------- ssh's side

// `Match originalhost ID exec`: ssh runs this while reading its config, before it loads the
// certificate. Renew a certificate that is about to end, unless Orca put the workspace to sleep.
// Quiet and always exit 0: the exit status changes nothing, and stdout is discarded by ssh.
export function certHook(id, env = process.env) {
  const started = Date.now();
  try {
    if (!isSandboxId(id)) return;
    const entry = getEntry(id, env);
    if (!entry?.cliConfig || entry.asleep) return;
    const fresh = () => {
      const cli = readCliConfig(entry.cliConfig);
      const before = cli.certificateFile && fs.existsSync(cli.certificateFile) ? certValidBefore(fs.readFileSync(cli.certificateFile, "utf8")) : null;
      return before && before.getTime() - Date.now() > CERT_RENEW_MS;
    };
    if (fresh()) return;
    withLock(path.join(paths(env).home, `renew-${id}.lock`), () => {
      // Again under the lock: Orca may have put the workspace to sleep meanwhile, and opening a
      // shell would wake the sandbox.
      if (fresh() || getEntry(id, env)?.asleep) return;
      const prized = findPrized(loadConfig(env), env);
      if (!prized) return;
      openShell(prized, id, { env, timeoutMs: 90_000 });
      trace({ cmd: "cert-hook", sandbox: id, renewed: true, ms: Date.now() - started }, env);
    }, 120_000);
  } catch (error) {
    trace({ cmd: "cert-hook", sandbox: id, ok: false, error: error.message }, env);
  }
}

// ssh's ProxyCommand for a sandbox. Refuse while Orca has the workspace asleep (so a reconnect never
// wakes it), resume one that paused itself for being idle, then hand the stream to the prized CLI's
// own bridge.
export async function proxy(id, env = process.env) {
  if (!isSandboxId(id)) throw new UserError(`not a sandbox id: ${id}`);
  const entry = getEntry(id, env);
  if (entry?.asleep) {
    // Orca's connection reuse sends a ProxyCommand's stderr nowhere, so say it in the log too.
    trace({ cmd: "proxy", sandbox: id, refused: "asleep" }, env);
    process.stderr.write(`prized-orca: ${id} is asleep in Orca. Wake the workspace in Orca to resume it.\n`);
    return 1;
  }
  const cliConfig = entry?.cliConfig || findCliConfig(id, cliSandboxDirs(id, env));
  if (!cliConfig) {
    process.stderr.write(`prized-orca: no ssh files for ${id}: run \`prized sandbox ssh ${id}\` once\n`);
    return 1;
  }
  const cli = readCliConfig(cliConfig);
  const prized = findPrized(loadConfig(env), env);
  if (prized) {
    const state = cliSandboxState(prized, id, { env });
    if ((state === "paused" || state === "pausing") && !getEntry(id, env)?.asleep) {
      const r = runPrized(prized, ["sandbox", "resume", id], { env, timeoutMs: 120_000 });
      if (r.status !== 0) {
        process.stderr.write(`prized-orca: could not resume ${id}: ${r.stderr.trim().split("\n").pop() ?? ""}\n`);
        return 1;
      }
      trace({ cmd: "proxy", sandbox: id, resumed: true }, env);
    }
  }
  return new Promise((resolve) => {
    const child = spawn("/bin/sh", ["-c", cli.proxyCommand], { stdio: "inherit", env: childEnv(env) });
    const forward = (signal) => child.kill(signal);
    for (const signal of ["SIGTERM", "SIGHUP", "SIGINT"]) process.on(signal, forward);
    child.on("error", (error) => {
      process.stderr.write(`prized-orca: ${error.message}\n`);
      resolve(1);
    });
    child.on("exit", (code, signal) => resolve(code ?? (signal ? 1 : 0)));
  });
}

// ---------------------------------------------------------------- setup, doctor, list, uninstall

function parseFlags(argv) {
  const flags = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) continue;
    const eq = a.indexOf("=");
    const [k, v] = eq === -1 ? [a.slice(2), undefined] : [a.slice(2, eq), a.slice(eq + 1)];
    flags[k] = v ?? (argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : true);
  }
  return flags;
}

async function confirm(question) {
  if (!process.stdin.isTTY) return false;
  const rl = readline.createInterface({ input: process.stdin, output: process.stderr });
  const answer = await new Promise((resolve) => rl.question(question, resolve));
  rl.close();
  return /^y(es)?$/i.test(answer.trim());
}

function packageRoot() {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
}

// Copy this package to ~/.prized-orca/app and put a wrapper at ~/.prized-orca/bin/prized-orca that
// runs it with this Node, by absolute path: Orca started from the Dock has no shell PATH.
function installSelf(env) {
  const p = paths(env);
  const root = packageRoot();
  if (path.resolve(root) !== path.resolve(p.app)) {
    const next = `${p.app}.new-${process.pid}`;
    fs.rmSync(next, { recursive: true, force: true });
    fs.mkdirSync(next, { recursive: true, mode: 0o700 });
    for (const f of ["package.json", "bin", "src", "orca-plugin.json", "vm-recipe.json", "LICENSE", "README.md"]) {
      const from = path.join(root, f);
      if (fs.existsSync(from)) fs.cpSync(from, path.join(next, f), { recursive: true });
    }
    fs.rmSync(p.app, { recursive: true, force: true });
    fs.renameSync(next, p.app);
  }
  const node = checkPlainPath(process.execPath, "Node's path");
  const entry = checkPlainPath(path.join(p.app, "bin", "prized-orca.mjs"), "the adapter path");
  writeFileAtomic(p.bin, `#!/bin/sh\n# Installed by \`prized-orca setup\` (the Prized plugin for Orca). Orca and ssh run this by its full path.\nexec ${sq(node)} ${sq(entry)} "$@"\n`, 0o755);
  return p.bin;
}

function intFlag(flags, name, min, max) {
  if (flags[name] === undefined) return undefined;
  const n = Number(flags[name]);
  if (!Number.isInteger(n) || n < min || n > max) throw new UserError(`--${name} must be a whole number from ${min} to ${max}`);
  return n;
}

export async function setup(argv, env = process.env) {
  const flags = parseFlags(argv);
  const p = paths(env);
  const major = Number(process.versions.node.split(".")[0]);
  if (major < 22) throw new UserError(`Node ${process.versions.node} is too old: the plugin needs Node 22 or later`);
  adapterPath(env);

  const prized = findPrized({}, env);
  if (!prized) throw new UserError("the prized CLI is not installed", "install it from https://prized.dev/docs/quickstart?via=orca, run `prized login`, then run setup again");
  const version = prizedVersion(prized, env);
  if (!versionOk(version)) {
    throw new UserError(`the prized CLI is ${version ? version.join(".") : "of an unknown version"}; the plugin needs ${MIN_CLI.join(".")} or later`, "run `prized update`, then setup again");
  }
  const signedIn = runPrized(prized, ["sandbox", "ls", "--json"], { env, timeoutMs: 60_000 });
  if (signedIn.status === 8) throw new UserError("the prized CLI is not signed in", "run `prized login` (sandbox shells open with your login), then run setup again");
  if (signedIn.status !== 0) throw new UserError(`\`prized sandbox ls\` failed (exit ${signedIn.status}): ${signedIn.stderr.trim()}`);
  const signIn = shellSignIn(prized, { env });
  if (signIn.ok === false) throw shellSignInError(signIn);
  say(`prized CLI: ${prized} ${version.join(".")} (${signIn.ok ? "signed in with prized login" : "signed in"})`);
  if (signIn.ok === null) say(`note: could not confirm the CLI opens sandbox shells with your prized login (${signIn.detail})`);

  const { client, source } = api(env);
  const me = await client.me();
  if (!canWriteSandboxes(me)) throw new UserError("this PRIZED_API_KEY can't create sandboxes", "make a key with Sandboxes: Write at https://prized.dev/dashboard/workspace/api-keys?via=orca");
  say(`PRIZED_API_KEY: found in your ${source}, workspace ${me?.workspace?.name ?? me?.workspace?.id ?? "?"}`);
  if (source === "environment" && !keyFromLoginShell(env)) {
    say("note: your login shell doesn't export PRIZED_API_KEY, so Orca started from the Dock won't see it. Add the export to ~/.zprofile (or ~/.bash_profile).");
  }

  const ssh = spawnSync(env.PRIZED_ORCA_SSH_BIN || "ssh", ["-V"], { encoding: "utf8", env: childEnv(env) });
  if (ssh.error) throw new UserError("OpenSSH's ssh is not installed");

  const config = { ...loadConfig(env), prizedBin: prized };
  const overrides = {
    template: typeof flags.template === "string" ? flags.template : undefined,
    vcpu: intFlag(flags, "vcpu", 1, 32),
    memoryGb: intFlag(flags, "memory-gb", 1, 128),
    diskGb: intFlag(flags, "disk-gb", 10, 128),
    autoPauseMin: intFlag(flags, "auto-pause-min", 0, 40320),
    maxRuntimeMin: intFlag(flags, "max-runtime-min", 0, 10080),
  };
  for (const [k, v] of Object.entries(overrides)) if (v !== undefined) config[k] = v;

  if (!hasUserInclude(env)) {
    const file = userSshConfig(env);
    const ok = flags.yes === true || (await confirm(`Add one line to ${file} so ssh can reach your Orca sandboxes (Include "${p.sshInclude}")? [y/N] `));
    if (!ok) throw new UserError(`setup needs that line in ${file}`, "run setup again and answer y, or pass --yes");
  }
  fs.mkdirSync(p.home, { recursive: true, mode: 0o700 });
  fs.chmodSync(p.home, 0o700);
  writeFileAtomic(p.config, `${JSON.stringify(config, null, 2)}\n`);
  loadConfig(env);
  say(`adapter: ${installSelf(env)}`);
  writeInclude(adapterPath(env), env);
  say(`ssh: ${addUserInclude(env)}`);
  say("done. In Orca: Settings > Experimental > turn on Cloud VM; Settings > Plugins > install Prized Sandbox;");
  say("then create a workspace and pick Prized Sandbox under Run on > Per-Workspace Environment.");
}

export async function doctor(env = process.env) {
  const checks = [];
  const add = (name, ok, detail) => checks.push({ name, ok, detail });
  const p = paths(env);
  add("Node 22 or later", Number(process.versions.node.split(".")[0]) >= 22, process.versions.node);
  add("adapter installed", fs.existsSync(p.bin), p.bin);
  add("ssh include in ~/.ssh/config", hasUserInclude(env), userSshConfig(env));
  let config = {};
  try {
    config = loadConfig(env);
    add("config", true, p.config);
  } catch (error) {
    add("config", false, error.message);
  }
  const prized = findPrized(config, env);
  add("prized CLI", Boolean(prized), prized ?? "not found");
  if (prized) {
    const version = prizedVersion(prized, env);
    add(`prized CLI ${MIN_CLI.join(".")} or later`, versionOk(version), version ? version.join(".") : "unknown version");
    const signIn = shellSignIn(prized, { env });
    add(
      "prized login (opens shells)",
      signIn.ok !== false,
      signIn.ok
        ? "signed in"
        : signIn.kind === "api-key"
          ? "signed in with an API key, which can't open a shell: run prized login and sign in in the browser"
          : signIn.kind === "signed-out"
            ? "not signed in: run prized login"
            : `could not tell (${signIn.detail})`,
    );
  }
  try {
    const { client, source } = api(env);
    const me = await client.me();
    add("PRIZED_API_KEY (makes sandboxes)", canWriteSandboxes(me), `from your ${source}; workspace ${me?.workspace?.name ?? "?"}`);
    // Sandboxes this machine made more than 10 minutes ago that no workspace here knows about (a
    // create killed outright). Other machines' and teammates' sandboxes carry another orca-host.
    try {
      const known = readState(env).sandboxes;
      const strays = (await client.listByLabel("orca-host", machineLabel())).filter(
        (s) => !known[s.id] && s.labels?.via === "orca" && Date.now() - Date.parse(s.createdAt ?? 0) > 10 * 60_000,
      );
      add(
        "no stray sandboxes from this machine",
        strays.length === 0,
        strays.length
          ? `${strays.map((s) => `${s.id} (${s.state}${s.labels?.["orca-workspace"] ? `, workspace "${s.labels["orca-workspace"]}"` : ""})`).join(", ")} back no Orca workspace on this machine; if you don't need them, prized sandbox rm ID`
          : "none",
      );
    } catch (error) {
      add("no stray sandboxes from this machine", false, `could not list them: ${error.message}`);
    }
  } catch (error) {
    add("PRIZED_API_KEY (makes sandboxes)", false, error.message);
  }
  const live = Object.entries(readState(env).sandboxes);
  add("sandboxes in use", true, live.length ? live.map(([id, e]) => `${id}${e.asleep ? " (asleep)" : ""}`).join(", ") : "none");
  for (const c of checks) say(`${c.ok ? "ok  " : "FAIL"} ${c.name}: ${c.detail}`);
  return checks.every((c) => c.ok) ? 0 : 1;
}

export function list(env = process.env) {
  process.stdout.write(`${JSON.stringify(readState(env).sandboxes, null, 2)}\n`);
}

export function uninstall(argv, env = process.env) {
  const flags = parseFlags(argv);
  const p = paths(env);
  const live = Object.keys(readState(env).sandboxes);
  if (live.length && flags.force !== true) {
    throw new UserError(`these sandboxes still back Orca workspaces: ${live.join(", ")}`, "delete those workspaces in Orca first (that deletes the sandboxes), or pass --force");
  }
  say(`ssh: ${removeUserInclude(env)}`);
  for (const f of [p.sshInclude, p.bin, p.config, p.state]) fs.rmSync(f, { force: true });
  fs.rmSync(p.app, { recursive: true, force: true });
  say(`removed the plugin's files from ${p.home}${live.length ? `; still running: ${live.join(", ")}` : ""}`);
  say("remove the plugin in Orca under Settings > Plugins too.");
}

export const HELP = `prized-orca ${VERSION}: the Prized Sandbox plugin for Orca (${DOCS_URL})

  setup [--yes] [--template T] [--vcpu N] [--memory-gb N] [--disk-gb N] [--auto-pause-min N] [--max-runtime-min N]
             check the prized CLI and PRIZED_API_KEY, install the adapter, add the ssh include
  doctor     check this machine without creating anything
  list       the sandboxes Orca workspaces run on
  pull [ID] [--as BRANCH]
             copy a workspace's branch into the project's repository here, to push it from here
  uninstall [--force]
             take the ssh include out and remove the plugin's files

Orca runs: create, suspend, resume, destroy. ssh runs: cert-hook ID, proxy ID.`;
