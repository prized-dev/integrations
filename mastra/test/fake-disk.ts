/**
 * A sandbox disk for the filesystem's offline tests: a temp directory on this machine, reached the
 * two ways the real sandbox is.
 *
 * - `readBytes` / `write` follow Prized's files API: only paths under the
 *   home root, no `..`, symlinks followed only while they stay under it, a 100 MiB limit, a write
 *   that keeps the replaced file's permission bits and gives a new one 0666 less the umask.
 * - `shell` runs a command with this machine's `sh` (or PRIZED_TEST_SHELL), under the same umask,
 *   with only PATH and HOME.
 *
 * Sandbox paths are this machine's paths (inside a realpath'd temp dir), so the same absolute path
 * works for both.
 */
import { execFile } from 'node:child_process';
import { existsSync } from 'node:fs';
import { chmod, lstat, mkdir, open, readFile, readlink, realpath, rename, rm, stat } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';

import { PrizedError } from '@prized/sdk';
import type { ExecResult } from '@prized/sdk';

export interface FakeDiskOptions {
  umask?: number;
  /**
   * The files API of images before r4 (2026-10-25): an upload always comes out 0600, and any
   * symlink in a path is refused.
   */
  legacy?: boolean;
  /** The files API's per-file limit. */
  maxBytes?: number;
  /** The exec edge's per-stream output limit. */
  maxOutputBytes?: number;
}

const fault = (code: string, status: number): PrizedError => new PrizedError(code, null, { status });

export class FakeDisk {
  readonly umask: number;
  readonly legacy: boolean;
  readonly maxBytes: number;
  maxOutputBytes: number;
  /** Every files API call, as `read <path>` / `write <path> <bytes>`. */
  readonly calls: string[] = [];
  /** Every command run. */
  readonly commands: string[] = [];
  /** The data of every upload, by path. */
  readonly uploads: Array<{ path: string; data: Uint8Array }> = [];
  /** Answer the next files API calls with these errors instead, once each. */
  failNext: PrizedError[] = [];

  constructor(
    readonly home: string,
    options: FakeDiskOptions = {},
  ) {
    this.umask = options.umask ?? 0o022;
    this.legacy = options.legacy ?? false;
    this.maxBytes = options.maxBytes ?? 100 * 1024 * 1024;
    this.maxOutputBytes = options.maxOutputBytes ?? 4 * 1024 * 1024;
  }

  /** The files API's path checks, then its symlink resolution: the real path the call acts on. */
  private async resolve(raw: string): Promise<string> {
    if (raw === '') throw fault('missing_path', 400);
    if (!raw.startsWith('/')) throw fault('path_not_absolute', 400);
    if (raw.includes('\0') || raw.split('/').includes('..')) throw fault('invalid_path', 400);
    const clean = resolve(raw);
    if (clean !== this.home && !clean.startsWith(this.home + sep)) throw fault('path_outside_root', 403);
    const rootReal = await realpath(this.home);
    let current = rootReal;
    const parts = relative(this.home, clean).split(sep).filter(Boolean);
    for (let i = 0; i < parts.length; i++) {
      let next = join(current, parts[i]!);
      for (let hops = 0; ; hops++) {
        if (hops > 40) throw fault('symlink_not_allowed', 400);
        const info = await lstat(next).catch(() => undefined);
        if (!info) return join(next, ...parts.slice(i + 1));
        if (!info.isSymbolicLink()) break;
        if (this.legacy) throw fault('symlink_not_allowed', 400);
        const target = await readlink(next);
        next = isAbsolute(target) ? resolve(target) : resolve(dirname(next), target);
        if (next !== rootReal && !next.startsWith(rootReal + sep)) throw fault('symlink_not_allowed', 400);
      }
      current = next;
    }
    return current;
  }

  private failure(): void {
    const error = this.failNext.shift();
    if (error) throw error;
  }

