/** The extension against Pi's real tool factories and a fake Prized sandbox (test/fake-sdk.ts). */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { PrizedError } from '@prized/sdk';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { readConfig } from '../src/config.ts';
import { createExtension } from '../src/extension.ts';
import { toSandboxPath } from '../src/tools.ts';
import { type FakePi, type FakeCtx, fakeCtx, fakePi } from './fake-pi.ts';
import { FakeSandbox, fakeSdk, inSandbox, world } from './fake-sdk.ts';

const git = (cwd: string, ...args: string[]) => execFileSync('git', args, { cwd, encoding: 'utf8' });

let host: string;
/** Every temp directory a test made; the only things afterEach removes. */
const made: string[] = [];
let fp: FakePi;
let ctx: FakeCtx;
const KEY = 'dcp_000000000000000000000000000000000000000000';

/** A checkout named `proj` with two files. */
function makeHost(): string {
  const root = mkdtempSync(path.join(tmpdir(), 'pi-host-'));
  made.push(root);
  const dir = path.join(root, 'proj');
  execFileSync('mkdir', [dir]);
  git(dir, 'init', '-q', '-b', 'main');
  git(dir, 'config', 'user.name', 'Host User');
  git(dir, 'config', 'user.email', 'host@example.com');
  writeFileSync(path.join(dir, 'a.txt'), 'one\ntwo\nthree\n');
  writeFileSync(path.join(dir, 'hello.py'), 'print("hello")\n');
  git(dir, 'add', '-A');
  git(dir, 'commit', '-q', '-m', 'init');
  return dir;
}

/** Load the extension into a fake Pi with these flags and start a session, as Pi does. */
async function boot(
  flags: Record<string, boolean | string | undefined> = { prized: true },
  opts: { sessionId?: string; saved?: boolean; entries?: FakePi['entries']; maxUploadBytes?: number } = {},
): Promise<void> {
  fp = fakePi(flags);
  fp.entries.push(...(opts.entries ?? []));
  createExtension(fakeSdk(), { maxUploadBytes: opts.maxUploadBytes })(fp.api);
  ctx = fakeCtx(fp, { cwd: host, sessionId: opts.sessionId, saved: opts.saved });
  await fp.fire('session_start', { reason: 'startup' }, ctx);
}

/** The first prompt: what starts the sandbox. */
async function prompt(): Promise<void> {
  const event = { systemPromptOptions: { cwd: host, promptGuidelines: [] as string[] } };
  await fp.fire('before_agent_start', event, ctx);
  await vi.waitFor(() => expect(world.creates.length + world.forks.length + world.connects.length).toBeGreaterThan(0), { timeout: 20_000 });
  await vi.waitFor(() => expect(ctx.notices.some((n) => /Prized sandbox sbx_/.test(n.text))).toBe(true), { timeout: 20_000 });
  return;
}

async function callTool(name: string, params: Record<string, unknown>, signal?: AbortSignal): Promise<string> {
  const tool = fp.tools.get(name);
  const result = await tool.execute(`call-${Math.random()}`, params, signal, undefined, ctx);
  return result.content.map((c: { text?: string }) => c.text ?? '').join('');
}

const sandboxes = () => [...world.sandboxes.values()];
const current = () => sandboxes().at(-1) as FakeSandbox;

beforeEach(() => {
  world.reset();
  host = makeHost();
  process.env.PRIZED_API_KEY = KEY;
});

afterEach(async () => {
  // What Pi does when the runtime goes away: timers are cancelled (nothing is paused or deleted on a reload).
  await fp?.fire('session_shutdown', { reason: 'reload' }, ctx);
  for (const dir of made.splice(0)) rmSync(dir, { recursive: true, force: true });
  (globalThis as unknown as Record<symbol, Map<string, unknown> | undefined>)[Symbol.for('@prized/pi.unsaved')]?.clear();
  delete process.env.PRIZED_API_KEY;
  vi.useRealTimers();
  world.reset();
});

describe('flags', () => {
  it('reads sizes, pause, idle, runtime, keep and pull', () => {
    const c = readConfig((n) => ({ 'prized-vcpu': '4', 'prized-memory': '8', 'prized-disk': '30', 'prized-pause': '5', 'prized-idle': '0', 'prized-max-runtime': '90', 'prized-keep': true, 'prized-pull': true, 'prized-template': 'prized-agent-v1' })[n]);
    expect(c).toEqual({
      template: 'prized-agent-v1',
      resources: { vcpu: 4, memoryGb: 8, diskGb: 30 },
      pauseSecs: 5,
      idleMin: 0,
      maxRuntimeMin: 90,
      keep: true,
      pullOnExit: true,
    });
  });

  it('has defaults that pause 20 seconds after a turn and 10 idle minutes after a crash', () => {
    expect(readConfig(() => undefined)).toEqual({
      template: undefined,
      resources: undefined,
      pauseSecs: 20,
      idleMin: 10,
      maxRuntimeMin: undefined,
      keep: false,
      pullOnExit: false,
    });
  });

  it.each([
    ['prized-vcpu', '0', /--prized-vcpu must be a whole number, 1 to 32/],
    ['prized-vcpu', '2.5', /--prized-vcpu/],
    ['prized-vcpu', 'many', /--prized-vcpu/],
    ['prized-memory', '-1', /--prized-memory/],
    ['prized-disk', '0', /--prized-disk/],
    ['prized-pause', '-3', /--prized-pause/],
    ['prized-idle', '1.5', /--prized-idle/],
    ['prized-max-runtime', '0', /--prized-max-runtime/],
  ])('rejects %s=%s by name', (flag, value, message) => {
    expect(() => readConfig((n) => (n === flag ? value : undefined))).toThrow(message);
  });

  it('registers every flag with Pi', async () => {
    await boot({});
    expect(fp.registeredFlags.sort()).toEqual(
      ['prized', 'prized-disk', 'prized-idle', 'prized-keep', 'prized-max-runtime', 'prized-memory', 'prized-pause', 'prized-pull', 'prized-template', 'prized-vcpu'].sort(),
    );
  });
});

