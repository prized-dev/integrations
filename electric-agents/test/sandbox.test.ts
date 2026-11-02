/**
 * The package as Electric uses it: `prizedSandbox()` and `prizedSandboxProfile()` on top of the
 * published `@electric-ax/agents-runtime` (its `remoteSandbox`, `lazySandbox` and helpers run
 * for real), driving the fake Prized SDK.
 */
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { join } from 'node:path';

import { ensureSandboxMaterialized, SandboxError, type Sandbox, type SandboxFactoryParams } from '@electric-ax/agents-runtime/sandbox';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { PRIZED_SANDBOX_NAME, prizedSandbox, prizedSandboxProfile, SANDBOX_KEY_LABEL, sandboxKeyTag } from '../src/index.js';
import { createFakePrized, type FakePrized } from './fake-prized.js';

let fake: FakePrized;
const open: Sandbox[] = [];

beforeEach(() => {
  fake = createFakePrized();
});

afterEach(async () => {
  for (const s of open.splice(0)) await s.dispose().catch(() => undefined);
  await fake.cleanup();
});

async function sandbox(opts: Parameters<typeof prizedSandbox>[0] = {}): Promise<Sandbox> {
  const s = await prizedSandbox({ sdk: fake.Sdk, heartbeatIntervalMs: 60_000, ...opts });
  open.push(s);
  return s;
}

function params(over: Partial<SandboxFactoryParams> = {}): SandboxFactoryParams {
  return {
    sandboxKey: 'https://agents.example/worker/7',
    persistent: true,
    owner: true,
    entityUrl: 'https://agents.example/worker/7',
    entityType: 'worker',
    args: {},
    ...over,
  };
}

