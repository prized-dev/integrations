/**
 * An in-memory stand-in for the Prized API, as `@prized/sdk`'s `Sandbox` presents it: enough for
 * the provider's offline tests. Each sandbox's disk is a temporary directory standing for its `/`,
 * and commands run with the real bash, with `HOME`, the working directory and the provider's
 * `EVE_*` path variables mapped into that directory. Errors are the real SDK classes.
 *
 * What it models: create, connect, list and delete by label; pause; a paused sandbox woken by a
 * command or file request (its running commands frozen meanwhile); fork (a copy of the disk, the
 * parent's environment, and new labels); and the files API's rules (only inside /home/prized, links
 * followed only while they stay there, regular files only).
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import {
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';

import { PrizedError } from '@prized/sdk';
import type {
  ClientOptions,
  CreateOptions,
  ExecOptions,
  ExecResult,
  ForkOptions,
  ListOptions,
  SandboxInfo,
  SandboxState,
  WaitableOptions,
  WaitOptions,
} from '@prized/sdk';

import type { CommandHandleLike, FilesLike, SandboxApi, SandboxLike } from '../src/types.ts';

const HOME = '/home/prized';

const notFound = (id: string) => new PrizedError('not_found', 'Not found.', { status: 404, sandboxId: id });
const deleted = (id: string) => new PrizedError('sandbox_deleted', 'The sandbox was deleted.', { status: 410, sandboxId: id });

/** One background command: a real bash process in its own process group. */
class FakeRun implements CommandHandleLike {
  readonly pid: number;
  stdout = '';
  stderr = '';
  exitCode: number | null = null;
  killedWith: string | undefined;
  cleaned = false;
  private readonly listeners = new Set<{ out?: (s: string) => void; err?: (s: string) => void }>();
  private readonly done: Promise<number>;

  private readonly owner: FakeSandbox;
  readonly child: ChildProcess;

  constructor(owner: FakeSandbox, child: ChildProcess) {
    this.owner = owner;
    this.child = child;
    this.pid = child.pid ?? -1;
    // Output shows sandbox paths: the directory standing for `/` is cut out of it.
    const view = (s: string) => s.replaceAll(owner.root, '');
    child.stdout?.setEncoding('utf8').on('data', (raw: string) => {
      const s = view(raw);
      this.stdout += s;
      for (const l of this.listeners) l.out?.(s);
    });
    child.stderr?.setEncoding('utf8').on('data', (raw: string) => {
      const s = view(raw);
      this.stderr += s;
      for (const l of this.listeners) l.err?.(s);
    });
    this.done = new Promise(resolve =>
      child.on('close', (code, signal) => {
        this.exitCode = code ?? (signal ? -1 : 1);
        resolve(this.exitCode);
      }),
    );
  }

  get running(): boolean {
    return this.exitCode === null;
  }

  async wait(options: WaitOptions = {}): Promise<ExecResult> {
    this.owner.wakeFor('wait');
    const started = Date.now();
    const listener = { out: options.onStdout, err: options.onStderr };
    if (this.stdout) options.onStdout?.(this.stdout);
    if (this.stderr) options.onStderr?.(this.stderr);
    this.listeners.add(listener);
    try {
      const aborted = new Promise<never>((_, reject) => {
        const fail = () => reject(new PrizedError('aborted', 'The wait was aborted.'));
        if (options.signal?.aborted) fail();
        options.signal?.addEventListener('abort', fail, { once: true });
      });
      const exitCode = await Promise.race([this.done, aborted]);
      return { stdout: this.stdout, stderr: this.stderr, exitCode, durationMs: Date.now() - started, timedOut: false, truncated: false };
    } finally {
      this.listeners.delete(listener);
    }
  }

