// A stand-in for Orca's per-workspace environment runner, written from Orca's documented contract and
// its source as published on GitHub (stablyai/orca at v1.4.245; nothing of Orca's is run here):
//
//   docs/reference/plugin-manifest.md, plugin-catalog.md, plugin-authoring.md
//   src/shared/plugins/artifacts/plugin-vm-recipe-artifact.ts   the recipe file's schema
//   src/shared/ephemeral-vm-recipe-process.ts                   how a phase runs: shell, cwd, ORCA_* env
//   src/shared/ephemeral-vm-recipe-lifecycle-payload.ts         what suspend/resume/destroy get on stdin
//   src/shared/ephemeral-vm-recipes.ts                          the result schema create/resume print
//   src/shared/ephemeral-vm-recipe-checkout-mode.ts             provisioned-root needs schema 2
//   src/main/ephemeral-vm/resume-integrity.ts                   what may not change across a resume
//   src/main/ssh/system-transport/system-ssh-args.ts            the ssh argv for a recipe's target

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

// ---------------------------------------------------------------- manifest and recipe files

const MANIFEST_KEYS = new Set([
  "$schema", "manifestVersion", "execution", "id", "publisher", "name", "version", "description", "author",
  "repository", "license", "homepage", "icon", "cover", "screenshots", "engines", "main", "contributes",
  "capabilities", "pluginApi",
]);
const CONTRIBUTION_KEYS = new Set([
  "panels", "commands", "events", "languagePacks", "keybindings", "vmRecipes", "themes", "terminalThemes",
  "iconThemes", "syntaxes", "customEditors", "newFiles", "settings", "agentHarnesses", "skills", "mcpServers",
  "hooks", "taskSources", "connections", "statusBarItems", "sidebarCards",
]);
const RESERVED_OWNERS = /^(stablyai|official|anthropic|openai|github|orca.*)$/;

function fail(errors, message) {
  errors.push(message);
}

function relativeOk(p) {
  return typeof p === "string" && p.length > 0 && !p.startsWith("/") && !p.split("/").includes("..") && !/^(con|prn|aux|nul)$/i.test(path.basename(p, path.extname(p)));
}

// width, height and type of a PNG, JPEG or WEBP file (just enough header parsing for the size rules)
export function imageInfo(file) {
  const b = fs.readFileSync(file);
  if (b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    const animated = b.includes(Buffer.from("acTL"));
    return { type: "png", width: b.readUInt32BE(16), height: b.readUInt32BE(20), bytes: b.length, animated };
  }
  return { type: "other", bytes: b.length };
}

function versionAtLeast(v, min) {
  const a = v.split(".").map(Number);
  const b = min.split(".").map(Number);
  for (let i = 0; i < 3; i++) if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0);
  return true;
}

