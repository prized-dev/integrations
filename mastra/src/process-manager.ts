/**
 * Runs every command as a background run in the Prized sandbox
 * (`sandbox.exec(cmd, { background: true })`): it has its own session, and a kill reaches
 * everything it started.
 *
 * A handle reads the run's output only while someone follows it: `wait()`, or output callbacks
 * given at spawn. `processes.get()` and `list()` catch up once. Every read is a request to the
 * sandbox, and requests count as activity, so a process nobody follows does not keep the sandbox
 * from pausing itself when idle.
 */

import type { CommandHandle } from '@prized/sdk';
import { CommandTimeoutError, PrizedError } from '@prized/sdk';
import type { CommandResult, ProcessHandle as ProcessHandleType, ProcessInfo, SpawnProcessOptions } from '@mastra/core/workspace';
import { ProcessHandle, SandboxProcessManager } from '@mastra/core/workspace';

import type { PrizedSandbox } from './sandbox.js';

export const LOG_PREFIX = '[PrizedSandbox]';

/** Exit code reported for a process killed by a signal (128 + SIGKILL). */
export const KILLED_EXIT_CODE = 137;
/** Exit code reported for a process that ran past its timeout, as `timeout(1)` reports it. */
export const TIMEOUT_EXIT_CODE = 124;

/** API error codes that mean the sandbox, or the run in it, no longer exists. */
const GONE_CODES = new Set(['not_found', 'sandbox_deleted']);

export function isGone(error: unknown): boolean {
  return error instanceof PrizedError && GONE_CODES.has(error.code);
}

class PrizedProcessHandle extends ProcessHandle {
  readonly pid: string;

  private readonly _run: CommandHandle;
  private readonly _startTime = Date.now();
  private _result: CommandResult | undefined;
  private _following: Promise<CommandResult> | undefined;
  private _stop: AbortController | undefined;
  private _killed = false;
  private _killing = false;
  private _killCall: Promise<boolean> | undefined;
  /** Characters of each stream already passed to emitStdout / emitStderr. */
  private _emittedOut = 0;
  private _emittedErr = 0;
  private _onSettled: Array<() => void> = [];

  constructor(run: CommandHandle, options: SpawnProcessOptions) {
    super(options);
    this.pid = run.id;
    this._run = run;
    // Output callbacks at spawn mean someone wants the output as it comes.
    if (options.onStdout || options.onStderr) this.wait().catch(() => {});
  }

  get exitCode(): number | undefined {
    return this._result?.exitCode;
  }

  /** Follow the run until it exits. Settles once; a follow that failed is retried by the next call. */
  async wait(): Promise<CommandResult> {
    if (this._result) return this._result;
    this._following ??= this._follow().finally(() => {
      this._following = undefined;
    });
    return this._following;
  }

  private async _follow(): Promise<CommandResult> {
    const stop = new AbortController();
    this._stop = stop;
    // The SDK replays a run's output from the start on every wait; pass on only what is new.
    let seenOut = 0;
    let seenErr = 0;
    try {
      const result = await this._run.wait({
        onStdout: chunk => {
          seenOut = this._emitNew('out', chunk, seenOut);
        },
        onStderr: chunk => {
          seenErr = this._emitNew('err', chunk, seenErr);
        },
        signal: stop.signal,
      });
      return this._settle(result.exitCode);
    } catch (error) {
      if (this._result) return this._result; // settled meanwhile (detach)
      if (error instanceof CommandTimeoutError) return this._settle(TIMEOUT_EXIT_CODE, { timedOut: true });
      if (isGone(error)) return this._settle(1, { reason: 'the sandbox or the command is gone' });
      throw error;
    } finally {
      if (this._stop === stop) this._stop = undefined;
    }
  }

  private _emitNew(stream: 'out' | 'err', chunk: string, seen: number): number {
    const end = seen + chunk.length;
    const emitted = stream === 'out' ? this._emittedOut : this._emittedErr;
    if (end > emitted) {
      const fresh = chunk.slice(Math.max(0, emitted - seen));
      if (stream === 'out') {
        this._emittedOut = end;
        this.emitStdout(fresh);
      } else {
        this._emittedErr = end;
        this.emitStderr(fresh);
      }
    }
    return end;
  }

  private _settle(rawExitCode: number, extra: { timedOut?: boolean; reason?: string } = {}): CommandResult {
    if (this._result) return this._result;
    // -1 from the SDK: the command ended without an exit code, as after SIGKILL.
    const exitCode = rawExitCode < 0 ? KILLED_EXIT_CODE : rawExitCode;
    if (this._killing && rawExitCode < 0) this._killed = true;
    this._result = {
      success: exitCode === 0,
      exitCode,
      stdout: this.stdout,
      stderr: extra.reason && !this.stderr ? `${LOG_PREFIX} ${extra.reason}` : this.stderr,
      executionTimeMs: Date.now() - this._startTime,
      ...(this._killed && { killed: true }),
      ...(extra.timedOut && { timedOut: true }),
    };
    void this._run.cleanup().catch(() => {});
    for (const callback of this._onSettled.splice(0)) callback();
    return this._result;
  }

  /** Run `callback` once the handle has a result. @internal */
  whenSettled(callback: () => void): void {
    if (this._result) callback();
    else this._onSettled.push(callback);
  }

