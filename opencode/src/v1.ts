/**
 * The plugin for OpenCode 1 (`opencode-ai`, plugin API `@opencode-ai/plugin`). Line numbers below are
 * OpenCode v1.19.5; see DESIGN.md.
 *
 * - Tools: a plugin tool with a built-in's name replaces it (the registry lists built-ins, then plugin
 *   tools, and the session keys them by id: packages/opencode/src/tool/registry.ts L256-259,
 *   packages/opencode/src/session/tools.ts L92-99). So bash, read, write, edit, glob, grep and apply_patch
 *   run in the sandbox, and the built-in versions are never offered to the model.
 * - Pause: `session.status` and `session.idle` events (packages/opencode/src/session/status.ts L39-46).
 * - Fork: OpenCode copies a forked session's messages and tool parts (packages/opencode/src/session/session.ts
 *   L691-733), so the copy carries the parent's sandbox id in the tool results' metadata.
 * - Commands: `/prized` and `/prized-pull`, added through the config hook and handled in
 *   `command.execute.before` (packages/opencode/src/session/prompt.ts L1460-1464) by the plugin itself.
 */

import { realpath } from 'node:fs/promises';

import { Sandbox } from '@prized/sdk';
import { z } from 'zod';

import { type Options, readConfig, readEnv } from './config.ts';
import { layoutFor } from './paths.ts';
import { type Host, type Sdk, Sandboxes, type SandboxRef } from './sandboxes.ts';
import { type Call, DEFAULT_TIMEOUT_MS, DESCRIPTIONS, type Result, Tools } from './tools.ts';
import { gitRoot } from './workspace.ts';

/** The slice of OpenCode 1's plugin input this plugin uses (`PluginInput`, packages/plugin/src/index.ts L56-66). */
export interface V1Input {
  directory: string;
  worktree?: string;
  client: {
    session: {
      get(o: { path: { id: string } }): Promise<{ data?: { id: string; parentID?: string } | undefined }>;
      messages(o: { path: { id: string } }): Promise<{ data?: Array<{ parts: ReadonlyArray<unknown> }> | undefined }>;
    };
    tui?: { showToast(o: { body: { title?: string; message: string; variant: 'info' | 'success' | 'warning' | 'error' } }): Promise<unknown> };
    app?: { log(o: { body: { service: string; level: 'debug' | 'info' | 'warn' | 'error'; message: string } }): Promise<unknown> };
  };
}

/** `ToolContext`, packages/plugin/src/tool.ts L3-20. */
interface V1ToolContext {
  sessionID: string;
  abort: AbortSignal;
  metadata(input: { title?: string; metadata?: { [k: string]: unknown } }): void;
  ask(input: { permission: string; patterns: string[]; always: string[]; metadata: { [k: string]: unknown } }): Promise<void>;
}

/** Tools that act on your machine whatever we do, so with the sandbox on they are refused. */
const HOST_ONLY = new Set(['lsp', 'list', 'multiedit', 'patch']);
const SERVICE = 'opencode-prized';

const SYSTEM_NOTE = (hostDir: string, dir: string) =>
  `Your bash, read, write, edit, glob, grep and apply_patch tools run in a Prized sandbox (a Linux microVM, user prized, no sudo) that holds a copy of the project, not on the user's machine. ` +
  `The project folder ${hostDir} is at ${dir} in the sandbox; paths under either name the same files there. ` +
  'Processes you start keep running between calls, and the sandbox pauses between turns with its memory. ' +
  "The user's environment variables and git ignored files (.env) are not in the sandbox. Your changes stay in the sandbox until the user runs /prized-pull; a server you start has no URL the user can open.";

function call(ctx: V1ToolContext): Call {
  return {
    sessionID: ctx.sessionID,
    abort: ctx.abort,
    progress: (u) => {
      try {
        ctx.metadata(u);
      } catch {
        // a progress update is not worth failing the tool for
      }
    },
    ask: (req) => ctx.ask(req),
  };
}

