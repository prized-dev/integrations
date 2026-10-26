/**
 * Prized Sandboxes provider for Mastra workspaces.
 *
 * A Prized sandbox is a Firecracker microVM run through `@prized/sdk`. `start()` finds the sandbox
 * this instance's logical `id` names (the `mastra-id` label the provider puts on the sandboxes it
 * creates), resumes it if it is paused, or creates one. `stop()` pauses it, keeping memory and
 * disk; `destroy()` deletes it.
 *
 * @see https://prized.dev/docs/sandboxes
 */

import { createHash } from 'node:crypto';

import type { RequestContext } from '@mastra/core/request-context';
import type {
  InstructionsOption,
  MastraSandboxOptions,
  ProviderStatus,
  SandboxCloneOptions,
  SandboxFileInput,
  SandboxInfo,
  WriteFilesOptions,
} from '@mastra/core/workspace';
import {
  MastraSandbox,
  SandboxAbortError,
  SandboxNotReadyError,
  validateSandboxFileMode,
} from '@mastra/core/workspace';
import { PrizedError, Sandbox } from '@prized/sdk';
import type { CreateOptions, ResourcesInput, SandboxState } from '@prized/sdk';

import { shellQuote } from './file-commands.js';
import { PrizedSandboxFilesystem } from './filesystem.js';
import { LOG_PREFIX, PrizedProcessManager, isGone } from './process-manager.js';

/** The label a sandbox this provider creates carries: its logical `id`, so a later `start()` finds it. */
export const ID_LABEL = 'mastra-id';
/** Where commands run when neither the command nor the sandbox names a directory. */
export const DEFAULT_WORKDIR = '/home/prized/work';
/** Foreground command timeout when none is configured: 5 minutes. */
export const DEFAULT_TIMEOUT_MS = 300_000;
/** Idle minutes before a sandbox this provider creates pauses itself. */
export const DEFAULT_AUTO_PAUSE_MIN = 10;

const MAX_LABEL_VALUE = 256;
/** States in which a sandbox will not come back. */
const DEAD: ReadonlySet<SandboxState> = new Set(['deleting', 'deleted', 'error']);
/** States a found sandbox can be adopted from. */
const ADOPTABLE: ReadonlySet<SandboxState> = new Set(['running', 'paused', 'pausing', 'starting', 'resizing']);
const SETTLE_TIMEOUT_MS = 180_000;
const SETTLE_POLL_MS = 500;

export interface PrizedSandboxOptions extends Omit<MastraSandboxOptions, 'processes'> {
  /**
   * Logical id. `start()` reuses the sandbox created under this id (found by its `mastra-id`
   * label) and creates one when there is none. Generated when omitted.
   */
  id?: string;
  /**
   * Attach to this sandbox (`sbx_…`) on `start()`. If it no longer exists, the provider falls back
   * to the one created under `id`, or creates one. Read the current one from `sandbox.sandboxId`.
   */
  sandboxId?: string;
  /** Prized API key. Default: the `PRIZED_API_KEY` environment variable. */
  apiKey?: string;
  /** Template a new sandbox starts from. Default `prized-base-v1`. */
  template?: string;
  /** vCPU, memory and disk of a new sandbox (`{ vcpu: 2, memoryGb: 4 }`); the template's defaults when omitted. */
  resources?: ResourcesInput;
  /** Labels for a new sandbox, to find it in `prized sandbox ls` or the API (`mastra-id` is always added). */
  labels?: Record<string, string>;
  /** Idle minutes before a new sandbox pauses itself (0: never). Default 10. */
  autoPauseMin?: number;
  /** Minutes after pausing before Prized deletes a new sandbox (0: never). Default: never, so a paused sandbox is kept until you delete it. */
  autoDeleteMin?: number;
  /** Minutes of running before Prized deletes a new sandbox. Default: your plan's (24 hours on paid plans). */
  maxRuntimeMin?: number;
  /** Foreground command timeout in milliseconds (0: none). Default 300 000. A command's own `timeout` wins. */
  timeout?: number;
  /** Replace or extend the instructions the agent gets about this sandbox. */
  instructions?: InstructionsOption;
}

export class PrizedSandbox extends MastraSandbox<Sandbox> {
  readonly id: string;
  readonly name = 'PrizedSandbox';
  readonly provider = 'prized';
  status: ProviderStatus = 'pending';

  declare readonly processes: PrizedProcessManager;

  private _sandbox: Sandbox | null = null;
  private _filesystem: PrizedSandboxFilesystem | undefined;
  private _recovery: Promise<void> | undefined;
  private _preferredSandboxId?: string;
  private readonly _apiKey?: string;
  private readonly _labelValue: string;
  private readonly _options: PrizedSandboxOptions;

