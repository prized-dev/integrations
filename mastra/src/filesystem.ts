/**
 * The disk of a {@link PrizedSandbox} as a Mastra workspace filesystem: `sandbox.filesystem`.
 *
 * A file's bytes move through Prized's files API; everything else is one command in the sandbox.
 * See file-commands.ts for the commands.
 */

import { Buffer } from 'node:buffer';

import type {
  CopyOptions,
  FileContent,
  FileEntry,
  FileStat,
  FilesystemInfo,
  ListOptions,
  ProviderStatus,
  ReadOptions,
  RemoveOptions,
  WalkEntry,
  WalkOptions,
  WriteOptions,
} from '@mastra/core/workspace';
import {
  DirectoryNotEmptyError,
  DirectoryNotFoundError,
  FileExistsError,
  FileNotFoundError,
  FilesystemError,
  IsDirectoryError,
  MastraFilesystem,
  NotDirectoryError,
  PermissionError,
  StaleFileError,
} from '@mastra/core/workspace';
import { PrizedError } from '@prized/sdk';
import type { ExecResult, Sandbox } from '@prized/sdk';

import {
  EXIT,
  appendFromStagingCommand,
  copyCommand,
  deleteFileCommand,
  existsCommand,
  isDirectoryCommand,
  linkFromStagingCommand,
  listCommand,
  mkdirCommand,
  moveCommand,
  moveNoClobberCommand,
  mtimeCommand,
  parseFindTime,
  removeCommand,
  replaceFromStagingCommand,
  rmdirCommand,
  stageForReadCommand,
  statCommand,
} from './file-commands.js';
import type { ExitOutcome } from './file-commands.js';
import type { PrizedSandbox } from './sandbox.js';

/** The most one read or write moves: the files API's limit per file. */
export const MAX_FILE_BYTES: number = 100 * 1024 * 1024;
/** The files API's root in Prized's templates: the sandbox user's home. */
export const PRIZED_HOME = '/home/prized';

const LOG_PREFIX = '[PrizedSandboxFilesystem]';
/** Default recursion depth for `readdir({ recursive: true })`, as `LocalFilesystem` and E2B use. */
const DEFAULT_MAX_DEPTH = 100;
/** Commands that only look (stat, list, mkdir, delete). */
const LOOK_TIMEOUT_MS = 60_000;
/** Commands that move data inside the sandbox (copy, move, staged writes): the longest one request waits. */
const MOVE_TIMEOUT_MS = 240_000;
/** Maps a command's exit outcomes to the errors they stand for. */
type ErrorMap = Partial<Record<ExitOutcome, (result: ExecResult) => Error>>;

/** Download refusals that a command run as the sandbox user can still do: staged instead. */
const STAGE_CODES: ReadonlySet<string> = new Set(['path_outside_root', 'symlink_not_allowed', 'invalid_path']);

/**
 * A read or write over the files API's 100 MiB limit. Nothing was moved: read part of the file
 * with a command (`head -c`, `tail`, `sed -n`), or split it.
 */
export class FileTooLargeError extends FilesystemError {
  readonly limitBytes: number;
  readonly sizeBytes: number | undefined;

  constructor(path: string, limitBytes: number, sizeBytes?: number) {
    super(
      `File too large: ${path}${sizeBytes !== undefined ? ` is ${sizeBytes} bytes,` : ' is'} over the ${limitBytes} bytes (100 MiB) one read or write can move. ` +
        'Read part of it with a command (head -c, tail, sed -n), or split it.',
      'EFBIG',
      path,
    );
    this.name = 'FileTooLargeError';
    this.limitBytes = limitBytes;
    this.sizeBytes = sizeBytes;
  }
}

/** @internal */
export interface PrizedSandboxFilesystemOptions {
  /** The files API's root (default /home/prized). For tests. */
  home?: string;
}

