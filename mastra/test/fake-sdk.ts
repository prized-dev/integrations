/**
 * An in-memory stand-in for `@prized/sdk`'s Sandbox, enough for the provider's offline tests. The
 * errors are the real SDK classes, so the provider's error handling is tested against them.
 */
import { CommandTimeoutError, PrizedError } from '@prized/sdk';
import type { CreateOptions, ExecResult, ListOptions, SandboxState } from '@prized/sdk';

import type { FakeDisk } from './fake-disk.js';

/** What a command does in the fake: its output, exit code, and whether it runs until killed. */
export interface Behaviour {
  stdout?: string[];
  stderr?: string[];
  exitCode?: number;
  /** Run until killed (or until its timeout, when it has one). */
  hang?: boolean;
  /** Fail to start with this error. */
  startError?: Error;
  /** kill() throws this. */
  killError?: Error;
  /** status() throws this. */
  statusError?: Error;
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
  /** How many times the run was followed (wait), checked (status) or read (logs): each is a request. */
  waits = 0;
  statusCalls = 0;
  logsCalls = 0;
  private state: 'running' | 'done' | 'killed' | 'timeout';
  private readonly outcome: Promise<'done' | 'killed' | 'timeout'>;
  private resolveOutcome!: (o: 'done' | 'killed' | 'timeout') => void;

  constructor(
    readonly sandboxId: string,
    private readonly behaviour: Behaviour,
    private readonly timeoutMs: number | null,
  ) {
    this.id = `run_${Math.random().toString(16).slice(2, 10)}`;
    this.outcome = new Promise(resolve => {
      this.resolveOutcome = resolve;
    });
    this.state = 'running';
    if (!behaviour.hang) this.finish('done');
    else if (timeoutMs) setTimeout(() => this.finish('timeout'), timeoutMs);
  }

  private finish(o: 'done' | 'killed' | 'timeout'): void {
    if (this.state !== 'running') return;
    this.state = o;
    this.resolveOutcome(o);
  }

  /** Let a hanging command exit by itself. */
  exit(): void {
    this.finish('done');
  }

  /** Like the SDK, every call on a run in a deleted sandbox is not_found. */
  private alive(): void {
    if (!FakeSandbox.store.has(this.sandboxId)) throw notFound(this.sandboxId);
  }

  private get out(): string {
    return (this.behaviour.stdout ?? []).join('');
  }

  private get err(): string {
    return (this.behaviour.stderr ?? []).join('');
  }

  private exitCode(): number {
    return this.state === 'killed' ? -1 : (this.behaviour.exitCode ?? 0);
  }

  /** Replays the output from the start, as the SDK does, then waits for the exit. */
  async wait(options: {
    onStdout?: (c: string) => void;
    onStderr?: (c: string) => void;
    signal?: AbortSignal;
  } = {}): Promise<ExecResult> {
    this.waits++;
    this.alive();
    for (const c of this.behaviour.stdout ?? []) options.onStdout?.(c);
    for (const c of this.behaviour.stderr ?? []) options.onStderr?.(c);
    const aborted = new Promise<'aborted'>(resolve => {
      if (options.signal?.aborted) resolve('aborted');
      options.signal?.addEventListener('abort', () => resolve('aborted'), { once: true });
    });
    const o = await Promise.race([this.outcome, aborted]);
    if (o === 'aborted') throw new PrizedError('aborted', 'The wait was aborted.');
    if (o === 'timeout') {
      throw new CommandTimeoutError(
        { stdout: this.out, stderr: this.err, exitCode: -1, durationMs: this.timeoutMs ?? 0, timedOut: true, truncated: false },
        this.timeoutMs,
        this.sandboxId,
      );
    }
    return { stdout: this.out, stderr: this.err, exitCode: this.exitCode(), durationMs: 1, timedOut: false, truncated: false };
  }

  async status(): Promise<{ running: boolean; exitCode: number | null }> {
    this.statusCalls++;
    if (this.behaviour.statusError) throw this.behaviour.statusError;
    this.alive();
    return this.state === 'running' ? { running: true, exitCode: null } : { running: false, exitCode: this.exitCode() };
  }

