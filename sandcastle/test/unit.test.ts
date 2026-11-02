/**
 * Offline tests: `@prized/sdk`'s Sandbox is replaced by the local-directory fake in fake-sdk.ts, so the
 * provider's commands really run (in bash on this machine) and its files really move.
 */
import { execFileSync } from 'node:child_process';
import { chmod, lstat, mkdir, mkdtemp, readFile, readdir, readlink, rm, stat, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FakeSandbox } from './fake-sdk.js';

vi.mock('@prized/sdk', async importOriginal => {
  const sdk = await importOriginal<typeof import('@prized/sdk')>();
  const fake = await import('./fake-sdk.js');
  fake.real.PrizedError = sdk.PrizedError;
  return { ...sdk, Sandbox: fake.FakeSandbox };
});

const { prized, WORKTREE_PATH, TRANSFER_DIR, DEFAULT_TEMPLATE, CREATED_BY_LABEL, insideHome, shellQuote } = await import('../src/index.js');
const { run } = await import('@ai-hero/sandcastle');
type AgentProvider = import('@ai-hero/sandcastle').AgentProvider;
type IsolatedSandboxHandle = import('@ai-hero/sandcastle').IsolatedSandboxHandle;
/** The provider as Sandcastle calls it: `tag` and `create` are marked internal in its published types. */
type Internal = { tag: string; name: string; env: Record<string, string>; create(o: { env: Record<string, string> }): Promise<IsolatedSandboxHandle> };
const internal = (provider: unknown): Internal => provider as Internal;

const KEY = 'dcp_0000000000000000000000000000';
const scratch: string[] = [];

async function tempDir(prefix: string): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), prefix));
  scratch.push(dir);
  return dir;
}

/** A handle on a fresh fake sandbox, made the way Sandcastle makes one. */
async function open(options: Parameters<typeof prized>[0] = {}, env: Record<string, string> = {}) {
  const provider = internal(prized({ apiKey: KEY, ...options }));
  const handle = await provider.create({ env });
  const sandbox = FakeSandbox.all.at(-1)!;
  return { provider, handle, sandbox };
}

async function transferLeft(sandbox: FakeSandbox): Promise<string[]> {
  return await readdir(sandbox.local(TRANSFER_DIR));
}

beforeEach(() => {
  FakeSandbox.reset();
  vi.unstubAllEnvs();
  vi.stubEnv('PRIZED_API_KEY', '');
});

afterEach(async () => {
  await FakeSandbox.cleanup();
});

afterAll(async () => {
  await Promise.all(scratch.map(dir => rm(dir, { recursive: true, force: true })));
});

describe('prized()', () => {
  it('is an isolated provider named prized, with the env it was given, and makes no request', () => {
    const provider = internal(prized({ apiKey: KEY, env: { FOO: 'bar' } }));
    expect(provider.tag).toBe('isolated');
    expect(provider.name).toBe('prized');
    expect(provider.env).toEqual({ FOO: 'bar' });
    expect(prized().env).toEqual({});
    expect(FakeSandbox.creates).toHaveLength(0);
  });

  it('creates from prized-agent-v1 with the run env, its labels and the orphan safety net', async () => {
    const { handle, sandbox } = await open({ labels: { team: 'web', 'created-by': 'me' } }, { ANTHROPIC_API_KEY: 'sk-test' });
    const [created] = FakeSandbox.creates;
    expect(created).toEqual({
      apiKey: KEY,
      template: DEFAULT_TEMPLATE,
      labels: { team: 'web', ...CREATED_BY_LABEL },
      autoPauseMin: 10,
      autoDeleteMin: 60,
      envVars: { ANTHROPIC_API_KEY: 'sk-test' },
    });
    expect(handle.worktreePath).toBe(WORKTREE_PATH);
    expect((await stat(sandbox.local(WORKTREE_PATH))).isDirectory()).toBe(true);
    expect((await stat(sandbox.local(TRANSFER_DIR))).isDirectory()).toBe(true);
  });

  it('passes template, resources, run time, pause and delete settings and the API origin through', async () => {
    await open({
      template: 'my-template',
      resources: { vcpu: 4, memoryGb: 8 },
      maxRuntimeMin: 30,
      autoPauseMin: 0,
      autoDeleteMin: 0,
      baseUrl: 'http://localhost:9',
    });
    expect(FakeSandbox.creates[0]).toMatchObject({
      template: 'my-template',
      resources: { vcpu: 4, memoryGb: 8 },
      maxRuntimeMin: 30,
      autoPauseMin: 0,
      autoDeleteMin: 0,
      baseUrl: 'http://localhost:9',
    });
    expect(FakeSandbox.creates[0]).not.toHaveProperty('envVars');
  });

  it('reads PRIZED_API_KEY from the environment', async () => {
    vi.stubEnv('PRIZED_API_KEY', KEY);
    await internal(prized()).create({ env: {} });
    expect(FakeSandbox.creates[0]?.apiKey).toBe(KEY);
  });

  it('says where to get a key, and creates nothing, without one', async () => {
    await expect(internal(prized()).create({ env: {} })).rejects.toThrow(/PRIZED_API_KEY.*api-keys\?via=sandcastle.*auth\.md\?via=sandcastle/s);
    expect(FakeSandbox.creates).toHaveLength(0);
  });

  it('deletes the sandbox when its setup fails', async () => {
    FakeSandbox.failNextSetup = true;
    await expect(internal(prized({ apiKey: KEY })).create({ env: {} })).rejects.toThrow(/Could not create \/home\/prized\/workspace: refused/);
    expect(FakeSandbox.all[0]?.deleted).toBe(true);
  });
});