  /** Read the output so far once, and settle if the run has exited. For get() and list(). @internal */
  async catchUp(): Promise<void> {
    if (this._result || this._following) return;
    try {
      const status = await this._run.status();
      if (!status.running) {
        await this.wait();
        return;
      }
      const { stdout, stderr } = await this._run.logs();
      // A read can end inside a multi-byte character, decoded as U+FFFD: leave it for the next read.
      this._emitNew('out', withoutPartialChar(stdout), 0);
      this._emitNew('err', withoutPartialChar(stderr), 0);
    } catch (error) {
      if (!isGone(error)) throw error;
      this._settle(1, { reason: 'the sandbox or the command is gone' });
    }
  }

  /** SIGKILL the command and everything it started. False if it had already exited. */
  async kill(): Promise<boolean> {
    if (this._result) return false;
    this._killCall ??= this._kill().finally(() => {
      this._killCall = undefined;
    });
    return this._killCall;
  }

  private async _kill(): Promise<boolean> {
    this._killing = true;
    try {
      const signalled = await this._run.kill('SIGKILL');
      if (signalled) this._killed = true;
      return signalled;
    } catch (error) {
      if (!isGone(error)) throw error;
      this._settle(1, { reason: 'the sandbox or the command is gone' });
      return false;
    } finally {
      this._killing = false;
    }
  }

  /** Stop following without waiting for the exit (after a kill, or when the sandbox pauses). @internal */
  detach(): void {
    this._settle(this._killed ? KILLED_EXIT_CODE : 1, { reason: 'stopped following the command' });
    this._stop?.abort();
  }

  async sendStdin(_data: string): Promise<void> {
    throw new Error(`${LOG_PREFIX} Prized commands have no stdin: pass input in a file or an argument`);
  }
}

function withoutPartialChar(text: string): string {
  return text.endsWith('\uFFFD') ? text.slice(0, -1) : text;
}

export class PrizedProcessManager extends SandboxProcessManager<PrizedSandbox> {
  constructor(options?: ConstructorParameters<typeof SandboxProcessManager>[0]) {
    super(options);
    // The base class follows every process spawned with an abort signal (it waits on it to
    // remove its listener), and following keeps the sandbox awake for the process's whole life.
    // Handle the signal here instead: an abort still kills the process, and nothing follows it.
    const spawnWithBase = this.spawn;
    this.spawn = async (command: string, options: SpawnProcessOptions = {}): Promise<ProcessHandleType> => {
      const { abortSignal, ...rest } = options;
      const handle = await spawnWithBase(command, rest);
      if (abortSignal) {
        const onAbort = (): void => {
          handle.killForAbort().catch(() => {});
        };
        if (abortSignal.aborted) onAbort();
        else {
          abortSignal.addEventListener('abort', onAbort, { once: true });
          if (handle instanceof PrizedProcessHandle) {
            handle.whenSettled(() => abortSignal.removeEventListener('abort', onAbort));
          }
        }
      }
      return handle;
    };
  }

  async spawn(command: string, options: SpawnProcessOptions = {}): Promise<ProcessHandleType> {
    // The base class has started the sandbox and merged the sandbox env into options.env.
    const env = Object.fromEntries(
      Object.entries(options.env ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined),
    );
    const cwd = options.cwd ?? this.sandbox.workingDirectory;
    const timeout = options.timeout;

    // A start refused because the sandbox is gone never ran, so it is safe to start a new sandbox
    // and try once more.
    const run = await this.sandbox.retryOnDead(() =>
      this.sandbox.sandbox.exec(command, {
        background: true,
        ...(cwd !== undefined && { cwd }),
        ...(Object.keys(env).length > 0 && { env }),
        ...(timeout !== undefined && timeout > 0 && { timeoutMs: timeout }),
      }),
    );
    const handle = new PrizedProcessHandle(run, options);
    this._tracked.set(handle.pid, handle);
    return handle;
  }

  /** A tracked process, with its output read up to now. */
  override async get(pid: string): Promise<ProcessHandleType | undefined> {
    const handle = this._tracked.get(pid);
    if (handle instanceof PrizedProcessHandle) await handle.catchUp();
    return handle;
  }

  /** The processes this instance started that it still tracks, each checked once. */
  async list(): Promise<ProcessInfo[]> {
    const handles = [...this._tracked.values()];
    await Promise.allSettled(handles.map(h => (h instanceof PrizedProcessHandle ? h.catchUp() : undefined)));
    return handles.map(handle => ({
      pid: handle.pid,
      ...(handle.command !== undefined && { command: handle.command }),
      running: handle.exitCode === undefined,
      ...(handle.exitCode !== undefined && { exitCode: handle.exitCode }),
    }));
  }

  /** Kill every running process this instance started and stop following them. @internal */
  async killAll(): Promise<void> {
    const running = [...this._tracked.values()].filter(handle => handle.exitCode === undefined);
    await Promise.all(
      running.map(async handle => {
        const killed = await handle.kill().catch(() => false);
        if (handle instanceof PrizedProcessHandle) {
          // Not killed: it may have exited by itself; read its real exit code before letting go.
          if (!killed) await handle.catchUp().catch(() => {});
          handle.detach();
        }
        this.release(handle.pid);
      }),
    );
  }
}