describe('without --prized', () => {
  it('does nothing: no tool is replaced, no hook acts, nothing is started', async () => {
    await boot({});
    expect(fp.tools.size).toBe(0); // Pi's own bash, read, write and edit keep their settings
    expect(fp.activeTools).toContain('grep');
    expect(await fp.fire('tool_call', { toolName: 'grep', input: {} }, ctx)).toEqual([undefined]);
    expect(await fp.fire('user_bash', { command: 'pwd' }, ctx)).toEqual([undefined]);
    expect(await fp.fire('before_agent_start', { systemPromptOptions: { cwd: host } }, ctx)).toEqual([undefined]);
    expect(world.creates).toHaveLength(0);
    expect(world.connects).toHaveLength(0);
  });
});

describe('start', () => {
  it('turns off the tools that would search your machine, and starts nothing until the first prompt', async () => {
    fp = fakePi({ prized: true });
    fp.activeTools = ['read', 'bash', 'edit', 'write', 'grep', 'find', 'ls', 'powershell'];
    createExtension(fakeSdk())(fp.api);
    ctx = fakeCtx(fp, { cwd: host });
    await fp.fire('session_start', { reason: 'startup' }, ctx);
    expect(fp.activeTools).toEqual(['read', 'bash', 'edit', 'write']);
    expect(world.creates).toHaveLength(0);
    for (const toolName of ['grep', 'find', 'ls', 'powershell']) {
      expect((await fp.fire('tool_call', { toolName, input: {} }, ctx))[0], toolName).toMatchObject({ block: true });
    }
    expect(await fp.fire('tool_call', { toolName: 'bash', input: {} }, ctx)).toEqual([undefined]);
  });

  it('creates the sandbox with the sizes you asked for, labels it, and uploads the project', async () => {
    await boot({ prized: true, 'prized-vcpu': '4', 'prized-memory': '8', 'prized-disk': '30', 'prized-template': 'prized-agent-v1', 'prized-max-runtime': '90', 'prized-idle': '5' });
    await prompt();
    expect(world.creates).toHaveLength(1);
    expect(world.creates[0]).toMatchObject({
      apiKey: KEY,
      template: 'prized-agent-v1',
      resources: { vcpu: 4, memoryGb: 8, diskGb: 30 },
      maxRuntimeMin: 90,
      autoPauseMin: 5,
      autoDeleteMin: 7 * 24 * 60,
      labels: { 'created-by': 'pi-prized', 'pi-session': 'session-aaaaaaaa-1111' },
    });
    const sb = current();
    expect(readFileSync(inSandbox(sb, '/home/prized/work/proj/a.txt'), 'utf8')).toBe('one\ntwo\nthree\n');
    expect(fp.entries).toHaveLength(1);
    expect(fp.entries[0]).toMatchObject({
      customType: 'prized-sandbox',
      data: { sandboxId: sb.id, dir: '/home/prized/work/proj', cwd: '/home/prized/work/proj', hostRoot: host, owner: 'session-aaaaaaaa-1111' },
    });
    expect(ctx.notices.at(-1)?.text).toMatch(/Prized sandbox sbx_\d+ ready in [\d.]+s \(4 vCPU, 8 GB\)/);
  });

  it('starts one sandbox however many calls arrive at once', async () => {
    await boot();
    // No prompt first: three calls find no sandbox and must share one start.
    await Promise.all([callTool('bash', { command: 'echo 1' }), callTool('bash', { command: 'echo 2' }), callTool('read', { path: 'a.txt' })]);
    expect(world.creates).toHaveLength(1);
    expect(world.execs.filter((e) => e.command.startsWith('git init')).length).toBe(1);
  });

  it('keeps the agent in the subdirectory you started Pi in', async () => {
    execFileSync('mkdir', [path.join(host, 'src')]);
    writeFileSync(path.join(host, 'src', 'x.txt'), 'in src\n');
    git(host, 'add', '-A');
    host = path.join(host, 'src');
    await boot();
    await prompt();
    expect(await callTool('bash', { command: 'pwd' })).toContain('/home/prized/work/proj/src');
    expect(await callTool('read', { path: 'x.txt' })).toContain('in src');
  });

  it('gives a folder that is not a checkout an empty workspace and nowhere to pull to', async () => {
    host = mkdtempSync(path.join(tmpdir(), 'plain-'));
    made.push(host);
    await boot();
    await prompt();
    expect(await callTool('bash', { command: 'pwd && git log --format=%s' })).toContain('Empty workspace');
    await fp.commands.get('prized').handler('pull', ctx);
    expect(ctx.notices.at(-1)?.text).toMatch(/not started in a git checkout/);
  });

  it('tells the model it is in a sandbox, not on your machine', async () => {
    await boot();
    const event = { systemPromptOptions: { cwd: host, promptGuidelines: ['keep it short'] as string[], skills: [{ name: 'deploy' }] as unknown[] } };
    await fp.fire('before_agent_start', event, ctx);
    expect(event.systemPromptOptions.skills).toEqual([]); // the model could not open files on your machine
    expect(event.systemPromptOptions.cwd).toBe('/home/prized/work/proj (in a Prized sandbox, not on the user\'s machine)');
    expect(event.systemPromptOptions.promptGuidelines[0]).toBe('keep it short');
    expect(event.systemPromptOptions.promptGuidelines[1]).toMatch(/Prized sandbox.*\/prized pull/);
  });

  it('stays off, and says why, with no API key', async () => {
    delete process.env.PRIZED_API_KEY;
    fp = fakePi({ prized: true });
    createExtension(fakeSdk())(fp.api);
    ctx = fakeCtx(fp, { cwd: host, hasUI: false });
    await fp.fire('session_start', { reason: 'startup' }, ctx);
    expect(ctx.notices.at(-1)).toMatchObject({ level: 'error' });
    expect(ctx.notices.at(-1)?.text).toMatch(/no API key.*PRIZED_API_KEY/);
    await expect(callTool('bash', { command: 'echo no' })).rejects.toThrow(/nothing ran on your machine/);
    expect(world.creates).toHaveLength(0);
  });

  it('asks for the key when there is a screen to ask on', async () => {
    delete process.env.PRIZED_API_KEY;
    await boot();
    await prompt();
    expect(world.creates[0]?.apiKey).toBe(KEY);
  });

  it('refuses a bad flag by name and keeps tools off', async () => {
    await boot({ prized: true, 'prized-vcpu': '99' });
    expect(ctx.notices.at(-1)?.text).toMatch(/--prized-vcpu must be a whole number, 1 to 32/);
    await expect(callTool('bash', { command: 'echo no' })).rejects.toThrow(/nothing ran on your machine/);
  });

  it('explains a refused create and leaves nothing behind', async () => {
    world.createError = new PrizedError('insufficient_balance', 'The balance is empty.', { status: 409 });
    await boot();
    await fp.fire('before_agent_start', { systemPromptOptions: {} }, ctx);
    await vi.waitFor(() => expect(ctx.notices.some((n) => n.level === 'error')).toBe(true));
    expect(ctx.notices.at(-1)?.text).toMatch(/could not start the sandbox: The balance is empty/);
    await expect(callTool('bash', { command: 'echo no' })).rejects.toThrow(/nothing ran on your machine.*balance is empty/);
    expect(sandboxes()).toHaveLength(0);
    await fp.fire('agent_settled', {}, ctx);
    expect(await callTool('bash', { command: 'echo retried' })).toContain('retried'); // outside a run, the next call tries again
    expect(sandboxes()).toHaveLength(1);
  });

  it('gives up waiting for the sandbox to start when Escape is pressed', async () => {
    const ac = new AbortController();
    const slow = FakeSandbox.create;
    FakeSandbox.create = async (o) => {
      await new Promise((r) => setTimeout(r, 1500));
      return slow.call(FakeSandbox, o);
    };
    try {
      await boot();
      const started = Date.now();
      const call = callTool('bash', { command: 'echo never' }, ac.signal);
      setTimeout(() => ac.abort(), 200);
      await expect(call).rejects.toThrow(/aborted/i);
      expect(Date.now() - started).toBeLessThan(1200);
      expect(world.execs.some((e) => e.command === 'echo never')).toBe(false);
    } finally {
      FakeSandbox.create = slow;
    }
  });

  it('deletes a sandbox it created when the upload fails', async () => {
    const big = path.join(host, 'big.bin');
    writeFileSync(big, 'x');
    // A sandbox whose first command fails.
    const create = FakeSandbox.create;
    FakeSandbox.create = async (o) => {
      const sb = (await create.call(FakeSandbox, o)) as unknown as FakeSandbox;
      sb.execOverride = () => ({ stdout: '', stderr: 'disk full', exitCode: 1, durationMs: 1, timedOut: false, truncated: false });
      return sb as never;
    };
    try {
      await boot();
      await fp.fire('before_agent_start', { systemPromptOptions: {} }, ctx);
      await vi.waitFor(() => expect(ctx.notices.some((n) => n.level === 'error')).toBe(true));
      expect(ctx.notices.at(-1)?.text).toMatch(/disk full/);
      expect(sandboxes().every((s) => s.deleted)).toBe(true);
    } finally {
      FakeSandbox.create = create;
    }
  });
});

