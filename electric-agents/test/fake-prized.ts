/**
 * A stand-in for `@prized/sdk`'s Sandbox class, for the offline tests.
 *
 * Each fake sandbox gets its own temp root: `/home/prized/...` maps to `<root>/home/prized/...`,
 * and commands run in a real `bash` on this machine with every `/home/prized` in the script
 * rewritten to that root, stdin from /dev/null and HOME inside the root, as in a real sandbox.
 * Paths outside /home/prized are this machine's own (tests only read those). The files API, as
 * the real one, serves only paths inside /home/prized and refuses links that leave it. Errors
 * are the real SDK's classes. Labels, states, list filters, keepAlive, resume and delete are
 * modelled as the API documents them.
 */
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, posix } from 'node:path';

import { CommandTimeoutError, PrizedError } from '@prized/sdk';
import type { ClientOptions, CreateOptions, ExecResult, ListOptions, SandboxState } from '@prized/sdk';

import type { PrizedSandboxClass, PrizedSandboxHandle } from '../src/client.js';

const HOME = '/home/prized';

export interface ExecCall {
  command: string;
  options: { cwd?: string; env?: Record<string, string>; timeoutMs?: number; fullOutput?: boolean };
}

export class FakeSandbox implements PrizedSandboxHandle {
  readonly id: string;
  readonly root: string;
  readonly labels: Record<string, string>;
  readonly createOptions: CreateOptions;
  state: SandboxState = 'running';
  createdAt: Date;
  execs: ExecCall[] = [];
  keepAlives = 0;
  resumes = 0;
  deletes = 0;
  /** Every files API call: [operation, path]. */
  fileCalls: [string, string][] = [];
  /** Make keepAlive reject (a network blip, say). */
  failKeepAlive: Error | undefined;
  /** Make the next resume reject with this (the sandbox deleted meanwhile, say). */
  failResume: Error | undefined;
  /** Throw this from the next exec whose command contains `match`, before it runs. */
  failExec: { match: string; error: Error } | undefined;
  /** Commands containing this time out at once (as CommandTimeoutError), without running. */
  timeOutWhen: string | undefined;
  /** The per-stream cap of a plain exec (4 MiB in the real one) and of a fullOutput one (100 MiB); tests lower them. */
  plainCap: number = 4 * 1024 * 1024;
  fullCap: number = 100 * 1024 * 1024;

  constructor(root: string, createOptions: CreateOptions, createdAt: Date) {
    this.id = `sbx_${Math.random().toString(16).slice(2, 18).padEnd(16, '0')}`;
    this.root = root;
    this.createOptions = createOptions;
    this.labels = { ...createOptions.labels };
    this.createdAt = createdAt;
  }

  /** A sandbox path on this machine. */
  local(path: string): string {
    return path === HOME || path.startsWith(`${HOME}/`) ? join(this.root, path) : path;
  }

  private alive(): void {
    if (this.state === 'deleted') throw new PrizedError('sandbox_deleted', 'The sandbox was deleted.', { status: 410, sandboxId: this.id });
  }

  /** Commands and file transfers wake a paused sandbox, as the edge does. */
  private touch(): void {
    this.alive();
    if (this.state === 'paused') this.state = 'running';
  }

  readonly files = {
    readBytes: async (path: string): Promise<Uint8Array> => {
      const local = await this.filePath('readBytes', path);
      const info = await stat(local).catch(() => undefined);
      if (!info) throw new PrizedError('not_found', `${path} does not exist.`, { status: 404, sandboxId: this.id });
      if (!info.isFile()) throw new PrizedError('not_regular_file', 'Only regular files can be downloaded.', { status: 400, sandboxId: this.id });
      return new Uint8Array(await readFile(local));
    },
    write: async (path: string, data: string | Uint8Array): Promise<unknown> => {
      const local = await this.filePath('write', path);
      await mkdir(dirname(local), { recursive: true });
      await writeFile(local, data, { mode: 0o600 });
      return { path, size: typeof data === 'string' ? Buffer.byteLength(data) : data.byteLength };
    },
    remove: async (path: string, options?: { recursive?: boolean }): Promise<void> => {
      const local = await this.filePath('remove', path);
      await rm(local, { recursive: options?.recursive === true, force: true });
    },
  };