  async kill(signal = 'SIGTERM'): Promise<boolean> {
    this.owner.requireLive();
    if (!this.running) return false;
    this.killedWith = signal;
    try {
      process.kill(-this.pid, 'SIGCONT');
      process.kill(-this.pid, signal as NodeJS.Signals);
    } catch {
      return false;
    }
    return true;
  }

  async cleanup(): Promise<void> {
    this.cleaned = true;
  }

  signalGroup(signal: NodeJS.Signals): void {
    if (!this.running) return;
    try {
      process.kill(-this.pid, signal);
    } catch {
      // Already gone.
    }
  }
}

export class FakeSandbox implements SandboxLike {
  readonly id = `sbx_${randomBytes(8).toString('hex')}`;
  state: SandboxState = 'running';
  readonly root: string;
  readonly runs: FakeRun[] = [];
  /** How many times a request woke it from paused. */
  wakes = 0;
  lastActiveAt: Date;
  readonly files: FilesLike;

  private readonly api: FakePrized;
  readonly labels: Record<string, string>;
  readonly envVars: Record<string, string>;
  readonly autoPauseMin: number | null;
  readonly template: string;
  readonly createdAt: Date;
  forkedFrom: string | undefined;
  maxRuntimeMin: number | undefined;

  constructor(
    api: FakePrized,
    labels: Record<string, string>,
    envVars: Record<string, string>,
    autoPauseMin: number | null,
    template: string,
    createdAt: Date,
    copyOf?: FakeSandbox,
  ) {
    this.api = api;
    this.labels = labels;
    this.envVars = envVars;
    this.autoPauseMin = autoPauseMin;
    this.template = template;
    this.createdAt = createdAt;
    this.root = join(api.dir, this.id);
    if (copyOf) cpSync(copyOf.root, this.root, { recursive: true, verbatimSymlinks: true });
    else mkdirSync(join(this.root, HOME, 'work'), { recursive: true });
    this.lastActiveAt = createdAt;
    this.files = {
      readBytes: async path => this.readBytes(path),
      write: async (path, data) => this.write(path, data),
    };
  }

  get info(): SandboxInfo {
    return {
      id: this.id,
      state: this.state,
      template: this.template,
      resources: { vcpu: 4, memoryMb: 8192, memoryGb: 8, diskGb: 10 },
      labels: this.labels,
      autoPauseMin: this.autoPauseMin,
      autoDeleteMin: null,
      expiresAt: null,
      createdAt: this.createdAt,
      lastActiveAt: this.lastActiveAt,
      errorReason: null,
      toolboxBase: 'https://term.example.invalid',
      raw: {},
    };
  }

  /** The host path standing for `path` in this sandbox. */
  host(path: string): string {
    return join(this.root, path);
  }

  requireLive(): void {
    if (this.state === 'deleted') throw notFound(this.id);
    if (this.state === 'deleting') throw deleted(this.id);
  }

  /** A command or file request: wakes a paused sandbox, as the edge does. */
  wakeFor(what: string): void {
    this.requireLive();
    if (this.state === 'paused' || this.state === 'pausing') {
      this.wakes++;
      this.api.log(`wake ${this.id} (${what})`);
      this.state = 'running';
      for (const run of this.runs) run.signalGroup('SIGCONT');
    }
    this.lastActiveAt = new Date();
  }

  private checkPath(path: string): void {
    if (!(path === HOME || path.startsWith(`${HOME}/`)) || path.split('/').includes('..')) {
      throw new PrizedError('path_outside_root', `${path} is outside ${HOME}.`, { status: 400, sandboxId: this.id });
    }
  }

  /** Links are followed only while they stay inside the home. */
  private checkLinks(path: string): void {
    const host = this.host(path);
    let real: string;
    try {
      real = realpathSync(host);
    } catch {
      return;
    }
    const home = realpathSync(this.host(HOME));
    if (real !== home && !real.startsWith(`${home}/`)) {
      throw new PrizedError('symlink_not_allowed', `${path} leads outside ${HOME}.`, { status: 400, sandboxId: this.id });
    }
  }