describe('tools', () => {
  beforeEach(async () => {
    await boot();
    await prompt();
  });

  it('runs bash in the sandbox, in its working directory, and not on your machine', async () => {
    const out = await callTool('bash', { command: 'pwd; echo "$HOME_CHECK-$((6*7))"; hostname >/dev/null; echo made > marker.txt' });
    expect(out).toContain('/home/prized/work/proj');
    expect(out).toContain('-42');
    expect(existsSync(inSandbox(current(), '/home/prized/work/proj/marker.txt'))).toBe(true);
    expect(existsSync(path.join(host, 'marker.txt'))).toBe(false);
    const last = world.execs.at(-1);
    expect(last?.options).toMatchObject({ cwd: '/home/prized/work/proj' });
  });

  it('does not forward your environment', async () => {
    process.env.HOST_SECRET_FOR_TEST = 'leak';
    try {
      await callTool('bash', { command: 'echo hi' });
      const last = world.execs.at(-1);
      expect(last?.command).toBe('echo hi');
      expect(last?.options).not.toHaveProperty('env'); // Pi hands its env to the operations; they must not send it
      expect(JSON.stringify(world.execs)).not.toContain('leak');
    } finally {
      delete process.env.HOST_SECRET_FOR_TEST;
    }
  });

  it('streams output as it arrives and reports a failing exit code as Pi does', async () => {
    const tool = fp.tools.get('bash');
    const result = await tool.execute('c1', { command: 'echo before; echo oops >&2; exit 3' }, undefined, undefined, ctx);
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toMatch(/before\s+oops[\s\S]*exited with code 3/);
  });

  it('turns the timeout Pi asks for into Pi\'s timeout error', async () => {
    await expect(callTool('bash', { command: 'sleep 20', timeout: 1 })).rejects.toThrow(/timed out after 1 seconds/);
  });

  it('turns an abort into Pi\'s aborted error and stops the command', async () => {
    const ac = new AbortController();
    const running = callTool('bash', { command: 'sleep 3; touch /home/prized/survived' }, ac.signal);
    await vi.waitFor(() => expect(world.execs.some((e) => e.command.startsWith('sleep 3'))).toBe(true));
    ac.abort();
    await expect(running).rejects.toThrow(/aborted/i);
    await new Promise((r) => setTimeout(r, 3500));
    expect(existsSync(inSandbox(current(), '/home/prized/survived'))).toBe(false); // it was stopped, not left to finish
  });

  it('returns when the shell does even if it started something in the background', async () => {
    const started = Date.now();
    const out = await callTool('bash', { command: '(sleep 5 &) ; echo started' });
    expect(out).toContain('started');
    expect(Date.now() - started).toBeLessThan(10_000);
  });

  it('writes, reads and edits files in the sandbox', async () => {
    await callTool('write', { path: 'notes/todo.md', content: 'first\nsecond\n' });
    const file = inSandbox(current(), '/home/prized/work/proj/notes/todo.md');
    expect(readFileSync(file, 'utf8')).toBe('first\nsecond\n');
    expect(existsSync(path.join(host, 'notes'))).toBe(false);
    expect(await callTool('read', { path: 'notes/todo.md' })).toContain('second');
    await callTool('edit', { path: 'notes/todo.md', edits: [{ oldText: 'second', newText: '2nd' }] });
    expect(readFileSync(file, 'utf8')).toBe('first\n2nd\n');
    expect(readFileSync(path.join(host, 'a.txt'), 'utf8')).toBe('one\ntwo\nthree\n');
  });

  it('reads and writes by absolute sandbox path', async () => {
    await callTool('write', { path: '/home/prized/out/result.txt', content: 'ok\n' });
    expect(await callTool('read', { path: '/home/prized/out/result.txt' })).toContain('ok');
  });

  it('says a missing file is missing, and does not take the sandbox for gone', async () => {
    await expect(callTool('read', { path: 'nope.txt' })).rejects.toThrow(/ENOENT|no such file/i);
    await expect(callTool('edit', { path: 'nope.txt', edits: [{ oldText: 'a', newText: 'b' }] })).rejects.toThrow();
    expect(await callTool('bash', { command: 'echo still here' })).toContain('still here');
    expect(world.creates).toHaveLength(1);
  });

  it('does not take a not_found for a file as the sandbox being gone', async () => {
    const sb = current();
    sb.files.exists = async () => true; // the file looks present and is gone by the time it is read: a race
    await expect(callTool('read', { path: 'vanished.txt' })).rejects.toThrow(/not found/i);
    expect(await callTool('bash', { command: 'echo alive' })).toContain('alive');
    expect(world.creates).toHaveLength(1);
  });

  it('maps your home directory to the sandbox home', () => {
    expect(toSandboxPath('/Users/me/.config/x', '/Users/me')).toBe('/home/prized/.config/x');
    expect(toSandboxPath('/Users/meow/x', '/Users/me')).toBe('/Users/meow/x');
    expect(toSandboxPath('/home/prized/work/x', '/Users/me')).toBe('/home/prized/work/x');
  });

  it('runs `!` commands in the sandbox too', async () => {
    const [res] = (await fp.fire('user_bash', { command: 'pwd', excludeFromContext: false }, ctx)) as Array<{ operations: { exec: Function } }>;
    let out = '';
    const r = await res?.operations.exec('pwd', host, { onData: (b: Buffer) => (out += b.toString()) });
    expect(r).toEqual({ exitCode: 0 });
    expect(out).toContain('/home/prized/work/proj');
  });
});

