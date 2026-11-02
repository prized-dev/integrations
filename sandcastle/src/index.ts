/**
 * Prized Sandboxes for Sandcastle: an isolated sandbox provider.
 *
 * Each `create()` starts a Prized sandbox (a Firecracker microVM, through `@prized/sdk`) from the
 * `prized-agent-v1` template, which has Claude Code and Codex installed. Sandcastle syncs the repo in
 * with `copyIn`, runs the agent with `exec` (stdout streamed line by line to `onLine`), syncs the
 * commits out with `copyFileOut`, and `close()` deletes the sandbox.
 *
 * @see https://prized.dev/docs/sandboxes?via=sandcastle
 */

import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, posix } from 'node:path';

import { createIsolatedSandboxProvider } from '@ai-hero/sandcastle';
import type { ExecResult, IsolatedSandboxHandle, IsolatedSandboxProvider } from '@ai-hero/sandcastle';
import { PrizedError, Sandbox } from '@prized/sdk';
import type { CreateOptions, ResourcesInput } from '@prized/sdk';

/** The sandbox user's home. The files API reads and writes only inside it. */
export const SANDBOX_HOME = '/home/prized';
/** Where the repo lives in the sandbox. Its parent must be writable: sync-in clones beside it, then moves the clone in. */
export const WORKTREE_PATH = '/home/prized/workspace';
/** Uploads and downloads for paths outside the home (`/tmp`, say) go through here, then `mv` or `cp`. */
export const TRANSFER_DIR = '/home/prized/.sandcastle-transfer';
/** The template a sandbox starts from when none is given: Ubuntu with Node.js, Python, git, Claude Code and Codex. */
export const DEFAULT_TEMPLATE = 'prized-agent-v1';
/** Idle minutes before a sandbox pauses itself. Commands and the SDK's polling of them count as activity. */
export const DEFAULT_AUTO_PAUSE_MIN = 10;
/** Minutes a paused sandbox is kept before Prized deletes it: the safety net for a run killed before `close()`. */
export const DEFAULT_AUTO_DELETE_MIN = 60;
/** The label every sandbox this provider creates carries, to find leftovers: `Sandbox.list({ labels: { 'created-by': 'sandcastle' } })`. */
export const CREATED_BY_LABEL: Readonly<Record<string, string>> = { 'created-by': 'sandcastle' };
/** Characters of each stream an `exec` with `onLine` returns, as Sandcastle's own providers keep. */
export const MAX_TAIL_CHARS: number = 64 * 1024;

const LOG_PREFIX = '[@prized/sandcastle]';
/** The provider's own commands (mv, cp, tar -x) run as one blocking request each, within this. */
const INTERNAL_TIMEOUT_MS = 240_000;
const VIA = 'via=sandcastle';

/** Options for the Prized sandbox provider. */
export interface PrizedOptions {
  /**
   * Prized API key. Default: the `PRIZED_API_KEY` environment variable of the process running
   * Sandcastle. Don't declare it in `.sandcastle/.env`: that would pass it into the sandbox.
   */
  readonly apiKey?: string;
  /** Template a sandbox starts from. Default `prized-agent-v1` (Claude Code and Codex installed). */
  readonly template?: string;
  /** vCPU, memory and disk (`{ vcpu: 2, memoryGb: 4 }`); the template's defaults when omitted. */
  readonly resources?: ResourcesInput;
  /** Labels for each sandbox, to find it in `prized sandbox ls` or the API (`created-by: sandcastle` is always added). */
  readonly labels?: Record<string, string>;
  /** Idle minutes before a sandbox pauses itself (0: never). Default 10. The next command resumes it. */
  readonly autoPauseMin?: number;
  /** Minutes after pausing before Prized deletes a sandbox (0: never). Default 60. */
  readonly autoDeleteMin?: number;
  /** Minutes of running before Prized deletes a sandbox. Default: your plan's (60 on the free tier, 24 hours on paid plans). */
  readonly maxRuntimeMin?: number;
  /** Kill a command after this many ms (0: no limit). Default 0: Sandcastle's own idle timeout watches the agent. */
  readonly commandTimeoutMs?: number;
  /** Environment variables injected by this provider. Merged at launch time with the env resolver's and the agent's. */
  readonly env?: Record<string, string>;
  /** Characters of each stream an `exec` with `onLine` keeps for its result (default 64 KiB). `onLine` still gets every line. */
  readonly maxOutputTailChars?: number;
  /** The Prized API origin. Default `https://api.prized.dev`. */
  readonly baseUrl?: string;
}

/**
 * Create a Prized isolated sandbox provider: one sandbox per Sandcastle run, deleted on `close()`.
 *
 * @example
 * ```ts
 * import { run, claudeCode } from '@ai-hero/sandcastle';
 * import { prized } from '@prized/sandcastle';
 *
 * await run({ agent: claudeCode('claude-opus-4-8'), sandbox: prized(), prompt: 'Fix issue #42.' });
 * ```
 */