  /** The files API's rules: absolute, inside /home/prized, links followed only while they stay inside. */
  private async filePath(op: string, path: string): Promise<string> {
    this.touch();
    this.fileCalls.push([op, path]);
    const absolute = posix.normalize(path);
    if (!absolute.startsWith(`${HOME}/`)) {
      throw new PrizedError('path_outside_root', `File paths must be inside ${HOME}.`, { status: 403, sandboxId: this.id });
    }
    const local = this.local(absolute);
    const resolved = await realpath(local).catch(() => undefined);
    if (resolved !== undefined && !resolved.startsWith(join(this.root, HOME) + '/')) {
      throw new PrizedError('symlink_not_allowed', 'File transfers cannot follow a symbolic link out of /home/prized.', {
        status: 400,
        sandboxId: this.id,
      });
    }
    return local;
  }

  async exec(command: string, options: ExecCall['options'] = {}): Promise<ExecResult> {
    this.touch();
    this.execs.push({ command, options });
    if (this.failExec && command.includes(this.failExec.match)) {
      const { error } = this.failExec;
      this.failExec = undefined;
      throw error;
    }
    if (this.timeOutWhen !== undefined && command.includes(this.timeOutWhen)) {
      const timedOut = { stdout: '', stderr: '', exitCode: -1, durationMs: 1, timedOut: true, truncated: false };
      throw new CommandTimeoutError(timedOut, options.timeoutMs ?? 60_000, this.id);
    }
    const script = command.replaceAll(HOME, join(this.root, HOME));
    const cwd = this.local(options.cwd ?? `${HOME}/work`);
    const timeoutMs = options.timeoutMs ?? 60_000;
    const started = Date.now();
    const result = await new Promise<ExecResult & { killed: boolean }>((resolve, reject) => {
      const child = spawn('bash', ['-c', script], {
        cwd,
        detached: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
          PATH: process.env.PATH ?? '/usr/bin:/bin',
          HOME: join(this.root, HOME),
          ...(this.createOptions.envVars ?? {}),
          ...options.env,
        },
      });
      let stdout = '';
      let stderr = '';
      let killed = false;
      child.stdout.setEncoding('utf8');
      child.stderr.setEncoding('utf8');
      child.stdout.on('data', (chunk: string) => (stdout += chunk));
      child.stderr.on('data', (chunk: string) => (stderr += chunk));
      const timer =
        timeoutMs > 0
          ? setTimeout(() => {
              killed = true;
              try {
                process.kill(-child.pid!, 'SIGKILL');
              } catch {
                // already gone
              }
            }, timeoutMs)
          : undefined;
      child.on('error', reject);
      child.on('close', code => {
        if (timer) clearTimeout(timer);
        resolve({
          stdout,
          stderr,
          exitCode: killed ? -1 : (code ?? -1),
          durationMs: Date.now() - started,
          timedOut: killed,
          truncated: false,
          killed,
        });
      });
    });
    const { killed, ...plain } = result;
    if (killed) throw new CommandTimeoutError(plain, timeoutMs, this.id);
    const cap = options.fullOutput ? this.fullCap : this.plainCap;
    if (plain.stdout.length > cap || plain.stderr.length > cap) {
      return { ...plain, stdout: plain.stdout.slice(0, cap), stderr: plain.stderr.slice(0, cap), truncated: true, truncatedReason: 'output_limit' };
    }
    return plain;
  }

  async keepAlive(): Promise<void> {
    this.alive();
    this.keepAlives++;
    if (this.failKeepAlive) throw this.failKeepAlive;
  }

  async resume(): Promise<void> {
    this.resumes++;
    if (this.failResume) {
      const err = this.failResume;
      this.failResume = undefined;
      // A sandbox_deleted answer means it really is gone (its autoDeleteMin ran out, say).
      if ((err as { code?: string }).code === 'sandbox_deleted') this.state = 'deleted';
      throw err;
    }
    this.alive();
    this.state = 'running';
  }

  /** What autoPauseMin does once the sandbox idles. */
  pause(): void {
    this.state = 'paused';
  }

  /** As the real SDK: deleting a sandbox that is already gone resolves (its 404 counts as done). */
  async delete(): Promise<void> {
    this.deletes++;
    if (this.state === 'deleted') return;
    this.state = 'deleted';
    await rm(this.root, { recursive: true, force: true });
  }
}