describe('pause between turns', () => {
  it('pauses a while after the turn ends and resumes at the next tool call', async () => {
    await boot({ prized: true, 'prized-pause': '1' });
    await prompt();
    const sb = current();
    await callTool('bash', { command: 'echo working' });
    await fp.fire('agent_settled', {}, ctx);
    expect(sb.state).toBe('running');
    await vi.waitFor(() => expect(sb.state).toBe('paused'), { timeout: 5000 });
    await prompt2(); // the next prompt wakes it while the model thinks
    await vi.waitFor(() => expect(sb.state).toBe('running'));
    expect(world.events.filter((e) => e.startsWith('pause')).length).toBe(1);
    expect(await callTool('bash', { command: 'echo back' })).toContain('back');
  });

  it('resumes by itself when a tool call finds it paused', async () => {
    await boot({ prized: true, 'prized-pause': '1' });
    await prompt();
    const sb = current();
    await fp.fire('agent_settled', {}, ctx);
    await vi.waitFor(() => expect(sb.state).toBe('paused'), { timeout: 5000 });
    expect(await callTool('bash', { command: 'echo after pause' })).toContain('after pause');
    expect(world.events).toContain(`resume ${sb.id}`);
  });

  it('does not pause while a tool is running or the agent is working', async () => {
    await boot({ prized: true, 'prized-pause': '1' });
    await prompt();
    const sb = current();
    await fp.fire('agent_settled', {}, ctx);
    const long = callTool('bash', { command: 'sleep 2.5; echo done' });
    await new Promise((r) => setTimeout(r, 1500));
    expect(sb.state).toBe('running');
    expect(await long).toContain('done');
    await prompt2(); // a new run starts: the timer from the last settle must not fire into it
    await new Promise((r) => setTimeout(r, 1600));
    expect(world.events.filter((e) => e.startsWith('pause'))).toHaveLength(0);
  });

  it('never pauses between turns with --prized-pause 0', async () => {
    await boot({ prized: true, 'prized-pause': '0' });
    await prompt();
    await fp.fire('agent_settled', {}, ctx);
    await new Promise((r) => setTimeout(r, 1200));
    expect(world.events.filter((e) => e.startsWith('pause'))).toHaveLength(0);
  });

  it('pauses on /prized pause and says so', async () => {
    await boot();
    await prompt();
    await fp.fire('agent_settled', {}, ctx);
    await fp.commands.get('prized').handler('pause', ctx);
    expect(current().state).toBe('paused');
    expect(ctx.notices.at(-1)?.text).toMatch(/Paused sbx_/);
  });
});