  async readBytes(raw: string): Promise<Uint8Array> {
    this.calls.push(`read ${raw}`);
    this.failure();
    const path = await this.resolve(raw);
    const info = await stat(path).catch((error: NodeJS.ErrnoException) => {
      if (error.code === 'EACCES') throw fault('permission_denied', 403);
      // The files API checks each component: a file on the way is `not_directory`.
      if (error.code === 'ENOTDIR') throw fault('not_directory', 400);
      throw fault('not_found', 404);
    });
    if (!info.isFile()) throw fault('not_regular_file', 400);
    if (info.size > this.maxBytes) throw fault('file_too_large', 413);
    try {
      return new Uint8Array(await readFile(path));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'EACCES') throw fault('permission_denied', 403);
      throw error;
    }
  }

  async write(raw: string, data: string | Uint8Array): Promise<{ path: string; size: number }> {
    const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : new Uint8Array(data);
    this.calls.push(`write ${raw} ${bytes.byteLength}`);
    this.failure();
    // The SDK refuses an oversized write before sending it.
    if (bytes.byteLength > this.maxBytes) throw fault('file_too_large', 413);
    const path = await this.resolve(raw);
    const existing = await lstat(path).catch(() => undefined);
    if (existing && !existing.isFile()) throw fault('unsupported_file_type', 400);
    await this.mkdirs(dirname(path));
    const tmp = join(dirname(path), `.prized-upload-${Math.random().toString(16).slice(2)}`);
    const handle = await open(tmp, 'wx', 0o600);
    try {
      await handle.write(bytes);
      // The replaced file's permission bits, read just before the rename; else 0666 less the umask.
      const replaced = await lstat(path).catch(() => undefined);
      await handle.chmod(this.legacy ? 0o600 : replaced?.isFile() ? replaced.mode & 0o777 : 0o666 & ~this.umask);
    } finally {
      await handle.close();
    }
    await rename(tmp, path);
    this.uploads.push({ path: raw, data: bytes });
    return { path: raw, size: bytes.byteLength };
  }

  /** `mkdir -p` with 0777 less the umask for each directory it creates. */
  private async mkdirs(dir: string): Promise<void> {
    const info = await lstat(dir).catch(() => undefined);
    if (info?.isDirectory()) return;
    if (info) throw fault('not_directory', 400);
    await this.mkdirs(dirname(dir));
    try {
      await mkdir(dir);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'EEXIST') return; // made by a concurrent call, as MkdirAll allows
      throw error;
    }
    await chmod(dir, 0o777 & ~this.umask);
  }

  /**
   * Run a command the way the exec edge does, answering with its exit code and output. Without a
   * `cwd` it runs in the home's `work` directory, and fails to start if that is gone, as Prized does.
   */
  shell = (command: string, cwd?: string): Promise<ExecResult> => {
    this.commands.push(command);
    const dir = cwd ?? join(this.home, 'work');
    if (!existsSync(dir)) return Promise.reject(fault('spawn_failed', 400));
    return new Promise(done => {
      execFile(
        // The sandbox runs commands with bash; `sh` (dash on Ubuntu) checks the scripts stay POSIX.
        process.env.PRIZED_TEST_SHELL ?? 'sh',
        ['-c', `umask ${this.umask.toString(8).padStart(4, '0')}\n${command}`],
        { cwd: dir, env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: this.home }, encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 },
        (error, stdout, stderr) => {
          const code = error ? (typeof error.code === 'number' ? error.code : 1) : 0;
          const truncated = stdout.length >= this.maxOutputBytes || stderr.length >= this.maxOutputBytes;
          done({
            stdout: stdout.slice(0, this.maxOutputBytes),
            stderr: stderr.slice(0, this.maxOutputBytes),
            exitCode: code,
            durationMs: 1,
            timedOut: false,
            truncated,
          });
        },
      );
    });
  };

  /** What is left in the staging directory (should be nothing after any operation). */
  async staged(): Promise<string[]> {
    const { readdir } = await import('node:fs/promises');
    return readdir(join(this.home, '.cache/prized-mastra')).catch(() => []);
  }

  async cleanup(root: string): Promise<void> {
    await rm(root, { recursive: true, force: true });
  }
}