describe('exec', () => {
  it('runs in the worktree by default and returns stdout, stderr and the exit code', async () => {
    const { handle, sandbox } = await open();
    expect(await handle.exec('pwd; echo oops >&2; exit 3')).toEqual({
      stdout: `${sandbox.local(WORKTREE_PATH)}\n`,
      stderr: 'oops\n',
      exitCode: 3,
    });
    expect(sandbox.execs.at(-1)?.options.cwd).toBe(WORKTREE_PATH);
  });

  it('runs where cwd says', async () => {
    const { handle, sandbox } = await open();
    const result = await handle.exec('pwd', { cwd: '/home/prized' });
    expect(result.stdout.trim()).toBe(sandbox.local('/home/prized'));
    expect(sandbox.execs.at(-1)?.options.cwd).toBe('/home/prized');
  });

  it('runs with no time limit unless commandTimeoutMs sets one', async () => {
    const a = await open();
    await a.handle.exec('true');
    expect(a.sandbox.execs.at(-1)?.options.timeoutMs).toBe(0);
    const b = await open({ commandTimeoutMs: 5000 });
    await b.handle.exec('true', { onLine: () => {} });
    expect(b.sandbox.execs.at(-1)?.options.timeoutMs).toBe(5000);
  });

  it('streams stdout to onLine line by line while the command runs', async () => {
    const { handle } = await open();
    const seen: { line: string; at: number }[] = [];
    const started = Date.now();
    const result = await handle.exec("printf 'one\\ntwo\\n'; sleep 1; printf 'three\\nlast'", {
      onLine: line => seen.push({ line, at: Date.now() - started }),
    });
    expect(seen.map(s => s.line)).toEqual(['one', 'two', 'three', 'last']);
    // Live, not batched after exit: the first lines arrive a second before the last.
    expect(seen[2]!.at - seen[0]!.at).toBeGreaterThanOrEqual(800);
    expect(result).toEqual({ stdout: 'one\ntwo\nthree\nlast', stderr: '', exitCode: 0 });
  });

  it('keeps only the tail of a long streamed output, while onLine gets every line', async () => {
    const { handle } = await open({ maxOutputTailChars: 100 });
    let lines = 0;
    const result = await handle.exec('for i in $(seq 1 1000); do echo "line $i"; done; seq 1 2000 >&2', { onLine: () => lines++ });
    expect(lines).toBe(1000);
    expect(result.stdout.length).toBeLessThanOrEqual(100);
    expect(result.stdout.endsWith('line 999\nline 1000')).toBe(true);
    expect(result.stderr.length).toBeLessThanOrEqual(100);
    expect(result.stderr.endsWith('1999\n2000\n')).toBe(true);
  });

  it('pipes stdin to the command and leaves no file behind', async () => {
    const { handle, sandbox } = await open();
    const prompt = `line one\nit's "quoted" $HOME \`x\`\n${'x'.repeat(200_000)}`;
    const result = await handle.exec('cat; echo; echo done', { stdin: prompt, onLine: () => {} });
    expect(result.exitCode).toBe(0);
    const streamed = await handle.exec('wc -c', { stdin: prompt });
    expect(streamed.stdout.trim()).toBe(String(Buffer.byteLength(prompt)));
    expect(await transferLeft(sandbox)).toEqual([]);
  });

  it('pipes stdin to a command that ends in a comment or exits early', async () => {
    const { handle } = await open();
    expect((await handle.exec('cat # a comment', { stdin: 'hi' })).stdout).toBe('hi');
    expect((await handle.exec('exit 7', { stdin: 'hi' })).exitCode).toBe(7);
  });

  it('refuses sudo with a reason, running nothing', async () => {
    const { handle, sandbox } = await open();
    const before = sandbox.execs.length;
    await expect(handle.exec('apt-get install -y jq', { sudo: true })).rejects.toThrow(/without root.*sudo: true/s);
    expect(sandbox.execs.length).toBe(before);
  });
});