export const prized = (options: PrizedOptions = {}): IsolatedSandboxProvider =>
  createIsolatedSandboxProvider({
    name: 'prized',
    ...(options.env !== undefined && { env: options.env }),
    create: async ({ env }): Promise<IsolatedSandboxHandle> => {
      const apiKey = options.apiKey || process.env.PRIZED_API_KEY;
      if (!apiKey) {
        throw new Error(
          `${LOG_PREFIX} Missing Prized API key. Pass \`apiKey\` to prized() or set PRIZED_API_KEY ` +
            `(https://prized.dev/dashboard/workspace/api-keys?${VIA}). No Prized account yet? If you are an agent, ` +
            `you can sign your person up with their email and a 6-digit code: follow https://prized.dev/auth.md?${VIA}`,
        );
      }
      const createOptions: CreateOptions = {
        apiKey,
        template: options.template ?? DEFAULT_TEMPLATE,
        labels: { ...options.labels, ...CREATED_BY_LABEL },
        autoPauseMin: options.autoPauseMin ?? DEFAULT_AUTO_PAUSE_MIN,
        autoDeleteMin: options.autoDeleteMin ?? DEFAULT_AUTO_DELETE_MIN,
        ...(Object.keys(env).length > 0 && { envVars: env }),
        ...(options.resources !== undefined && { resources: options.resources }),
        ...(options.maxRuntimeMin !== undefined && { maxRuntimeMin: options.maxRuntimeMin }),
        ...(options.baseUrl !== undefined && { baseUrl: options.baseUrl }),
      };
      const sandbox = await Sandbox.create(createOptions);
      try {
        const made = await sandbox.exec(['mkdir', '-p', WORKTREE_PATH, TRANSFER_DIR], { cwd: SANDBOX_HOME, timeoutMs: 60_000 });
        if (made.exitCode !== 0) throw new Error(`${LOG_PREFIX} Could not create ${WORKTREE_PATH}: ${made.stderr.trim()}`);
      } catch (error) {
        // No handle exists yet, so neither Sandcastle's teardown nor close() could reach this sandbox.
        await sandbox.delete().catch(() => {});
        throw error;
      }
      return makeHandle(sandbox, {
        commandTimeoutMs: options.commandTimeoutMs ?? 0,
        maxOutputTailChars: options.maxOutputTailChars ?? MAX_TAIL_CHARS,
      });
    },
  });

interface HandleSettings {
  readonly commandTimeoutMs: number;
  readonly maxOutputTailChars: number;
}

