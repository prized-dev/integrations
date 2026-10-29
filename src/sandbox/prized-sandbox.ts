import type { WorkspaceStore } from "../workspace/workspace-store.ts";
import type { DurableMap } from "../persistence/durable-map.ts";
import type { AdvisoryLock } from "../persistence/advisory-lock.ts";
import type { CredentialPathSpec } from "../credentials/resident-paths.ts";
import { ephemeralCredLinkPaths } from "../credentials/resident-paths.ts";
import type { LayerInstallFile } from "../deployment/load-layer.ts";
import { errMessage, swallowAs } from "../util/errors.ts";
import { NonRetryableTurnError } from "../core/turn-error.ts";
import { createLayerToolInstaller } from "./layer-tool-install.ts";
import { AWS_CLI_HOME_ENSURE } from "./aws-cli.ts";
import { GH_CLI_HOME_ENSURE } from "./gh-cli.ts";
import { createExecSandboxBase, sandboxScopeName } from "./exec-sandbox-base.ts";
import { createExecProcessSessions } from "./exec-process-session.ts";
import {
  createBackendBlobStaging,
  createExecExport,
  createExecFileOps,
  type BlobStagingOptions,
} from "./exec-file-ops.ts";
import {
  visibleNotInstalled,
  visibleTools,
  type AgentComputerProfile,
  type ComputerStatus,
  type ExecResult,
  type Sandbox,
} from "./sandbox.ts";
import { PRIZED_HOME_DIR, type PrizedClient, type PrizedSandboxInfo } from "./prized-client.ts";

const DEFAULT_KEEP_WARM_SEC = 3600;
const SCRATCH_AUTO_PAUSE_MIN = 10;
const SCRATCH_AUTO_DELETE_MIN = 24 * 60;
const GUEST_PROBE_TIMEOUT_SEC = 15;
const TOOLS_TIMEOUT_SEC = 300;
const MAX_RUNTIME_MIN = 7 * 24 * 60;
export const PRIZED_TOOLS_ENSURE = `(${GH_CLI_HOME_ENSURE}) & gh=$!; (${AWS_CLI_HOME_ENSURE}) & aws=$!; rc=0; wait $gh || rc=$?; wait $aws || rc=$?; exit $rc`;
const DELETED_STATES = new Set(["deleting", "deleted"]);
const SYSTEM_PREFIX = "/usr/local/";
const HOME_PREFIX = `${PRIZED_HOME_DIR}/.local/`;
const PARKED_STATES = new Set(["pausing", "paused"]);

const PRIZED_LABELS = { deployment: "qm-deployment", name: "qm-name", kind: "qm-kind" } as const;

export interface StoredPrizedSandbox {
  sandboxId: string;
  createdAtMs: number;
  toolsReady?: boolean;
}

export interface PrizedSandboxOptions extends BlobStagingOptions {
  client: PrizedClient;
  store: DurableMap<StoredPrizedSandbox>;
  advisoryLock: AdvisoryLock;
  namePrefix?: string;
  defaultTimeoutSec?: number;
  keepWarmSec?: number;
  labels?: Record<string, string>;
  extraTools?: string[];
  credentialPaths?: CredentialPathSpec[];
  layerToolFiles?: () => readonly LayerInstallFile[];
  onError?: (e: { category: string; code: string; message: string; scopeLabel?: string }) => void;
}