describe('copyIn', () => {
  it('uploads a file inside the home straight to its path, keeping its mode', async () => {
    const { handle, sandbox } = await open();
    const dir = await tempDir('psc-host-');
    await writeFile(join(dir, 'run.sh'), '#!/bin/sh\necho ran\n');
    await chmod(join(dir, 'run.sh'), 0o755);
    await handle.copyIn(join(dir, 'run.sh'), `${WORKTREE_PATH}/bin/run.sh`);
    expect(sandbox.fileCalls.at(-1)).toEqual(['upload', `${WORKTREE_PATH}/bin/run.sh`]);
    expect((await handle.exec('./bin/run.sh')).stdout).toBe('ran\n');
    expect((await stat(sandbox.local(`${WORKTREE_PATH}/bin/run.sh`))).mode & 0o777).toBe(0o755);
  });

  it('places a file outside the home (where sync-in puts its bundle) through the transfer directory', async () => {
    const { handle, sandbox } = await open();
    const host = await tempDir('psc-host-');
    await writeFile(join(host, 'repo.bundle'), 'bundle bytes');
    // As Sandcastle does: mktemp in the sandbox, then copyIn to a path under it.
    const sandboxTmp = (await handle.exec('mktemp -d -t sandcastle-XXXXXX')).stdout.trim();
    await handle.copyIn(join(host, 'repo.bundle'), `${sandboxTmp}/repo.bundle`);
    expect(await readFile(`${sandboxTmp}/repo.bundle`, 'utf8')).toBe('bundle bytes');
    expect(await transferLeft(sandbox)).toEqual([]);
  });

  it('copies a directory as one tarball, keeping nesting, modes, links and empty directories', async () => {
    const { handle, sandbox } = await open();
    const src = await tempDir('psc-dir-');
    await mkdir(join(src, 'a/b'), { recursive: true });
    await mkdir(join(src, 'empty'));
    await writeFile(join(src, 'a/b/deep.txt'), 'deep');
    await writeFile(join(src, 'tool'), '#!/bin/sh\n');
    await chmod(join(src, 'tool'), 0o750);
    await symlink('a/b/deep.txt', join(src, 'link'));
    await handle.copyIn(src, `${WORKTREE_PATH}/copied`);
    const dest = sandbox.local(`${WORKTREE_PATH}/copied`);
    expect(await readFile(join(dest, 'a/b/deep.txt'), 'utf8')).toBe('deep');
    expect((await stat(join(dest, 'tool'))).mode & 0o777).toBe(0o750);
    expect(await readlink(join(dest, 'link'))).toBe('a/b/deep.txt');
    expect((await lstat(join(dest, 'empty'))).isDirectory()).toBe(true);
    expect(sandbox.fileCalls.filter(([op]) => op === 'upload')).toHaveLength(1);
    expect(await transferLeft(sandbox)).toEqual([]);
  });

  it('copies a directory to a path outside the home', async () => {
    const { handle } = await open();
    const src = await tempDir('psc-dir-');
    await writeFile(join(src, 'f.txt'), 'f');
    const sandboxTmp = (await handle.exec('mktemp -d')).stdout.trim();
    await handle.copyIn(src, `${sandboxTmp}/dir`);
    expect(await readFile(`${sandboxTmp}/dir/f.txt`, 'utf8')).toBe('f');
  });

  it('fails, and cleans up, when the file cannot be placed', async () => {
    const { handle, sandbox } = await open();
    const host = await tempDir('psc-host-');
    await writeFile(join(host, 'f'), 'x');
    const blocker = (await handle.exec('f=$(mktemp); echo "$f"')).stdout.trim();
    await expect(handle.copyIn(join(host, 'f'), `${blocker}/under-a-file`)).rejects.toThrow(/copyIn to .* failed \(exit 1\)/);
    expect(await transferLeft(sandbox)).toEqual([]);
  });
});

