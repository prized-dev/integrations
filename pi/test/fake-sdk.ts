/**
 * A stand-in for `@prized/sdk`'s Sandbox for the offline tests. A "sandbox" is a temp directory: its
 * commands run with the host's real bash (so the git and tar scripts the extension sends are checked
 * for what they do), and `/home/prized` in a command or a path means that directory. The errors are
 * the real SDK classes, so the extension's error handling is tested against them.
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { CommandTimeoutError, PrizedError } from '@prized/sdk';
import type { ExecResult, Sandbox, SandboxState } from '@prized/sdk';

import type { Sdk } from '../src/extension.ts';

export interface CreateCall {
  apiKey?: string;
  template?: string;
  resources?: Record<string, number | undefined>;
  labels?: Record<string, string>;
  maxRuntimeMin?: number;
  autoPauseMin?: number;
  autoDeleteMin?: number;
}

export interface ExecCall {
  sandboxId: string;
  command: string;
  options: Record<string, unknown>;
}

const notFound = (id: string): PrizedError =>
  new PrizedError('not_found', 'Not found (or not visible to this API key).', { status: 404, sandboxId: id });

/** Every fake sandbox, created, forked, or deleted: the test reads this to see what the extension did. */
export const world = {
  sandboxes: new Map<string, FakeSandbox>(),
  creates: [] as CreateCall[],
  forks: [] as string[],
  connects: [] as string[],
  execs: [] as ExecCall[],
  /** Order of lifecycle calls, e.g. "pause sbx_1". */
  events: [] as string[],
  seq: 0,
  /** Make the next create fail with this. */
  createError: undefined as Error | undefined,
  reset(): void {
    for (const s of this.sandboxes.values()) s.dispose();
    this.sandboxes.clear();
    this.creates.length = 0;
    this.forks.length = 0;
    this.connects.length = 0;
    this.execs.length = 0;
    this.events.length = 0;
    this.seq = 0;
    this.createError = undefined;
  },
};

export class FakeSandbox {
  readonly id: string;
  state: SandboxState = 'running';
  labels: Record<string, string>;
  readonly root: string;
  resources: { vcpu: number; memoryMb: number; memoryGb: number; diskGb: number };
  /** Make resume or any command fail like a deleted sandbox. */
  deleted = false;
  /** When its run time ends; null while paused or unlimited. */
  expiresAt: Date | null = null;
  /** Replace the next exec's answer. */
  execOverride?: (command: string) => ExecResult | undefined;

  readonly files = {
    write: async (p: string, data: string | Uint8Array): Promise<void> => {
      this.alive();
      const full = this.map(p);
      mkdirSync(path.dirname(full), { recursive: true });
      writeFileSync(full, data);
    },
    readBytes: async (p: string): Promise<Uint8Array> => {
      this.alive();
      const full = this.map(p);
      if (!existsSync(full)) throw notFound(this.id);
      return new Uint8Array(readFileSync(full));
    },
    exists: async (p: string): Promise<boolean> => {
      this.alive();
      return existsSync(this.map(p));
    },
  };

  constructor(labels: Record<string, string> = {}, resources: Record<string, number | undefined> = {}) {
    world.seq += 1;
    this.id = `sbx_${String(world.seq).padStart(16, '0')}`;
    this.labels = labels;
    const vcpu = resources.vcpu ?? 2;
    const memoryMb = resources.memoryMb ?? (resources.memoryGb ?? 4) * 1024;
    this.resources = { vcpu, memoryMb, memoryGb: memoryMb / 1024, diskGb: resources.diskGb ?? 10 };
    this.root = mkdtempSync(path.join(tmpdir(), 'fake-sbx-'));
    mkdirSync(path.join(this.root, 'work'), { recursive: true });
    world.sandboxes.set(this.id, this);
  }

  dispose(): void {
    rmSync(this.root, { recursive: true, force: true });
  }

  private map(p: string): string {
    return p.startsWith('/home/prized') ? this.root + p.slice('/home/prized'.length) : p;
  }

  private alive(): void {
    if (this.deleted) throw new PrizedError('sandbox_deleted', 'The sandbox was deleted.', { status: 410, sandboxId: this.id });
  }

  static async create(options: CreateCall = {}): Promise<Sandbox> {
    if (world.createError) {
      const e = world.createError;
      world.createError = undefined;
      throw e;
    }
    world.creates.push(options);
    const sb = new FakeSandbox(options.labels, options.resources);
    world.events.push(`create ${sb.id}`);
    return sb as unknown as Sandbox;
  }

