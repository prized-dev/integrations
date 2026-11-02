/**
 * A stand-in for `@prized/sdk`'s Sandbox, backed by a local directory, for the offline tests.
 *
 * Each fake sandbox gets its own temp root. `/home/prized/...` maps to `<root>/home/prized/...`, and
 * commands run in a real `bash` on this machine with every `/home/prized` in the script rewritten to
 * that root, stdin from /dev/null (as in a real sandbox) and HOME and TMPDIR inside the root. The files
 * API, as the real one, serves only paths inside /home/prized, and gives a new file mode 0600. The
 * errors are the real SDK classes.
 */
import { spawn } from 'node:child_process';
import { chmod, copyFile, mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, posix } from 'node:path';

import type { CreateOptions, ExecOptions, ExecResult, PrizedError, PrizedErrorInit } from '@prized/sdk';

/**
 * The real SDK's error class, handed in by the test's `vi.mock` factory. This module imports nothing
 * from `@prized/sdk` at run time: it is loaded while that mock is being built.
 */
export const real: { PrizedError?: typeof PrizedError } = {};

function sdkError(code: string, message: string, init?: PrizedErrorInit): PrizedError {
  return new real.PrizedError!(code, message, init);
}

const HOME = '/home/prized';

export interface ExecCall {
  command: string;
  options: ExecOptions;
}

export class FakeSandbox {
  static creates: CreateOptions[] = [];
  static live: Map<string, FakeSandbox> = new Map();
  static all: FakeSandbox[] = [];
  /** Make the next create's sandbox refuse commands, to test cleanup after a failed setup. */
  static failNextSetup = false;
  /** The files API's limit on one transfer (100 MiB in the real one); tests lower it. */
  static maxFileBytes: number = 100 * 1024 * 1024;

  readonly id: string;
  readonly root: string;
  readonly env: Record<string, string>;
  execs: ExecCall[] = [];
  deletes = 0;
  deleted = false;
  refuseCommands = false;
  /** Throw this from the next exec whose command contains `match`, before it runs (a dropped connection, say). */
  failExec: { match: string; error: Error } | undefined;
  /** Throw this from the next delete. */
  failDelete: Error | undefined;
  /** Every files API call: [operation, path]. */
  fileCalls: [string, string][] = [];

  static reset(): void {
    FakeSandbox.creates = [];
    FakeSandbox.live.clear();
    FakeSandbox.all = [];
    FakeSandbox.failNextSetup = false;
    FakeSandbox.maxFileBytes = 100 * 1024 * 1024;
  }

  private constructor(root: string, env: Record<string, string>) {
    this.id = `sbx_${Math.random().toString(16).slice(2, 18)}`;
    this.root = root;
    this.env = env;
  }

  static async create(options: CreateOptions = {}): Promise<FakeSandbox> {
    FakeSandbox.creates.push(options);
    if (!options.apiKey) throw sdkError('missing_api_key', 'No API key.');
    const root = await mkdtemp(join(tmpdir(), 'fake-prized-'));
    await mkdir(join(root, 'home/prized/work'), { recursive: true });
    await mkdir(join(root, 'tmp'), { recursive: true });
    const sandbox = new FakeSandbox(root, options.envVars ?? {});
    if (FakeSandbox.failNextSetup) {
      sandbox.refuseCommands = true;
      FakeSandbox.failNextSetup = false;
    }
    FakeSandbox.live.set(sandbox.id, sandbox);
    FakeSandbox.all.push(sandbox);
    return sandbox;
  }

  /** A sandbox path on this machine. */
  local(path: string): string {
    return path === HOME || path.startsWith(`${HOME}/`) ? join(this.root, path) : path;
  }

  private alive(): void {
    if (this.deleted) throw sdkError('sandbox_deleted', 'The sandbox was deleted.', { sandboxId: this.id });
  }

