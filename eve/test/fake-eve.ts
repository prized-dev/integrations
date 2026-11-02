/**
 * A stand-in for eve's side of `defineSandboxProvider`: the contexts eve passes to `prepare()`,
 * `start()`, `resume()` and `onSessionEnd()`, `ctx.populate()`, and a `run()` built on the
 * connection's `spawn()` the way eve builds its own. It follows eve's documented contract
 * (docs/sandbox/custom-providers.mdx) and is typed with eve's published types, so a change to
 * those types breaks the typecheck; it loads nothing from eve at run time.
 */
import { createHash } from 'node:crypto';

import type {
  SandboxConnection,
  SandboxIdentity,
  SandboxPrepareContext,
  SandboxSessionContext,
} from 'eve/sandbox/provider';

import type { EveRuntime } from '../src/provider.ts';

/** eve's `SandboxSessionGoneError`: eve recognises it by `name`, as this one has. */
export class SandboxSessionGoneError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SandboxSessionGoneError';
  }
}

/** eve's `SandboxTemplateNotProvisionedError`, recognised by `name`, `providerName` and `templateKey`. */
export class SandboxTemplateNotProvisionedError extends Error {
  readonly providerName: string;
  readonly templateKey: string;
  constructor(input: { readonly providerName: string; readonly reason?: string; readonly templateKey: string }) {
    super(`Sandbox template "${input.templateKey}" for provider "${input.providerName}" is not available. ${input.reason ?? ''}`);
    this.name = 'SandboxTemplateNotProvisionedError';
    this.providerName = input.providerName;
    this.templateKey = input.templateKey;
  }
}

export const EVE: EveRuntime = {
  SANDBOX_AGENT_ENV: Object.freeze({ AI_AGENT: 'eve' }),
  SandboxSessionGoneError,
  SandboxTemplateNotProvisionedError,
};

export function sessionContext(id: string): SandboxSessionContext {
  return {
    session: { id } as SandboxSessionContext['session'],
    kind: 'session',
    storagePath: '/tmp/eve-storage',
    resolveProjectPath: path => `/project/${path}`,
  };
}

export interface RunResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

async function drain(stream: ReadableStream<Uint8Array>): Promise<string> {
  return await new Response(stream).text();
}

/** eve's `sandbox.run()`: spawn in the workspace root (or `workingDirectory`), read both streams, wait. */
export async function run(
  connection: SandboxConnection<object>,
  command: string,
  options: { env?: Record<string, string>; workingDirectory?: string; signal?: AbortSignal } = {},
): Promise<RunResult> {
  const process = await connection.spawn({
    command,
    env: options.env ?? {},
    workingDirectory: options.workingDirectory ?? connection.workspaceRoot ?? '/workspace',
    ...(options.signal && { signal: options.signal }),
  });
  const [stdout, stderr, { exitCode }] = await Promise.all([drain(process.stdout), drain(process.stderr), process.wait()]);
  return { exitCode, stdout, stderr };
}

/** Read a file through the connection as text, or null. */
export async function readText(connection: SandboxConnection<object>, path: string): Promise<string | null> {
  const bytes = await connection.readFile({ path });
  if (bytes === null) return null;
  if (bytes instanceof Uint8Array) return new TextDecoder().decode(bytes);
  return await drain(bytes);
}

export interface PrepareInput {
  /** Workspace files, relative to the workspace root. */
  workspace?: Record<string, string>;
  /** Skill files, relative to `$HOME/.agents/skills`. */
  skills?: Record<string, string>;
  /** The authored `prepare` callback, given eve's `run()` over the build connection. */
  prepare?: (sandbox: { run(command: string): Promise<RunResult> }) => Promise<void>;
  /** Stands in for the sandbox file's source revision, which eve folds into every template key. */
  revision?: string;
}

export type FakePrepareContext = SandboxPrepareContext & { readonly logs: string[]; populated: number };

/** The context eve passes to `prepare()` during `eve build`. */
export function prepareContext(input: PrepareInput = {}): FakePrepareContext {
  const workspace = input.workspace ?? {};
  const skills = input.skills ?? {};
  const logs: string[] = [];
  const all = { ...Object.fromEntries(Object.entries(workspace).map(([p, c]) => [`workspace/${p}`, c])), ...Object.fromEntries(Object.entries(skills).map(([p, c]) => [`skills/${p}`, c])) };
  const ctx: FakePrepareContext = {
    logs,
    populated: 0,
    files: {
      list: async () => Object.keys(all),
      read: async path => new TextEncoder().encode(all[path] ?? ''),
      readText: async path => all[path] ?? '',
    },
    isEmpty: Object.keys(all).length === 0 && input.prepare === undefined,
    storagePath: '/tmp/eve-storage',
    log: message => void logs.push(message),
    resolveProjectPath: path => `/project/${path}`,
    resourcesDirectory: async () => undefined,
    templateKey(identity: SandboxIdentity): string {
      if ('resources' in identity || 'sourceRevision' in identity) {
        throw new Error('templateKey identity cannot set the reserved keys resources or sourceRevision');
      }
      return createHash('sha256')
        .update(JSON.stringify({ identity, all, revision: input.revision ?? 'r1' }))
        .digest('hex');
    },
    async populate(connection) {
      ctx.populated++;
      const root = connection.workspaceRoot ?? '/workspace';
      const home = (await run(connection, 'printf %s "$HOME"', { workingDirectory: root })).stdout;
      const encoder = new TextEncoder();
      await connection.writeFiles({
        files: [
          ...Object.entries(workspace).map(([p, c]) => ({ path: `${root}/${p}`, content: encoder.encode(c) })),
          ...Object.entries(skills).map(([p, c]) => ({ path: `${home}/.agents/skills/${p}`, content: encoder.encode(c) })),
        ],
      });
      if (input.prepare) await input.prepare({ run: command => run(connection, command) });
    },
  };
  return ctx;
}