/**
 * The sandbox's own disk, for a Mastra workspace's file tools (read, write, edit, list, grep, ...).
 *
 * ```ts
 * const sandbox = new PrizedSandbox();
 * const workspace = new Workspace({ sandbox, filesystem: sandbox.filesystem });
 * ```
 *
 * Absolute paths are paths in the sandbox; relative ones start in the sandbox's working directory
 * (/home/prized/work by default). Paths are not confined to a root: the agent can already read
 * anything its commands can. Each operation starts (or resumes) the sandbox if needed, and is
 * retried once on a new sandbox if this one was deleted, like a command.
 *
 * A file's bytes move through the files API, at most 100 MiB a file ({@link FileTooLargeError}
 * above that, before anything is sent). A read is one download. A write is one upload to a
 * staging file under ~/.cache plus one command that puts it in place, whole or not at all,
 * keeping the replaced file's permission bits (a 0755 script stays executable); a new file gets
 * 0666 less the sandbox's umask. Listing, stat, mkdir, delete, copy and move are one command
 * each, and copies and moves never send file bytes over the network. Paths outside /home/prized,
 * which the files API can't reach, are read through a staged copy and written the same way.
 */
export class PrizedSandboxFilesystem extends MastraFilesystem {
  readonly id: string;
  readonly name = 'PrizedSandboxFilesystem';
  readonly provider = 'prized';
  status: ProviderStatus = 'ready';

  readonly #sandbox: PrizedSandbox;
  readonly #home: string;

  constructor(sandbox: PrizedSandbox, options: PrizedSandboxFilesystemOptions = {}) {
    super({ name: 'PrizedSandboxFilesystem' });
    this.id = `${sandbox.id}-filesystem`;
    this.#sandbox = sandbox;
    this.#home = (options.home ?? PRIZED_HOME).replace(/\/+$/, '');
  }

  getInstructions(): string {
    return (
      `File tools act on the Prized sandbox's own disk. Absolute paths are paths in the sandbox; ` +
      `relative paths start in ${this.#workdir()}. One read or write moves at most 100 MiB.`
    );
  }

  async getInfo(): Promise<FilesystemInfo> {
    return {
      id: this.id,
      name: this.name,
      provider: this.provider,
      status: this.status,
      metadata: { ...(this.#sandbox.sandboxId && { sandboxId: this.#sandbox.sandboxId }) },
    };
  }

  // ---------------------------------------------------------------------------
  // Files
  // ---------------------------------------------------------------------------

  async readFile(path: string, options?: ReadOptions): Promise<string | Buffer> {
    const abs = this.#abs(path);
    let bytes: Uint8Array | undefined;
    if (this.#direct(abs)) {
      try {
        bytes = await this.#call(sandbox => sandbox.files.readBytes(abs));
      } catch (error) {
        if (!STAGE_CODES.has(codeOf(error))) throw await this.#readError(error, abs, path);
      }
    }
    bytes ??= await this.#stagedRead(abs, path);
    const buffer = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    return options?.encoding ? buffer.toString(options.encoding) : buffer;
  }

  async writeFile(path: string, content: FileContent, options?: WriteOptions): Promise<void> {
    const abs = this.#abs(path);
    const data = toWriteData(content);
    this.#checkSize(data, path);
    // The staged commands check the parent themselves (recursive: false); only a stale-file check comes first.
    if (options?.expectedMtime && options.overwrite !== false) await this.#checkMtime(abs, path, options.expectedMtime);
    if (options?.overwrite === false) {
      await this.#publishStaged('writeFile', path, data, staging =>
        linkFromStagingCommand(staging, abs, { recursive: options.recursive }),
      );
      return;
    }
    // Always staged, never uploaded in place: the files API on images before r4 (2026-10-25)
    // writes a replaced file 0600, so a 0755 script would stop being executable.
    await this.#publishStaged('writeFile', path, data, staging =>
      replaceFromStagingCommand(staging, abs, { recursive: options?.recursive }),
    );
  }

  async appendFile(path: string, content: FileContent): Promise<void> {
    const abs = this.#abs(path);
    const data = toWriteData(content);
    this.#checkSize(data, path);
    // Only the new bytes cross the network; the append itself runs in the sandbox.
    await this.#publishStaged('appendFile', path, data, staging => appendFromStagingCommand(staging, abs));
  }