describe('copyFileOut', () => {
  it('downloads a file inside the home, making the host directories', async () => {
    const { handle } = await open();
    await handle.exec('mkdir -p out && printf "result" > out/r.txt');
    const host = join(await tempDir('psc-out-'), 'x/y/r.txt');
    await handle.copyFileOut(`${WORKTREE_PATH}/out/r.txt`, host);
    expect(await readFile(host, 'utf8')).toBe('result');
  });

  it('copies a file outside the home (where sync-out writes its patches) through the transfer directory', async () => {
    const { handle, sandbox } = await open();
    const patch = (await handle.exec('d=$(mktemp -d -t sandcastle-patches-XXXXXX); printf patch > "$d/0001.patch"; echo "$d/0001.patch"')).stdout.trim();
    const host = join(await tempDir('psc-out-'), '0001.patch');
    await handle.copyFileOut(patch, host);
    expect(await readFile(host, 'utf8')).toBe('patch');
    expect(await transferLeft(sandbox)).toEqual([]);
  });

  it('fails on a missing file, leaving nothing behind', async () => {
    const { handle, sandbox } = await open();
    const host = join(await tempDir('psc-out-'), 'f');
    await expect(handle.copyFileOut('/tmp/does-not-exist', host)).rejects.toThrow(/copyFileOut of \/tmp\/does-not-exist failed/);
    await expect(handle.copyFileOut(`${WORKTREE_PATH}/missing`, host)).rejects.toMatchObject({ code: 'not_found' });
    expect(await transferLeft(sandbox)).toEqual([]);
  });
});

describe('close', () => {
  it('deletes the sandbox once, however often it is called', async () => {
    const { handle, sandbox } = await open();
    await handle.close();
    await handle.close();
    expect(sandbox.deleted).toBe(true);
    expect(sandbox.deletes).toBe(1);
  });

  it('counts a sandbox that is already gone as closed', async () => {
    const { handle, sandbox } = await open();
    await sandbox.delete();
    await expect(handle.close()).resolves.toBeUndefined();
  });
});

describe('helpers', () => {
  it('knows which paths the files API serves', () => {
    expect(insideHome('/home/prized/workspace/a')).toBe(true);
    expect(insideHome('/home/prized/../etc/passwd')).toBe(false);
    expect(insideHome('/home/prizedx/a')).toBe(false);
    expect(insideHome('/tmp/sandcastle-1/repo.bundle')).toBe(false);
  });

  it('quotes for the shell', () => {
    expect(execFileSync('bash', ['-c', `printf %s ${shellQuote("it's $HOME `x`")}`]).toString()).toBe("it's $HOME `x`");
  });
});

/**
 * Sandcastle's own run(), end to end against the fake: sync-in (a git bundle copied to /tmp in the
 * sandbox and cloned), the agent's streamed exec with its prompt on stdin, sync-out (patches copied
 * back out of /tmp) and teardown. The agent is a stand-in script, not a model: what it proves is the
 * provider's seam, not an agent's.
 */
describe("Sandcastle's run() over the provider", () => {
  const scriptedAgent: AgentProvider = {
    name: 'scripted',
    env: {},
    captureSessions: false,
    buildPrintCommand: () => ({
      command:
        'read -r first; printf "%s\\n" "$first" > agent-note.txt; git add agent-note.txt; ' +
        'git -c user.name=Agent -c user.email=agent@example.com commit -qm "agent: note"; echo "RESULT:wrote note"',
      stdin: 'write the note\nsecond line',
    }),
    parseStreamLine: (line: string): unknown[] => (line.startsWith('RESULT:') ? [{ type: 'result', result: line.slice(7) }] : [{ type: 'text', text: line }]),
  } as unknown as AgentProvider;

  it('lands the agent commit on the host branch and deletes the sandbox', async () => {
    const repo = await tempDir('psc-repo-');
    const git = (...args: string[]) => execFileSync('git', args, { cwd: repo, env: { ...process.env, GIT_CONFIG_NOSYSTEM: '1', HOME: repo } }).toString().trim();
    git('init', '-q', '-b', 'main');
    git('-c', 'user.name=Host', '-c', 'user.email=host@example.com', 'commit', '-q', '--allow-empty', '-m', 'init');
    git('config', 'user.name', 'Host');
    git('config', 'user.email', 'host@example.com');

    const result = await run({
      agent: scriptedAgent,
      sandbox: prized({ apiKey: KEY }),
      prompt: 'write the note',
      cwd: repo,
      branchStrategy: { type: 'branch', branch: 'agent/note' },
      logging: { type: 'file', path: join(repo, '.sandcastle-log.txt') },
    } as Parameters<typeof run>[0]);

    expect(result.commits.length).toBe(1);
    expect(git('show', 'agent/note:agent-note.txt')).toBe('write the note');
    expect(git('log', '-1', '--format=%s', 'agent/note')).toBe('agent: note');
    const sandbox = FakeSandbox.all.at(-1)!;
    expect(sandbox.deleted).toBe(true);
    // Sync-in copied the bundle to /tmp in the sandbox, and sync-out copied patches back from there.
    expect(sandbox.fileCalls.some(([op, path]) => op === 'upload' && path.startsWith(`${TRANSFER_DIR}/in-`))).toBe(true);
    expect(sandbox.execs.some(e => e.options.onStdout !== undefined)).toBe(true);
  }, 60_000);
});
