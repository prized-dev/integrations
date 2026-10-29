import { randomUUID } from "node:crypto";
import { posix } from "node:path";
import type { ExecResult as SdkExecResult, Sandbox } from "@prized/sdk";
import { NonRetryableTurnError } from "../core/turn-error.ts";
import { swallowAs } from "../util/errors.ts";
import { shq } from "../util/shell.ts";

export const PRIZED_HOME_DIR = "/home/prized";
const PRIZED_TEMPLATE = "prized-agent-v1";
const GUEST_PATH = `${PRIZED_HOME_DIR}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`;
const STAGE_DIR = `${PRIZED_HOME_DIR}/.qm-stage`;
const TIMEOUT_EXIT_CODE = 124;
const MISSING_EXIT_CODE = 44;
const STAGE_TIMEOUT_SEC = 120;
const REFUSAL_CODES = new Set([
  "unauthorized",
  "token_expired",
  "key_access_denied",
  "invalid_api_key",
  "missing_api_key",
  "insufficient_balance",
  "paid_balance_required",
  "sandbox_quota_exceeded",
  "workspace_held",
  "workspace_closing",
  "template_not_found",
  "template_inactive",
]);
const STAGED_CODES = new Set(["symlink_not_allowed", "path_outside_root"]);

export type PrizedState = "starting" | "running" | "pausing" | "paused" | "resizing" | "deleting" | "deleted" | "error";

export interface PrizedSandboxInfo {
  id: string;
  state: PrizedState;
  labels: Record<string, string>;
  expiresAtMs: number | null;
  errorReason: string | null;
  cpus: number;
  memoryMb: number;
  diskGb: number;
}

export interface PrizedCreateOptions {
  labels: Record<string, string>;
  autoPauseMin: number;
  autoDeleteMin?: number;
  maxRuntimeMin?: number;
}

interface PrizedExecResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  timedOut: boolean;
}

export interface PrizedClient {
  create(opts: PrizedCreateOptions): Promise<PrizedSandboxInfo>;
  get(sandboxId: string): Promise<PrizedSandboxInfo | null>;
  list(labels: Record<string, string>): Promise<PrizedSandboxInfo[]>;
  resume(sandboxId: string): Promise<void>;
  pause(sandboxId: string, opts?: { wait?: boolean }): Promise<void>;
  keepAlive(sandboxId: string): Promise<void>;
  delete(sandboxId: string): Promise<void>;
  deleteByLabels(labels: Record<string, string>): Promise<string[]>;
  exec(sandboxId: string, script: string, timeoutSec: number): Promise<PrizedExecResult>;
  readFileBytes(sandboxId: string, absPath: string): Promise<Uint8Array | null>;
  writeFileBytes(sandboxId: string, absPath: string, data: Uint8Array): Promise<void>;
}

export interface SdkPrizedClientOptions {
  apiKey: string;
}

type PrizedSdk = typeof import("@prized/sdk");

const codeOf = (error: unknown): string | undefined => {
  const code = error instanceof Error ? (error as Error & { code?: unknown }).code : undefined;
  return typeof code === "string" ? code : undefined;
};

export function prizedFailure(error: unknown): unknown {
  const code = codeOf(error);
  if (!code || !REFUSAL_CODES.has(code)) return error;
  return new NonRetryableTurnError(`Prized refused this agent computer (${code}): ${(error as Error).message}`);
}

const isNotFound = (error: unknown): boolean => codeOf(error) === "not_found";

const needsStaging = (error: unknown): boolean => STAGED_CODES.has(codeOf(error) ?? "");

const isGone = (error: unknown): boolean => isNotFound(error) || codeOf(error) === "sandbox_deleted";

const timedOutResult = (error: unknown): SdkExecResult | undefined =>
  codeOf(error) === "command_timeout" ? (error as { result?: SdkExecResult }).result : undefined;

export const inPrizedHome = (absPath: string): boolean => posix.normalize(absPath).startsWith(`${PRIZED_HOME_DIR}/`);

function infoOf(sandbox: Sandbox): PrizedSandboxInfo {
  const { id, state, labels, expiresAt, errorReason, resources } = sandbox.info;
  return {
    id,
    state,
    labels,
    expiresAtMs: expiresAt ? expiresAt.getTime() : null,
    errorReason,
    cpus: resources.vcpu,
    memoryMb: resources.memoryMb,
    diskGb: resources.diskGb,
  };
}

