/**
 * eve's `SandboxConnection` over one Prized sandbox: commands, file bytes, and `stop()` as a pause.
 *
 * A paused sandbox wakes on its own when a command or file request reaches it, so nothing here
 * resumes it first: a turn that never touches the sandbox never wakes it.
 */
import { randomBytes } from 'node:crypto';

import { CommandTimeoutError, PrizedError } from '@prized/sdk';
import type { ExecResult } from '@prized/sdk';
import type { SandboxConnection } from 'eve/sandbox/provider';
import type { SandboxProcess } from 'eve/sandbox';

import { EXISTS, EXIT, REMOVE, STAGE_READ, STAGE_WRITE } from './scripts.ts';
import type { CommandHandleLike, SandboxLike } from './types.ts';

/** The sandbox user's home: the only directory the files API reads and writes. */
export const HOME = '/home/prized';
/** Where commands start and relative paths resolve unless `workspaceRoot` says otherwise. */
export const DEFAULT_WORKSPACE_ROOT = `${HOME}/work`;
/** Staged copies for paths the files API can't reach (outside the home, or behind a link). */
const STAGING_DIR = `${HOME}/.cache/prized-eve`;
/** The prefix of every error this package throws itself. */
export const LOG_PREFIX = '[prized]';

/** Files API refusals that a command run as the sandbox user can still do: staged instead. */
const STAGE_CODES: ReadonlySet<string> = new Set(['path_outside_root', 'symlink_not_allowed', 'invalid_path']);
/** Codes that mean the sandbox (not a file in it) is gone. */
const GONE_CODES: ReadonlySet<string> = new Set(['not_found', 'sandbox_deleted']);
/** How long a staging command may take. */
const MOVE_TIMEOUT_MS = 120_000;
/** Files written at once by one `writeFiles` call (the edge takes 8 requests at a time per sandbox). */
const WRITE_CONCURRENCY = 4;

/** What the provider adds to eve's sandbox: the Prized sandbox id, for the SDK's other calls. */
export interface PrizedSandboxExtensions {
  /** The Prized sandbox id (`sbx_…`), for `Sandbox.connect()` from `@prized/sdk`. */
  readonly sandboxId: string;
}

export interface ConnectionOptions {
  /** Absolute directory where commands start; it must exist. */
  readonly workspaceRoot: string;
  /** Variables set on every command beneath the call's own `env` (eve's `AI_AGENT`). */
  readonly agentEnv: Readonly<Record<string, string>>;
}

export function codeOf(error: unknown): string {
  return error instanceof PrizedError ? error.code : '';
}

/** Whether an error says the sandbox is gone (deleted or never there). */
export function isGone(error: unknown): boolean {
  return GONE_CODES.has(codeOf(error));
}

/** An Error with a Node-style `code`, as eve's file tools expect from a filesystem. */
function fsError(code: string, message: string): Error {
  return Object.assign(new Error(`${LOG_PREFIX} ${message}`), { code });
}

function commandError(what: string, result: ExecResult): Error {
  const detail = result.stderr.trim() || result.stdout.trim() || `exit ${result.exitCode}`;
  return new Error(`${LOG_PREFIX} ${what}: ${detail}`);
}

/** Whether the files API reads and writes `path` itself: inside the home, with no `..`. */
function direct(path: string): boolean {
  return (path === HOME || path.startsWith(`${HOME}/`)) && !path.split('/').includes('..');
}

function stagingPath(kind: string): string {
  return `${STAGING_DIR}/${kind}-${randomBytes(8).toString('hex')}`;
}

