/**
 * An in-memory stand-in for `@prized/sdk`'s Sandbox, enough for the provider's offline tests. The
 * errors are the real SDK classes, so the provider's error handling is tested against them.
 */
import { CommandTimeoutError, PrizedError } from '@prized/sdk';
import type { CreateOptions, ExecResult, ListOptions, SandboxState } from '@prized/sdk';

/** What a command does in the fake: its output, exit code, and whether it runs until killed. */
export interface Behaviour {
  stdout?: string[];
  stderr?: string[];
  exitCode?: number;
  /** Run until killed (or until its timeout, when it has one). */
  hang?: boolean;
  /** Fail to start with this error. */
  startError?: Error;
}

export interface ExecCall {
  sandboxId: string;
  command: string;
  options: Record<string, unknown>;
}

const notFound = (id: string): PrizedError =>
  new PrizedError('not_found', 'Not found (or not visible to this API key).', { status: 404, sandboxId: id });

export class FakeRun {
  readonly id: string;
  readonly pid = 4242;
  killedWith: string | undefined;
  cleaned = false;
  private finished = false;
  private wake: (() => void) | undefined;

  constructor(
    readonly sandboxId: string,
    private readonly behaviour: Behaviour,
    private readonly timeoutMs: number | null,
  ) {
    this.id = `run_${Math.random().toString(16).slice(2, 10)}`;
  }

  async wait(options: {
    onStdout?: (c: string) => void;
    onStderr?: (c: string) => void;
    signal?: AbortSignal;
  } = {}): Promise<ExecResult> {
    for (const c of this.behaviour.stdout ?? []) options.onStdout?.(c);
    for (const c of this.behaviour.stderr ?? []) options.onStderr?.(c);
    const out = (this.behaviour.stdout ?? []).join('');
    const err = (this.behaviour.stderr ?? []).join('');
    if (this.behaviour.hang && !this.killedWith) {
      const outcome = await new Promise<'killed' | 'timeout' | 'aborted'>(resolve => {
        this.wake = () => resolve('killed');
        if (this.timeoutMs) setTimeout(() => resolve('timeout'), this.timeoutMs);
        options.signal?.addEventListener('abort', () => resolve('aborted'), { once: true });
      });
      if (outcome === 'aborted') throw new PrizedError('aborted', 'The wait was aborted.');
      if (outcome === 'timeout') {
        this.finished = true;
        throw new CommandTimeoutError(
          { stdout: out, stderr: err, exitCode: -1, durationMs: this.timeoutMs ?? 0, timedOut: true, truncated: false },
          this.timeoutMs,
          this.sandboxId,
        );
      }
    }
    this.finished = true;
    return {
      stdout: out,
      stderr: err,
      exitCode: this.killedWith ? -1 : (this.behaviour.exitCode ?? 0),
      durationMs: 1,
      timedOut: false,
      truncated: false,
    };
  }

  async kill(signal = 'SIGTERM'): Promise<boolean> {
    if (this.finished || this.killedWith) return false;
    this.killedWith = signal;
    this.wake?.();
    return true;
  }

  async cleanup(): Promise<void> {
    this.cleaned = true;
  }
}

export class FakeSandbox {
  static store: Map<string, FakeSandbox> = new Map();
  static creates: CreateOptions[] = [];
  static lists: ListOptions[] = [];
  static connects: string[] = [];
  static execs: ExecCall[] = [];
  static runs: FakeRun[] = [];
  static writes: Array<{ sandboxId: string; path: string; data: unknown }> = [];
  /** Decides what each command does. */
  static behave: (command: string) => Behaviour = () => ({});
  /** Errors the next create throws, once each. */
  static createErrors: Error[] = [];