  async logs(): Promise<{ stdout: string; stderr: string }> {
    this.logsCalls++;
    this.alive();
    return { stdout: this.out, stderr: this.err };
  }

  async kill(signal = 'SIGTERM'): Promise<boolean> {
    if (this.behaviour.killError) throw this.behaviour.killError;
    this.alive();
    if (this.state !== 'running') return false;
    this.killedWith = signal;
    this.finish('killed');
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
  /** Set to make list / deleteByLabels throw. */
  static listError: Error | undefined;
  static deleteByLabelsCalls: Array<Record<string, string>> = [];
  /** When set, file transfers and foreground commands act on this disk (see fake-disk.ts). */
  static disk: FakeDisk | undefined;
  /** How many times any sandbox was refreshed. */
  static refreshes = 0;
  /** Errors the next refreshes throw, once each. */
  static refreshErrors: Error[] = [];

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
    FakeSandbox.listError = undefined;
    FakeSandbox.deleteByLabelsCalls = [];
    FakeSandbox.disk = undefined;
    FakeSandbox.refreshes = 0;
    FakeSandbox.refreshErrors = [];
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

  readonly files: {
    write(path: string, data: unknown): Promise<{ path: string; size: number }>;
    readBytes(path: string): Promise<Uint8Array>;
  } = {
    write: async (path: string, data: unknown) => {
      this.alive();
      FakeSandbox.writes.push({ sandboxId: this.id, path, data });
      if (FakeSandbox.disk) return FakeSandbox.disk.write(path, data as string | Uint8Array);
      return { path, size: typeof data === 'string' ? data.length : (data as Uint8Array).byteLength };
    },
    readBytes: async (path: string) => {
      this.alive();
      if (!FakeSandbox.disk) throw new Error('no fake disk');
      return FakeSandbox.disk.readBytes(path);
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

  static async deleteByLabels(labels: Record<string, string>): Promise<string[]> {
    FakeSandbox.deleteByLabelsCalls.push(labels);
    if (FakeSandbox.listError) throw FakeSandbox.listError;
    const doomed = [...FakeSandbox.store.values()].filter(s => Object.entries(labels).every(([k, v]) => s.labels[k] === v));
    for (const s of doomed) {
      s.deletes++;
      s.state = 'deleted';
      FakeSandbox.store.delete(s.id);
    }
    return doomed.map(s => s.id);
  }

  static async list(options: ListOptions = {}): Promise<FakeSandbox[]> {
    FakeSandbox.lists.push(options);
    if (FakeSandbox.listError) throw FakeSandbox.listError;
    const want = Object.entries(options.labels ?? {});
    return [...FakeSandbox.store.values()]
      .filter(s => want.every(([k, v]) => s.labels[k] === v))
      .reverse();
  }

  /** States the next refreshes move through (a starting sandbox becoming running, say). */
  nextStates: SandboxState[] = [];

  async refresh(): Promise<this> {
    FakeSandbox.refreshes++;
    const error = FakeSandbox.refreshErrors.shift();
    if (error) throw error;
    this.alive();
    const next = this.nextStates.shift();
    if (next) this.state = next;
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
    if (this.state === 'deleting') {
      throw new PrizedError('sandbox_not_running', 'The sandbox is being deleted.', { status: 409, sandboxId: this.id });
    }
    FakeSandbox.execs.push({ sandboxId: this.id, command, options });
    if (this.state === 'paused') this.state = 'running'; // a command wakes a paused sandbox
    if (FakeSandbox.disk && !options.background) return FakeSandbox.disk.shell(command, options.cwd as string | undefined);
    const behaviour = FakeSandbox.behave(command);
    if (behaviour.startError) throw behaviour.startError;
    const run = new FakeRun(this.id, behaviour, (options.timeoutMs as number | undefined) ?? null);
    FakeSandbox.runs.push(run);
    return options.background ? run : run.wait();
  }
}