export function createPrizedSandbox(workspace: WorkspaceStore, opts: PrizedSandboxOptions): Sandbox {
  const { client, store, advisoryLock } = opts;
  const prefix = opts.namePrefix ?? "qm";
  const defaultTimeoutSec = opts.defaultTimeoutSec ?? 600;
  const autoPauseMin = Math.max(1, Math.ceil((opts.keepWarmSec ?? DEFAULT_KEEP_WARM_SEC) / 60));
  const idByName = new Map<string, string>();
  const woken = new Set<string>();
  const observed: { cpus?: number; memoryMb?: number; diskGb?: number } = {};
  const lifecycleKey = (scope: string): string => `prized-provision:${prefix}:${scope}`;
  const selector = (name: string, kind: "computer" | "scratch"): Record<string, string> => ({
    [PRIZED_LABELS.deployment]: prefix,
    [PRIZED_LABELS.name]: name,
    [PRIZED_LABELS.kind]: kind,
  });
  const report = (category: string, code: string, message: string, scopeLabel?: string): void =>
    opts.onError?.({ category, code, message, ...(scopeLabel ? { scopeLabel } : {}) });
  const refuse = (code: string, message: string, scope: string): NonRetryableTurnError => {
    idByName.delete(sandboxScopeName(prefix, scope));
    report("sandbox_preservation", code, message, scope);
    return new NonRetryableTurnError(message);
  };
  const homeInstalled = (file: LayerInstallFile): LayerInstallFile =>
    file.to.startsWith(SYSTEM_PREFIX) ? { ...file, to: HOME_PREFIX + file.to.slice(SYSTEM_PREFIX.length) } : file;

  const note = (info: PrizedSandboxInfo): PrizedSandboxInfo => {
    observed.cpus = info.cpus;
    observed.memoryMb = info.memoryMb;
    observed.diskGb = info.diskGb;
    return info;
  };

  async function locate(name: string): Promise<PrizedSandboxInfo | null> {
    const listed = await client.list(selector(name, "computer"));
    return listed.find((info) => !DELETED_STATES.has(info.state)) ?? null;
  }

  async function idFor(name: string): Promise<string> {
    const cached = idByName.get(name);
    if (cached) return cached;
    const scope = base.scopeFor(name);
    const id = (scope ? (await store.get(scope))?.sandboxId : undefined) ?? (await locate(name))?.id;
    if (!id) throw new Error(`prized sandbox ${name} is not provisioned`);
    idByName.set(name, id);
    return id;
  }

  const exec = async (name: string, script: string, timeoutSec: number): Promise<ExecResult> => {
    const result = await client.exec(await idFor(name), script, timeoutSec);
    return { stdout: result.stdout, stderr: result.stderr, code: result.exitCode, timedOut: result.timedOut };
  };
  const writeAbsBytes = async (name: string, absPath: string, data: Uint8Array): Promise<void> =>
    client.writeFileBytes(await idFor(name), absPath, data);
  const readAbsBytes = async (name: string, absPath: string): Promise<Uint8Array | null> =>
    client.readFileBytes(await idFor(name), absPath);

  async function installTools(info: PrizedSandboxInfo, scope?: string): Promise<void> {
    const failure = await client.exec(info.id, PRIZED_TOOLS_ENSURE, TOOLS_TIMEOUT_SEC).then(
      (installed) =>
        installed.exitCode === 0
          ? null
          : `exit ${installed.exitCode}: ${installed.stderr.slice(-300)} ${installed.stdout.slice(-300)}`.trim(),
      (error: unknown) => errMessage(error),
    );
    if (!failure && scope) await store.merge(scope, { toolsReady: true });
    if (failure)
      report(
        "sandbox_template",
        "tools_install_failed",
        `Prized sandbox ${info.id} could not install gh and the AWS CLI (${failure})`,
        scope,
      );
  }

  async function adoptOrCreate(
    name: string,
    scope: string,
    onStatus?: (text: string) => void,
  ): Promise<{ coldStart: boolean }> {
    const stored = await store.get(scope);
    const info = stored ? await client.get(stored.sandboxId) : await locate(name);
    if (stored && (!info || DELETED_STATES.has(info.state)))
      throw refuse(
        "computer_lost",
        `This scope's Prized computer ${stored.sandboxId} no longer exists for this API key (it was deleted outside QM, its run time ran out, or PRIZED_API_KEY now belongs to another workspace). Retire this sandbox and create a new one to start again with an empty home.`,
        scope,
      );
    if (info?.state === "error")
      throw refuse(
        "computer_error",
        `This scope's Prized computer ${info.id} is in error${info.errorReason ? ` (${info.errorReason})` : ""}. Try again later, as it may recover; if it stays in error, retire this sandbox and create a new one.`,
        scope,
      );
    if (info) {
      note(info);
      idByName.set(name, info.id);
      if (!stored) await store.put(scope, { sandboxId: info.id, createdAtMs: Date.now() });
      if (info.state !== "running") {
        onStatus?.("Resuming your computer…");
        await client.resume(info.id);
        woken.add(name);
      }
      if (!stored?.toolsReady) await installTools(info, scope);
      return { coldStart: false };
    }
    onStatus?.("Starting your computer…");
    const created = note(
      await client.create({
        labels: { ...opts.labels, ...selector(name, "computer") },
        autoPauseMin,
        maxRuntimeMin: MAX_RUNTIME_MIN,
      }),
    );
    idByName.set(name, created.id);
    woken.add(name);
    await store.put(scope, { sandboxId: created.id, createdAtMs: Date.now() });
    await installTools(created, scope);
    return { coldStart: true };
  }

  async function destroyScope(scope: string): Promise<void> {
    const name = sandboxScopeName(prefix, scope);
    await base.provisionQueue(scope, () =>
      advisoryLock.withLock(lifecycleKey(scope), async () => {
        const stored = await store.get(scope);
        if (stored) await client.delete(stored.sandboxId);
        await client.deleteByLabels(selector(name, "computer"));
        idByName.delete(name);
        await store.delete(scope);
      }),
    );
  }

  const base = createExecSandboxBase({
    workspace,
    label: "prized",
    prefix,
    homeDir: PRIZED_HOME_DIR,
    defaultTimeoutSec,
    credentialPaths: opts.credentialPaths ?? [],
    deleteFailureCode: "prized_delete_failed",
    onError: opts.onError,
    exec,
    writeAbsBytes,
    readAbsBytes,
    ensureResident: (name, onStatus) => {
      const scope = base.scopeFor(name) ?? "default";
      return advisoryLock.withLock(lifecycleKey(scope), () => adoptOrCreate(name, scope, onStatus));
    },
    isProvisioned: (name) => idByName.has(name),
    async recreateScratch(name) {
      await client
        .deleteByLabels(selector(name, "scratch"))
        .catch(swallowAs("prized-sandbox: stale scratch delete", undefined));
      const info = await client.create({
        labels: { ...opts.labels, ...selector(name, "scratch") },
        autoPauseMin: SCRATCH_AUTO_PAUSE_MIN,
        autoDeleteMin: SCRATCH_AUTO_DELETE_MIN,
      });
      idByName.set(name, info.id);
      await installTools(info);
    },
    async deleteInstance(name) {
      const scope = base.scopeFor(name);
      if (scope) return destroyScope(scope);
      idByName.delete(name);
      await client.deleteByLabels(selector(name, "scratch"));
    },
    forgetInstance: (name) => idByName.delete(name),
    ...(opts.layerToolFiles
      ? { installLayerTools: createLayerToolInstaller(() => opts.layerToolFiles!().map(homeInstalled)) }
      : {}),
  });

  const processes = createExecProcessSessions({
    run: (handle, command, execOpts) =>
      exec(handle.id, command, execOpts?.timeoutMs ? Math.ceil(execOpts.timeoutMs / 1000) : defaultTimeoutSec),
  });
  const staging = createBackendBlobStaging("prized", exec, opts);
  const files = createExecFileOps({ label: "prized", exec, writeInline: writeAbsBytes });
  const exports = createExecExport({
    label: "prized",
    exec,
    readAbsBytes,
    defaultHomeDir: PRIZED_HOME_DIR,
    ephemeralCredentialPrefixes: ephemeralCredLinkPaths(opts.credentialPaths ?? []).map(({ rel }) => rel),
  });

  const profile: AgentComputerProfile = {
    backend: "prized",
    writablePersistence: "provider_managed",
    processSessions: true,
    parksOnTeardown: true,
    egressEnforcement: "none",
    spec: {
      os: "Ubuntu 24.04, Prized Firecracker sandbox (provider pause preserves state; no sudo, so install tools under $HOME and pip packages in a venv; publish durable work to git or Files)",
      runtimes: ["Node 22", "Python 3"],
      homeDir: PRIZED_HOME_DIR,
      workdir: base.workspaceDir,
      get cpus() {
        return observed.cpus;
      },
      get memoryMb() {
        return observed.memoryMb;
      },
      get diskGb() {
        return observed.diskGb;
      },
      get tools() {
        return visibleTools([
          "git",
          "curl",
          "jq",
          "rg",
          "tar",
          "python3",
          "node",
          "npm",
          "pnpm",
          "claude",
          "codex",
          "gh",
          "aws",
          ...(opts.extraTools ?? []),
        ]);
      },
      get notInstalled() {
        return visibleNotInstalled(["gcloud", "kubectl", "flyctl", "glab", "docker"], opts.extraTools ?? []);
      },
    },
  };

  return {
    profile,
    async provision(layers, provOpts) {
      const writable = layers.find((layer) => layer.mode === "rw") ?? layers[0];
      const name = sandboxScopeName(prefix, writable?.scopeId ?? "default");
      try {
        const handle = await base.provision(layers, provOpts);
        woken.delete(name);
        return handle;
      } catch (error) {
        const id = idByName.get(name);
        if (!provOpts?.scratch && woken.delete(name) && id)
          await client.pause(id).catch(swallowAs("prized-sandbox: pause after failed provision", undefined));
        throw error;
      }
    },
    run: base.run,
    readFile: base.readFile,
    writeFile: base.writeFile,
    readFileBytes: base.readFileBytes,
    writeFileBytes: base.writeFileBytes,
    ...files,
    ...staging,
    exportFiles: exports.exportFiles,
    startProcess: processes.startProcess,
    readProcess: processes.readProcess,
    writeStdin: processes.writeStdin,
    signalProcess: processes.signalProcess,
    listProcesses: processes.listProcesses,
    destroyScope,

    async teardown(handle, tdOpts) {
      if (handle.scratch || tdOpts?.destroy) return base.teardown(handle, tdOpts);
      const id = idByName.get(handle.id);
      if (!id) return;
      const scope = base.scopeFor(handle.id);
      await base.provisionQueue(scope ?? handle.id, async () => {
        try {
          if (tdOpts?.keepWarm) await client.keepAlive(id);
          else await client.pause(id);
        } catch (error) {
          report(
            "sandbox_preservation",
            tdOpts?.keepWarm ? "keep_warm_failed" : "pause_failed",
            errMessage(error),
            scope,
          );
        }
      });
    },

    async computerStatus(scope): Promise<ComputerStatus> {
      const stored = await store.get(scope);
      if (!stored) return { machine: "no Prized sandbox provisioned yet", provisioned: false, guestResponsive: false };
      const recovery = { strategy: "provider_pause" as const };
      try {
        const info = await client.get(stored.sandboxId);
        if (!info)
          return {
            machine: `Prized sandbox ${stored.sandboxId}: gone`,
            provisioned: false,
            guestResponsive: false,
            recovery,
          };
        note(info);
        const machine = `Prized sandbox ${info.id}: ${info.state}${info.errorReason ? ` (${info.errorReason})` : ""}`;
        const known = {
          machine,
          listed: info.state,
          recovery,
          ...(info.expiresAtMs !== null ? { expiresAtMs: info.expiresAtMs } : {}),
        };
        if (info.state !== "running")
          return {
            ...known,
            provisioned: !DELETED_STATES.has(info.state) && info.state !== "error",
            guestResponsive: false,
            ...(PARKED_STATES.has(info.state) ? { lifecycleState: "paused" as const } : {}),
          };
        const probe = await client.exec(info.id, "true", GUEST_PROBE_TIMEOUT_SEC);
        return { ...known, provisioned: true, lifecycleState: "running", guestResponsive: probe.exitCode === 0 };
      } catch (error) {
        return {
          machine: `Prized sandbox ${stored.sandboxId}`,
          guestResponsive: false,
          probeError: errMessage(error),
          recovery,
        };
      }
    },
  };
}