export interface FakePrized {
  Sdk: PrizedSandboxClass;
  sandboxes: FakeSandbox[];
  creates: CreateOptions[];
  lists: ListOptions[];
  deletesByLabels: Record<string, string>[];
  /** Runs inside each create, after the new sandbox exists (to stage a race with another host). */
  onCreate: ((sandbox: FakeSandbox) => Promise<void> | void) | undefined;
  /** A sandbox made as if by another host, `ageMs` before now. */
  seed(options: CreateOptions, ageMs?: number): Promise<FakeSandbox>;
  live(): FakeSandbox[];
  cleanup(): Promise<void>;
}

export function createFakePrized(): FakePrized {
  const sandboxes: FakeSandbox[] = [];
  const creates: CreateOptions[] = [];
  const lists: ListOptions[] = [];
  const deletesByLabels: Record<string, string>[] = [];
  let clock = Date.parse('2026-11-02T00:00:00Z');

  const make = async (options: CreateOptions, createdAt: Date): Promise<FakeSandbox> => {
    if ((options.labels && Object.keys(options.labels).length > 32) || Object.entries(options.labels ?? {}).some(([k, v]) => k.length > 64 || v.length > 256)) {
      throw new PrizedError('bad_request', 'labels out of range', { status: 400 });
    }
    const root = await mkdtemp(join(tmpdir(), 'fake-prized-'));
    await mkdir(join(root, HOME, 'work'), { recursive: true });
    const sandbox = new FakeSandbox(root, options, createdAt);
    sandboxes.push(sandbox);
    return sandbox;
  };

  const matches = (sandbox: FakeSandbox, labels: Record<string, string> | undefined): boolean =>
    Object.entries(labels ?? {}).every(([k, v]) => sandbox.labels[k] === v);

  const fake: FakePrized = {
    sandboxes,
    creates,
    lists,
    deletesByLabels,
    onCreate: undefined,
    Sdk: {
      async create(options: CreateOptions = {}): Promise<PrizedSandboxHandle> {
        creates.push(options);
        clock += 1000;
        const sandbox = await make(options, new Date(clock));
        await fake.onCreate?.(sandbox);
        return sandbox;
      },
      async list(options: ListOptions = {}): Promise<PrizedSandboxHandle[]> {
        lists.push(options);
        const states = options.state === undefined ? undefined : ([] as SandboxState[]).concat(options.state);
        return sandboxes
          .filter(s => s.state !== 'deleted' && matches(s, options.labels) && (!states || states.includes(s.state)))
          .sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime());
      },
      async deleteByLabels(labels: Record<string, string>, _options?: ClientOptions): Promise<string[]> {
        deletesByLabels.push(labels);
        const doomed = sandboxes.filter(s => s.state !== 'deleted' && matches(s, labels));
        for (const s of doomed) await s.delete();
        return doomed.map(s => s.id);
      },
    },
    async seed(options: CreateOptions, ageMs = 60_000): Promise<FakeSandbox> {
      return make(options, new Date(clock - ageMs));
    },
    live(): FakeSandbox[] {
      return sandboxes.filter(s => s.state !== 'deleted');
    },
    async cleanup(): Promise<void> {
      await Promise.all(sandboxes.map(s => rm(s.root, { recursive: true, force: true })));
    },
  };
  return fake;
}
