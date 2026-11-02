import { readFile as hostReadFile, readdir as hostReaddir, symlink, utimes, writeFile as hostWriteFile } from 'node:fs/promises';
import { join } from 'node:path';

import { SandboxError } from '@electric-ax/agents-runtime/sandbox';
import { Sandbox as RealSdkSandbox } from '@prized/sdk';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { PrizedError } from '@prized/sdk';

import {
  adaptPrized,
  connectOrCreatePrizedSandbox,
  createPrizedClient,
  heartbeatIntervalFor,
  reclaimPrizedSandboxByKey,
  SANDBOX_KEY_LABEL,
  sandboxKeyTag,
  type PrizedRemoteSandboxClient,
  type PrizedSandboxClass,
} from '../src/index.js';
import { createFakePrized, type FakePrized, type FakeSandbox } from './fake-prized.js';

// The real SDK class fits the interface the adapter injects (checked by the compiler).
const realFits: PrizedSandboxClass = RealSdkSandbox;
void realFits;

let fake: FakePrized;
const clients: PrizedRemoteSandboxClient[] = [];

beforeEach(() => {
  fake = createFakePrized();
});

afterEach(async () => {
  for (const c of clients.splice(0)) await c.suspend();
  await fake.cleanup();
});

async function client(opts: Parameters<typeof createPrizedClient>[0] = {}): Promise<PrizedRemoteSandboxClient> {
  const c = await createPrizedClient({ apiKey: 'dcp_000000000000', sdk: fake.Sdk, ...opts });
  clients.push(c);
  return c;
}

function sandboxOf(c: PrizedRemoteSandboxClient): FakeSandbox {
  const s = fake.sandboxes.find(x => x.id === c.sandboxId);
  if (!s) throw new Error(`no fake sandbox ${c.sandboxId}`);
  return s;
}

const errorCode = async (p: Promise<unknown>): Promise<string | undefined> => {
  try {
    await p;
  } catch (err) {
    return (err as { code?: string }).code;
  }
  throw new Error('expected a rejection');
};