// Orca's checks of orca-plugin.json for a declarative plugin with VM recipes, listed under `owner`.
export function validateManifest(dir, owner) {
  const errors = [];
  const m = JSON.parse(fs.readFileSync(path.join(dir, "orca-plugin.json"), "utf8"));
  for (const k of Object.keys(m)) if (!MANIFEST_KEYS.has(k)) fail(errors, `unknown key ${k}`);
  if (m.manifestVersion !== 2) fail(errors, "manifestVersion must be 2");
  if (!["data", "declarative", "sandboxed-ui", "trusted-worker"].includes(m.execution)) fail(errors, "bad execution");
  if (m.contributes?.vmRecipes?.length && ["data"].includes(m.execution)) fail(errors, "vmRecipes need declarative or higher");
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(m.id ?? "") || ["constructor", "prototype"].includes(m.id)) fail(errors, "id must be kebab-case");
  if (/^orca-/.test(m.id)) fail(errors, "ids starting orca- install only from the official catalog");
  if (RESERVED_OWNERS.test(owner)) fail(errors, "reserved owner");
  const engine = /^>=(\d+\.\d+\.\d+)$/.exec(m.engines?.orca ?? "");
  if (!engine) fail(errors, "engines.orca must be >=x.y.z");
  else if (!versionAtLeast(engine[1], "1.4.245")) fail(errors, "format 2 needs engines.orca >=1.4.245");
  if (m.publisher === undefined) {
    if (engine && !versionAtLeast(engine[1], "1.4.246")) fail(errors, "leaving publisher out needs engines.orca >=1.4.246");
  } else if (m.publisher !== owner) fail(errors, `publisher ${m.publisher} is not the repository owner ${owner}`);
  if (typeof m.name !== "string" || !m.name) fail(errors, "name is required");
  if (!/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/.test(m.version ?? "")) fail(errors, "version must be semver");
  if (m.homepage !== undefined && !/^https:\/\//.test(m.homepage)) fail(errors, "homepage must be https");
  if (!m.license && !fs.existsSync(path.join(dir, "LICENSE"))) fail(errors, "no licence");
  for (const k of Object.keys(m.contributes ?? {})) if (!CONTRIBUTION_KEYS.has(k)) fail(errors, `unknown contribution ${k}`);
  if (m.icon !== undefined) {
    if (!relativeOk(m.icon) || !fs.existsSync(path.join(dir, m.icon))) fail(errors, "icon missing");
    else {
      const i = imageInfo(path.join(dir, m.icon));
      if (i.type !== "png" || i.animated || i.width !== i.height || i.width < 128 || i.bytes > 256 * 1024) fail(errors, `icon must be a static square PNG of at least 128 px and 256 KB at most (${JSON.stringify(i)})`);
    }
  }
  if (m.cover !== undefined) {
    if (!m.cover.alt) fail(errors, "cover.alt is required");
    if (!relativeOk(m.cover.path) || !fs.existsSync(path.join(dir, m.cover.path))) fail(errors, "cover missing");
    else {
      const i = imageInfo(path.join(dir, m.cover.path));
      if (i.type !== "png" || i.animated || i.width < 800 || i.height < 500 || i.bytes > 1024 * 1024) fail(errors, `cover must be at least 800x500 and 1024 KB at most (${JSON.stringify(i)})`);
    }
  }
  const recipes = m.contributes?.vmRecipes ?? [];
  if (recipes.length > 64) fail(errors, "at most 64 recipes");
  for (const r of recipes) {
    if (Object.keys(r).some((k) => k !== "path") || !relativeOk(r.path)) fail(errors, "vmRecipes entries are {path}");
    else if (!fs.existsSync(path.join(dir, r.path))) fail(errors, `recipe ${r.path} missing`);
    else {
      try {
        parseRecipe(fs.readFileSync(path.join(dir, r.path), "utf8"));
      } catch (error) {
        fail(errors, `recipe ${r.path}: ${error.message}`);
      }
    }
  }
  return { manifest: m, errors };
}

const RECIPE_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;

function recipeCommand(v, name) {
  if (typeof v !== "string" || !v.trim() || v.length > 32 * 1024 || v.includes("\0")) throw new Error(`${name} must be a command`);
  return v.trim();
}

// plugin-vm-recipe-artifact.ts: a strict object; suspend and resume come together.
export function parseRecipe(raw) {
  const r = JSON.parse(raw);
  const allowed = new Set(["schemaVersion", "id", "name", "description", "checkoutMode", "create", "suspend", "resume", "destroy"]);
  for (const k of Object.keys(r)) if (!allowed.has(k)) throw new Error(`unknown key ${k}`);
  if (r.schemaVersion !== 1) throw new Error("schemaVersion must be 1");
  if (!RECIPE_ID.test(r.id ?? "")) throw new Error("bad recipe id");
  if (typeof r.name !== "string" || !r.name.trim() || r.name.trim().length > 128) throw new Error("bad name");
  if (r.description !== undefined && (typeof r.description !== "string" || !r.description.trim() || r.description.length > 1024)) throw new Error("bad description");
  if (r.checkoutMode !== undefined && !["orca-worktree", "provisioned-root"].includes(r.checkoutMode)) throw new Error("bad checkoutMode");
  const recipe = { id: r.id, name: r.name, checkoutMode: r.checkoutMode, create: recipeCommand(r.create, "create") };
  if (Boolean(r.suspend) !== Boolean(r.resume)) throw new Error("suspend and resume must be declared together");
  if (r.suspend) recipe.suspend = recipeCommand(r.suspend, "suspend");
  if (r.resume) recipe.resume = recipeCommand(r.resume, "resume");
  if (r.destroy === "none") recipe.destroyDisabled = true;
  else if (r.destroy !== undefined) recipe.destroy = recipeCommand(r.destroy, "destroy");
  return recipe;
}

// ---------------------------------------------------------------- results

const TARGET_KEYS = new Set([
  "label", "configHost", "host", "port", "username", "identityFile", "identityAgent", "identitiesOnly",
  "proxyCommand", "jumpHost", "relayGracePeriodSeconds", "portForwards",
]);

function strict(obj, keys, where) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw new Error(`${where} must be an object`);
  for (const k of Object.keys(obj)) if (!keys.has(k)) throw new Error(`${where}: unknown key ${k}`);
}

function nonEmpty(v, where) {
  if (typeof v !== "string" || v.length === 0) throw new Error(`${where} must be a non-empty string`);
}