describe('prizedSandbox', () => {
  it('is a remote sandbox named remote:prized, rooted in /home/prized/work', async () => {
    const s = await sandbox();
    expect(s.name).toBe(PRIZED_SANDBOX_NAME);
    expect(s.workingDirectory).toBe('/home/prized/work');
    const root = fake.sandboxes[0]!.root;
    const r = await s.exec({ command: 'pwd' });
    expect(r.stdout.toString().trim()).toBe(join(root, '/home/prized/work'));
    expect(r).toMatchObject({ exitCode: 0, timedOut: false, aborted: false, outputTruncated: false });
  });

  it('resolves relative paths against the working directory and round-trips files', async () => {
    const s = await sandbox();
    await s.writeFile('notes/a.md', '# hi');
    expect((await s.readFile('/home/prized/work/notes/a.md')).toString()).toBe('# hi');
    expect((await s.stat('notes/a.md')).size).toBe(4);
    await s.mkdir('x/y/z', { recursive: true });
    expect((await s.readdir('.')).map(e => e.name).sort()).toEqual(['notes', 'x']);
    await s.remove('x', { recursive: true });
    expect(await s.exists('x')).toBe(false);
  });

  it("keeps Electric's write containment: writes outside the working directory are a policy error", async () => {
    const s = await sandbox();
    for (const write of [() => s.writeFile('/home/prized/.bashrc', 'x'), () => s.writeFile('../escape', 'x'), () => s.mkdir('/tmp/x')]) {
      const err = await write().catch((e: unknown) => e);
      expect(err).toBeInstanceOf(SandboxError);
      expect((err as SandboxError).kind).toBe('policy');
    }
    // Reads anywhere in the VM are allowed on remote sandboxes.
    expect((await s.readFile('/etc/passwd')).length).toBeGreaterThan(0);
  });

  it('truncates output to maxOutputBytes as Electric does', async () => {
    const s = await sandbox();
    const r = await s.exec({ command: 'printf 1234567890', maxOutputBytes: 4 });
    expect(r.stdout.toString()).toBe('1234');
    expect(r.outputTruncated).toBe(true);
  });

  describe('fetch runs inside the sandbox', () => {
    let server: Server;
    let url: string;
    beforeEach(async () => {
      server = createServer((req, res) => {
        res.writeHead(200, { 'content-type': 'text/plain' });
        res.end(`seen ${req.headers['user-agent']}`);
      });
      await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
      url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/page`;
    });
    afterEach(async () => {
      await new Promise(resolve => server.close(resolve));
    });

    it('through the in-sandbox client over exec', async () => {
      const s = await sandbox();
      const res = await s.fetch(url, { headers: { 'user-agent': 'prized-test' } });
      expect(res.status).toBe(200);
      expect(await res.text()).toBe('seen prized-test');
      expect(fake.sandboxes[0]!.execs.some(e => e.options.env?.FETCH_URL === url)).toBe(true);
    });
  });

  it('dispose deletes an ephemeral sandbox', async () => {
    const s = await sandbox();
    await s.dispose();
    expect(fake.sandboxes[0]!.state).toBe('deleted');
    await expect(s.exec({ command: 'true' })).rejects.toBeInstanceOf(SandboxError);
  });

  it('dispose keeps a persistent one for the next wake, and reclaim wipes it', async () => {
    const first = await sandbox({ sandboxKey: 'entity-1', persistent: true });
    await first.writeFile('state.json', '{"n":1}');
    await first.dispose();
    expect(fake.sandboxes[0]!.state).toBe('running');
    fake.sandboxes[0]!.pause();

    const second = await sandbox({ sandboxKey: 'entity-1', persistent: true });
    expect((await second.readFile('state.json')).toString()).toBe('{"n":1}');
    expect(fake.creates).toHaveLength(1);
    await second.dispose({ reclaim: true });
    expect(fake.sandboxes[0]!.state).toBe('deleted');
  });

  it('an attacher never deletes the owner sandbox, even on reclaim', async () => {
    const owner = await sandbox({ sandboxKey: 'shared', persistent: false });
    const attacher = await sandbox({ sandboxKey: 'shared', owner: false });
    await attacher.dispose({ reclaim: true });
    expect(fake.sandboxes[0]!.state).toBe('running');
    await owner.dispose();
    expect(fake.sandboxes[0]!.state).toBe('deleted');
  });

  it('refuses a policy Prized cannot enforce, and a working directory it cannot write, up front', async () => {
    await expect(prizedSandbox({ sdk: fake.Sdk, initialNetworkPolicy: { mode: 'deny-all' } })).rejects.toBeInstanceOf(SandboxError);
    await expect(prizedSandbox({ sdk: fake.Sdk, workingDirectory: '/work' })).rejects.toBeInstanceOf(SandboxError);
    expect(fake.creates).toHaveLength(0);
  });
});

describe('prizedSandboxProfile', () => {
  it('describes a remote profile named prized', () => {
    const p = prizedSandboxProfile({ sdk: fake.Sdk });
    expect(p).toMatchObject({ name: 'prized', label: 'Prized', remote: true });
    expect(p.description).toMatch(/Prized microVM/);
    expect(prizedSandboxProfile({ sdk: fake.Sdk, name: 'prized-big', label: 'Prized (8 vCPU)' })).toMatchObject({
      name: 'prized-big',
      label: 'Prized (8 vCPU)',
    });
  });

  it('refuses bad options at registration', () => {
    expect(() => prizedSandboxProfile({ initialNetworkPolicy: { mode: 'allowlist', allow: ['x.com'] } })).toThrow(SandboxError);
    expect(() => prizedSandboxProfile({ workingDirectory: '/srv' })).toThrow(SandboxError);
  });

  it('creates nothing until the wake uses the sandbox, then labels it with the key and entity type', async () => {
    const p = prizedSandboxProfile({ sdk: fake.Sdk, heartbeatIntervalMs: 60_000 });
    const s = await p.factory(params());
    open.push(s);
    expect(s.workingDirectory).toBe('/home/prized/work');
    expect(fake.creates).toHaveLength(0);
    expect((await s.exec({ command: 'echo used' })).stdout.toString()).toBe('used\n');
    expect(s.name).toBe(PRIZED_SANDBOX_NAME);
    expect(fake.creates[0]!.labels).toMatchObject({
      'created-by': 'electric-agents',
      'electric-entity-type': 'worker',
      [SANDBOX_KEY_LABEL]: sandboxKeyTag('https://agents.example/worker/7'),
    });
  });

  it('materializes on demand for an inheriting child', async () => {
    const p = prizedSandboxProfile({ sdk: fake.Sdk, heartbeatIntervalMs: 60_000 });
    const s = await p.factory(params());
    open.push(s);
    await ensureSandboxMaterialized(s);
    expect(fake.creates).toHaveLength(1);
    const child = await p.factory(params({ owner: false, entityUrl: 'https://agents.example/worker/8' }));
    open.push(child);
    await child.exec({ command: 'true' });
    expect(fake.creates).toHaveLength(1);
  });

  it('reclaim on an unused lease wipes what earlier wakes kept, for owners only', async () => {
    const p = prizedSandboxProfile({ sdk: fake.Sdk, heartbeatIntervalMs: 60_000 });
    const earlier = await p.factory(params());
    await earlier.exec({ command: 'true' });
    await earlier.dispose();
    expect(fake.live()).toHaveLength(1);

    const attacher = await p.factory(params({ owner: false }));
    await attacher.dispose({ reclaim: true });
    expect(fake.live()).toHaveLength(1);

    const last = await p.factory(params());
    await last.dispose({ reclaim: true });
    expect(fake.live()).toHaveLength(0);
    expect(fake.deletesByLabels).toEqual([{ [SANDBOX_KEY_LABEL]: sandboxKeyTag('https://agents.example/worker/7') }]);
  });

  it('lazy: false creates at the start of the wake', async () => {
    const p = prizedSandboxProfile({ sdk: fake.Sdk, lazy: false, heartbeatIntervalMs: 60_000 });
    const s = await p.factory(params({ persistent: false }));
    open.push(s);
    expect(fake.creates).toHaveLength(1);
    expect(fake.creates[0]!.autoDeleteMin).toBe(1);
  });
});