/** A second prompt in the same session (the sandbox already exists). */
async function prompt2(): Promise<void> {
  await fp.fire('before_agent_start', { systemPromptOptions: {} }, ctx);
}

describe('a session that comes back, and a fork', () => {
  async function firstSession(): Promise<{ id: string; entries: FakePi['entries'] }> {
    await boot();
    await prompt();
    const entries = [...fp.entries];
    return { id: current().id, entries };
  }

  it('reconnects to the session\'s own sandbox (pi -c) without creating another', async () => {
    const { id, entries } = await firstSession();
    await fp.fire('session_shutdown', { reason: 'new' }, ctx); // leaves it paused
    expect(world.sandboxes.get(id)?.state).toBe('paused');
    await boot({ prized: true }, { entries });
    await prompt();
    expect(world.creates).toHaveLength(1);
    expect(world.connects).toContain(id);
    expect(await callTool('bash', { command: 'cat a.txt' })).toContain('one');
    expect(world.sandboxes.get(id)?.state).toBe('running');
    expect(fp.entries).toHaveLength(entries.length); // nothing new recorded
  });

  it('copies the sandbox when a forked session starts, and records the copy', async () => {
    const { id, entries } = await firstSession();
    await callTool('bash', { command: 'echo parent > parent.txt' });
    await fp.fire('session_shutdown', { reason: 'fork' }, ctx);
    await boot({ prized: true }, { sessionId: 'session-bbbbbbbb-2222', entries });
    await prompt();
    expect(world.creates).toHaveLength(1); // the fork did not create
    expect(world.forks).toEqual([id]);
    const copy = current();
    expect(copy.id).not.toBe(id);
    expect(copy.labels).toMatchObject({ 'created-by': 'pi-prized', 'pi-session': 'session-bbbbbbbb-2222' });
    expect(readFileSync(inSandbox(copy, '/home/prized/work/proj/parent.txt'), 'utf8')).toBe('parent\n');
    // Each session now has its own files.
    await callTool('bash', { command: 'echo child > child.txt' });
    expect(existsSync(inSandbox(world.sandboxes.get(id) as FakeSandbox, '/home/prized/work/proj/child.txt'))).toBe(false);
    expect(fp.entries.at(-1)).toMatchObject({ data: { sandboxId: copy.id, owner: 'session-bbbbbbbb-2222' } });
    expect(ctx.notices.some((n) => n.text.includes(`forked from ${id}`))).toBe(true);
  });

  it('starts a new sandbox when the recorded one is gone, and says so', async () => {
    const { id, entries } = await firstSession();
    world.sandboxes.get(id)!.deleted = true;
    await boot({ prized: true }, { entries });
    await prompt();
    expect(world.creates).toHaveLength(2);
    expect(ctx.notices.some((n) => n.level === 'warning' && n.text.includes(`${id} is gone`))).toBe(true);
  });

  it('does not create a second sandbox when it only fails to reach the first', async () => {
    const { id, entries } = await firstSession();
    const connect = FakeSandbox.connect;
    FakeSandbox.connect = async () => {
      throw new PrizedError('network_error', 'no answer');
    };
    try {
      await boot({ prized: true }, { entries });
      await fp.fire('before_agent_start', { systemPromptOptions: {} }, ctx);
      await vi.waitFor(() => expect(ctx.notices.some((n) => n.level === 'error')).toBe(true));
      expect(world.creates).toHaveLength(1);
      expect(world.sandboxes.get(id)?.deleted).toBe(false);
    } finally {
      FakeSandbox.connect = connect;
    }
  });
});

describe('a sandbox that disappears mid-session', () => {
  it('fails the call with what happened, then starts a fresh one from your checkout', async () => {
    await boot();
    await prompt();
    const first = current();
    await callTool('bash', { command: 'echo agent work > work.txt' });
    first.deleted = true;
    await expect(callTool('bash', { command: 'cat work.txt' })).rejects.toThrow(/is gone.*everything the agent changed/);
    expect(await callTool('bash', { command: 'ls' })).toContain('a.txt');
    expect(world.creates).toHaveLength(2);
    const second = current();
    expect(second.id).not.toBe(first.id);
    expect(existsSync(inSandbox(second, '/home/prized/work/proj/work.txt'))).toBe(false);
    expect(fp.entries.at(-1)).toMatchObject({ data: { sandboxId: second.id } });
  });
});

