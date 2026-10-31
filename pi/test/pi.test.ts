/**
 * The extension inside a real Pi session: Pi's own agent loop, tools, session files and extension
 * runner, with a scripted model (Pi's faux provider) and a fake Prized sandbox. What the model asks
 * for goes through everything Pi does with a tool call before the extension sees it.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from '@earendil-works/pi-ai';
import {
  type AgentSession,
  ModelRuntime,
  SessionManager,
  createAgentSessionFromServices,
  createAgentSessionServices,
} from '@earendil-works/pi-coding-agent';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { createExtension } from '../src/extension.ts';
import { fakeSdk, inSandbox, world, type FakeSandbox } from './fake-sdk.ts';

const git = (cwd: string, ...args: string[]) => execFileSync('git', args, { cwd, encoding: 'utf8' });
const made: string[] = [];
let host: string;
let agentDir: string;
let session: AgentSession | undefined;
let faux: ReturnType<typeof fauxProvider>;
let modelRuntime: ModelRuntime;

beforeEach(async () => {
  world.reset();
  const root = mkdtempSync(path.join(tmpdir(), 'pi-int-'));
  made.push(root);
  host = path.join(root, 'proj');
  agentDir = path.join(root, 'agent');
  for (const d of [host, agentDir]) execFileSync('mkdir', ['-p', d]);
  git(host, 'init', '-q', '-b', 'main');
  git(host, 'config', 'user.name', 'Host User');
  git(host, 'config', 'user.email', 'host@example.com');
  writeFileSync(path.join(host, 'calc.py'), 'def add(a, b):\n    return a - b\n');
  git(host, 'add', '-A');
  git(host, 'commit', '-q', '-m', 'init');
  process.env.PRIZED_API_KEY = 'dcp_000000000000000000000000000000000000000000';
  faux = fauxProvider();
  modelRuntime = await ModelRuntime.create({ authPath: path.join(agentDir, 'auth.json'), modelsPath: null, refreshOnCreate: false });
  modelRuntime.registerNativeProvider(faux.provider);
});

afterEach(async () => {
  await session?.dispose?.();
  session = undefined;
  for (const d of made.splice(0)) rmSync(d, { recursive: true, force: true });
  delete process.env.PRIZED_API_KEY;
  world.reset();
});

/** Start a Pi session with the extension loaded and `--prized` set, as `pi --prized` would. */
async function startPi(
  flags: Record<string, boolean | string> = { prized: true },
  extra: { tools?: string[] } = {},
): Promise<AgentSession> {
  const services = await createAgentSessionServices({
    cwd: host,
    agentDir,
    modelRuntime,
    extensionFlagValues: new Map(Object.entries(flags)),
    resourceLoaderOptions: { noExtensions: true, extensionFactories: [createExtension(fakeSdk())] },
  });
  const created = await createAgentSessionFromServices({
    services,
    sessionManager: SessionManager.inMemory(host),
    model: faux.getModel(),
    ...extra,
  });
  session = created.session;
  await session.bindExtensions({});
  return session;
}

const tool = (name: string, args: Record<string, any>, id = `t-${name}`) => fauxToolCall(name, args, { id });

describe('a Pi session with --prized', () => {
  it('fixes a bug in the sandbox with the model\'s own tool calls, leaving your machine alone until you pull', async () => {
    faux.setResponses([
      fauxAssistantMessage([tool('read', { path: 'calc.py' })]),
      fauxAssistantMessage([tool('edit', { path: 'calc.py', edits: [{ oldText: 'a - b', newText: 'a + b' }] })]),
      fauxAssistantMessage([tool('write', { path: 'test_calc.py', content: 'from calc import add\nassert add(2, 3) == 5\nprint("ok")\n' })]),
      fauxAssistantMessage([tool('bash', { command: "grep -q 'return a + b' calc.py && grep -q 'assert add' test_calc.py && echo ok" })]),
      fauxAssistantMessage([fauxText('Fixed: add now adds.')]),
    ]);
    const s = await startPi();
    await s.prompt('fix add in calc.py');
    await s.agent.waitForIdle?.();

    const results = s.messages.filter((m) => m.role === 'toolResult') as Array<{ toolName: string; isError: boolean; content: Array<{ text?: string }> }>;
    expect(results.map((r) => [r.toolName, r.isError])).toEqual([
      ['read', false],
      ['edit', false],
      ['write', false],
      ['bash', false],
    ]);
    expect(results[0]?.content[0]?.text).toContain('a - b');
    expect(results[3]?.content[0]?.text).toContain('ok'); // the edit and the new file were there when the command ran, in the sandbox

    expect(world.creates).toHaveLength(1);
    const sb = [...world.sandboxes.values()][0] as FakeSandbox;
    expect(readFileSync(inSandbox(sb, '/home/prized/work/proj/calc.py'), 'utf8')).toContain('a + b');
    expect(readFileSync(path.join(host, 'calc.py'), 'utf8')).toContain('a - b'); // your file is untouched
    expect(existsSync(path.join(host, 'test_calc.py'))).toBe(false);
  });

  it('pulls the agent\'s work with /prized pull', async () => {
    faux.setResponses([
      fauxAssistantMessage([tool('edit', { path: 'calc.py', edits: [{ oldText: 'a - b', newText: 'a + b' }] })]),
      fauxAssistantMessage([fauxText('done')]),
    ]);
    const s = await startPi();
    await s.prompt('fix add');
    // Pi runs extension commands through prompt(); the confirm dialog needs a UI, so give it one that says yes.
    await s.bindExtensions({
      uiContext: {
        confirm: async () => true,
        notify: () => undefined,
        setStatus: () => undefined,
        input: async () => undefined,
        select: async () => undefined,
        theme: { fg: (_c: string, t: string) => t },
      } as never,
    }).catch(() => undefined);
    await s.prompt('/prized pull');
    expect(readFileSync(path.join(host, 'calc.py'), 'utf8')).toContain('a + b');
  });

  it('turns off grep, find, ls and powershell, which would act on your machine, even when asked for', async () => {
    const asked = { tools: ['read', 'bash', 'edit', 'write', 'grep', 'find', 'ls'] };
    const plain = await startPi({}, asked);
    expect(plain.getActiveToolNames()).toEqual(expect.arrayContaining(['grep', 'find', 'ls'])); // Pi on its own keeps them
    await plain.dispose();
    const s = await startPi({ prized: true }, asked);
    expect(s.getActiveToolNames()).toEqual(expect.arrayContaining(['read', 'bash', 'edit', 'write']));
    for (const name of ['grep', 'find', 'ls', 'powershell']) expect(s.getActiveToolNames()).not.toContain(name);
  });

  it('runs Pi\'s own tools on your machine without the flag', async () => {
    faux.setResponses([
      fauxAssistantMessage([tool('bash', { command: 'cat calc.py' })]),
      fauxAssistantMessage([fauxText('ok')]),
    ]);
    const s = await startPi({});
    await s.prompt('look');
    const result = s.messages.find((m) => m.role === 'toolResult') as { content: Array<{ text?: string }> };
    expect(result.content[0]?.text).toContain('a - b');
    expect(world.creates).toHaveLength(0);
  });
});
