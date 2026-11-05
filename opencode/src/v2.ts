/**
 * The plugin for OpenCode 2 (`@opencode/cli`, plugin API `@opencode/plugin`, Promise flavour). Line
 * numbers are OpenCode's dev branch at 5ea5c5e (2.2.5); see DESIGN.md.
 *
 * - Tools: `ctx.tool.transform` + `editor.update(id, ...)` keeps OpenCode's own name, input schema and
 *   options for shell, read, write, edit, patch, glob and grep, and swaps the executor
 *   (packages/plugin/src/promise/tool.ts L26-36; packages/core/src/tool.ts L216-226). External plugins
 *   load after the built-ins (packages/core/src/plugin/supervisor.ts L94-98), so the built-ins exist to update.
 * - Permissions: each executor asks `ctx.permission.assert` with OpenCode's own actions (shell, read, edit,
 *   glob, grep), as the built-ins do (packages/core/src/tool/plugin/shell.ts L127-132).
 * - Pause: the `experimental.idle` session hook (packages/plugin/src/promise/session.ts L147-198).
 * - Fork: a forked session records where it came from (`fork.sessionID`, packages/schema/src/session.ts L33-38).
 * - Commands: `/prized` and `/prized-pull` run plugin code (packages/plugin/src/promise/command.ts L7-26)
 *   and post the result into the session with `ctx.session.synthetic`.
 */

import { realpath } from 'node:fs/promises';

import { Sandbox } from '@prized/sdk';

import { type Options, readConfig, readEnv } from './config.ts';
import { layoutFor } from './paths.ts';
import { type Host, type Sdk, Sandboxes } from './sandboxes.ts';
import { type Call, DESCRIPTIONS, type Result, Tools } from './tools.ts';
import { gitRoot } from './workspace.ts';

/** `ToolContext` (packages/plugin/src/promise/tool.ts L11-14; packages/schema/src/tool.ts L14-20). */
interface V2ToolContext {
  sessionID: string;
  agent?: string;
  messageID?: string;
  id?: string;
  signal: AbortSignal;
  progress(update: { [k: string]: unknown }): Promise<void>;
}

type Exec = (input: never, context: V2ToolContext) => Promise<unknown>;
interface V2Tool {
  description: string;
  execute: Exec;
  output?: unknown;
  options?: { permission?: string };
}

/** The slice of OpenCode 2's plugin `Context` this plugin uses (packages/plugin/src/promise/plugin.ts L27-59). */
export interface V2Context {
  options?: { [k: string]: unknown };
  location: { directory: string };
  tool: {
    transform(
      fn: (editor: {
        update(id: string, fn: (tool: V2Tool) => void): void;
        remove(id: string): void;
        get(id: string): (V2Tool & { id: string }) | undefined;
        list(): ReadonlyArray<V2Tool & { id: string }>;
      }) => void,
    ): Promise<unknown>;
  };
  session: {
    hook(name: string, fn: (event: never) => void | Promise<void>): Promise<unknown>;
    get(input: { sessionID: string }): Promise<{ parentID?: string; fork?: { sessionID: string } } | undefined>;
    synthetic(input: { sessionID: string; text: string; resume?: boolean }): Promise<unknown>;
  };
  command: { transform(fn: (editor: { add(def: { name: string; description?: string; execute(input: { sessionID: string; prompt?: unknown }): Promise<void> }): void }) => void): Promise<unknown> };
  permission: { assert(input: { [k: string]: unknown }): Promise<void> };
  event?: { subscribe(options: { types?: string[]; signal?: AbortSignal }): AsyncIterable<{ type: string; data?: { [k: string]: unknown }; location?: { directory?: string } }> };
}

const ACTION: { [permission: string]: string } = { bash: 'shell' };
/** OpenCode 2 built-ins that read or write your machine (packages/core/src/tool/plugin/image-generate.ts, opencode.ts). */
/** The built-ins this plugin swaps the executor of. */
const REPLACED = ['shell', 'read', 'write', 'edit', 'patch', 'glob', 'grep'];
/** Names a file or shell tool goes by in OpenCode, for the case where one of REPLACED is missing. */
const FILE_TOOL = /^(bash|shell|exec|read|write|edit|multiedit|patch|apply_patch|glob|grep|list|ls)$/;
const HOST_ONLY = ['experimental_image_generate', 'opencode_worktree_create', 'opencode_worktree_remove', 'opencode_session_move', 'worktree_create', 'worktree_remove', 'session_move', 'lsp'];

function argsText(prompt: unknown): string {
  // The prompt after the command name: a string, or parts with text.
  if (typeof prompt === 'string') return prompt;
  const p = prompt as { text?: string; parts?: Array<{ type?: string; text?: string }> } | undefined;
  if (typeof p?.text === 'string') return p.text;
  return (p?.parts ?? []).filter((x) => x.type === 'text').map((x) => x.text ?? '').join(' ');
}