  constructor(options: PrizedSandboxOptions = {}) {
    super({
      ...options,
      name: 'PrizedSandbox',
      workingDirectory: options.workingDirectory ?? DEFAULT_WORKDIR,
      processes: new PrizedProcessManager(),
    });
    this.id = options.id ?? `mastra-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    this._labelValue = labelValueFor(this.id);
    this._preferredSandboxId = options.sandboxId;
    this._apiKey = options.apiKey ?? process.env.PRIZED_API_KEY;
    this.defaultCommandTimeout = options.timeout ?? DEFAULT_TIMEOUT_MS;
    this._options = { ...options };
  }

  /** The `@prized/sdk` sandbox, for the rest of its API (files, fork, ...). Throws before `start()`. */
  get sandbox(): Sandbox {
    if (!this._sandbox) throw new SandboxNotReadyError(this.id);
    return this._sandbox;
  }

  /**
   * The sandbox's own disk as a Mastra workspace filesystem, for the agent's file tools. A
   * `Workspace` doesn't use it by itself: pass it as `filesystem`.
   *
   * ```ts
   * const workspace = new Workspace({ sandbox, filesystem: sandbox.filesystem });
   * ```
   */
  get filesystem(): PrizedSandboxFilesystem {
    this._filesystem ??= new PrizedSandboxFilesystem(this);
    return this._filesystem;
  }

  /** The Prized sandbox id (`sbx_…`) in use, or the one `sandboxId` named before `start()`. */
  get sandboxId(): string | undefined {
    return this._sandbox?.id ?? this._preferredSandboxId;
  }

  private get apiKey(): string {
    if (!this._apiKey) {
      throw new Error(
        `${LOG_PREFIX} Missing Prized API key. Pass \`apiKey\` or set PRIZED_API_KEY (https://prized.dev/dashboard/workspace/api-keys).`,
      );
    }
    return this._apiKey;
  }

  // ---------------------------------------------------------------------------
  // Lifecycle: the base class runs find() -> connect(), else create().
  // ---------------------------------------------------------------------------

  /** The sandbox to adopt: this instance's own, the `sandboxId` one, else the newest one labelled with `id`. */
  protected override async find(options?: { abortSignal?: AbortSignal }): Promise<Sandbox | undefined> {
    const apiKey = this.apiKey;
    if (this._sandbox) {
      const current = await this._sandbox.refresh().catch(error => {
        if (isGone(error)) return null;
        throw error;
      });
      if (current && ADOPTABLE.has(current.state)) return current;
      this._sandbox = null;
    }
    if (this._preferredSandboxId) {
      try {
        const attached = await Sandbox.connect(this._preferredSandboxId, { apiKey });
        if (ADOPTABLE.has(attached.state)) return attached;
        this.logger.warn(`${LOG_PREFIX} Sandbox ${this._preferredSandboxId} is ${attached.state}; not reattaching`);
      } catch (error) {
        if (!isGone(error)) throw error;
        this.logger.warn(`${LOG_PREFIX} Sandbox ${this._preferredSandboxId} no longer exists`);
      }
      this._preferredSandboxId = undefined;
    }
    throwIfAborted(options?.abortSignal);
    const found = await Sandbox.list({ apiKey, labels: { [ID_LABEL]: this._labelValue } });
    return found.find(s => ADOPTABLE.has(s.state));
  }

  /** Adopt a found sandbox: resume it if it is paused, wait for it if it is starting. */
  protected override async connect(sandbox: Sandbox, options?: { abortSignal?: AbortSignal }): Promise<void> {
    const signal = options?.abortSignal;
    if (sandbox.state === 'paused' || sandbox.state === 'pausing') {
      await sandbox.resume({ signal });
    } else if (sandbox.state !== 'running') {
      await settle(sandbox, signal);
    }
    this._sandbox = sandbox;
    this.logger.debug(`${LOG_PREFIX} Connected`, { id: this.id, sandboxId: sandbox.id });
  }

  /** Create a sandbox labelled with this instance's `id`. */
  protected override async create(options?: { abortSignal?: AbortSignal }): Promise<void> {
    const o = this._options;
    const createOptions: CreateOptions = {
      apiKey: this.apiKey,
      labels: { ...o.labels, [ID_LABEL]: this._labelValue },
      autoPauseMin: o.autoPauseMin ?? DEFAULT_AUTO_PAUSE_MIN,
      ...(o.template !== undefined && { template: o.template }),
      ...(o.resources !== undefined && { resources: o.resources }),
      ...(o.maxRuntimeMin !== undefined && { maxRuntimeMin: o.maxRuntimeMin }),
      ...(o.autoDeleteMin !== undefined && { autoDeleteMin: o.autoDeleteMin }),
      ...(options?.abortSignal && { signal: options.abortSignal }),
    };
    this._sandbox = await Sandbox.create(createOptions);
    this.logger.debug(`${LOG_PREFIX} Created`, { id: this.id, sandboxId: this._sandbox.id });
  }

  /**
   * Kill the background processes this instance started and pause the sandbox. Memory, disk and
   * processes started any other way are kept (a paused sandbox bills storage only); `start()`, or
   * the next command, resumes it.
   */
  override async stop(): Promise<void> {
    await this._killTrackedProcesses();
    const sandbox = this._sandbox;
    if (!sandbox) return;
    try {
      await sandbox.refresh();
      if (sandbox.state === 'starting' || sandbox.state === 'resizing') await settle(sandbox);
      if (sandbox.state === 'running') await sandbox.pause();
    } catch (error) {
      if (!isGone(error)) throw error;
      this._sandbox = null;
    }
  }

  /**
   * Delete the sandbox and everything in it, with any other sandbox labelled with this `id`. One
   * that no longer exists counts as deleted. Works on a new instance too: `new PrizedSandbox({ id
   * }).destroy()` deletes the sandbox created under `id` without resuming it.
   */
  override async destroy(): Promise<void> {
    await this._killTrackedProcesses();
    const apiKey = this.apiKey;
    let sandbox = this._sandbox;
    this._sandbox = null;
    if (!sandbox && this._preferredSandboxId) {
      sandbox = await Sandbox.connect(this._preferredSandboxId, { apiKey }).catch(error => {
        if (isGone(error)) return null;
        throw error;
      });
    }
    if (sandbox) {
      try {
        await sandbox.delete();
      } catch (error) {
        if (!isGone(error)) throw error;
      }
    }
    await Sandbox.deleteByLabels({ [ID_LABEL]: this._labelValue }, { apiKey });
  }

  private async _killTrackedProcesses(): Promise<void> {
    await this.processes.killAll().catch(error => {
      this.logger.warn(`${LOG_PREFIX} Could not kill background processes`, { error });
    });
  }

  /**
   * Run `fn`; if the sandbox turns out to be gone (deleted, or past its run time), forget it, start
   * again (which creates a new one under the same `id`) and run `fn` once more. Only for calls that
   * did not run when they failed, such as starting a command. Concurrent callers share one restart.
   * @internal
   */
  async retryOnDead<T>(fn: () => Promise<T>): Promise<T> {
    try {
      return await fn();
    } catch (error) {
      if (this.status === 'destroying' || this.status === 'destroyed') throw error;
      if (!(await this._isDead(error))) throw error;
      await this._recover();
      return await fn();
    }
  }

  /**
   * Whether an error means the sandbox is gone: deleted, not found, or refused while it is being
   * deleted. A file transfer's `not_found` reads the same as a missing sandbox's, so a `not_found`
   * counts only once the sandbox itself is confirmed gone.
   */
  private async _isDead(error: unknown): Promise<boolean> {
    if (!(error instanceof PrizedError)) return false;
    if (error.code === 'sandbox_deleted') return true;
    if (error.code !== 'not_found' && error.code !== 'sandbox_not_running') return false;
    const sandbox = this._sandbox;
    if (!sandbox) return error.code === 'not_found';
    try {
      await sandbox.refresh();
    } catch (refreshError) {
      if (isGone(refreshError)) return true;
      // Unconfirmed: a not_found must not pass for a missing file, so say why it is unknown.
      if (error.code === 'not_found') throw refreshError;
      return false;
    }
    return DEAD.has(sandbox.state);
  }

  private _recover(): Promise<void> {
    this._recovery ??= (async () => {
      this.logger.warn(`${LOG_PREFIX} Sandbox is gone; starting a new one`, { id: this.id });
      this._sandbox = null;
      this.status = 'stopped';
      await this.ensureRunning();
    })().finally(() => {
      this._recovery = undefined;
    });
    return this._recovery;
  }

  // ---------------------------------------------------------------------------
  // Files, clone, info
  // ---------------------------------------------------------------------------

  /**
   * Write files through the sandbox's file API (paths inside /home/prized, at most 100 MiB each).
   * Missing directories are created; a `mode` is applied with one `chmod` afterwards.
   */
  async writeFiles(files: SandboxFileInput[], options?: WriteFilesOptions): Promise<void> {
    for (const file of files) if (file.mode !== undefined) validateSandboxFileMode(file.mode);
    throwIfAborted(options?.abortSignal);
    await this.ensureRunning();
    for (const file of files) {
      throwIfAborted(options?.abortSignal);
      await this.sandbox.files.write(file.path, typeof file.content === 'string' ? file.content : new Uint8Array(file.content));
    }
    const modes = files.filter(f => f.mode !== undefined);
    if (modes.length > 0) {
      const script = modes.map(f => `chmod ${f.mode!.toString(8)} -- ${shellQuote(f.path)}`).join(' && ');
      const result = await this.sandbox.exec(script, { timeoutMs: 60_000 });
      if (result.exitCode !== 0) throw new Error(`${LOG_PREFIX} chmod failed: ${result.stderr.trim()}`);
    }
  }

  /**
   * A new, unstarted `PrizedSandbox` with this one's settings and the given overrides. No request
   * is made: the copy finds or creates its own sandbox on `start()`.
   */
  clone(options: SandboxCloneOptions = {}): PrizedSandbox {
    const { id: _id, sandboxId: _sandboxId, ...base } = this._options;
    return new PrizedSandbox({
      ...base,
      env: options.env ?? this.getEnv(),
      ...(options.id !== undefined && { id: options.id }),
      ...(options.sandboxId !== undefined && { sandboxId: options.sandboxId }),
      ...(options.workingDirectory !== undefined && { workingDirectory: options.workingDirectory }),
      ...(options.idleTimeoutMinutes !== undefined && { autoPauseMin: options.idleTimeoutMinutes }),
    });
  }

  async getInfo(): Promise<SandboxInfo> {
    const sandbox = this._sandbox;
    return {
      id: this.id,
      name: this.name,
      provider: this.provider,
      status: this.status,
      createdAt: sandbox?.createdAt ?? new Date(),
      ...(sandbox?.expiresAt && { timeoutAt: sandbox.expiresAt }),
      ...(sandbox && {
        resources: {
          cpuCores: sandbox.resources.vcpu,
          memoryMB: sandbox.resources.memoryMb,
          diskMB: sandbox.resources.diskGb * 1024,
        },
      }),
      metadata: {
        ...(sandbox && { sandboxId: sandbox.id, state: sandbox.state, template: sandbox.template }),
      },
    };
  }

  getInstructions(opts?: { requestContext?: RequestContext }): string {
    const override = this._options.instructions;
    if (typeof override === 'string') return override;
    const defaultInstructions = this._defaultInstructions();
    if (typeof override === 'function') return override({ defaultInstructions, requestContext: opts?.requestContext });
    return defaultInstructions;
  }

  private _defaultInstructions(): string {
    const timeout = this.defaultCommandTimeout;
    return [
      'Prized sandbox: an Ubuntu 24.04 microVM with git, curl, Python 3 and Node.js.',
      'Commands run as the user `prized` (home /home/prized, no sudo).',
      `Commands run in ${this.workingDirectory ?? DEFAULT_WORKDIR} unless a cwd is given.`,
      'Install Python packages into a virtualenv (python3 -m venv ~/.venv).',
      'Commands get no stdin.',
      ...(timeout ? [`Foreground commands time out after ${Math.ceil(timeout / 1000)}s.`] : []),
    ].join(' ');
  }
}