function checkConnection(c) {
  if (c?.type === "ssh") {
    strict(c, new Set(["type", "target", "projectRoot"]), "connection");
    nonEmpty(c.projectRoot, "projectRoot");
    const t = c.target;
    strict(t, TARGET_KEYS, "target");
    nonEmpty(t.label, "label");
    nonEmpty(t.host, "host");
    if (!Number.isInteger(t.port) || t.port < 1 || t.port > 65535) throw new Error("bad port");
    if (typeof t.username !== "string") throw new Error("username must be a string");
    for (const k of ["configHost", "identityFile", "identityAgent", "proxyCommand", "jumpHost"]) if (t[k] !== undefined) nonEmpty(t[k], k);
    if (t.identitiesOnly !== undefined && typeof t.identitiesOnly !== "boolean") throw new Error("identitiesOnly must be boolean");
    if (t.relayGracePeriodSeconds !== undefined && !(t.relayGracePeriodSeconds === 0 || (t.relayGracePeriodSeconds >= 60 && t.relayGracePeriodSeconds <= 604800))) throw new Error("bad relay grace period");
  } else if (c?.type === "orca-server") {
    strict(c, new Set(["type", "pairingCode", "projectRoot"]), "connection");
  } else throw new Error("connection.type must be ssh or orca-server");
}

function isAbsoluteRuntimePath(p) {
  return p.startsWith("/") || /^[A-Za-z]:[\\/]/.test(p) || p.startsWith("\\\\") || p.startsWith("//");
}

// parseEphemeralVmRecipeResult: one JSON object, one of the four result shapes, absolute projectRoot.
export function parseResult(stdout) {
  const trimmed = String(stdout).trim();
  if (!trimmed) throw new Error("recipeResultMissing");
  let r;
  try {
    r = JSON.parse(trimmed);
  } catch {
    throw new Error("recipeResultNotJsonObject");
  }
  if (r?.userData !== undefined && (typeof r.userData !== "object" || Array.isArray(r.userData))) throw new Error("bad userData");
  if (r.schemaVersion === 2) {
    if (r.checkoutMode !== "provisioned-root") throw new Error("schema 2 needs checkoutMode provisioned-root");
    if ("connection" in r) strict(r, new Set(["schemaVersion", "checkoutMode", "connection", "userData"]), "result");
    else strict(r, new Set(["schemaVersion", "checkoutMode", "pairingCode", "projectRoot", "userData"]), "result");
  } else if (r.schemaVersion === 1) {
    if ("connection" in r) strict(r, new Set(["schemaVersion", "connection", "userData"]), "result");
    else strict(r, new Set(["schemaVersion", "pairingCode", "projectRoot", "userData"]), "result");
  } else throw new Error("bad schemaVersion");
  const connection = "connection" in r ? r.connection : { type: "orca-server", pairingCode: r.pairingCode, projectRoot: r.projectRoot };
  checkConnection(connection);
  if (!isAbsoluteRuntimePath(connection.projectRoot)) throw new Error("recipeResultProjectRootNotAbsolute");
  return { result: r, connection };
}

// getEphemeralVmRecipeCheckoutModeFailure
export function checkoutModeFailure(recipe, result) {
  const configured = recipe.checkoutMode ?? "orca-worktree";
  const got = result.schemaVersion === 2 ? "provisioned-root" : "orca-worktree";
  return configured === got ? null : configured === "provisioned-root" ? "provisionedRootResultRequired" : "provisionedRootNotConfigured";
}

// getProvisionedRootResumeIntegrityError
export function resumeIntegrityError(previous, resumed) {
  if (previous.schemaVersion !== 2) return null;
  const a = previous.connection;
  const b = resumed.connection;
  if (a.projectRoot !== b.projectRoot) return "The provisioned workspace root changed while the runtime was suspended.";
  if (a.type !== b.type) return "The provisioned workspace connection type changed while the runtime was suspended.";
  if (a.type === "ssh") {
    for (const f of ["configHost", "username", "identityFile", "identityAgent", "identitiesOnly", "proxyCommand", "jumpHost"]) {
      if (a.target[f] !== b.target[f]) return "The provisioned workspace SSH ownership changed while the runtime was suspended.";
    }
  }
  return null;
}

// ---------------------------------------------------------------- running a phase