function result(r: Result): { content: string | Array<{ type: string; text?: string; uri?: string; mime?: string }>; metadata: { [k: string]: unknown } } {
  if (!r.attachments?.length) return { content: r.output, metadata: r.metadata };
  return { content: [{ type: 'text', text: r.output }, ...r.attachments.map((a) => ({ type: 'file', uri: a.url, mime: a.mime }))], metadata: r.metadata };
}

export function createV2Setup(deps: { sdk?: Sdk; env?: NodeJS.ProcessEnv; limits?: { maxUploadBytes?: number; stateDir?: string } } = {}) {
  return async function setup(ctx: V2Context): Promise<() => Promise<void>> {
    // Never throw from here: a plugin that fails to set up leaves OpenCode's own tools running on your machine.
    // A bad option instead makes every tool call fail with the reason.
    let setupError: string | undefined;
    let config = readConfig(undefined);
    try {
      config = readConfig(ctx.options as Options);
    } catch (err) {
      setupError = `${(err as Error).message}.`;
    }
    const env = readEnv(deps.env ?? process.env);
    const hostCwd = await realpath(ctx.location.directory).catch(() => ctx.location.directory);
    const layout = layoutFor(hostCwd, await gitRoot(hostCwd));
    const host: Host = {
      async rootOf(sessionID) {
        let id = sessionID;
        for (let i = 0; i < 16; i += 1) {
          const s = await ctx.session.get({ sessionID: id });
          if (!s?.parentID) return id;
          id = s.parentID;
        }
        return id;
      },
      async inherited(sessionID) {
        const from = (await ctx.session.get({ sessionID }))?.fork?.sessionID;
        if (!from) return undefined;
        const owner = await this.rootOf(from);
        const [sb] = await (deps.sdk ?? { Sandbox }).Sandbox.list({ apiKey: env.apiKey, baseUrl: env.baseUrl, edgeUrl: env.edgeUrl, labels: { 'created-by': 'opencode-prized', 'opencode-session': owner } });
        return sb ? { sandboxId: sb.id, owner } : undefined;
      },
      notify(text, level) {
        // OpenCode 2's server has no toast; its log is the place.
        const line = text.startsWith('Prized') ? text : `Prized: ${text}`;
        if (level === 'error') console.error(line);
        else if (level === 'warning') console.warn(line);
        else console.info(line);
      },
    };
    const sandboxes = new Sandboxes(deps.sdk ?? { Sandbox }, config, env, layout, host, deps.limits, setupError);
    if (setupError) host.notify(`${setupError} The tools refuse to run until it is fixed.`, 'error');
    const tools = new Tools(sandboxes);

    const call = (c: V2ToolContext): Call => ({
      sessionID: c.sessionID,
      abort: c.signal,
      // No live progress: OpenCode 2 kills a call whose progress lands before its runner has marked it
      // called or after it settled (packages/core/src/session/runner/publish-llm-event.ts L548-551), and its
      // TUI streams shell output only from its own local shell service anyway. The result carries it all.
      progress: () => undefined,
      ask: (req) =>
        ctx.permission.assert({
          sessionID: c.sessionID,
          agent: c.agent,
          action: ACTION[req.permission] ?? req.permission,
          resources: req.patterns,
          save: req.always,
          metadata: req.metadata,
          ...(c.messageID && c.id ? { source: { type: 'tool', messageID: c.messageID, id: c.id } } : {}),
        }),
    });

    const swap = (t: V2Tool, description: string, run: (input: never, c: V2ToolContext) => Promise<Result>) => {
      t.description = description;
      t.output = undefined; // our results are content, not the built-in's structured output
      t.execute = async (input, c) => {
        // The tool runs: OpenCode is working in this session until the idle hook says otherwise.
        await sandboxes.status(c.sessionID, true);
        return result(await run(input, c));
      };
    };

    let missing: string[] = [];
    await ctx.tool.transform((editor) => {
      // Built-ins that act on your machine whatever we do: off while the sandbox is on.
      for (const id of HOST_ONLY) editor.remove(id);
      // `update` skips an id that is not there. If OpenCode renamed a tool this plugin replaces, take out every
      // tool that could touch your files rather than leave one running on your machine.
      missing = REPLACED.filter((id) => !editor.get(id));
      if (missing.length) {
        for (const t of editor.list()) {
          if (!REPLACED.includes(t.id) && (t.options?.permission === 'edit' || FILE_TOOL.test(t.id))) editor.remove(t.id);
        }
      }
      editor.update('shell', (t) => swap(t, DESCRIPTIONS.bash, (i: { command: string; workdir?: string; timeout?: number; background?: boolean }, c) => tools.bash(i, call(c))));
      editor.update('read', (t) => {
        const builtin = t.execute;
        t.description = DESCRIPTIONS.read;
        t.output = undefined;
        t.execute = async (input: never, c) => {
          const i = input as { path: string; offset?: number; limit?: number };
          // skill:// files are OpenCode's own, served from memory: the built-in reads them.
          if (typeof i.path === 'string' && i.path.startsWith('skill://')) return builtin(input, c);
          await sandboxes.status(c.sessionID, true);
          return result(await tools.read({ filePath: i.path, offset: i.offset, limit: i.limit }, call(c)));
        };
      });
      editor.update('write', (t) => swap(t, DESCRIPTIONS.write, (i: { path: string; content: string }, c) => tools.write({ filePath: i.path, content: i.content }, call(c))));
      editor.update('edit', (t) =>
        swap(t, DESCRIPTIONS.edit, (i: { path: string; oldString: string; newString: string; replaceAll?: boolean }, c) =>
          tools.edit({ filePath: i.path, oldString: i.oldString, newString: i.newString, replaceAll: i.replaceAll }, call(c)),
        ),
      );
      editor.update('patch', (t) => swap(t, DESCRIPTIONS.apply_patch, (i: { patchText: string }, c) => tools.applyPatch(i, call(c))));
      editor.update('glob', (t) => swap(t, DESCRIPTIONS.glob, (i: { pattern: string; path?: string; limit?: number }, c) => tools.glob(i, call(c))));
      editor.update('grep', (t) =>
        swap(t, DESCRIPTIONS.grep, (i: { pattern: string; path?: string; include?: string; literal?: boolean; caseSensitive?: boolean; limit?: number }, c) => tools.grep(i, call(c))),
      );
    });

    if (missing.length) {
      host.notify(`OpenCode's tools are not the ones this plugin knows (${missing.join(', ')} not found), so it took out the file and shell tools it could not replace. Update opencode-prized.`, 'error');
    }

    const note =
      `Your shell, read, write, edit, patch, glob and grep tools run in a Prized sandbox (a Linux microVM, user prized, no sudo) that holds a copy of the project, not on the user's machine. ` +
      `The project folder ${layout.hostDir} is at ${layout.dir} in the sandbox; paths under either name the same files there. ` +
      'Processes you start keep running between calls, and the sandbox pauses between turns with its memory. ' +
      "The user's environment variables and git ignored files (.env) are not in the sandbox. Your changes stay there until the user runs /prized-pull.";
    for (const name of ['context', 'compaction', 'generate']) {
      await ctx.session.hook(name, (e: never) => {
        const event = e as { system?: Array<{ type: string; text: string }>; tools?: { [k: string]: { description: string } } };
        event.system?.push({ type: 'text', text: note });
        // The built-in shell hook describes your machine's OS and shell; the sandbox is Linux and bash.
        if (event.tools?.shell) event.tools.shell.description = DESCRIPTIONS.bash;
      });
    }
    await ctx.session.hook('experimental.idle', async (e: never) => {
      const event = e as { sessionID: string };
      await sandboxes.status(event.sessionID, false).catch(() => undefined);
    });

    // The command runs here, not in the model. Its result goes into the session and wakes it, so the user
    // sees it at once (OpenCode 2's server has no other way to show text) and the model knows it happened.
    const post = (sessionID: string, command: string, text: string) =>
      ctx.session.synthetic({
        sessionID,
        text: `The user ran /${command}, which the Prized plugin has already carried out. Its result:\n\n${text}\n\nTell the user the result in a sentence or two. Do not call any tools for this.`,
        resume: true,
      });
    await ctx.command.transform((editor) => {
      editor.add({
        name: 'prized',
        description: 'Prized sandbox: id, state, size, changes not pulled yet',
        execute: async ({ sessionID }) => {
          const text = await sandboxes.statusText(sessionID).catch((err: Error) => `Prized: ${err.message}`);
          await post(sessionID, 'prized', text);
        },
      });
      editor.add({
        name: 'prized-pull',
        description: 'Bring the Prized sandbox changes to your checkout [--branch] [--all]',
        execute: async ({ sessionID, prompt }) => {
          const args = argsText(prompt).trim();
          const text = await sandboxes.pullText(sessionID, args).catch((err: Error) => `Prized: ${err.message}`);
          await post(sessionID, `prized-pull${args ? ` ${args}` : ''}`, text);
        },
      });
    });

    // Turn ends that skip the idle hook (an interrupt, a failure), and deleted sessions.
    const stop = new AbortController();
    if (ctx.event) {
      void (async () => {
        for await (const ev of ctx.event!.subscribe({ types: ['session.execution.failed', 'session.execution.interrupted', 'session.deleted'], signal: stop.signal })) {
          const id = ev.data?.sessionID;
          if (typeof id !== 'string') continue;
          if (ev.location?.directory && ev.location.directory !== ctx.location.directory) continue;
          if (ev.type === 'session.deleted') await sandboxes.sessionDeleted(id).catch(() => undefined);
          else await sandboxes.status(id, false).catch(() => undefined);
        }
      })().catch(() => undefined);
    }

    return async () => {
      stop.abort();
      for (const n of await sandboxes.dispose()) console.warn(n);
    };
  };
}