describe('/prized', () => {
  beforeEach(async () => {
    await boot();
  });

  it('says there is nothing yet before the first prompt', async () => {
    await fp.commands.get('prized').handler('', ctx);
    expect(ctx.notices.at(-1)?.text).toMatch(/starts with your first prompt/);
    expect(world.creates).toHaveLength(0);
  });

  it('shows the sandbox and what has changed', async () => {
    await prompt();
    await callTool('write', { path: 'new.txt', content: 'hi\n' });
    await fp.commands.get('prized').handler('', ctx);
    const text = ctx.notices.at(-1)?.text ?? '';
    expect(text).toMatch(/Prized sandbox sbx_\d+ \(running, 2 vCPU, 4 GB\)/);
    expect(text).toContain('cwd: /home/prized/work/proj');
    expect(text).toMatch(/new\.txt/);
  });

  it('pulls into your checkout after you confirm, and not when you decline', async () => {
    await prompt();
    await callTool('write', { path: 'new.txt', content: 'hi\n' });
    ctx.confirmAnswer = false;
    await fp.commands.get('prized').handler('pull', ctx);
    expect(existsSync(path.join(host, 'new.txt'))).toBe(false);
    expect(ctx.confirms.at(-1)).toMatch(/Apply to .*new\.txt/s);
    ctx.confirmAnswer = true;
    await fp.commands.get('prized').handler('pull', ctx);
    expect(readFileSync(path.join(host, 'new.txt'), 'utf8')).toBe('hi\n');
    expect(ctx.notices.at(-1)?.text).toMatch(/Pulled/);
    await fp.commands.get('prized').handler('pull', ctx);
    expect(ctx.notices.at(-1)?.text).toMatch(/^Prized: No changes to pull\.$/);
  });

  it('commits on a branch with pull --branch', async () => {
    await prompt();
    await callTool('write', { path: 'new.txt', content: 'hi\n' });
    await fp.commands.get('prized').handler('pull --branch', ctx);
    expect(git(host, 'rev-parse', '--abbrev-ref', 'HEAD').trim()).toBe('pi/session-');
    expect(git(host, 'show', '--stat', '--format=', 'HEAD')).toMatch(/new\.txt/);
  });

  it('reports a pull that does not apply instead of throwing', async () => {
    await prompt();
    await callTool('edit', { path: 'a.txt', edits: [{ oldText: 'two', newText: 'TWO' }] });
    writeFileSync(path.join(host, 'a.txt'), 'one\nlocal\nthree\n');
    await fp.commands.get('prized').handler('pull', ctx);
    expect(ctx.notices.at(-1)).toMatchObject({ level: 'error' });
    expect(ctx.notices.at(-1)?.text).toMatch(/Pull failed: your files changed in a way that conflicts/);
  });

  it('deletes the sandbox on request and starts a new one at the next call', async () => {
    await prompt();
    const first = current();
    await fp.commands.get('prized').handler('delete', ctx);
    expect(first.deleted).toBe(true);
    expect(await callTool('bash', { command: 'echo again' })).toContain('again');
    expect(world.creates).toHaveLength(2);
  });

  it('rejects an unknown subcommand', async () => {
    await prompt();
    await fp.commands.get('prized').handler('frobnicate', ctx);
    expect(ctx.notices.at(-1)?.text).toMatch(/Unknown subcommand "frobnicate"/);
  });
});