  static async connect(id: string): Promise<Sandbox> {
    world.connects.push(id);
    const sb = world.sandboxes.get(id);
    if (!sb || sb.deleted) throw notFound(id);
    return sb as unknown as Sandbox;
  }

  async refresh(): Promise<this> {
    if (this.deleted) throw notFound(this.id);
    return this;
  }

  async pause(): Promise<void> {
    this.alive();
    world.events.push(`pause ${this.id}`);
    this.state = 'paused';
  }

  async resume(): Promise<void> {
    this.alive();
    world.events.push(`resume ${this.id}`);
    this.state = 'running';
  }

  async fork(options: { count?: number; labels?: Record<string, string> } = {}): Promise<Sandbox[]> {
    this.alive();
    world.forks.push(this.id);
    world.events.push(`fork ${this.id}`);
    const out: Sandbox[] = [];
    for (let i = 0; i < (options.count ?? 1); i += 1) {
      const copy = new FakeSandbox(options.labels ?? this.labels, this.resources);
      // A copy has the parent's disk.
      await new Promise<void>((resolve, reject) => {
        const c = spawn('cp', ['-a', `${this.root}/.`, copy.root]);
        c.on('close', (code) => (code === 0 ? resolve() : reject(new Error('cp failed'))));
      });
      out.push(copy as unknown as Sandbox);
    }
    return out;
  }

  async delete(): Promise<void> {
    world.events.push(`delete ${this.id}`);
    this.deleted = true;
    this.state = 'deleted';
  }

  async exec(
    command: string,
    options: {
      cwd?: string;
      timeoutMs?: number;
      signal?: AbortSignal;
      onStdout?: (c: string) => void;
      onStderr?: (c: string) => void;
      fullOutput?: boolean;
    } = {},
  ): Promise<ExecResult> {
    this.alive();
    world.execs.push({ sandboxId: this.id, command, options: { ...options, signal: undefined } });
    const override = this.execOverride?.(command);
    if (override) return override;
    if (this.state === 'paused') {
      // A command wakes a paused sandbox, as the edge does.
      this.state = 'running';
      world.events.push(`wake ${this.id}`);
    }
    const started = Date.now();
    const cwd = options.cwd ? this.map(options.cwd) : path.join(this.root, 'work');
    const script = command.replaceAll('/home/prized', this.root);
    return new Promise<ExecResult>((resolve, reject) => {
      if (options.signal?.aborted) return reject(new PrizedError('aborted', 'aborted'));
      const child = spawn('bash', ['-c', script], { cwd, detached: true, stdio: ['ignore', 'pipe', 'pipe'], env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: this.root } });
      let out = '';
      let err = '';
      let timedOut = false;
      let aborted = false;
      const killGroup = () => {
        try {
          if (child.pid) process.kill(-child.pid, 'SIGKILL');
        } catch {
          // already gone
        }
      };
      const show = (d: string) => d.replaceAll(this.root, '/home/prized');
      child.stdout.setEncoding('utf8').on('data', (d: string) => {
        out += d;
        options.onStdout?.(show(d));
      });
      child.stderr.setEncoding('utf8').on('data', (d: string) => {
        err += d;
        options.onStderr?.(show(d));
      });
      const timer = options.timeoutMs
        ? setTimeout(() => {
            timedOut = true;
            killGroup();
          }, options.timeoutMs)
        : undefined;
      const onAbort = () => {
        aborted = true;
        killGroup();
      };
      options.signal?.addEventListener('abort', onAbort, { once: true });
      // Like the real sandbox, answer when the shell exits, even if a background child still holds the pipes.
      child.on('exit', (code) => {
        setTimeout(() => {
          child.stdout.destroy();
          child.stderr.destroy();
          if (timer) clearTimeout(timer);
          options.signal?.removeEventListener('abort', onAbort);
          const result: ExecResult = {
            stdout: out.replaceAll(this.root, '/home/prized'),
            stderr: err.replaceAll(this.root, '/home/prized'),
            exitCode: timedOut ? -1 : (code ?? 137),
            durationMs: Date.now() - started,
            timedOut,
            truncated: false,
          };
          if (aborted) reject(new PrizedError('aborted', 'The command was aborted.'));
          else if (timedOut) reject(new CommandTimeoutError(result, options.timeoutMs ?? null, this.id));
          else resolve(result);
        }, 40);
      });
    });
  }
}

export const fakeSdk = (): Sdk => ({ Sandbox: FakeSandbox as unknown as Sdk['Sandbox'] });

/** The sandbox's directory for a path inside it, for assertions. */
export const inSandbox = (sb: FakeSandbox, p: string): string => path.join(sb.root, p.replace(/^\/home\/prized\/?/, ''));