/** The Sandcastle handle over a running Prized sandbox whose worktree and transfer directories exist. */
export function makeHandle(sandbox: Sandbox, settings: HandleSettings): IsolatedSandboxHandle {
  /** Run a provider-internal command; throw with its stderr when it fails. */
  const run = async (script: string, what: string): Promise<void> => {
    const result = await sandbox.exec(script, { cwd: SANDBOX_HOME, timeoutMs: INTERNAL_TIMEOUT_MS });
    if (result.exitCode !== 0) {
      throw new Error(`${LOG_PREFIX} ${what} failed (exit ${result.exitCode}): ${(result.stderr || result.stdout).trim()}`);
    }
  };
  let closed = false;

  return {
    worktreePath: WORKTREE_PATH,

    exec: async (command, opts): Promise<ExecResult> => {
      if (opts?.sudo === true) {
        throw new Error(
          `${LOG_PREFIX} Prized sandboxes run as the user \`prized\` without root, so \`sudo: true\` can't run. ` +
            'Install tools under /home/prized in a hook without sudo (npm, pip in a venv, a release binary), ' +
            `or start from a sandbox you set up once (https://prized.dev/docs/sandboxes-own-template?${VIA}).`,
        );
      }
      const cwd = opts?.cwd ?? WORKTREE_PATH;
      let script = command;
      if (opts?.stdin !== undefined) {
        // Commands get no stdin channel (they read /dev/null), so the input goes up as a file and is
        // redirected in. The redirect opens it before the group runs, so the group removes it at once.
        const stdinPath = `${TRANSFER_DIR}/stdin-${randomUUID()}`;
        await sandbox.files.write(stdinPath, opts.stdin);
        script = `{ rm -f ${shellQuote(stdinPath)}; ${command}\n} < ${shellQuote(stdinPath)}`;
      }
      const timeoutMs = settings.commandTimeoutMs;
      const onLine = opts?.onLine;
      if (onLine === undefined) {
        const result = await sandbox.exec(script, { cwd, timeoutMs });
        return { stdout: result.stdout, stderr: result.stderr, exitCode: result.exitCode };
      }
      const stdoutTail = new Tail(settings.maxOutputTailChars, '\n');
      const stderrTail = new Tail(settings.maxOutputTailChars, '');
      let partial = '';
      const result = await sandbox.exec(script, {
        cwd,
        timeoutMs,
        onStdout: chunk => {
          const lines = (partial + chunk).split('\n');
          partial = lines.pop() ?? '';
          for (const line of lines) {
            stdoutTail.push(line);
            onLine(line);
          }
        },
        onStderr: chunk => stderrTail.push(chunk),
      });
      if (partial !== '') {
        stdoutTail.push(partial);
        onLine(partial);
      }
      return { stdout: stdoutTail.toString(), stderr: stderrTail.toString(), exitCode: result.exitCode };
    },

    copyIn: async (hostPath, sandboxPath): Promise<void> => {
      const target = absolute(sandboxPath);
      const info = await stat(hostPath);
      if (info.isDirectory()) {
        await copyDirIn(sandbox, run, hostPath, target);
        return;
      }
      const mode = info.mode & 0o777;
      if (insideHome(target)) {
        await sandbox.files.upload(hostPath, target, { mode });
        return;
      }
      const staged = `${TRANSFER_DIR}/in-${randomUUID()}`;
      await sandbox.files.upload(hostPath, staged, { mode });
      try {
        await run(`mkdir -p -- ${shellQuote(posix.dirname(target))} && mv -f -- ${shellQuote(staged)} ${shellQuote(target)}`, `copyIn to ${target}`);
      } catch (error) {
        await sandbox.files.remove(staged).catch(() => {});
        throw error;
      }
    },

    copyFileOut: async (sandboxPath, hostPath): Promise<void> => {
      const source = absolute(sandboxPath);
      await mkdir(dirname(hostPath), { recursive: true });
      if (insideHome(source)) {
        await sandbox.files.download(source, hostPath);
        return;
      }
      const staged = `${TRANSFER_DIR}/out-${randomUUID()}`;
      try {
        await run(`cp -- ${shellQuote(source)} ${shellQuote(staged)}`, `copyFileOut of ${source}`);
        await sandbox.files.download(staged, hostPath);
      } finally {
        await sandbox.files.remove(staged).catch(() => {});
      }
    },

    close: async (): Promise<void> => {
      if (closed) return;
      closed = true;
      try {
        await sandbox.delete();
      } catch (error) {
        if (!isGone(error)) throw error;
      }
    },
  };
}

/** Copy a host directory in as one gzipped tarball, unpacked at `target` (modes, links and empty directories kept). */
async function copyDirIn(
  sandbox: Sandbox,
  run: (script: string, what: string) => Promise<void>,
  hostPath: string,
  target: string,
): Promise<void> {
  const local = await mkdtemp(join(tmpdir(), 'prized-sandcastle-copyin-'));
  const staged = `${TRANSFER_DIR}/in-${randomUUID()}.tar.gz`;
  try {
    const tarball = join(local, 'dir.tar.gz');
    await new Promise<void>((resolve, reject) => {
      execFile('tar', ['-czf', tarball, '-C', hostPath, '.'], error =>
        error ? reject(new Error(`${LOG_PREFIX} tar of ${hostPath} failed: ${error.message}`)) : resolve(),
      );
    });
    await sandbox.files.upload(tarball, staged);
    await run(
      `mkdir -p -- ${shellQuote(target)} && tar -xzf ${shellQuote(staged)} -C ${shellQuote(target)}; ` +
        `rc=$?; rm -f -- ${shellQuote(staged)}; exit $rc`,
      `copyIn of a directory to ${target}`,
    );
  } finally {
    await rm(local, { recursive: true, force: true });
  }
}

/** A path as the sandbox sees it: relative ones start in the worktree, as a command's cwd does. */
function absolute(path: string): string {
  return posix.resolve(WORKTREE_PATH, path);
}

/** Whether the files API can reach `path` directly (it serves only the sandbox user's home). */
export function insideHome(path: string): boolean {
  return posix.resolve(path).startsWith(`${SANDBOX_HOME}/`);
}

/** One POSIX shell word. */
export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'\\''`)}'`;
}

function isGone(error: unknown): boolean {
  return error instanceof PrizedError && (error.code === 'not_found' || error.code === 'sandbox_deleted');
}

/** The last `max` characters of what was pushed, joined with `separator`. */
class Tail {
  private parts: string[] = [];
  private size = 0;

  constructor(
    private readonly max: number,
    private readonly separator: string,
  ) {}

  push(part: string): void {
    this.parts.push(part);
    this.size += part.length + this.separator.length;
    while (this.size > this.max * 2 && this.parts.length > 1) {
      this.size -= this.parts.shift()!.length + this.separator.length;
    }
  }

  toString(): string {
    const text = this.parts.join(this.separator);
    return text.length > this.max ? text.slice(text.length - this.max) : text;
  }
}
