/**
 * The OpenCode 2 plugin against a stand-in for OpenCode 2's plugin Context: the built-in tools it
 * updates, the session hooks, commands and permissions, with the fake sandbox running real git and bash.
 */
import { existsSync, readFileSync } from 'node:fs';

import { beforeEach, describe, expect, it } from 'vitest';

import { type V2Context, createV2Setup } from '../src/v2.ts';
import { type FakeSandbox, fakeSdk, inSandbox, world } from './fake-sdk.ts';
import { makeRepo } from './helpers.ts';

interface Tool {
  description: string;
  output?: unknown;
  execute: (input: never, c: unknown) => Promise<unknown>;
}

let repo: string;

function fakeOpenCode(sessions: Record<string, { parentID?: string; fork?: { sessionID: string } }> = {}) {
  const builtinCalls: string[] = [];
  const tools: Record<string, Tool> = {};
  for (const name of ['shell', 'read', 'write', 'edit', 'patch', 'glob', 'grep', 'webfetch']) {
    tools[name] = { description: `built-in ${name}`, output: { schema: true }, execute: async () => builtinCalls.push(name) };
  }
  const hooks: Record<string, Array<(e: never) => unknown>> = {};
  const commands: Record<string, (i: { sessionID: string; prompt?: unknown }) => Promise<void>> = {};
  const asserts: Array<{ [k: string]: unknown }> = [];
  const synthetic: Array<{ sessionID: string; text: string }> = [];
  const ctx: V2Context = {
    options: { pauseSeconds: 1 },
    location: { directory: repo },
    tool: {
      transform: async (fn) => {
        fn({ update: (id, f) => (tools[id] ? f(tools[id] as never) : undefined) });
      },
    },
    session: {
      hook: async (name, fn) => {
        (hooks[name] ??= []).push(fn);
      },
      get: async ({ sessionID }) => sessions[sessionID] ?? {},
      synthetic: async (i) => {
        synthetic.push(i);
      },
    },
    command: {
      transform: async (fn) => {
        fn({ add: (d) => (commands[d.name] = d.execute) });
      },
    },
    permission: {
      assert: async (i) => {
        asserts.push(i);
      },
    },
  };
  const run = (name: string, input: unknown, sessionID = 'ses_1') =>
    (tools[name] as Tool).execute(input as never, { sessionID, agent: 'build', messageID: 'msg_1', id: 'call_1', signal: new AbortController().signal, progress: async () => undefined }) as Promise<{ content: unknown; metadata: { [k: string]: unknown } }>;
  return { ctx, tools, hooks, commands, asserts, synthetic, builtinCalls, run };
}

const live = () => [...world.sandboxes.values()].filter((s) => !s.deleted);
const name = () => repo.split('/').at(-1) as string;

beforeEach(() => {
  world.reset();
  repo = makeRepo();
});