const out = (r: Result) => ({ title: r.title, output: r.output, metadata: r.metadata, ...(r.attachments ? { attachments: r.attachments } : {}) });

export function createV1Plugin(deps: { sdk?: Sdk; env?: NodeJS.ProcessEnv; limits?: { maxUploadBytes?: number } } = {}) {
  return async function server(input: V1Input, options?: Options) {
    const config = readConfig(options);
    const env = readEnv(deps.env ?? process.env);
    const hostCwd = await realpath(input.directory);
    const layout = layoutFor(hostCwd, await gitRoot(hostCwd));
    const log = (level: 'info' | 'warn' | 'error', message: string) => void input.client.app?.log({ body: { service: SERVICE, level, message } }).catch(() => undefined);
    const host: Host = {
      async rootOf(sessionID) {
        let id = sessionID;
        for (let i = 0; i < 16; i += 1) {
          const s = (await input.client.session.get({ path: { id } })).data;
          if (!s?.parentID) return id;
          id = s.parentID;
        }
        return id;
      },
      async inherited(sessionID) {
        const msgs = (await input.client.session.messages({ path: { id: sessionID } })).data ?? [];
        for (const m of [...msgs].reverse()) {
          for (const part of [...m.parts].reverse()) {
            const p = part as { type?: string; state?: { metadata?: { prized?: Partial<SandboxRef> } } };
            const rec = p.type === 'tool' ? p.state?.metadata?.prized : undefined;
            if (rec && typeof rec.sandboxId === 'string' && typeof rec.owner === 'string') return { sandboxId: rec.sandboxId, owner: rec.owner };
          }
        }
        return undefined;
      },
      notify(text, level) {
        const message = text.startsWith('Prized') ? text : `Prized: ${text}`;
        log(level === 'warning' ? 'warn' : level, message);
        void input.client.tui?.showToast({ body: { message, variant: level } }).catch(() => undefined);
      },
    };
    const sandboxes = new Sandboxes(deps.sdk ?? { Sandbox }, config, env, layout, host, deps.limits);
    const tools = new Tools(sandboxes);
    const s = z;

    return {
      tool: {
        bash: {
          description: DESCRIPTIONS.bash,
          args: {
            command: s.string().describe('The command to execute'),
            timeout: s.number().int().positive().optional().describe(`Optional timeout in milliseconds (default ${DEFAULT_TIMEOUT_MS})`),
            workdir: s.string().optional().describe("The working directory to run the command in. Defaults to the project folder. Use this instead of 'cd' commands."),
            description: s.string().optional().describe('Clear, concise description of what this command does in 5-10 words'),
          },
          execute: async (args: { command: string; timeout?: number; workdir?: string; description?: string }, ctx: V1ToolContext) => out(await tools.bash(args, call(ctx))),
        },
        read: {
          description: DESCRIPTIONS.read,
          args: {
            filePath: s.string().describe('The absolute path to the file or directory to read'),
            offset: s.number().int().nonnegative().optional().describe('The line number to start reading from (1-indexed)'),
            limit: s.number().int().nonnegative().optional().describe('The maximum number of lines to read (defaults to 2000)'),
          },
          execute: async (args: { filePath: string; offset?: number; limit?: number }, ctx: V1ToolContext) => out(await tools.read(args, call(ctx))),
        },
        write: {
          description: DESCRIPTIONS.write,
          args: {
            content: s.string().describe('The content to write to the file'),
            filePath: s.string().describe('The absolute path to the file to write (must be absolute, not relative)'),
          },
          execute: async (args: { content: string; filePath: string }, ctx: V1ToolContext) => out(await tools.write(args, call(ctx))),
        },
        edit: {
          description: DESCRIPTIONS.edit,
          args: {
            filePath: s.string().describe('The absolute path to the file to modify'),
            oldString: s.string().describe('The text to replace'),
            newString: s.string().describe('The text to replace it with (must be different from oldString)'),
            replaceAll: s.boolean().optional().describe('Replace all occurrences of oldString (default false)'),
          },
          execute: async (args: { filePath: string; oldString: string; newString: string; replaceAll?: boolean }, ctx: V1ToolContext) => out(await tools.edit(args, call(ctx))),
        },
        glob: {
          description: DESCRIPTIONS.glob,
          args: {
            pattern: s.string().describe('The glob pattern to match files against'),
            path: s.string().optional().describe('The directory to search in. Omit it for the project folder.'),
          },
          execute: async (args: { pattern: string; path?: string }, ctx: V1ToolContext) => out(await tools.glob(args, call(ctx))),
        },
        grep: {
          description: DESCRIPTIONS.grep,
          args: {
            pattern: s.string().describe('The regex pattern to search for in file contents'),
            path: s.string().optional().describe('The directory to search in. Defaults to the project folder.'),
            include: s.string().optional().describe('File pattern to include in the search (e.g. "*.js", "*.{ts,tsx}")'),
          },
          execute: async (args: { pattern: string; path?: string; include?: string }, ctx: V1ToolContext) => out(await tools.grep(args, call(ctx))),
        },
        apply_patch: {
          description: DESCRIPTIONS.apply_patch,
          args: { patchText: s.string().describe('The full patch text that describes all changes to be made') },
          execute: async (args: { patchText: string }, ctx: V1ToolContext) => out(await tools.applyPatch(args, call(ctx))),
        },
      },

      'tool.execute.before': async (i: { tool: string }) => {
        if (HOST_ONLY.has(i.tool)) throw new Error(`${i.tool} would act on the user's machine, not the Prized sandbox. Use bash, grep or glob.`);
      },

      event: async ({ event }: { event: { type: string; properties?: { [k: string]: unknown } } }) => {
        const p = event.properties ?? {};
        try {
          if (event.type === 'session.status' && typeof p.sessionID === 'string') {
            const type = (p.status as { type?: string } | undefined)?.type;
            await sandboxes.status(p.sessionID, type === 'busy' || type === 'retry');
          } else if (event.type === 'session.idle' && typeof p.sessionID === 'string') {
            await sandboxes.status(p.sessionID, false);
          } else if (event.type === 'session.deleted') {
            const id = (p.info as { id?: string } | undefined)?.id;
            if (id) await sandboxes.sessionDeleted(id);
          }
        } catch (err) {
          log('warn', `event ${event.type}: ${(err as Error).message}`);
        }
      },

      config: async (cfg: { command?: { [name: string]: unknown } }) => {
        cfg.command ??= {};
        cfg.command.prized ??= {
          description: 'Prized sandbox: id, state, size, changes not pulled yet',
          template: 'Reply with one line: the Prized plugin did not handle /prized.',
        };
        cfg.command['prized-pull'] ??= {
          description: 'Bring the Prized sandbox changes to your checkout [--branch] [--all]',
          template: 'Reply with one line: the Prized plugin did not handle /prized-pull.',
        };
      },

      'command.execute.before': async (i: { command: string; sessionID: string; arguments: string }, o: { parts: unknown[] }) => {
        if (i.command !== 'prized' && i.command !== 'prized-pull') return;
        let text: string;
        try {
          text = i.command === 'prized' ? await sandboxes.statusText(i.sessionID) : await sandboxes.pullText(i.sessionID, i.arguments ?? '');
        } catch (err) {
          text = `Prized: ${(err as Error).message}`;
        }
        host.notify(text.split('\n')[0] ?? text, 'info');
        o.parts.length = 0;
        o.parts.push({
          type: 'text',
          text: `The user ran /${i.command}${i.arguments ? ` ${i.arguments}` : ''}, which the Prized plugin has already carried out. Its result:\n\n${text}\n\nTell the user the result in a sentence or two. Do not call any tools for this.`,
        });
      },

      'experimental.chat.system.transform': async (_i: unknown, o: { system: string[] }) => {
        o.system.push(SYSTEM_NOTE(layout.hostDir, layout.dir));
      },

      dispose: async () => {
        const notes = await sandboxes.dispose();
        for (const n of notes) {
          log('warn', n);
          process.stderr.write(`${n}\n`);
        }
      },
    };
  };
}