export function createSdkPrizedClient(opts: SdkPrizedClientOptions): PrizedClient {
  const auth = { apiKey: opts.apiKey };
  const handles = new Map<string, Sandbox>();
  let sdk: Promise<PrizedSdk> | null = null;
  const loadSdk = (): Promise<PrizedSdk> =>
    (sdk ??= import("@prized/sdk").catch((error: unknown) => {
      sdk = null;
      throw error;
    }));

  const guarded = async <T>(work: () => Promise<T>): Promise<T> => {
    try {
      return await work();
    } catch (error) {
      throw prizedFailure(error);
    }
  };

  const handleFor = async (sandboxId: string): Promise<Sandbox> => {
    const cached = handles.get(sandboxId);
    if (cached) return cached;
    const { Sandbox: Sdk } = await loadSdk();
    const sandbox = await Sdk.connect(sandboxId, auth);
    handles.set(sandboxId, sandbox);
    return sandbox;
  };

  const onSandbox = <T>(sandboxId: string, work: (sandbox: Sandbox) => Promise<T>): Promise<T> =>
    guarded(async () => {
      try {
        return await work(await handleFor(sandboxId));
      } catch (error) {
        if (isGone(error)) handles.delete(sandboxId);
        throw error;
      }
    });

  const run = (sandbox: Sandbox, script: string, timeoutSec: number): Promise<PrizedExecResult> =>
    sandbox.exec(script, { timeoutMs: timeoutSec * 1000, fullOutput: true }).then(
      (result) => ({ stdout: result.stdout, stderr: result.stderr, exitCode: result.exitCode, timedOut: false }),
      (error: unknown) => {
        const partial = timedOutResult(error);
        if (!partial) throw error;
        return { stdout: partial.stdout, stderr: partial.stderr, exitCode: TIMEOUT_EXIT_CODE, timedOut: true };
      },
    );

  const discard = (sandbox: Sandbox, stage: string): Promise<void> =>
    run(sandbox, `rm -f ${shq(stage)}`, 30).then(
      () => undefined,
      swallowAs("prized-client: remove staged file", undefined),
    );

  const readBytes = async (sandbox: Sandbox, absPath: string): Promise<Uint8Array | null> => {
    try {
      return await sandbox.files.readBytes(absPath);
    } catch (error) {
      if (!isNotFound(error)) throw error;
      await sandbox.refresh();
      return null;
    }
  };

  const readStaged = async (sandbox: Sandbox, absPath: string): Promise<Uint8Array | null> => {
    const stage = `${STAGE_DIR}/${randomUUID()}`;
    const copied = await run(
      sandbox,
      `[ -f ${shq(absPath)} ] || exit ${MISSING_EXIT_CODE}; mkdir -p ${shq(STAGE_DIR)} && cp ${shq(absPath)} ${shq(stage)}`,
      STAGE_TIMEOUT_SEC,
    );
    if (copied.exitCode === MISSING_EXIT_CODE) return null;
    if (copied.exitCode !== 0)
      throw new Error(`prized: could not read ${absPath} (exit ${copied.exitCode}): ${copied.stderr.trim()}`);
    try {
      return await readBytes(sandbox, stage);
    } finally {
      await discard(sandbox, stage);
    }
  };

  const writeStaged = async (sandbox: Sandbox, absPath: string, data: Uint8Array): Promise<void> => {
    const stage = `${STAGE_DIR}/${randomUUID()}`;
    try {
      await sandbox.files.write(stage, data);
      const moved = await run(
        sandbox,
        `mkdir -p ${shq(posix.dirname(absPath))} && (umask 077; cat ${shq(stage)} > ${shq(absPath)}) && rm -f ${shq(stage)}`,
        STAGE_TIMEOUT_SEC,
      );
      if (moved.exitCode !== 0)
        throw new Error(`prized: could not write ${absPath} (exit ${moved.exitCode}): ${moved.stderr.trim()}`);
    } catch (error) {
      await discard(sandbox, stage);
      throw error;
    }
  };

  return {
    create: (createOpts) =>
      guarded(async () => {
        const { Sandbox: Sdk } = await loadSdk();
        const sandbox = await Sdk.create({
          ...auth,
          template: PRIZED_TEMPLATE,
          envVars: { PATH: GUEST_PATH },
          labels: createOpts.labels,
          autoPauseMin: createOpts.autoPauseMin,
          ...(createOpts.autoDeleteMin ? { autoDeleteMin: createOpts.autoDeleteMin } : {}),
          ...(createOpts.maxRuntimeMin ? { maxRuntimeMin: createOpts.maxRuntimeMin } : {}),
        });
        handles.set(sandbox.id, sandbox);
        return infoOf(sandbox);
      }),

    get: (sandboxId) =>
      guarded(async () => {
        try {
          const cached = handles.get(sandboxId);
          return infoOf(cached ? await cached.refresh() : await handleFor(sandboxId));
        } catch (error) {
          if (!isGone(error)) throw error;
          handles.delete(sandboxId);
          return null;
        }
      }),

    list: (labels) =>
      guarded(async () => {
        const { Sandbox: Sdk } = await loadSdk();
        const found = await Sdk.list({ ...auth, labels });
        for (const sandbox of found) handles.set(sandbox.id, sandbox);
        return found.map(infoOf);
      }),

    resume: (sandboxId) => onSandbox(sandboxId, (sandbox) => sandbox.resume()),

    pause: (sandboxId, pauseOpts) =>
      onSandbox(sandboxId, (sandbox) => sandbox.pause({ wait: pauseOpts?.wait ?? false })),

    keepAlive: (sandboxId) => onSandbox(sandboxId, (sandbox) => sandbox.keepAlive()),

    delete: (sandboxId) =>
      guarded(async () => {
        try {
          await (await handleFor(sandboxId)).delete();
        } catch (error) {
          if (!isGone(error)) throw error;
        } finally {
          handles.delete(sandboxId);
        }
      }),

    deleteByLabels: (labels) =>
      guarded(async () => {
        const { Sandbox: Sdk } = await loadSdk();
        const ids = await Sdk.deleteByLabels(labels, auth);
        for (const id of ids) handles.delete(id);
        return ids;
      }),

    exec: (sandboxId, script, timeoutSec) => onSandbox(sandboxId, (sandbox) => run(sandbox, script, timeoutSec)),

    readFileBytes: (sandboxId, absPath) =>
      onSandbox(sandboxId, async (sandbox) => {
        if (!inPrizedHome(absPath)) return readStaged(sandbox, absPath);
        try {
          return await readBytes(sandbox, absPath);
        } catch (error) {
          if (!needsStaging(error)) throw error;
          return readStaged(sandbox, absPath);
        }
      }),

    writeFileBytes: (sandboxId, absPath, data) =>
      onSandbox(sandboxId, async (sandbox) => {
        if (!inPrizedHome(absPath)) return writeStaged(sandbox, absPath, data);
        try {
          await sandbox.files.write(absPath, data);
        } catch (error) {
          if (!needsStaging(error)) throw error;
          await writeStaged(sandbox, absPath, data);
        }
      }),
  };
}