describe('the OpenCode 2 plugin', () => {
  it("swaps the executors of OpenCode's file and shell tools and leaves the rest alone", async () => {
    const oc = fakeOpenCode();
    const dispose = await createV2Setup({ sdk: fakeSdk(), env: { PRIZED_API_KEY: 'dcp_000test' } })(oc.ctx);
    for (const n of ['shell', 'read', 'write', 'edit', 'patch', 'glob', 'grep']) {
      expect(oc.tools[n]?.description).not.toMatch(/^built-in/);
      expect(oc.tools[n]?.output).toBeUndefined();
    }
    expect(oc.tools.webfetch?.description).toBe('built-in webfetch');
    const r = await oc.run('shell', { command: 'pwd' });
    expect(String(r.content)).toContain(`/home/prized/work/${name()}`);
    expect(oc.asserts[0]).toMatchObject({ action: 'shell', resources: ['pwd'], sessionID: 'ses_1', source: { type: 'tool', messageID: 'msg_1', id: 'call_1' } });
    await oc.run('write', { path: `${repo}/n.txt`, content: 'n\n' });
    await oc.run('edit', { path: `${repo}/app.py`, oldString: 'print("Hi")', newString: 'print("V2")' });
    expect(oc.asserts.filter((a) => a.action === 'edit')).toHaveLength(2);
    const read = await oc.run('read', { path: `${repo}/app.py` });
    expect(String(read.content)).toContain('print("V2")');
    expect(readFileSync(`${repo}/app.py`, 'utf8')).toContain('"Hi"');
    await oc.run('read', { path: 'skill://x/SKILL.md' });
    expect(oc.builtinCalls).toEqual(['read']);
    await dispose();
  });

  it('pauses on the idle hook, tells the model where it works, and replaces the local shell description', async () => {
    const oc = fakeOpenCode();
    await createV2Setup({ sdk: fakeSdk(), env: { PRIZED_API_KEY: 'dcp_000test' } })(oc.ctx);
    await oc.run('shell', { command: 'true' });
    await new Promise((r) => setTimeout(r, 1300));
    expect(world.events.some((e) => e.startsWith('pause'))).toBe(false); // the session is still working
    for (const h of oc.hooks['experimental.idle'] ?? []) await h({ sessionID: 'ses_1', background: [] } as never);
    await new Promise((r) => setTimeout(r, 1300));
    expect(world.events.at(-1)).toMatch(/^pause /);
    const ev = { system: [] as Array<{ type: string; text: string }>, tools: { shell: { description: 'Runs on darwin with zsh' } } };
    for (const h of oc.hooks.context ?? []) await h(ev as never);
    expect(ev.system[0]?.text).toContain(`${repo} is at /home/prized/work/${name()}`);
    expect(ev.tools.shell.description).toContain('Prized sandbox');
  });

  it('forks the parent sandbox for a forked session', async () => {
    const oc = fakeOpenCode({ ses_2: { fork: { sessionID: 'ses_1' } } });
    await createV2Setup({ sdk: fakeSdk(), env: { PRIZED_API_KEY: 'dcp_000test' } })(oc.ctx);
    await oc.run('write', { path: `${repo}/base.txt`, content: 'b\n' });
    const r = await oc.run('shell', { command: 'cat base.txt; echo f > fork.txt' }, 'ses_2');
    expect(String(r.content)).toContain('b');
    expect(world.forks).toHaveLength(1);
    const [parent, copy] = live() as FakeSandbox[];
    expect(existsSync(inSandbox(parent as FakeSandbox, `work/${name()}/fork.txt`))).toBe(false);
    expect(existsSync(inSandbox(copy as FakeSandbox, `work/${name()}/fork.txt`))).toBe(true);
  });

  it('/prized-pull posts its result into the session and applies with --all', async () => {
    const oc = fakeOpenCode();
    await createV2Setup({ sdk: fakeSdk(), env: { PRIZED_API_KEY: 'dcp_000test' } })(oc.ctx);
    await oc.run('shell', { command: 'echo x >> bin/run' });
    await oc.commands['prized-pull']?.({ sessionID: 'ses_1', prompt: { text: '' } });
    expect(oc.synthetic.at(-1)?.text).toContain('a change to an executable file: bin/run');
    await oc.commands['prized-pull']?.({ sessionID: 'ses_1', prompt: { text: '--all' } });
    expect(oc.synthetic.at(-1)?.text).toContain('review with git diff');
    expect(readFileSync(`${repo}/bin/run`, 'utf8')).toContain('x');
  });

  it('fails closed without a key', async () => {
    const oc = fakeOpenCode();
    await createV2Setup({ sdk: fakeSdk(), env: {} })(oc.ctx);
    await expect(oc.run('shell', { command: `touch ${repo}/pwned` })).rejects.toThrow(/No Prized API key/);
    expect(existsSync(`${repo}/pwned`)).toBe(false);
    expect(oc.builtinCalls).toEqual([]);
  });
});