  static reset(): void {
    FakeSandbox.store.clear();
    FakeSandbox.creates = [];
    FakeSandbox.lists = [];
    FakeSandbox.connects = [];
    FakeSandbox.execs = [];
    FakeSandbox.runs = [];
    FakeSandbox.writes = [];
    FakeSandbox.behave = () => ({});
    FakeSandbox.createErrors = [];
    FakeSandbox.seq = 0;
  }

  private static seq = 0;

  /** Put a sandbox in the store, as if it had been created earlier (by another process). */
  static seed(state: SandboxState, labels: Record<string, string> = {}): FakeSandbox {
    const sandbox = new FakeSandbox(labels);
    sandbox.state = state;
    return sandbox;
  }

  readonly id: string;
  state: SandboxState = 'running';
  readonly labels: Record<string, string>;
  readonly template: string | null = 'prized-base-v1';
  readonly resources: { vcpu: number; memoryMb: number; memoryGb: number; diskGb: number } = {
    vcpu: 2,
    memoryMb: 8192,
    memoryGb: 8,
    diskGb: 10,
  };
  readonly createdAt: Date | null = new Date('2026-10-26T00:00:00Z');
  readonly expiresAt: Date | null = new Date('2026-10-27T00:00:00Z');
  readonly errorReason: string | null = null;
  pauses = 0;
  resumes = 0;
  deletes = 0;

  readonly files: { write(path: string, data: unknown): Promise<{ path: string; size: number }> } = {
    write: async (path: string, data: unknown) => {
      this.alive();
      FakeSandbox.writes.push({ sandboxId: this.id, path, data });
      return { path, size: typeof data === 'string' ? data.length : (data as Uint8Array).byteLength };
    },
  };

  private constructor(labels: Record<string, string>) {
    this.id = `sbx_${(++FakeSandbox.seq).toString(16).padStart(16, '0')}`;
    this.labels = labels;
    FakeSandbox.store.set(this.id, this);
  }

  private alive(): void {
    if (!FakeSandbox.store.has(this.id)) throw notFound(this.id);
  }

  static async create(options: CreateOptions = {}): Promise<FakeSandbox> {
    FakeSandbox.creates.push(options);
    const error = FakeSandbox.createErrors.shift();
    if (error) throw error;
    if (options.template === 'no-such-template') {
      throw new PrizedError('template_not_found', 'No such template.', { status: 404 });
    }
    return new FakeSandbox(options.labels ?? {});
  }

  static async connect(id: string): Promise<FakeSandbox> {
    FakeSandbox.connects.push(id);
    const found = FakeSandbox.store.get(id);
    if (!found) throw notFound(id);
    return found;
  }

  static async list(options: ListOptions = {}): Promise<FakeSandbox[]> {
    FakeSandbox.lists.push(options);
    const want = Object.entries(options.labels ?? {});
    return [...FakeSandbox.store.values()]
      .filter(s => want.every(([k, v]) => s.labels[k] === v))
      .reverse();
  }

  async refresh(): Promise<this> {
    this.alive();
    return this;
  }

  async pause(): Promise<void> {
    this.alive();
    this.pauses++;
    this.state = 'paused';
  }

  async resume(): Promise<void> {
    this.alive();
    this.resumes++;
    this.state = 'running';
  }

  async delete(): Promise<void> {
    this.deletes++;
    if (!FakeSandbox.store.delete(this.id)) throw notFound(this.id);
    this.state = 'deleted';
  }

  /** A background command returns its run; a foreground one its result. */
  async exec(command: string, options: Record<string, unknown> = {}): Promise<FakeRun | ExecResult> {
    this.alive();
    FakeSandbox.execs.push({ sandboxId: this.id, command, options });
    if (this.state === 'paused') this.state = 'running'; // a command wakes a paused sandbox
    const behaviour = FakeSandbox.behave(command);
    if (behaviour.startError) throw behaviour.startError;
    const run = new FakeRun(this.id, behaviour, (options.timeoutMs as number | undefined) ?? null);
    FakeSandbox.runs.push(run);
    return options.background ? run : run.wait();
  }
}