/** Run `fn` over `items`, at most `limit` at a time; the first failure rejects (the rest still finish). */
async function eachLimit<T>(items: readonly T[], limit: number, fn: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  let failed: { error: unknown } | undefined;
  const worker = async (): Promise<void> => {
    while (next < items.length && failed === undefined) {
      const item = items[next++] as T;
      try {
        await fn(item);
      } catch (error) {
        failed ??= { error };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  if (failed !== undefined) throw failed.error;
}

/** A process whose output polling `stop()` can end, so a paused sandbox isn't woken by a poll. */
interface Tracked {
  stopPolling(): void;
}

export function createConnection(
  sandbox: SandboxLike,
  options: ConnectionOptions,
): SandboxConnection<PrizedSandboxExtensions> {
  const active = new Set<Tracked>();

  const run = (script: string, env: Record<string, string>, signal?: AbortSignal): Promise<ExecResult> =>
    sandbox.exec(script, { env, timeoutMs: MOVE_TIMEOUT_MS, ...(signal && { signal }) });

  const removeQuietly = async (path: string): Promise<void> => {
    await run(REMOVE, { EVE_DST: path, EVE_FORCE: '1' }).catch(() => undefined);
  };

  /** Copy a file the files API can't reach to a staging file it can, download that, and delete it. */
  const stagedRead = async (path: string, signal?: AbortSignal): Promise<Uint8Array | null> => {
    const staging = stagingPath('down');
    const result = await run(STAGE_READ, { EVE_SRC: path, EVE_DST: staging }, signal);
    switch (result.exitCode) {
      case 0:
        break;
      case EXIT.missing:
        return null;
      case EXIT.isDirectory:
        throw fsError('EISDIR', `${path} is a directory`);
      case EXIT.notRegular:
        throw fsError('EINVAL', `${path} is not a regular file`);
      default:
        throw commandError(`Could not read ${path}`, result);
    }
    try {
      return await sandbox.files.readBytes(staging);
    } finally {
      await removeQuietly(staging);
    }
  };

  /** Upload to a staging file, then copy it into place with a command (which removes the staging file). */
  const stagedWrite = async (path: string, content: Uint8Array, signal?: AbortSignal): Promise<void> => {
    const staging = stagingPath('up');
    let result: ExecResult;
    try {
      await sandbox.files.write(staging, content);
      result = await run(STAGE_WRITE, { EVE_SRC: staging, EVE_DST: path }, signal);
    } catch (error) {
      await removeQuietly(staging);
      throw error;
    }
    if (result.exitCode === EXIT.isDirectory) throw fsError('EISDIR', `${path} is a directory`);
    if (result.exitCode !== 0) throw commandError(`Could not write ${path}`, result);
  };

  const exists = async (path: string): Promise<boolean> => {
    const result = await run(EXISTS, { EVE_SRC: path });
    return result.exitCode === 0;
  };

  const toProcess = (handle: CommandHandleLike, signal: AbortSignal | undefined): SandboxProcess => {
    const encoder = new TextEncoder();
    let out!: ReadableStreamDefaultController<Uint8Array>;
    let err!: ReadableStreamDefaultController<Uint8Array>;
    const stdout = new ReadableStream<Uint8Array>({ start: c => void (out = c) });
    const stderr = new ReadableStream<Uint8Array>({ start: c => void (err = c) });
    const push = (c: ReadableStreamDefaultController<Uint8Array>, text: string): void => {
      try {
        c.enqueue(encoder.encode(text));
      } catch {
        // The reader cancelled the stream: the rest of its output is not wanted.
      }
    };
    const end = (c: ReadableStreamDefaultController<Uint8Array>, error?: unknown): void => {
      try {
        if (error === undefined) c.close();
        else c.error(error);
      } catch {
        // Already closed or cancelled.
      }
    };

    const polling = new AbortController();
    const tracked: Tracked = { stopPolling: () => polling.abort() };
    active.add(tracked);

    const kill = async (): Promise<void> => {
      try {
        await handle.kill('SIGKILL');
      } catch (error) {
        // A sandbox that is gone has no process left to kill.
        if (!isGone(error)) throw error;
      }
    };
    const onAbort = (): void => void kill().catch(() => undefined);
    signal?.addEventListener('abort', onAbort, { once: true });

    const settled = handle
      .wait({ onStdout: text => push(out, text), onStderr: text => push(err, text), signal: polling.signal })
      .then(
        result => result.exitCode,
        error => {
          // A command that ran past a time limit was killed; its exit code is -1.
          if (error instanceof CommandTimeoutError) return error.result.exitCode;
          if (polling.signal.aborted) {
            throw new Error(`${LOG_PREFIX} The sandbox was stopped while a command was running; its output stops here.`, {
              cause: error,
            });
          }
          throw error;
        },
      )
      .then(
        exitCode => {
          end(out);
          end(err);
          void handle.cleanup().catch(() => undefined);
          return { exitCode };
        },
        error => {
          end(out, error);
          end(err, error);
          throw error;
        },
      )
      .finally(() => {
        active.delete(tracked);
        signal?.removeEventListener('abort', onAbort);
      });
    // eve may never call wait(); a failure must not become an unhandled rejection.
    settled.catch(() => undefined);

    return { pid: handle.pid, stdout, stderr, wait: () => settled, kill };
  };

  return {
    workspaceRoot: options.workspaceRoot,
    extensions: { sandboxId: sandbox.id },

    async spawn({ command, workingDirectory, env, signal, stdin }) {
      if (stdin) {
        throw new Error(
          `${LOG_PREFIX} Prized sandboxes don't take stdin yet: start the process without stdin, or pass its input in a file.`,
        );
      }
      signal?.throwIfAborted();
      const handle = await sandbox.exec(['bash', '-lc', command], {
        background: true,
        cwd: workingDirectory,
        env: { ...options.agentEnv, ...env },
        ...(signal && { signal }),
      });
      return toProcess(handle, signal);
    },

    async readFile({ path, signal }) {
      signal?.throwIfAborted();
      if (direct(path)) {
        try {
          return await sandbox.files.readBytes(path);
        } catch (error) {
          const code = codeOf(error);
          if (code === 'not_found' || code === 'not_directory') {
            // A missing file and a missing sandbox both answer not_found: a command tells them
            // apart (it fails if the sandbox is gone), and reaches a file behind a link too.
            return (await exists(path)) ? await stagedRead(path, signal) : null;
          }
          if (code === 'not_regular_file') throw fsError('EISDIR', `${path} is a directory or not a regular file`);
          if (!STAGE_CODES.has(code)) throw error;
        }
      }
      return await stagedRead(path, signal);
    },

    async writeFiles({ files, signal }) {
      signal?.throwIfAborted();
      await eachLimit(files, WRITE_CONCURRENCY, async ({ path, content }) => {
        signal?.throwIfAborted();
        if (direct(path)) {
          try {
            await sandbox.files.write(path, content);
            return;
          } catch (error) {
            const code = codeOf(error);
            if (code === 'unsupported_file_type') throw fsError('EISDIR', `${path} is a directory`);
            if (!STAGE_CODES.has(code)) throw error;
          }
        }
        await stagedWrite(path, content, signal);
      });
    },

    async removePath({ path, force, recursive, signal }) {
      signal?.throwIfAborted();
      const result = await run(
        REMOVE,
        { EVE_DST: path, EVE_FORCE: force ? '1' : '0', EVE_RECURSIVE: recursive ? '1' : '0' },
        signal,
      );
      if (result.exitCode === 0) return;
      if (result.exitCode === EXIT.missing) throw fsError('ENOENT', `${path} does not exist`);
      if (result.exitCode === EXIT.notEmpty) throw fsError('ENOTEMPTY', `${path} is a directory that is not empty`);
      throw commandError(`Could not remove ${path}`, result);
    },

    /** Pause: memory and disk are kept and only storage is billed. A no-op when already paused or gone. */
    async stop() {
      // Output polls would wake a paused sandbox, so they end first.
      for (const tracked of active) tracked.stopPolling();
      try {
        await sandbox.refresh();
        if (sandbox.state === 'running' || sandbox.state === 'starting') await sandbox.pause();
      } catch (error) {
        if (!isGone(error)) throw error;
      }
    },
  };
}
