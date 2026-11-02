// The adapter's commands. Orca runs create, suspend, resume and destroy (vm-recipe.json); ssh runs
// cert-hook and proxy (the include file); people run setup, doctor, list and uninstall.

import { spawn, spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import { PrizedApi, PrizedApiError, apiBase, canWriteSandboxes } from "./api.mjs";
import { childEnv, cliSandboxState, findPrized, openShell, requirePrized, runPrized, sshRun } from "./cli.mjs";
import { resolveKey, keyFromLoginShell } from "./key.mjs";
import {
  checkoutScript,
  ensureCommit,
  gitIdentity,
  makeBundle,
  parsePayload,
  readStdin,
  recipeResult,
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

  const src = sourceFromOrca(env);
  if (!src.fromOrca) say(`no pinned commit from Orca (a doctor run?): using ${src.head.slice(0, 12)} on ${src.branch}`);
  ensureCommit(src);
  const identity = gitIdentity(src.repoPath);
  const bundle = makeBundle(src);
  say(`bundled ${src.head.slice(0, 12)} (${(bundle.bytes / 1e6).toFixed(1)} MB)`);
  if (bundle.bytes > MAX_BUNDLE_BYTES) {
    fs.rmSync(bundle.dir, { recursive: true, force: true });
    throw new UserError(`the repository's history is ${(bundle.bytes / 1e6).toFixed(0)} MB; a sandbox takes an upload of up to 100 MB`);
  }

  const instance = env.ORCA_VM_INSTANCE_ID || `orca-${randomUUID()}`;
  const labels = {
    via: "orca",
    "orca-instance": labelValue(instance, 256),
    ...(env.ORCA_WORKSPACE_NAME ? { "orca-workspace": labelValue(env.ORCA_WORKSPACE_NAME, 256) } : {}),
  };
  say(`creating a sandbox (${config.template}, ${config.vcpu} vCPU, ${config.memoryGb} GB)…`);
  let sandbox = await createSandbox(client, config, labels, instance);
  const id = sandbox?.id;
  if (!isSandboxId(id)) throw new UserError(`Prized answered a create without a sandbox id`);
  trace({ cmd: "create", sandbox: id, instance });

  let cleaned = false;
  const cleanup = async (why) => {
    if (cleaned) return;
    cleaned = true;
    say(`create failed (${why}); deleting ${id}…`);
    try {
      await client.remove(id);
      say(`deleted ${id}`);
    } catch (error) {
      say(`could not delete ${id}: ${error.message}. Delete it at https://prized.dev/dashboard/sandboxes?via=orca`);
    }
    try {
      dropEntry(id, env);
      writeInclude(adapter, env);
    } catch {
      // the state is a record; the sandbox is what matters
    }
  };
  const onSignal = (signal) => {
    cleanup(signal).finally(() => process.exit(130));
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);

  try {
    if (sandbox.state !== "running") sandbox = await client.waitFor(id, ["running"]);
    const home = (await client.exec(sandbox, 'printf %s "$HOME"', 60)).stdout.trim();
    if (!home.startsWith("/") || /[\s'"\\]/.test(home)) throw new UserError(`the sandbox's home directory is ${JSON.stringify(home)}`);
    const root = `${home}/work/${src.repoName}`;
    const bundlePath = `${home}/.prized-orca/repo.bundle`;
    const mk = await client.exec(sandbox, `mkdir -p ${sq(path.posix.dirname(bundlePath))}`, 60);
    if (mk.exitCode !== 0) throw new UserError(`mkdir in the sandbox failed: ${mk.stderr.trim()}`);
    say("uploading the repository…");
    await client.upload(sandbox, bundlePath, fs.readFileSync(bundle.file));
    const run = await client.exec(
      sandbox,
      checkoutScript({ root, bundlePath, bundleRef: bundle.ref, head: src.head, branch: src.branch, repoUrl: src.repoUrl, identity }),
      240,
    );
    if (run.exitCode !== 0) throw new UserError(`the checkout in the sandbox failed (exit ${run.exitCode}): ${run.stderr.trim()}`);
    const got = run.stdout.trim().split("\n").pop();
    if (got !== src.head) throw new UserError(`the checkout landed on ${got}, not ${src.head}`);

    putEntry(
      id,
      {
        instanceId: instance,
        workspaceId: env.ORCA_WORKSPACE_ID || null,
        workspaceName: env.ORCA_WORKSPACE_NAME || null,
        repoPath: src.repoPath,
        projectRoot: root,
        asleep: false,
      },
      env,
    );
    say("opening a shell with your prized login…");
    openShell(prized, id, { env });
    const cliConfig = findCliConfig(id, cliSandboxDirs(id, env));
    if (!cliConfig) throw new UserError(`the prized CLI opened a shell but wrote no ssh_config for ${id}: is it current? (prized update)`);
    const cli = readCliConfig(cliConfig);
    putEntry(id, { cliConfig, user: cli.user }, env);
    writeInclude(adapter, env);

    const result = recipeResult({ id, user: cli.user, projectRoot: root, proxyCommand: proxyCommandFor(adapter, id) });
    await checkDial(result, root, env);
    writeResult(result);
    say(`${id} is ready: ${root}`);
  } catch (error) {
    await cleanup(error.message);
    throw error;
  } finally {
    fs.rmSync(bundle.dir, { recursive: true, force: true });
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
  // Asleep first: from now on ssh's hooks leave the sandbox alone instead of waking it.
  putEntry(p.id, { asleep: true }, env);
  const { client } = api(env);
  const sandbox = await client.get(p.id);
  if (!sandbox || ["deleting", "deleted"].includes(sandbox.state)) {
    say(`${p.id} is already gone (its run time ended, or it was deleted)`);
    return;
  }
  if (sandbox.state === "paused") {
    say(`${p.id} is already paused`);
    return;
  }
  if (sandbox.state === "pausing") await client.waitFor(p.id, ["paused"]);
  else await client.pause(p.id);
  say(`paused ${p.id}: its memory, processes and files are kept`);
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

export async function destroy(env = process.env) {
  const p = parsePayload(await readStdin());
  const { client } = api(env);
  const deleted = await client.remove(p.id);
  say(deleted ? `deleting ${p.id}` : `${p.id} was already gone`);
  dropEntry(p.id, env);
  if (fs.existsSync(paths(env).bin)) writeInclude(adapterPath(env), env);
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
      if (fresh()) return;
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
    if (state === "paused" || state === "pausing") {
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
    const [k, v] = a.slice(2).split("=", 2);
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
  const signedIn = runPrized(prized, ["sandbox", "ls", "--json"], { env, timeoutMs: 60_000 });
  if (signedIn.status === 8) throw new UserError("the prized CLI is not signed in", "run `prized login` (sandbox shells open with your login), then run setup again");
  if (signedIn.status !== 0) throw new UserError(`\`prized sandbox ls\` failed (exit ${signedIn.status}): ${signedIn.stderr.trim()}`);
  say(`prized CLI: ${prized} (signed in)`);

  const { client, source } = api(env);
  const me = await client.me();
  if (!canWriteSandboxes(me)) throw new UserError("this PRIZED_API_KEY can't create sandboxes", "make a key with Sandboxes: Write at https://prized.dev/dashboard/workspace/api-keys?via=orca");
  say(`PRIZED_API_KEY: found in your ${source}, workspace ${me?.workspace?.name ?? me?.workspace?.id ?? "?"}`);
  if (source === "environment" && !keyFromLoginShell(env)) {
    say("note: your login shell doesn't export PRIZED_API_KEY, so Orca started from the Dock won't see it. Add the export to ~/.zprofile (or ~/.bash_profile).");
  }

  const ssh = spawnSync(env.PRIZED_ORCA_SSH_BIN || "ssh", ["-V"], { encoding: "utf8" });
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
    const r = runPrized(prized, ["sandbox", "ls", "--json"], { env, timeoutMs: 60_000 });
    add("prized login (opens shells)", r.status === 0, r.status === 0 ? "signed in" : r.status === 8 ? "not signed in: run prized login" : `exit ${r.status}`);
  }
  try {
    const { client, source } = api(env);
    const me = await client.me();
    add("PRIZED_API_KEY (makes sandboxes)", canWriteSandboxes(me), `from your ${source}; workspace ${me?.workspace?.name ?? "?"}`);
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
  uninstall [--force]
             take the ssh include out and remove the plugin's files

Orca runs: create, suspend, resume, destroy. ssh runs: cert-hook ID, proxy ID.`;
