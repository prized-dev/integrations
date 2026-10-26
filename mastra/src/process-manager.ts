/**
 * Runs every command as a background run in the Prized sandbox
 * (`sandbox.exec(cmd, { background: true })`): it has its own session, a kill reaches everything it
 * started, and its output is polled into the handle as it arrives.
 */

import type { CommandHandle } from '@prized/sdk';
import { CommandTimeoutError } from '@prized/sdk';
import type { CommandResult, ProcessInfo, SpawnProcessOptions } from '@mastra/core/workspace';
import { ProcessHandle, SandboxProcessManager } from '@mastra/core/workspace';

import type { PrizedSandbox } from './sandbox.js';

export const LOG_PREFIX = '[PrizedSandbox]';

/** Exit code reported for a process killed with `kill()` (128 + SIGKILL). */
export const KILLED_EXIT_CODE = 137;
/** Exit code reported for a process that ran past its timeout, as `timeout(1)` reports it. */
export const TIMEOUT_EXIT_CODE = 124;

class PrizedProcessHandle extends ProcessHandle {
  readonly pid: string;

  private readonly _run: CommandHandle;
  private readonly _startTime = Date.now();
  private readonly _polling = new AbortController();
  private readonly _result: Promise<CommandResult>;
  private _exitCode: number | undefined;
  private _killed = false;

  constructor(run: CommandHandle, options: SpawnProcessOptions) {
    super(options);
    this.pid = run.id;
    this._run = run;
    this._result = this._follow();
  }

  get exitCode(): number | undefined {
    return this._exitCode;
  }

  /** Poll the run's output into this handle until it exits, then settle its result once. */
  private async _follow(): Promise<CommandResult> {
    let timedOut = false;
    let failure: string | undefined;
    try {
      const result = await this._run.wait({
        onStdout: chunk => this.emitStdout(chunk),
        onStderr: chunk => this.emitStderr(chunk),
        signal: this._polling.signal,
      });
      this._exitCode = this._killed ? KILLED_EXIT_CODE : result.exitCode < 0 ? 1 : result.exitCode;
    } catch (error) {
      if (error instanceof CommandTimeoutError) {
        timedOut = true;
        this._exitCode = TIMEOUT_EXIT_CODE;
      } else {
        this._exitCode = this._killed ? KILLED_EXIT_CODE : 1;
        if (!this._killed) failure = error instanceof Error ? error.message : String(error);
      }
    }
    void this._run.cleanup().catch(() => {});
    const exitCode = this._exitCode;
    return {
      success: exitCode === 0,
      exitCode,
      stdout: this.stdout,
      stderr: failure && !this.stderr ? `${LOG_PREFIX} ${failure}` : this.stderr,
      executionTimeMs: Date.now() - this._startTime,
      ...(this._killed && { killed: true }),
      ...(timedOut && { timedOut: true }),
    };
  }

  async wait(): Promise<CommandResult> {
    return this._result;
  }

  /** SIGKILL the command and everything it started. False if it had already exited. */
  async kill(): Promise<boolean> {
    if (this._exitCode !== undefined) return false;
    this._killed = true;
    try {
      const signalled = await this._run.kill('SIGKILL');
      // Not signalled: it had already exited, and polling reports its own exit code.
      if (!signalled) this._killed = false;
      return signalled;
    } catch {
      // The sandbox is gone or unreachable: nothing more will come from this run.
      this._polling.abort();
      return false;
    }
  }

  /** Stop polling without killing the command (it keeps running in the sandbox). @internal */
  detach(): void {
    this._polling.abort();
  }

  async sendStdin(_data: string): Promise<void> {
    throw new Error(`${LOG_PREFIX} Prized commands have no stdin: pass input in a file or an argument`);
  }
}

export class PrizedProcessManager extends SandboxProcessManager<PrizedSandbox> {
  async spawn(command: string, options: SpawnProcessOptions = {}): Promise<ProcessHandle> {
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

  /** The processes this instance started that it still tracks. */
  async list(): Promise<ProcessInfo[]> {
    return [...this._tracked.values()].map(handle => ({
      pid: handle.pid,
      ...(handle.command !== undefined && { command: handle.command }),
      running: handle.exitCode === undefined,
      ...(handle.exitCode !== undefined && { exitCode: handle.exitCode }),
    }));
  }

  /** Kill every running process this instance started. Does not start the sandbox. @internal */
  async killAll(): Promise<void> {
    const running = [...this._tracked.values()].filter(handle => handle.exitCode === undefined);
    await Promise.all(
      running.map(async handle => {
        await handle.kill().catch(() => false);
        if (handle instanceof PrizedProcessHandle) handle.detach();
        this.release(handle.pid);
      }),
    );
  }
}