  async readBytes(path: string): Promise<Uint8Array> {
    this.api.log(`read ${path}`);
    this.wakeFor('read');
    this.checkPath(path);
    this.checkLinks(path);
    const host = this.host(path);
    if (!existsSync(host)) throw new PrizedError('not_found', `${path} does not exist.`, { status: 404, sandboxId: this.id });
    if (!statSync(host).isFile()) throw new PrizedError('not_regular_file', `${path} is not a regular file.`, { status: 400, sandboxId: this.id });
    return new Uint8Array(readFileSync(host));
  }

  async write(path: string, data: string | Uint8Array): Promise<{ path: string; size: number }> {
    this.api.log(`write ${path}`);
    this.wakeFor('write');
    this.checkPath(path);
    this.checkLinks(path);
    const host = this.host(path);
    if (existsSync(host) && statSync(host).isDirectory()) {
      throw new PrizedError('unsupported_file_type', `${path} is a directory.`, { sandboxId: this.id });
    }
    mkdirSync(dirname(host), { recursive: true });
    const isNew = !existsSync(host);
    writeFileSync(host, data, isNew ? { mode: 0o600 } : {});
    return { path, size: typeof data === 'string' ? Buffer.byteLength(data) : data.byteLength };
  }

  exec(command: string | readonly string[], options: ExecOptions & { background: true }): Promise<CommandHandleLike>;
  exec(command: string | readonly string[], options?: ExecOptions & { background?: false }): Promise<ExecResult>;
  async exec(command: string | readonly string[], options: ExecOptions = {}): Promise<ExecResult | CommandHandleLike> {
    options.signal?.throwIfAborted();
    this.wakeFor('exec');
    this.api.execs.push({ sandboxId: this.id, command, options });
    const injected = this.api.execError?.(this, command, options);
    if (injected) throw injected;
    const env: Record<string, string> = { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: this.host(HOME), ...this.envVars };
    for (const [name, value] of Object.entries(options.env ?? {})) {
      env[name] = name.startsWith('EVE_') && value.startsWith('/') ? this.host(value) : value;
    }
    const cwd = this.host(options.cwd ?? `${HOME}/work`);
    if (!existsSync(cwd)) throw new PrizedError('invalid_argument', `cwd ${options.cwd} does not exist.`, { status: 400 });
    const argv = typeof command === 'string' ? ['bash', '-c', command] : [...command];
    const child = spawn(argv[0]!, argv.slice(1), { cwd, env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const run = new FakeRun(this, child);
    this.runs.push(run);
    if (options.background) return run;
    return await run.wait();
  }

  async refresh(): Promise<this> {
    if (this.state === 'deleted') throw notFound(this.id);
    return this;
  }

  async pause(_options?: WaitableOptions): Promise<void> {
    this.requireLive();
    this.api.log(`pause ${this.id}`);
    if (this.state === 'paused') return;
    for (const run of this.runs) run.signalGroup('SIGSTOP');
    this.state = 'paused';
  }

  async resume(_options?: WaitableOptions): Promise<void> {
    this.wakeFor('resume');
  }

  async fork(options: ForkOptions = {}): Promise<FakeSandbox[]> {
    this.requireLive();
    if (this.api.forkError) throw this.api.forkError;
    if (this.state !== 'running' && this.state !== 'paused') {
      throw new PrizedError('parent_not_running', `${this.id} is ${this.state}.`, { status: 409, sandboxId: this.id });
    }
    this.api.log(`fork ${this.id}`);
    const copies: FakeSandbox[] = [];
    for (let i = 0; i < (options.count ?? 1); i++) {
      const copy = new FakeSandbox(
        this.api,
        { ...(options.labels ?? this.labels) },
        { ...this.envVars },
        this.autoPauseMin,
        this.template,
        this.api.tick(),
        this,
      );
      copy.forkedFrom = this.id;
      copy.maxRuntimeMin = options.maxRuntimeMin;
      this.api.add(copy);
      copies.push(copy);
    }
    return copies;
  }

  async delete(_options?: WaitableOptions): Promise<void> {
    if (this.state === 'deleted') throw notFound(this.id);
    this.api.log(`delete ${this.id}`);
    for (const run of this.runs) {
      run.signalGroup('SIGCONT');
      run.signalGroup('SIGKILL');
    }
    this.state = 'deleted';
  }
}

export interface ExecCall {
  sandboxId: string;
  command: string | readonly string[];
  options: ExecOptions;
}

/** The fake API: `Sandbox`'s static side, plus what the tests look at. */
export class FakePrized implements SandboxApi {
  readonly dir = mkdtempSync(join(tmpdir(), 'prized-eve-fake-'));
  readonly sandboxes = new Map<string, FakeSandbox>();
  readonly creates: CreateOptions[] = [];
  readonly execs: ExecCall[] = [];
  readonly events: string[] = [];
  /** When set, every fork throws it. */
  forkError: Error | undefined;
  /** When set, an exec it returns an error for throws that error (as a lost or refused request would). */
  execError: ((sandbox: FakeSandbox, command: string | readonly string[], options: ExecOptions) => Error | undefined) | undefined;
  private clock = Date.now();

  log(event: string): void {
    this.events.push(event);
  }

  tick(): Date {
    this.clock += 1000;
    return new Date(this.clock);
  }

  add(sandbox: FakeSandbox): void {
    this.sandboxes.set(sandbox.id, sandbox);
  }

  /** Sandboxes not deleted, optionally only those carrying every one of `labels`. */
  live(labels: Record<string, string> = {}): FakeSandbox[] {
    return [...this.sandboxes.values()].filter(
      s => s.state !== 'deleted' && Object.entries(labels).every(([k, v]) => s.labels[k] === v),
    );
  }

  get(id: string): FakeSandbox {
    const sandbox = this.sandboxes.get(id);
    if (!sandbox) throw new Error(`no sandbox ${id}`);
    return sandbox;
  }

  private key(options?: ClientOptions): void {
    if (!options?.apiKey) throw new PrizedError('missing_api_key', 'No API key.');
  }

  async create(options: CreateOptions = {}): Promise<FakeSandbox> {
    this.key(options);
    this.creates.push(options);
    const sandbox = new FakeSandbox(
      this,
      { ...options.labels },
      { ...options.envVars },
      options.autoPauseMin ?? null,
      options.template ?? 'prized-base-v1',
      this.tick(),
    );
    this.log(`create ${sandbox.id}`);
    this.add(sandbox);
    return sandbox;
  }

  async connect(id: string, options?: ClientOptions): Promise<FakeSandbox> {
    this.key(options);
    const sandbox = this.sandboxes.get(id);
    if (!sandbox || sandbox.state === 'deleted') throw notFound(id);
    return sandbox;
  }

  async list(options: ListOptions = {}): Promise<FakeSandbox[]> {
    this.key(options);
    this.log('list');
    return this.live(options.labels).sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime());
  }

  async deleteByLabels(labels: Record<string, string>, options?: ClientOptions): Promise<string[]> {
    this.key(options);
    const ids: string[] = [];
    for (const sandbox of this.live(labels)) {
      await sandbox.delete();
      ids.push(sandbox.id);
    }
    return ids;
  }

  /** Stop every process the fake started and remove its directories. */
  close(): void {
    for (const sandbox of this.sandboxes.values()) {
      for (const run of sandbox.runs) {
        run.signalGroup('SIGCONT');
        run.signalGroup('SIGKILL');
      }
    }
    rmSync(this.dir, { recursive: true, force: true });
  }
}

export function isLink(path: string): boolean {
  try {
    return lstatSync(path).isSymbolicLink();
  } catch {
    return false;
  }
}