describe('leaving Pi', () => {
  async function quit(flags: Record<string, boolean | string | undefined> = { prized: true }, opts: Parameters<typeof boot>[1] = {}) {
    await boot(flags, opts);
    await prompt();
    return current();
  }

  it('deletes a sandbox with nothing left to pull', async () => {
    const sb = await quit();
    await callTool('bash', { command: 'cat a.txt' });
    await fp.fire('session_shutdown', { reason: 'quit' }, ctx);
    expect(sb.deleted).toBe(true);
  });

  it('keeps a sandbox that has changes you have not pulled, paused, and says how to get them', async () => {
    const written: string[] = [];
    const spy = vi.spyOn(process.stderr, 'write').mockImplementation((s) => (written.push(String(s)), true));
    const exitHandlers: Array<() => void> = [];
    const once = vi.spyOn(process, 'once').mockImplementation(((ev: string, fn: () => void) => (ev === 'exit' && exitHandlers.push(fn), process)) as never);
    try {
      const sb = await quit();
      await callTool('write', { path: 'new.txt', content: 'unpulled\n' });
      await fp.fire('session_shutdown', { reason: 'quit' }, ctx);
      expect(sb.deleted).toBe(false);
      expect(sb.state).toBe('paused');
      exitHandlers.forEach((f) => f());
      expect(written.join('')).toMatch(/paused with changes you have not pulled:[\s\S]*new\.txt[\s\S]*pi -c and \/prized pull[\s\S]*prized sandbox rm sbx_/);
    } finally {
      spy.mockRestore();
      once.mockRestore();
    }
  });

  it('pulls on the way out with --prized-pull, then deletes', async () => {
    const sb = await quit({ prized: true, 'prized-pull': true });
    await callTool('write', { path: 'new.txt', content: 'brought home\n' });
    await fp.fire('session_shutdown', { reason: 'quit' }, ctx);
    expect(readFileSync(path.join(host, 'new.txt'), 'utf8')).toBe('brought home\n');
    expect(sb.deleted).toBe(true);
  });

  it('keeps and pauses with --prized-keep, changes or not', async () => {
    const sb = await quit({ prized: true, 'prized-keep': true });
    await fp.fire('session_shutdown', { reason: 'quit' }, ctx);
    expect(sb.deleted).toBe(false);
    expect(sb.state).toBe('paused');
    expect(ctx.notices.at(-1)?.text).toMatch(/pi -c picks it up again/);
  });

  it('does not delete when it cannot tell whether there is anything to lose', async () => {
    const sb = await quit();
    sb.execOverride = () => ({ stdout: '', stderr: 'git: command not found', exitCode: 127, durationMs: 1, timedOut: false, truncated: false });
    await fp.fire('session_shutdown', { reason: 'quit' }, ctx);
    expect(sb.deleted).toBe(false);
    expect(ctx.notices.at(-1)?.text).toMatch(/paused, not deleted: git: command not found/);
  });

  it('only pauses when the session goes on elsewhere (/new, /resume, /fork)', async () => {
    for (const reason of ['new', 'resume', 'fork']) {
      world.reset();
      const sb = await quit();
      await fp.fire('session_shutdown', { reason }, ctx);
      expect(sb.deleted, reason).toBe(false);
      expect(sb.state, reason).toBe('paused');
    }
  });

  it('leaves a saved session\'s sandbox alone on reload', async () => {
    const sb = await quit();
    await fp.fire('session_shutdown', { reason: 'reload' }, ctx);
    expect(sb.deleted).toBe(false);
    expect(sb.state).toBe('running');
  });

  it('does nothing when the sandbox never started', async () => {
    await boot();
    await fp.fire('session_shutdown', { reason: 'quit' }, ctx);
    expect(sandboxes()).toHaveLength(0);
  });

  it('writes no entry for a session that is not saved', async () => {
    await boot({ prized: true }, { saved: false });
    await prompt();
    expect(fp.entries).toHaveLength(0);
  });
});

describe('starting, when it goes wrong or takes time', () => {
  it('packs before it creates: a project that is too big costs no sandbox', async () => {
    writeFileSync(path.join(host, 'big.bin'), (await import('node:crypto')).randomBytes(8 * 1024));
    await boot({ prized: true }, { maxUploadBytes: 1024 });
    await fp.fire('before_agent_start', { systemPromptOptions: {} }, ctx);
    await vi.waitFor(() => expect(ctx.notices.some((n) => n.level === 'error')).toBe(true));
    expect(ctx.notices.at(-1)?.text).toMatch(/uploads stop at 100 MiB/);
    expect(world.creates).toHaveLength(0);
    expect(sandboxes()).toHaveLength(0);
  });

  it('does not try again on every tool call of a run that could not start, but does on the next prompt', async () => {
    world.createError = new PrizedError('sandbox_capacity', 'No capacity.', { status: 503 });
    await boot();
    await fp.fire('before_agent_start', { systemPromptOptions: {} }, ctx);
    await vi.waitFor(() => expect(ctx.notices.some((n) => n.level === 'error')).toBe(true));
    const errors = () => ctx.notices.filter((n) => n.level === 'error').length;
    expect(errors()).toBe(1);
    await expect(callTool('bash', { command: 'echo 1' })).rejects.toThrow(/No capacity/);
    await expect(callTool('bash', { command: 'echo 2' })).rejects.toThrow(/No capacity/);
    expect(errors()).toBe(1); // those two did not start anything
    expect(world.creates).toHaveLength(0);
    await fp.fire('agent_settled', {}, ctx);
    await prompt(); // the next prompt tries again, and now there is room
    expect(world.creates).toHaveLength(1);
  });

  it('pauses after a run that settled before the sandbox finished starting', async () => {
    const create = FakeSandbox.create;
    FakeSandbox.create = async (o) => {
      await new Promise((r) => setTimeout(r, 600));
      return create.call(FakeSandbox, o);
    };
    try {
      await boot({ prized: true, 'prized-pause': '1' });
      await fp.fire('before_agent_start', { systemPromptOptions: {} }, ctx); // a prompt that needs no tools...
      await fp.fire('agent_settled', {}, ctx); // ...is over before the sandbox is up
      await vi.waitFor(() => expect(world.creates).toHaveLength(1), { timeout: 5000 });
      await vi.waitFor(() => expect(current().state).toBe('paused'), { timeout: 5000 });
    } finally {
      FakeSandbox.create = create;
    }
  });

  it('leaves nested repositories and submodules out and says so', async () => {
    const nested = path.join(host, 'vendor', 'lib');
    execFileSync('mkdir', ['-p', nested]);
    git(nested, 'init', '-q');
    writeFileSync(path.join(nested, '.env'), 'NESTED_SECRET=1\n');
    writeFileSync(path.join(nested, 'code.js'), '1\n');
    await boot();
    await prompt();
    expect(existsSync(inSandbox(current(), '/home/prized/work/proj/vendor'))).toBe(false);
    expect(ctx.notices.some((n) => n.level === 'warning' && /left out 1 nested repository.*vendor\/lib/.test(n.text))).toBe(true);
  });
});

