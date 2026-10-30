/**
 * Sandbox-backed versions of Pi's default tools: bash, read, write and edit.
 *
 * Pi's tool factories take pluggable operations, so each tool keeps Pi's own logic (truncation, edit
 * matching, rendering) and only the I/O goes to the sandbox. grep, find and ls are off by default in
 * Pi and always look at your machine, so with `--prized` they are turned off and blocked; the agent
 * searches with bash instead.
 */

import os from 'node:os';
import path from 'node:path';

import {
  type BashOperations,
  type EditOperations,
  type ExtensionAPI,
  type ExtensionContext,
  type ReadOperations,
  type ToolDefinition,
  type WriteOperations,
  createBashToolDefinition,
  createEditToolDefinition,
  createReadToolDefinition,
  createWriteToolDefinition,
} from '@earendil-works/pi-coding-agent';
import { CommandTimeoutError, type Sandbox } from '@prized/sdk';

import { shq } from './workspace.ts';

export const SANDBOX_HOME = '/home/prized';
const HOST_ONLY_TOOLS = ['grep', 'find', 'ls'];

export interface Target {
  /** Working directory in the sandbox. */
  cwd: string;
  /** Run `fn` with the awake sandbox; the sandbox does not pause while `fn` runs. */
  use<T>(fn: (sandbox: Sandbox) => Promise<T>): Promise<T>;
}

/** Pi expands `~` to your home directory. Point such paths at the sandbox's home. */
export function toSandboxPath(p: string, hostHome: string = os.homedir()): string {
  return p === hostHome || p.startsWith(`${hostHome}/`) ? `${SANDBOX_HOME}${p.slice(hostHome.length)}` : p;
}

/**
 * Register Pi's four tools so that, with `--prized`, they run in the sandbox. `target` gives the
 * session's sandbox, starting it on the first call. Without the flag the tools are Pi's own.
 */
export function registerTools(pi: ExtensionAPI, target: (ctx: ExtensionContext) => Promise<Target>): void {
  const enabled = () => pi.getFlag('prized') === true;
  const sandboxOf = async (ctx: ExtensionContext): Promise<Target> =>
    target(ctx).catch((err: Error) => {
      throw new Error(`The Prized sandbox is not available, so nothing ran on your machine. ${err.message}`);
    });

  const localCwd = process.cwd();
  const route = <T extends ToolDefinition<any, any, any>>(local: T, remote: (t: Target) => T): T => ({
    ...local,
    async execute(id, params, signal, onUpdate, ctx) {
      if (!enabled()) return local.execute(id, params, signal, onUpdate, ctx);
      const t = await sandboxOf(ctx);
      // Pi resolves paths against ctx.cwd, which is your machine's directory. Swap in the sandbox's.
      const sandboxCtx: typeof ctx = Object.create(ctx, { cwd: { value: t.cwd } });
      return remote(t).execute(id, params, signal, onUpdate, sandboxCtx);
    },
  });

  pi.registerTool(route(createBashToolDefinition(localCwd), (t) => createBashToolDefinition(t.cwd, { operations: bashOps(t) })));
  pi.registerTool(route(createReadToolDefinition(localCwd), (t) => createReadToolDefinition(t.cwd, { operations: readOps(t) })));
  pi.registerTool(route(createWriteToolDefinition(localCwd), (t) => createWriteToolDefinition(t.cwd, { operations: writeOps(t) })));
  const edit = route(createEditToolDefinition(localCwd), (t) =>
    createEditToolDefinition(t.cwd, { operations: editOps(t) }),
  );
  const renderEditCall = edit.renderCall;
  pi.registerTool({
    ...edit,
    // Pi previews an edit by reading the file from your disk, where the sandbox's files do not exist.
    // Skip the preview with --prized; the real diff still shows when the edit finishes.
    renderCall: renderEditCall
      ? (args, theme, context) => renderEditCall(args, theme, enabled() ? Object.create(context, { argsComplete: { value: false } }) : context)
      : undefined,
  });

  // Blocked even if you turn them back on: they would look at your machine, not the sandbox.
  pi.on('tool_call', (event) => {
    if (!enabled() || !HOST_ONLY_TOOLS.includes(event.toolName)) return undefined;
    return { block: true, reason: `${event.toolName} would search the user's machine, not the sandbox. Use bash.` };
  });

  // `!` commands. Pi passes your machine's cwd here, so pin the sandbox's.
  pi.on('user_bash', async (_event, ctx) => {
    if (!enabled()) return undefined;
    try {
      const t = await sandboxOf(ctx);
      return { operations: bashOps(t, t.cwd) };
    } catch (err) {
      return { result: { output: (err as Error).message, exitCode: 1, cancelled: false, truncated: false } };
    }
  });
}

/** Turn the host-only tools off for `--prized`. */
export function useSandboxTools(pi: ExtensionAPI): void {
  pi.setActiveTools(pi.getActiveTools().filter((name) => !HOST_ONLY_TOOLS.includes(name)));
}

/**
 * Commands run through the SDK's streamed exec: the sandbox runs the command in its own session with
 * its output in files, and the SDK polls them. So `npm run dev &` returns when the shell does, a
 * long build is not cut off by an idle connection, and an abort or timeout stops the command and
 * everything it started. Your environment is not forwarded: it would leak your secrets.
 */
export function bashOps(t: Target, cwdOverride?: string): BashOperations {
  return {
    async exec(command, cwd, { onData, signal, timeout }) {
      if (signal?.aborted) throw new Error('aborted');
      try {
        const r = await t.use((sandbox) =>
          sandbox.exec(command, {
            cwd: toSandboxPath(cwdOverride ?? cwd),
            timeoutMs: timeout ? Math.ceil(timeout * 1000) : 0,
            signal,
            onStdout: (chunk) => onData(Buffer.from(chunk)),
            onStderr: (chunk) => onData(Buffer.from(chunk)),
          }),
        );
        return { exitCode: r.exitCode };
      } catch (err) {
        if (signal?.aborted) throw new Error('aborted');
        if (err instanceof CommandTimeoutError) throw new Error(`timeout:${timeout}`);
        throw err;
      }
    },
  };
}

const IMAGE_TYPES = new Map([
  ['.png', 'image/png'],
  ['.jpg', 'image/jpeg'],
  ['.jpeg', 'image/jpeg'],
  ['.gif', 'image/gif'],
  ['.webp', 'image/webp'],
]);

export function readOps(t: Target): ReadOperations {
  return {
    readFile: async (p) => Buffer.from(await t.use((sb) => sb.files.readBytes(toSandboxPath(p)))),
    access: async (p) => {
      if (!(await t.use((sb) => sb.files.exists(toSandboxPath(p))))) throw new Error(`ENOENT: no such file '${p}'`);
    },
    detectImageMimeType: async (p) => IMAGE_TYPES.get(path.posix.extname(p).toLowerCase()) ?? null,
  };
}

export function writeOps(t: Target): WriteOperations {
  return {
    // files.write creates missing parent directories.
    writeFile: async (p, content) => {
      await t.use((sb) => sb.files.write(toSandboxPath(p), content));
    },
    mkdir: async (dir) => {
      await t.use((sb) => sb.exec(`mkdir -p ${shq(toSandboxPath(dir))}`, { timeoutMs: 30_000 }));
    },
  };
}

export function editOps(t: Target): EditOperations {
  const read = readOps(t);
  return { readFile: read.readFile, access: read.access, writeFile: writeOps(t).writeFile };
}