  async exec(command: string | readonly string[], options: ExecOptions = {}): Promise<ExecResult> {
    this.alive();
    const text = typeof command === 'string' ? command : command.map(word => `'${word.replaceAll("'", `'\\''`)}'`).join(' ');
    this.execs.push({ command: text, options });
    if (this.failExec && text.includes(this.failExec.match)) {
      const { error } = this.failExec;
      this.failExec = undefined;
      throw error;
    }
    if (this.refuseCommands) return { stdout: '', stderr: 'refused', exitCode: 1, durationMs: 1, timedOut: false, truncated: false } as ExecResult;
    const script = text.replaceAll(HOME, join(this.root, HOME));
    const cwd = this.local(posix.resolve(`${HOME}/work`, options.cwd ?? `${HOME}/work`));
    const started = Date.now();
    return await new Promise<ExecResult>((resolve, reject) => {
      const child = spawn('bash', ['-c', script], {
        cwd,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
          PATH: process.env.PATH ?? '/usr/bin:/bin',
          HOME: join(this.root, HOME),
          TMPDIR: join(this.root, 'tmp'),
          GIT_CONFIG_NOSYSTEM: '1',
          ...this.env,
          ...options.env,
        },
      });
      let stdout = '';
      let stderr = '';
      child.stdout.setEncoding('utf8');
      child.stderr.setEncoding('utf8');
      child.stdout.on('data', (chunk: string) => {
        stdout += chunk;
        options.onStdout?.(chunk);
      });
      child.stderr.on('data', (chunk: string) => {
        stderr += chunk;
        options.onStderr?.(chunk);
      });
      child.on('error', reject);
      child.on('close', code => {
        resolve({ stdout, stderr, exitCode: code ?? -1, durationMs: Date.now() - started, timedOut: false, truncated: false } as ExecResult);
      });
    });
  }

  /** The files API: only inside /home/prized, like the real one. */
  private filePath(op: string, path: string): string {
    this.alive();
    this.fileCalls.push([op, path]);
    const absolute = posix.resolve(`${HOME}/work`, path);
    if (!absolute.startsWith(`${HOME}/`)) {
      throw sdkError('invalid_argument', `The path must be inside ${HOME}: ${path}`, { status: 400, sandboxId: this.id });
    }
    return this.local(absolute);
  }

  private checkSize(bytes: number): void {
    if (bytes > FakeSandbox.maxFileBytes) throw sdkError('file_too_large', 'Files move 100 MiB at a time at most.', { sandboxId: this.id });
  }

  private async place(target: string, write: () => Promise<void>, mode?: number): Promise<void> {
    const existed = await stat(target).then(() => true, () => false);
    await mkdir(dirname(target), { recursive: true });
    await write();
    if (mode !== undefined) await chmod(target, mode);
    else if (!existed) await chmod(target, 0o600);
  }

  readonly files = {
    write: async (path: string, data: string | Uint8Array, options: { mode?: number } = {}): Promise<{ path: string }> => {
      const target = this.filePath('write', path);
      this.checkSize(typeof data === 'string' ? Buffer.byteLength(data) : data.byteLength);
      await this.place(target, () => writeFile(target, data), options.mode);
      return { path };
    },
    upload: async (localPath: string, remotePath: string, options: { mode?: number } = {}): Promise<{ path: string }> => {
      const target = this.filePath('upload', remotePath);
      this.checkSize((await stat(localPath)).size);
      await this.place(target, () => copyFile(localPath, target), options.mode);
      return { path: remotePath };
    },
    readBytes: async (remotePath: string): Promise<Uint8Array> => {
      const source = this.filePath('readBytes', remotePath);
      const data = await readFile(source).catch(() => {
        throw sdkError('not_found', `No such file: ${remotePath}`, { status: 404, sandboxId: this.id });
      });
      this.checkSize(data.length);
      return new Uint8Array(data);
    },
    download: async (remotePath: string, localPath: string): Promise<number> => {
      this.fileCalls.push(['download', remotePath]);
      const data = await this.files.readBytes(remotePath);
      this.fileCalls.pop(); // the readBytes inside
      await writeFile(localPath, data);
      return data.length;
    },
    remove: async (path: string): Promise<void> => {
      await rm(this.filePath('remove', path), { recursive: true, force: true });
    },
  };

  async delete(): Promise<void> {
    this.deletes++;
    if (this.failDelete) {
      const error = this.failDelete;
      this.failDelete = undefined;
      throw error;
    }
    // Like the real SDK, deleting a sandbox that is already gone succeeds.
    this.deleted = true;
    FakeSandbox.live.delete(this.id);
  }

  /** Remove the local roots (the tests' own cleanup, not part of the SDK). */
  static async cleanup(): Promise<void> {
    await Promise.all(FakeSandbox.all.map(s => rm(s.root, { recursive: true, force: true })));
  }
}