describe('what the agent cannot do to your machine', () => {
  it('keeps pull reviewable: a symlink, an executable and a hook are called out, and a long list is cut', async () => {
    await boot();
    await prompt();
    await callTool('bash', { command: 'ln -s /etc/passwd link && printf "#!/bin/sh\\n" > run.sh && chmod +x run.sh && mkdir .husky && echo hi > .husky/pre-commit' });
    for (let i = 0; i < 45; i += 1) await callTool('write', { path: `many/f${i}.txt`, content: 'x\n' });
    await fp.commands.get('prized').handler('pull', ctx);
    const asked = ctx.confirms.at(-1) ?? '';
    expect(asked).toMatch(/Look twice at:/);
    expect(asked).toMatch(/a symlink: link/);
    expect(asked).toMatch(/an executable: run\.sh/);
    expect(asked).toMatch(/a file your tools run on their own: \.husky\/pre-commit/);
    expect(asked).toMatch(/and \d+ more files/);
    expect(asked.split('\n').length).toBeLessThan(45);
  });

  it('does not pull on its own what needs a second look, and keeps the sandbox', async () => {
    await boot({ prized: true, 'prized-pull': true });
    await prompt();
    const sb = current();
    await callTool('bash', { command: 'ln -s /etc/passwd link && echo fine > fine.txt' });
    await fp.fire('session_shutdown', { reason: 'quit' }, ctx);
    expect(existsSync(path.join(host, 'fine.txt'))).toBe(false);
    expect(existsSync(path.join(host, 'link'))).toBe(false);
    expect(sb.deleted).toBe(false);
    expect(sb.state).toBe('paused');
  });

  it('answers a unsaved session\'s kept sandbox honestly: pi -c cannot find it', async () => {
    const written: string[] = [];
    const spy = vi.spyOn(process.stderr, 'write').mockImplementation((s) => (written.push(String(s)), true));
    const exitHandlers: Array<() => void> = [];
    const once = vi.spyOn(process, 'once').mockImplementation(((ev: string, fn: () => void) => (ev === 'exit' && exitHandlers.push(fn), process)) as never);
    try {
      await boot({ prized: true }, { saved: false });
      await prompt();
      expect(world.creates[0]?.autoDeleteMin).toBe(24 * 60); // a day, not a week: nobody can come back to it
      await callTool('write', { path: 'new.txt', content: 'x\n' });
      await fp.fire('session_shutdown', { reason: 'quit' }, ctx);
      exitHandlers.forEach((f) => f());
      const out = written.join('');
      expect(out).toMatch(/not saved, so pi -c cannot reconnect: use --prized-pull next time, or read the files with the prized CLI/);
      expect(out).not.toMatch(/Run pi -c and \/prized pull/);
      expect(out).toMatch(/Prized deletes it a day after it paused/);
    } finally {
      spy.mockRestore();
      once.mockRestore();
    }
  });

  it('keeps an unsaved session\'s sandbox through a reload, in the same process', async () => {
    await boot({ prized: true }, { saved: false });
    await prompt();
    const sb = current();
    await fp.fire('session_shutdown', { reason: 'reload' }, ctx);
    expect(sb.deleted).toBe(false);
    expect(sb.state).toBe('running');
    await boot({ prized: true }, { saved: false }); // the new runtime
    await fp.fire('before_agent_start', { systemPromptOptions: {} }, ctx);
    await vi.waitFor(() => expect(ctx.notices.some((n) => /reconnected/.test(n.text))).toBe(true));
    expect(world.creates).toHaveLength(1);
    await fp.fire('session_shutdown', { reason: 'quit' }, ctx);
    expect(sb.deleted).toBe(true); // clean: nothing to pull
  });
});

describe('commands that could go wrong', () => {
  beforeEach(async () => {
    await boot();
    await prompt();
  });

  it('keeps its state when the delete fails', async () => {
    const sb = current();
    const del = sb.delete.bind(sb);
    sb.delete = async () => {
      throw new PrizedError('network_error', 'no answer');
    };
    await fp.commands.get('prized').handler('delete', ctx);
    expect(ctx.notices.at(-1)).toMatchObject({ level: 'error' });
    expect(ctx.notices.at(-1)?.text).toMatch(/Could not delete/);
    expect(await callTool('bash', { command: 'echo still mine' })).toContain('still mine');
    expect(world.creates).toHaveLength(1);
    sb.delete = del;
  });

  it('does not pause a sandbox that is in use', async () => {
    const long = callTool('bash', { command: 'sleep 1.5; echo done' });
    await vi.waitFor(() => expect(world.execs.some((e) => e.command.startsWith('sleep 1.5'))).toBe(true));
    await fp.commands.get('prized').handler('pause', ctx);
    expect(current().state).toBe('running');
    expect(ctx.notices.at(-1)?.text).toMatch(/in use right now/);
    expect(await long).toContain('done');
  });

  it('does not pause under a long call just because a short one beside it finished', async () => {
    await fp.fire('session_shutdown', { reason: 'reload' }, ctx);
    await boot({ prized: true, 'prized-pause': '1' });
    await prompt();
    await fp.fire('agent_settled', {}, ctx);
    const sb = current();
    const long = callTool('bash', { command: 'sleep 3; echo long done' });
    await vi.waitFor(() => expect(world.execs.some((e) => e.command.startsWith('sleep 3'))).toBe(true));
    await callTool('bash', { command: 'echo short' }); // ends while the long one runs, and arms the pause timer
    await new Promise((r) => setTimeout(r, 1800));
    expect(sb.state).toBe('running');
    expect(await long).toContain('long done');
  });
});