// buildRecipeEnv: Orca's own environment, then these.
export function recipeEnv(baseEnv, mode, context, schemaVersion) {
  return {
    ...baseEnv,
    ORCA_VM_MODE: mode,
    ORCA_VM_INSTANCE_ID: context.instanceId ?? "",
    ORCA_RECIPE_ID: context.recipeId,
    ORCA_PROJECT_ID: context.projectId ?? "",
    ORCA_WORKSPACE_ID: context.workspaceId ?? "",
    ORCA_WORKSPACE_NAME: context.workspaceName ?? "",
    ORCA_REPO_PATH: context.repoPath,
    ORCA_REPO_URL: context.repoUrl ?? "",
    ORCA_REPO_BRANCH: context.branch ?? "",
    ORCA_REPO_REF: context.ref ?? "",
    ORCA_REPO_REF_HEAD: context.expectedRefHead ?? "",
    ORCA_RECIPE_RESULT_SCHEMA_VERSION: String(schemaVersion),
    ORCA_VERSION: context.orcaVersion ?? "",
  };
}

// runRecipeCommand: the command through a shell, in the repo, as its own process group; stdin is
// the payload (or closed at once). cancelAfterMs is Orca's Cancel: SIGTERM to the group, then
// SIGKILL 5 seconds later.
export function runPhase({ command, mode, context, recipe, stdin, baseEnv, cancelAfterMs }) {
  const schemaVersion = recipe.checkoutMode === "provisioned-root" ? 2 : 1;
  return new Promise((resolve, reject) => {
    const child = spawn(command, {
      cwd: context.repoPath,
      detached: true,
      env: recipeEnv(baseEnv, mode, context, schemaVersion),
      shell: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    const timers = [];
    if (cancelAfterMs !== undefined) {
      const kill = (signal) => {
        try {
          process.kill(-child.pid, signal);
        } catch {
          // the group is gone already
        }
      };
      timers.push(setTimeout(() => kill("SIGTERM"), cancelAfterMs));
      timers.push(setTimeout(() => kill("SIGKILL"), cancelAfterMs + 5_000));
    }
    child.on("error", reject);
    child.on("close", (exitCode, signal) => {
      timers.forEach(clearTimeout);
      resolve({ exitCode, signal, stdout, stderr });
    });
    child.stdin.end(stdin ?? undefined);
  });
}

// buildEphemeralVmRecipeLifecyclePayload
export function payload(mode, recipe, context, recipeResult) {
  return `${JSON.stringify({
    schemaVersion: 1,
    mode,
    recipeId: recipe.id,
    instanceId: context.instanceId,
    projectId: context.projectId,
    workspaceId: context.workspaceId,
    workspaceName: context.workspaceName,
    recipeResult,
  })}\n`;
}

// buildSshArgs for a recipe's target (source "manual": configHost is not used), with Orca's
// connection reuse: ControlMaster=auto under a control path of its own, kept 300 s after the last
// session.
export function orcaSshArgs(target, { configFile, controlPath } = {}) {
  const args = [];
  if (configFile) args.push("-F", configFile);
  args.push("-o", "BatchMode=no", "-T");
  if (controlPath) args.push("-o", "ControlMaster=auto", "-o", `ControlPath=${controlPath}`, "-o", "ControlPersist=300", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3");
  else args.push("-S", "none");
  if (target.port !== 22) args.push("-p", String(target.port));
  if (target.identityFile) args.push("-i", target.identityFile);
  if (target.identityAgent) args.push("-o", `IdentityAgent=${target.identityAgent}`);
  if (target.identitiesOnly) args.push("-o", "IdentitiesOnly=yes");
  if (target.jumpHost) args.push("-J", target.jumpHost);
  if (target.proxyCommand) args.push("-o", `ProxyCommand=${target.proxyCommand}`);
  args.push("--", target.username ? `${target.username}@${target.host}` : target.host);
  return args;
}

function run(args, { env, timeoutMs = 60_000 } = {}) {
  return new Promise((resolve) => {
    const child = spawn("ssh", args, { env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    const timer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    child.on("close", (status) => {
      clearTimeout(timer);
      resolve({ status, stdout, stderr });
    });
  });
}

// Run a command on the target the way Orca's system-ssh transport does (a target with a
// proxyCommand always goes through system OpenSSH): resolve the host with `ssh -G` first, as Orca
// does before it connects, then run the command over the shared connection.
export async function orcaSsh(target, command, { configFile, controlPath, env, timeoutMs = 60_000 } = {}) {
  const resolved = await run([...(configFile ? ["-F", configFile] : []), "-G", "--", target.host], { env, timeoutMs });
  if (resolved.status !== 0) return { ...resolved, stage: "ssh -G" };
  return run([...orcaSshArgs(target, { configFile, controlPath }), command], { env, timeoutMs });
}

// Orca's disconnect: close the shared connection.
export function orcaSshExit(target, { configFile, controlPath, env } = {}) {
  return run([...orcaSshArgs(target, { configFile, controlPath }).filter((a) => a !== "--" && a !== `${target.username}@${target.host}`), "-O", "exit", `${target.username}@${target.host}`], { env, timeoutMs: 15_000 });
}