/** The `mastra-id` label value for a logical id: the id itself, or a hash of one too long for a label. */
function labelValueFor(id: string): string {
  if (id.length <= MAX_LABEL_VALUE && !id.includes('\0')) return id;
  return `sha256-${createHash('sha256').update(id).digest('hex')}`;
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new SandboxAbortError('PrizedSandbox operation aborted');
}

/** Wait for a starting or resizing sandbox to run. */
async function settle(sandbox: Sandbox, signal?: AbortSignal): Promise<void> {
  const deadline = Date.now() + SETTLE_TIMEOUT_MS;
  while (sandbox.state !== 'running') {
    throwIfAborted(signal);
    if (!ADOPTABLE.has(sandbox.state)) {
      throw new Error(`${LOG_PREFIX} Sandbox ${sandbox.id} is ${sandbox.state}${sandbox.errorReason ? `: ${sandbox.errorReason}` : ''}`);
    }
    if (sandbox.state === 'paused' || sandbox.state === 'pausing') {
      await sandbox.resume({ signal });
      return;
    }
    if (Date.now() > deadline) throw new Error(`${LOG_PREFIX} Sandbox ${sandbox.id} did not start in time`);
    await new Promise(resolve => setTimeout(resolve, SETTLE_POLL_MS));
    await sandbox.refresh();
  }
}

export { shellQuote };