  async deleteFile(path: string, options?: RemoveOptions): Promise<void> {
    const abs = this.#abs(path);
    try {
      await this.#command('deleteFile', deleteFileCommand(abs), path, {
        notFound: () => new FileNotFoundError(path),
        isDirectory: () => new IsDirectoryError(path),
      });
    } catch (error) {
      if (options?.force && error instanceof FileNotFoundError) return;
      throw error;
    }
  }

  /** One `cp` in the sandbox. A recursive copy keeps symlinks inside the tree as links; `overwrite: false` claims the destination atomically. */
  async copyFile(src: string, dest: string, options?: CopyOptions): Promise<void> {
    const command = copyCommand(this.#abs(src), this.#abs(dest), {
      recursive: options?.recursive,
      overwrite: options?.overwrite,
    });
    await this.#command('copyFile', command, `${src} -> ${dest}`, {
      notFound: () => new FileNotFoundError(src),
      isDirectory: () => new IsDirectoryError(src),
      destIsDirectory: () => new IsDirectoryError(dest),
      exists: () => new FileExistsError(dest),
      notDirectory: () => new NotDirectoryError(parentOf(dest)),
    }, MOVE_TIMEOUT_MS);
  }

  /** One `mv` in the sandbox, creating the destination's parent. */
  async moveFile(src: string, dest: string, options?: CopyOptions): Promise<void> {
    const srcAbs = this.#abs(src);
    const destAbs = this.#abs(dest);
    const command = options?.overwrite === false ? moveNoClobberCommand(srcAbs, destAbs) : moveCommand(srcAbs, destAbs);
    await this.#command('moveFile', command, `${src} -> ${dest}`, {
      notFound: () => new FileNotFoundError(src),
      exists: () => new FileExistsError(dest),
      destIsDirectory: () => new IsDirectoryError(dest),
      notDirectory: () => new NotDirectoryError(parentOf(dest)),
    }, MOVE_TIMEOUT_MS);
  }

  // ---------------------------------------------------------------------------
  // Directories
  // ---------------------------------------------------------------------------

  async mkdir(path: string, options?: { recursive?: boolean }): Promise<void> {
    const abs = this.#abs(path);
    await this.#command('mkdir', mkdirCommand(abs, options?.recursive !== false), path, {
      exists: () => new FileExistsError(path),
      notDirectory: () => new NotDirectoryError(parentOf(path)),
      parentNotFound: () => new DirectoryNotFoundError(parentOf(path)),
    });
  }

  async rmdir(path: string, options?: RemoveOptions): Promise<void> {
    const abs = this.#abs(path);
    try {
      await this.#command('rmdir', rmdirCommand(abs, options?.recursive === true), path, {
        notFound: () => new DirectoryNotFoundError(path),
        notDirectory: () => new NotDirectoryError(path),
        notEmpty: () => new DirectoryNotEmptyError(path),
      }, options?.recursive ? MOVE_TIMEOUT_MS : LOOK_TIMEOUT_MS);
    } catch (error) {
      if (options?.force && error instanceof DirectoryNotFoundError) return;
      throw error;
    }
  }

  /**
   * One `find` in the sandbox. Names are relative to `path` (`a/b.ts` when recursive), symlinks
   * are listed with their targets and typed by what they point at, and broken ones are left out.
   */
  async readdir(path: string, options?: ListOptions): Promise<FileEntry[]> {
    const maxDepth = options?.recursive ? (options.maxDepth ?? DEFAULT_MAX_DEPTH) + 1 : 1;
    const entries = await this.#list('readdir', path, { maxDepth });
    const result: FileEntry[] = [];
    for (const entry of entries) {
      if (entry.type === 'file' && !matchesExtension(entry.path, options?.extension)) continue;
      result.push(toFileEntry(entry, entry.path));
    }
    return result;
  }

  /**
   * The whole tree under `path` in one `find`, for list-style tools: hidden entries and
   * `skipDirectoryNames` are pruned in the sandbox, so a large `.git` or `node_modules` never
   * crosses the network. Symlinked directories are listed, not entered.
   */
  async walk(path: string, options?: WalkOptions): Promise<WalkEntry[]> {
    const entries = await this.#list('walk', path, {
      ...(options?.maxDepth !== undefined && Number.isFinite(options.maxDepth) && { maxDepth: options.maxDepth }),
      includeHidden: options?.includeHidden === true,
      keepGitignore: options?.includeGitignoreFiles === true,
      skipDirectoryNames: options?.skipDirectoryNames,
    });
    return entries.map(entry => ({ ...toFileEntry(entry, baseName(entry.path)), path: entry.path }));
  }

  async exists(path: string): Promise<boolean> {
    const result = await this.#exec(existsCommand(this.#abs(path)), LOOK_TIMEOUT_MS);
    if (result.exitCode === 0) return true;
    if (result.exitCode === 1) return false;
    throw new Error(`${LOG_PREFIX} exists ${path} failed (exit ${result.exitCode}): ${result.stderr.trim()}`);
  }

  /** Follows symlinks like `fs.stat`: the type, size and time are the target's. */
  async stat(path: string): Promise<FileStat> {
    const abs = this.#abs(path);
    const result = await this.#command('stat', statCommand(abs), path, { notFound: () => new FileNotFoundError(path) });
    const [type = 'f', size = '0', mtime = '0'] = result.stdout.trim().split(' ');
    const modifiedAt = parseFindTime(mtime);
    const isDir = type === 'd';
    return {
      name: baseName(abs),
      path: abs,
      type: isDir ? 'directory' : 'file',
      size: isDir ? 0 : Number(size) || 0,
      createdAt: modifiedAt,
      modifiedAt,
    };
  }

  // ---------------------------------------------------------------------------
  // Internals
  // ---------------------------------------------------------------------------

  #workdir(): string {
    return this.#sandbox.workingDirectory ?? `${this.#home}/work`;
  }

  /**
   * The absolute sandbox path for `path`: relative paths start in the working directory. `.` and
   * empty segments are dropped; `..` is kept, because only the sandbox knows where it leads after
   * a symlink (such paths are staged, since the files API refuses them).
   */
  #abs(path: string): string {
    if (path.includes('\0')) throw new FilesystemError(`Invalid path (NUL byte): ${JSON.stringify(path)}`, 'EINVAL', path);
    const full = path.startsWith('/') ? path : `${this.#workdir()}/${path}`;
    return `/${full.split('/').filter(segment => segment !== '' && segment !== '.').join('/')}`;
  }

  /** Whether the files API takes `abs` as it is: under its root, without `..`. */
  #direct(abs: string): boolean {
    return (abs === this.#home || abs.startsWith(`${this.#home}/`)) && !abs.split('/').includes('..');
  }

  #stagingPath(kind: string): string {
    return `${this.#home}/.cache/prized-mastra/${kind}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  }

  #checkSize(data: string | Uint8Array, path: string): void {
    // A UTF-8 string is at most 3 bytes per UTF-16 unit, so only long strings need encoding.
    const size = typeof data === 'string' ? (data.length * 3 <= MAX_FILE_BYTES ? 0 : Buffer.byteLength(data)) : data.byteLength;
    if (size > MAX_FILE_BYTES) throw new FileTooLargeError(path, MAX_FILE_BYTES, size);
  }

  /**
   * Run one SDK call on the sandbox: start it if needed, and if it turns out to be gone (deleted,
   * or past its run time) start a new one and run the call once more. Each call is retried on
   * its own; calls before it are never replayed.
   */
  async #call<T>(fn: (sandbox: Sandbox) => Promise<T>): Promise<T> {
    await this.#sandbox.ensureRunning();
    return this.#sandbox.retryOnDead(() => fn(this.#sandbox.sandbox));
  }

  #exec(script: string, timeoutMs: number): Promise<ExecResult> {
    // Every path in a file command is absolute; `/` keeps them working if the agent removes the
    // default working directory.
    return this.#call(sandbox => sandbox.exec(script, { cwd: '/', timeoutMs }) as Promise<ExecResult>);
  }

  /** Run a file command and turn its known exit codes into typed errors. */
  async #command(
    operation: string,
    script: string,
    subject: string,
    errors: ErrorMap,
    timeoutMs: number = LOOK_TIMEOUT_MS,
  ): Promise<ExecResult> {
    return this.#check(operation, await this.#exec(script, timeoutMs), subject, errors);
  }

  /** A command's result, or the typed error its exit code stands for. */
  #check(operation: string, result: ExecResult, subject: string, errors: ErrorMap): ExecResult {
    if (result.exitCode === 0) return result;
    for (const [outcome, code] of Object.entries(EXIT)) {
      const toError = errors[outcome as ExitOutcome];
      if (code === result.exitCode && toError) throw toError(result);
    }
    const stderr = result.stderr.trim();
    if (/Permission denied/i.test(stderr)) throw new PermissionError(subject, operation);
    throw new Error(`${LOG_PREFIX} ${operation} ${subject} failed (exit ${result.exitCode})${stderr ? `: ${stderr}` : ''}`);
  }

  async #list(
    operation: string,
    path: string,
    options: Parameters<typeof listCommand>[1],
  ): Promise<Array<{ type: 'file' | 'directory'; path: string; size: number; symlinkTarget?: string }>> {
    const result = await this.#command(operation, listCommand(this.#abs(path), options), path, {
      notFound: () => new DirectoryNotFoundError(path),
      notDirectory: () => new NotDirectoryError(path),
    });
    if (result.truncated) {
      // Past the per-command output limit the listing is incomplete: say so rather than return part of it.
      throw new FilesystemError(
        `Too many entries under ${path} to list in one call; list a narrower directory or a smaller depth.`,
        'E2BIG',
        path,
      );
    }
    const fields = result.stdout.split('\0');
    const entries: Array<{ type: 'file' | 'directory'; path: string; size: number; symlinkTarget?: string }> = [];
    for (let i = 0; i + 2 < fields.length; i += 3) {
      const [types = '', size = '0'] = fields[i]!.split(' ');
      const own = types[0];
      const resolved = types[1];
      // A broken link (N) or a loop (L) has no type to report.
      if (!own || !resolved || resolved === 'N' || resolved === 'L' || resolved === '?') continue;
      entries.push({
        type: resolved === 'd' ? 'directory' : 'file',
        path: fields[i + 1]!,
        size: own === 'f' ? Number(size) || 0 : -1,
        ...(own === 'l' && { symlinkTarget: fields[i + 2]! }),
      });
    }
    entries.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
    return entries;
  }

  /** Refuse a write over a file that changed since `expected` (its mtime when it was read). */
  async #checkMtime(abs: string, path: string, expected: Date): Promise<void> {
    const printed = (await this.#command('writeFile', mtimeCommand(abs), path, {})).stdout.trim();
    // A file that doesn't exist yet can't have been changed by someone else.
    if (printed === '') return;
    const actual = parseFindTime(printed);
    if (actual.getTime() !== expected.getTime()) throw new StaleFileError(path, expected, actual);
  }

  /**
   * Upload `data` to a staging file under the files API's root, then run the command that puts it
   * in place. The command deletes the staging file on every path it controls; if the upload fails
   * (it may still have landed) or the command never answered, delete it here.
   */
  async #publishStaged(
    operation: string,
    path: string,
    data: string | Uint8Array,
    command: (staging: string) => string,
  ): Promise<void> {
    const staging = this.#stagingPath('up');
    let result: ExecResult;
    try {
      await this.#call(sandbox => sandbox.files.write(staging, data));
      result = await this.#exec(command(staging), MOVE_TIMEOUT_MS);
    } catch (error) {
      await this.#removeQuietly(staging);
      throw error;
    }
    this.#check(operation, result, path, {
      isDirectory: () => new IsDirectoryError(path),
      exists: () => new FileExistsError(path),
      notDirectory: () => new NotDirectoryError(parentOf(path)),
      parentNotFound: () => new DirectoryNotFoundError(parentOf(path)),
      badLink: () => new FileNotFoundError(path),
      notRegular: () => new FilesystemError(`Not a regular file: ${path}`, 'EINVAL', path),
    });
  }

  /** Copy a file the files API can't reach to a staging file it can, download it, and delete the copy. */
  async #stagedRead(abs: string, path: string): Promise<Uint8Array> {
    const staging = this.#stagingPath('down');
    try {
      // The command leaves no copy behind when it fails.
      this.#check('readFile', await this.#exec(stageForReadCommand(abs, staging, MAX_FILE_BYTES), MOVE_TIMEOUT_MS), path, {
        isDirectory: () => new IsDirectoryError(path),
        notFound: () => new FileNotFoundError(path),
        notRegular: () => new FilesystemError(`Not a regular file: ${path}`, 'EINVAL', path),
        tooLarge: result => new FileTooLargeError(path, MAX_FILE_BYTES, Number(result.stdout.trim()) || undefined),
      });
    } catch (error) {
      if (!(error instanceof FilesystemError)) await this.#removeQuietly(staging);
      throw error;
    }
    try {
      return await this.#call(sandbox => sandbox.files.readBytes(staging));
    } finally {
      await this.#removeQuietly(staging);
    }
  }

  async #removeQuietly(path: string): Promise<void> {
    await this.#exec(removeCommand(path), LOOK_TIMEOUT_MS).catch(error => {
      this.logger.debug(`${LOG_PREFIX} Could not remove ${path}`, { error });
    });
  }

  async #isDirectory(abs: string): Promise<boolean> {
    return (await this.#exec(isDirectoryCommand(abs), LOOK_TIMEOUT_MS)).exitCode === 0;
  }

  /** A files API read error as the typed error a caller expects. */
  async #readError(error: unknown, abs: string, path: string): Promise<unknown> {
    switch (codeOf(error)) {
      case 'not_found':
        // retryOnDead already checked the sandbox itself is there: the file isn't.
        return new FileNotFoundError(path);
      case 'not_directory':
        // A file where a directory should be on the way (`file.txt/x`).
        return new FileNotFoundError(path);
      case 'not_regular_file':
        return (await this.#isDirectory(abs)) ? new IsDirectoryError(path) : new FilesystemError(`Not a regular file: ${path}`, 'EINVAL', path);
      case 'file_too_large':
        return new FileTooLargeError(path, MAX_FILE_BYTES);
      case 'permission_denied':
        return new PermissionError(path, 'readFile');
      default:
        return error;
    }
  }

}

function codeOf(error: unknown): string {
  return error instanceof PrizedError ? error.code : '';
}

/** The bytes to send: a string as is (sent as UTF-8), a Buffer or Uint8Array as exactly its own view. */
function toWriteData(content: FileContent): string | Uint8Array {
  if (typeof content === 'string') return content;
  return new Uint8Array(content.buffer, content.byteOffset, content.byteLength);
}

function baseName(path: string): string {
  return path.slice(path.lastIndexOf('/') + 1);
}

function parentOf(path: string): string {
  const trimmed = path.replace(/\/+$/, '');
  const cut = trimmed.lastIndexOf('/');
  if (cut < 0) return '.';
  return cut === 0 ? '/' : trimmed.slice(0, cut);
}

function matchesExtension(name: string, extension: ListOptions['extension']): boolean {
  if (!extension) return true;
  const base = baseName(name);
  const dot = base.lastIndexOf('.');
  const ext = dot > 0 ? base.slice(dot) : '';
  const wanted = Array.isArray(extension) ? extension : [extension];
  return wanted.some(e => e === ext || (ext !== '' && e === ext.slice(1)));
}

function toFileEntry(
  entry: { type: 'file' | 'directory'; size: number; symlinkTarget?: string },
  name: string,
): FileEntry {
  return {
    name,
    type: entry.type,
    ...(entry.type === 'file' && entry.size >= 0 && { size: entry.size }),
    ...(entry.symlinkTarget !== undefined && { isSymlink: true, symlinkTarget: entry.symlinkTarget }),
  };
}