describe('creating and reattaching', () => {
  it('creates an ephemeral sandbox with the keep-alive window and a one-minute delete after pausing', async () => {
    const c = await client({ template: 'prized-agent-v1', resources: { vcpu: 2 }, labels: { team: 'a' } });
    expect(fake.creates).toHaveLength(1);
    const created = fake.creates[0]!;
    expect(created).toMatchObject({
      apiKey: 'dcp_000000000000',
      template: 'prized-agent-v1',
      resources: { vcpu: 2 },
      autoPauseMin: 2,
      autoDeleteMin: 1,
      labels: { team: 'a', 'created-by': 'electric-agents' },
    });
    expect(created.labels).not.toHaveProperty(SANDBOX_KEY_LABEL);
    expect(c.sandboxId).toMatch(/^sbx_/);
  });

  it('keeps a persistent sandbox paused for 7 days by default, or as long as asked', async () => {
    await client({ persistent: true, sandboxKey: 'a' });
    await client({ persistent: true, sandboxKey: 'b', pausedRetentionMin: 0, keepAliveMs: 30_000 });
    expect(fake.creates.map(c => [c.autoPauseMin, c.autoDeleteMin])).toEqual([
      [2, 10_080],
      [1, 0],
    ]);
  });

  it('reattaches by key from a second client (another wake or host) instead of creating', async () => {
    const first = await client({ sandboxKey: 'https://agents.example/worker/1', persistent: true });
    expect(fake.creates[0]!.labels![SANDBOX_KEY_LABEL]).toBe(sandboxKeyTag('https://agents.example/worker/1'));
    await first.writeFile('/home/prized/work/note.txt', 'kept');
    await first.suspend();
    sandboxOf(first).pause();

    const second = await client({ sandboxKey: 'https://agents.example/worker/1', persistent: true });
    expect(second.sandboxId).toBe(first.sandboxId);
    expect(fake.creates).toHaveLength(1);
    expect(sandboxOf(second).resumes).toBe(1);
    expect((await second.readFile('/home/prized/work/note.txt')).toString()).toBe('kept');
  });

  it('does not reattach to a sandbox for another key, or one that is deleted', async () => {
    const a = await client({ sandboxKey: 'a' });
    const b = await client({ sandboxKey: 'b' });
    expect(b.sandboxId).not.toBe(a.sandboxId);
    await a.kill();
    const a2 = await client({ sandboxKey: 'a' });
    expect(a2.sandboxId).not.toBe(a.sandboxId);
    expect(fake.creates).toHaveLength(3);
  });

  it('an attacher with no live sandbox throws unavailable and creates nothing', async () => {
    const err = await createPrizedClient({ sdk: fake.Sdk, sandboxKey: 'shared', owner: false }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(SandboxError);
    expect((err as SandboxError).kind).toBe('unavailable');
    expect(fake.creates).toHaveLength(0);
  });

  it('an attacher joins the owner sandbox', async () => {
    const owner = await client({ sandboxKey: 'shared', persistent: true });
    const attacher = await client({ sandboxKey: 'shared', owner: false });
    expect(attacher.sandboxId).toBe(owner.sandboxId);
  });

  it('converges on the oldest when two hosts create for one key at once, deleting the newer', async () => {
    const tag = sandboxKeyTag('race');
    let other: FakeSandbox | undefined;
    fake.onCreate = async () => {
      fake.onCreate = undefined;
      // Another host's create landed first (older), but after our first list came back empty.
      other = await fake.seed({ labels: { 'created-by': 'electric-agents', [SANDBOX_KEY_LABEL]: tag } }, 5_000);
    };
    const logs: string[] = [];
    const c = await client({ sandboxKey: 'race', log: m => logs.push(m) });
    expect(c.sandboxId).toBe(other!.id);
    expect(fake.live().map(s => s.id)).toEqual([other!.id]);
    expect(logs.join('\n')).toMatch(/another host created/);
  });

  it('picks the oldest when several are live for a key', async () => {
    const labels = { [SANDBOX_KEY_LABEL]: sandboxKeyTag('k') };
    await fake.seed({ labels }, 10_000);
    const oldest = await fake.seed({ labels }, 50_000);
    const c = await client({ sandboxKey: 'k' });
    expect(c.sandboxId).toBe(oldest.id);
  });

  it('refuses deny-all and allowlist policies before creating anything', async () => {
    for (const policy of [{ mode: 'deny-all' as const }, { mode: 'allowlist' as const, allow: ['example.com'] }]) {
      const err = await createPrizedClient({ sdk: fake.Sdk, initialNetworkPolicy: policy }).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(SandboxError);
      expect((err as SandboxError).kind).toBe('unavailable');
      expect((err as Error).message).toMatch(/outbound internet/);
    }
    await client({ initialNetworkPolicy: { mode: 'allow-all' } });
    expect(fake.creates).toHaveLength(1);
  });

  it('refuses a working directory outside /home/prized before creating anything', async () => {
    for (const dir of ['/work', '/home/prizedx', 'work', '/home/prized/../etc', '/home/prized/w/']) {
      const err = await createPrizedClient({ sdk: fake.Sdk, workingDirectory: dir }).catch((e: unknown) => e);
      expect(err, dir).toBeInstanceOf(SandboxError);
      expect((err as SandboxError).kind).toBe('policy');
    }
    expect(fake.creates).toHaveLength(0);
  });

  it('makes a working directory other than the default', async () => {
    const c = await client({ workingDirectory: '/home/prized/projects/app' });
    const r = await c.exec({ command: 'pwd' });
    expect(r.stdout.toString().trim()).toBe(join(sandboxOf(c).root, '/home/prized/projects/app'));
  });

  it('reclaims every sandbox for a key by label', async () => {
    const c = await client({ sandboxKey: 'gone', persistent: true });
    const ids = await reclaimPrizedSandboxByKey('gone', { sdk: fake.Sdk });
    expect(ids).toEqual([c.sandboxId]);
    expect(fake.deletesByLabels).toEqual([{ [SANDBOX_KEY_LABEL]: sandboxKeyTag('gone') }]);
    expect(sandboxOf(c).state).toBe('deleted');
  });
});

describe('review fixes: identity, setup and teardown', () => {
  it('a namespace keeps two deployments with the same entity keys apart', async () => {
    const dev = await client({ sandboxKey: '/worker/fixer', namespace: 'http://localhost:4437', persistent: true });
    const prod = await client({ sandboxKey: '/worker/fixer', namespace: 'https://agents.example', persistent: true });
    expect(prod.sandboxId).not.toBe(dev.sandboxId);
    expect(sandboxKeyTag('/worker/fixer', 'a')).not.toBe(sandboxKeyTag('/worker/fixer'));
    expect(sandboxKeyTag('/worker/fixer', '')).toBe(sandboxKeyTag('/worker/fixer'));
    await reclaimPrizedSandboxByKey('/worker/fixer', { sdk: fake.Sdk, namespace: 'http://localhost:4437' });
    expect(sandboxOf(dev).state).toBe('deleted');
    expect(sandboxOf(prod).state).toBe('running');
  });

  it('a persistent sandbox without a key is refused before anything is created', async () => {
    const err = await createPrizedClient({ sdk: fake.Sdk, persistent: true }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(SandboxError);
    expect(fake.creates).toHaveLength(0);
  });

  it('re-makes the working directory on attach if an earlier wake removed it', async () => {
    const first = await client({ sandboxKey: 'k', persistent: true });
    await first.exec({ command: 'rm -rf /home/prized/work' });
    await first.suspend();
    const second = await client({ sandboxKey: 'k', persistent: true });
    expect(second.sandboxId).toBe(first.sandboxId);
    expect((await second.exec({ command: 'pwd' })).exitCode).toBe(0);
  });

  it('deletes a sandbox it just created when the setup after it fails, but never one it attached to', async () => {
    fake.onCreate = s => {
      s.failExec = { match: 'mkdir -p', error: new PrizedError('network_error', 'edge gone') };
    };
    await expect(createPrizedClient({ sdk: fake.Sdk, sandboxKey: 'k', persistent: true })).rejects.toThrow('edge gone');
    expect(fake.sandboxes[0]!.state).toBe('deleted');

    fake.onCreate = undefined;
    const owner = await client({ sandboxKey: 'j', persistent: true });
    sandboxOf(owner).failExec = { match: 'mkdir -p', error: new PrizedError('network_error', 'edge gone') };
    await expect(createPrizedClient({ sdk: fake.Sdk, sandboxKey: 'j', owner: false })).rejects.toThrow('edge gone');
    expect(sandboxOf(owner).state).toBe('running');
  });

  it('a sandbox deleted between the list and the resume counts as not found', async () => {
    const labels = { [SANDBOX_KEY_LABEL]: sandboxKeyTag('gone-soon') };
    const stale = await fake.seed({ labels });
    stale.pause();
    stale.failResume = new PrizedError('sandbox_deleted', 'deleted', { status: 410 });
    const attach = await createPrizedClient({ sdk: fake.Sdk, sandboxKey: 'gone-soon', owner: false }).catch((e: unknown) => e);
    expect((attach as SandboxError).kind).toBe('unavailable');

    const again = await fake.seed({ labels });
    again.pause();
    again.failResume = new PrizedError('sandbox_deleted', 'deleted', { status: 410 });
    const c = await client({ sandboxKey: 'gone-soon' });
    expect(c.sandboxId).not.toBe(again.id);
    expect(fake.creates).toHaveLength(1);
  });

  it('resumes a sandbox found pausing; a refused resume is logged and left to the next command', async () => {
    const labels = { [SANDBOX_KEY_LABEL]: sandboxKeyTag('p') };
    const pausing = await fake.seed({ labels });
    pausing.state = 'pausing';
    const c = await client({ sandboxKey: 'p' });
    expect(c.sandboxId).toBe(pausing.id);
    expect(pausing.resumes).toBe(1);

    const refused = await fake.seed({ labels: { [SANDBOX_KEY_LABEL]: sandboxKeyTag('q') } });
    refused.pause();
    refused.failResume = new PrizedError('insufficient_balance', 'no balance', { status: 409 });
    const logs: string[] = [];
    const d = await client({ sandboxKey: 'q', log: m => logs.push(m) });
    expect(d.sandboxId).toBe(refused.id);
    expect(logs.join('\n')).toMatch(/resuming .* failed: no balance/);
  });

  it('kill on a keyed sandbox also removes any other sandbox left under its key', async () => {
    const c = await client({ sandboxKey: 'wipe' });
    const straggler = await fake.seed({ labels: { [SANDBOX_KEY_LABEL]: sandboxKeyTag('wipe') } }, -5_000);
    await c.kill();
    expect(sandboxOf(c).state).toBe('deleted');
    expect(straggler.state).toBe('deleted');
    expect(fake.deletesByLabels).toEqual([{ [SANDBOX_KEY_LABEL]: sandboxKeyTag('wipe') }]);
  });

  it('validates lifecycle numbers up front and keeps timers in range', async () => {
    for (const bad of [{ keepAliveMs: 0 }, { keepAliveMs: -1 }, { keepAliveMs: Number.POSITIVE_INFINITY }, { keepAliveMs: Number.NaN }, { heartbeatIntervalMs: 0 }, { pausedRetentionMin: -1 }]) {
      const err = await createPrizedClient({ sdk: fake.Sdk, ...bad }).catch((e: unknown) => e);
      expect(err, JSON.stringify(bad)).toBeInstanceOf(SandboxError);
    }
    expect(fake.creates).toHaveLength(0);
    // Default: half the window as sent in whole minutes, at least 15 s, never past a Node timer's limit.
    expect(heartbeatIntervalFor(120_000)).toBe(60_000);
    expect(heartbeatIntervalFor(30_000)).toBe(30_000);
    expect(heartbeatIntervalFor(10 ** 12)).toBe(40_320 * 30_000);
    expect(heartbeatIntervalFor(120_000, 10 ** 12)).toBe(2 ** 31 - 1);
    await client({ persistent: true, sandboxKey: 'r', pausedRetentionMin: 1.5, keepAliveMs: 10 ** 12 });
    expect(fake.creates[0]).toMatchObject({ autoDeleteMin: 2, autoPauseMin: 40_320 });
  });
});

describe('exec', () => {
  it('runs in the working directory with env, and reports the exit code', async () => {
    const c = await client();
    const r = await c.exec({ command: 'pwd; echo "$GREETING"; echo oops >&2; exit 3', env: { GREETING: 'hi' } });
    expect(r.stdout.toString()).toBe(`${join(sandboxOf(c).root, '/home/prized/work')}\nhi\n`);
    expect(r.stderr.toString()).toBe('oops\n');
    expect(r.exitCode).toBe(3);
    expect(r.timedOut).toBe(false);
    expect(sandboxOf(c).execs.at(-1)!.options).toMatchObject({ cwd: '/home/prized/work', fullOutput: false });
  });

  it('honours cwd; keeps whole output for Electric fetch() only, unless told', async () => {
    const auto = await client();
    const r = await auto.exec({ command: 'pwd', cwd: '/home/prized' });
    expect(r.stdout.toString().trim()).toBe(join(sandboxOf(auto).root, '/home/prized'));
    expect(sandboxOf(auto).execs.at(-1)!.options.fullOutput).toBe(false);
    await auto.exec({ command: 'true', env: { FETCH_URL: 'http://127.0.0.1:9/' } });
    expect(sandboxOf(auto).execs.at(-1)!.options.fullOutput).toBe(true);
    const always = await client({ fullOutput: true });
    await always.exec({ command: 'true' });
    expect(sandboxOf(always).execs.at(-1)!.options.fullOutput).toBe(true);
    const never = await client({ fullOutput: false });
    await never.exec({ command: 'true', env: { FETCH_URL: 'x' } });
    expect(sandboxOf(never).execs.at(-1)!.options.fullOutput).toBe(false);
  });

  it('says in stderr when the edge cut a stream', async () => {
    const c = await client();
    sandboxOf(c).plainCap = 5;
    const r = await c.exec({ command: 'printf 1234567890' });
    expect(r.stdout.toString()).toBe('12345');
    expect(r.stderr.toString()).toMatch(/\[prized: output cut at 4 MiB a stream\]/);
  });

  it('feeds stdin (text and bytes) and leaves no staged file behind', async () => {
    const c = await client();
    const text = await c.exec({ command: 'tr a-z A-Z', stdin: 'hello\nworld' });
    expect(text.stdout.toString()).toBe('HELLO\nWORLD');
    const bytes = Buffer.from([0, 1, 2, 255, 10, 0]);
    const bin = await c.exec({ command: 'base64 -w 0', stdin: bytes });
    expect(Buffer.from(bin.stdout.toString(), 'base64')).toEqual(bytes);
    const staged = join(sandboxOf(c).root, '/home/prized/.cache/electric-agents/stdin');
    expect(await hostReaddir(staged)).toEqual([]);
    // A command that ignores stdin, or exits early, still leaves nothing.
    await c.exec({ command: 'exit 0', stdin: 'unused' });
    expect(await hostReaddir(staged)).toEqual([]);
  });

  it('runs without stdin from /dev/null, not a pipe that hangs', async () => {
    const c = await client();
    const r = await c.exec({ command: 'cat; echo done', timeoutMs: 5_000 });
    expect(r.stdout.toString()).toBe('done\n');
  });

  it('turns a timeout into timedOut with what was printed, not a throw', async () => {
    const c = await client();
    const r = await c.exec({ command: 'echo started; sleep 5', timeoutMs: 300 });
    expect(r.timedOut).toBe(true);
    expect(r.exitCode).toBeNull();
    expect(r.stdout.toString()).toBe('started\n');
  });

  it('passes other SDK errors through and removes the staged stdin', async () => {
    const c = await client();
    const s = sandboxOf(c);
    const failing = s.exec.bind(s);
    s.exec = async () => {
      throw Object.assign(new Error('edge gone'), { code: 'network_error' });
    };
    await expect(c.exec({ command: 'cat', stdin: 'x' })).rejects.toThrow('edge gone');
    s.exec = failing;
    expect(await hostReaddir(join(s.root, '/home/prized/.cache/electric-agents/stdin'))).toEqual([]);
  });
});

describe('files', () => {
  let c: PrizedRemoteSandboxClient;
  let s: FakeSandbox;
  const W = '/home/prized/work';
  beforeEach(async () => {
    c = await client();
    s = sandboxOf(c);
  });

  it('writes and reads bytes through the files API, creating parents', async () => {
    const bytes = Buffer.from([0, 159, 146, 150, 255]);
    await c.writeFile(`${W}/deep/dir/blob.bin`, bytes);
    await c.writeFile(`${W}/a.txt`, 'text ü');
    expect(await c.readFile(`${W}/deep/dir/blob.bin`)).toEqual(bytes);
    expect((await c.readFile(`${W}/a.txt`)).toString()).toBe('text ü');
    expect(s.fileCalls.map(([op]) => op)).toEqual(['write', 'write', 'readBytes', 'readBytes']);
  });

  it('readFile: ENOENT for a missing file, EISDIR for a directory', async () => {
    expect(await errorCode(c.readFile(`${W}/missing`))).toBe('ENOENT');
    expect(await errorCode(c.readFile(W))).toBe('EISDIR');
  });

  it('readFile reads outside /home/prized, and through a link out of it, with a command', async () => {
    const expected = await hostReadFile('/etc/passwd');
    expect(await c.readFile('/etc/passwd')).toEqual(expected);
    await symlink('/etc/passwd', s.local(`${W}/passwd-link`));
    expect(await c.readFile(`${W}/passwd-link`)).toEqual(expected);
    expect(await errorCode(c.readFile('/etc/definitely-not-here'))).toBe('ENOENT');
  });

  it('mkdir: makes one level, EEXIST when there, ENOENT without a parent, -p when recursive', async () => {
    await c.mkdir(`${W}/one`);
    expect((await c.stat(`${W}/one`)).type).toBe('directory');
    expect(await errorCode(c.mkdir(`${W}/one`))).toBe('EEXIST');
    expect(await errorCode(c.mkdir(`${W}/no/such/parent`))).toBe('ENOENT');
    await c.mkdir(`${W}/no/such/parent`, { recursive: true });
    expect(await c.exists(`${W}/no/such/parent`)).toBe(true);
  });

  it('readdir: files, directories and links, odd names included; ENOENT and ENOTDIR', async () => {
    await c.writeFile(`${W}/a.txt`, 'a');
    await c.writeFile(`${W}/sub/x`, 'x');
    await c.writeFile(`${W}/tab\tand\nnewline name`, 'odd');
    await symlink('a.txt', s.local(`${W}/link`));
    const entries = [...(await c.readdir(W))].sort((x, y) => x.name.localeCompare(y.name));
    expect(entries).toEqual([
      { name: 'a.txt', type: 'file' },
      { name: 'link', type: 'symlink' },
      { name: 'sub', type: 'directory' },
      { name: 'tab\tand\nnewline name', type: 'file' },
    ]);
    expect(await c.readdir(`${W}/sub`)).toEqual([{ name: 'x', type: 'file' }]);
    expect(await errorCode(c.readdir(`${W}/missing`))).toBe('ENOENT');
    expect(await errorCode(c.readdir(`${W}/a.txt`))).toBe('ENOTDIR');
  });

  it('exists: true for files and directories, false for missing and dangling links', async () => {
    await c.writeFile(`${W}/f`, '');
    await symlink('nowhere', s.local(`${W}/dangling`));
    expect(await c.exists(`${W}/f`)).toBe(true);
    expect(await c.exists(W)).toBe(true);
    expect(await c.exists(`${W}/missing`)).toBe(false);
    expect(await c.exists(`${W}/dangling`)).toBe(false);
    expect(await c.exists(`${W}/it's "quoted" $(echo no)`)).toBe(false);
  });

  it('remove: a file; ENOENT when missing; a directory only when recursive', async () => {
    await c.writeFile(`${W}/f`, 'x');
    await c.remove(`${W}/f`);
    expect(await c.exists(`${W}/f`)).toBe(false);
    expect(await errorCode(c.remove(`${W}/f`))).toBe('ENOENT');
    await c.writeFile(`${W}/tree/a/b`, 'x');
    expect(await errorCode(c.remove(`${W}/tree`))).toBe('EISDIR');
    await c.remove(`${W}/tree`, { recursive: true });
    expect(await c.exists(`${W}/tree`)).toBe(false);
    expect(await errorCode(c.remove(`${W}/tree`, { recursive: true }))).toBe('ENOENT');
  });

  it('stat: type, size and mtime, following links; ENOENT when missing', async () => {
    await c.writeFile(`${W}/five`, '12345');
    await utimes(s.local(`${W}/five`), 1_700_000_000, 1_700_000_000);
    await symlink('five', s.local(`${W}/to-five`));
    expect(await c.stat(`${W}/five`)).toEqual({ type: 'file', size: 5, mtimeMs: 1_700_000_000_000 });
    expect((await c.stat(`${W}/to-five`)).type).toBe('file');
    expect((await c.stat(W)).type).toBe('directory');
    expect(await errorCode(c.stat(`${W}/missing`))).toBe('ENOENT');
  });

  it('a read with a command whose output was cut throws EFBIG instead of returning a short buffer', async () => {
    s.fullCap = 10;
    expect(await errorCode(c.readFile('/etc/passwd'))).toBe('EFBIG');
    await c.mkdir(`${W}/many`);
    for (const n of ['aaaa', 'bbbb', 'cccc']) await c.writeFile(`${W}/many/${n}`, '');
    expect(await errorCode(c.readdir(`${W}/many`))).toBe('EFBIG');
  });

  it('a filesystem command that times out is ETIMEDOUT', async () => {
    s.timeOutWhen = 'stat -L';
    expect(await errorCode(c.stat(W))).toBe('ETIMEDOUT');
  });

  it('writing onto a directory is EISDIR', async () => {
    const real = s.files.write;
    s.files.write = async () => {
      throw new PrizedError('unsupported_file_type', 'is a directory');
    };
    expect(await errorCode(c.writeFile(W, 'x'))).toBe('EISDIR');
    s.files.write = real;
  });

  it('mkdir of /home and /home/prized needs no round trip', async () => {
    const before = s.execs.length;
    await c.mkdir('/home/prized', { recursive: true });
    expect(await errorCode(c.mkdir('/home'))).toBe('EEXIST');
    expect(await errorCode(c.mkdir('/home/prized/'))).toBe('EEXIST');
    expect(s.execs.length).toBe(before);
  });

  it('quotes paths: spaces, quotes and $() reach the sandbox as they are', async () => {
    const odd = `${W}/it's a "file" $(touch pwned)`;
    await c.writeFile(odd, 'ok');
    expect(await c.exists(odd)).toBe(true);
    expect((await c.stat(odd)).size).toBe(2);
    await c.remove(odd);
    expect(await c.exists(`${W}/pwned`)).toBe(false);
  });
});

describe('lifecycle', () => {
  it('heartbeats while held; suspend stops it and keeps the sandbox', async () => {
    const c = await client({ persistent: true, sandboxKey: 'hb', heartbeatIntervalMs: 20 });
    const s = sandboxOf(c);
    await new Promise(r => setTimeout(r, 120));
    expect(s.keepAlives).toBeGreaterThan(1);
    await c.suspend();
    const after = s.keepAlives;
    await new Promise(r => setTimeout(r, 80));
    expect(s.keepAlives).toBe(after);
    expect(s.state).toBe('running');
    expect(s.deletes).toBe(0);
  });

  it('a failed keep-alive is logged, never thrown', async () => {
    const logs: string[] = [];
    const c = await client({ heartbeatIntervalMs: 20, log: m => logs.push(m) });
    sandboxOf(c).failKeepAlive = new Error('blip');
    await new Promise(r => setTimeout(r, 80));
    expect(logs.some(m => m.includes('keep-alive') && m.includes('blip'))).toBe(true);
  });

  it('kill deletes the sandbox, stops the heartbeat, and is quiet when it is already gone', async () => {
    const c = await client({ heartbeatIntervalMs: 20 });
    const s = sandboxOf(c);
    await c.kill();
    expect(s.state).toBe('deleted');
    const after = s.keepAlives;
    await new Promise(r => setTimeout(r, 60));
    expect(s.keepAlives).toBe(after);
    await expect(c.kill()).resolves.toBeUndefined();
  });

  it('adaptPrized and connectOrCreatePrizedSandbox work on their own, like the e2b adapter pieces', async () => {
    const handle = await connectOrCreatePrizedSandbox(fake.Sdk, { persistent: false, keepAliveMs: 600_000 });
    expect(fake.creates[0]).toMatchObject({ autoPauseMin: 10, autoDeleteMin: 1 });
    const c = adaptPrized(handle);
    clients.push(c);
    expect((await c.exec({ command: 'echo ok' })).stdout.toString()).toBe('ok\n');
    await hostWriteFile(sandboxOf(c).local('/home/prized/work/x'), 'y');
    expect((await c.readFile('/home/prized/work/x')).toString()).toBe('y');
  });
});
