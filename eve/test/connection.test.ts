/**
 * The connection eve builds its sandbox API on: spawn, readFile, writeFiles, removePath. Against
 * the fake Prized API, whose commands run with the real bash.
 */
import assert from 'node:assert/strict';
import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { afterEach, beforeEach, describe, it } from 'node:test';

import type { SandboxConnection } from 'eve/sandbox/provider';

import type { PrizedSandboxExtensions } from '../src/connection.ts';
import { createPrizedImplementation } from '../src/provider.ts';
import { EVE, readText, run, sessionContext } from './fake-eve.ts';
import { FakePrized, type FakeSandbox } from './fake-prized.ts';

const KEY = 'dcp_0000000000000000000000000000';
const WORK = '/home/prized/work';

let api: FakePrized;
let sandbox: FakeSandbox;
let connection: SandboxConnection<PrizedSandboxExtensions>;

let sessions = 0;

async function open(env: Record<string, string> = {}): Promise<void> {
  const ctx = sessionContext(`session-${++sessions}`);
  const started = await createPrizedImplementation({ apiKey: KEY, env }, api, EVE).start(ctx, undefined, {});
  connection = started.connection;
  sandbox = api.get(started.state.sandboxId);
}

beforeEach(async () => {
  api = new FakePrized();
  await open();
});
afterEach(() => api.close());

const bytes = (text: string) => new TextEncoder().encode(text);

/** Put a file straight on the fake's disk, at a sandbox path. */
function plant(path: string, content: string): void {
  mkdirSync(dirname(sandbox.host(path)), { recursive: true });
  writeFileSync(sandbox.host(path), content);
}

describe('spawn', () => {
  it('runs a bash login script in the working directory with the call env over AI_AGENT', async () => {
    mkdirSync(sandbox.host(`${WORK}/sub`), { recursive: true });
    const result = await run(connection, 'pwd; echo "$AI_AGENT/$X"; shopt -q login_shell && echo login', {
      workingDirectory: `${WORK}/sub`,
      env: { X: 'call' },
    });
    assert.deepEqual(result, { exitCode: 0, stdout: '/home/prized/work/sub\neve/call\nlogin\n', stderr: '' });
    const call = api.execs.at(-1)!;
    assert.deepEqual(call.command, ['bash', '-lc', 'pwd; echo "$AI_AGENT/$X"; shopt -q login_shell && echo login']);
    assert.equal(call.options.background, true);
  });

  it("lets a call's env, or the environment's env, override AI_AGENT", async () => {
    assert.equal((await run(connection, 'echo $AI_AGENT', { env: { AI_AGENT: 'mine' } })).stdout, 'mine\n');
    await open({ AI_AGENT: 'custom' });
    assert.equal((await run(connection, 'echo $AI_AGENT')).stdout, 'custom\n');
    assert.deepEqual(api.execs.at(-1)!.options.env, {}, 'nothing added per call once env sets it');
  });

  it('streams stdout and stderr and reports a non-zero exit', async () => {
    const result = await run(connection, 'echo out; echo err >&2; exit 3');
    assert.deepEqual(result, { exitCode: 3, stdout: 'out\n', stderr: 'err\n' });
  });

  it('kill() stops the process tree', async () => {
    const process = await connection.spawn({ command: 'sleep 30 & sleep 30; wait', env: {}, workingDirectory: WORK });
    await process.kill();
    const { exitCode } = await process.wait();
    assert.notEqual(exitCode, 0);
    assert.equal(sandbox.runs.at(-1)!.killedWith, 'SIGKILL');
  });

  it('aborting the signal kills the process', async () => {
    const controller = new AbortController();
    const process = await connection.spawn({ command: 'sleep 30', env: {}, workingDirectory: WORK, signal: controller.signal });
    controller.abort();
    const { exitCode } = await process.wait();
    assert.notEqual(exitCode, 0);
  });

  it('rejects stdin, and an already aborted signal, without starting anything', async () => {
    const before = api.execs.length;
    await assert.rejects(
      connection.spawn({ command: 'cat', env: {}, workingDirectory: WORK, stdin: true }),
      /don't take stdin/,
    );
    await assert.rejects(
      connection.spawn({ command: 'true', env: {}, workingDirectory: WORK, signal: AbortSignal.abort() }),
      { name: 'AbortError' },
    );
    assert.equal(api.execs.length, before);
  });

  it('removes the run’s output files once it has finished', async () => {
    await run(connection, 'true');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(sandbox.runs.at(-1)!.cleaned, true);
  });
});

describe('readFile', () => {
  it('reads bytes in the home through the files API', async () => {
    plant(`${WORK}/a.txt`, 'alpha');
    assert.equal(await readText(connection, `${WORK}/a.txt`), 'alpha');
  });

  it('returns null for a missing path, also under a file', async () => {
    assert.equal(await connection.readFile({ path: `${WORK}/missing.txt` }), null);
    plant(`${WORK}/file`, 'x');
    assert.equal(await connection.readFile({ path: `${WORK}/file/below` }), null);
    assert.equal(await connection.readFile({ path: '/tmp/eve-missing/x' }), null);
  });

  it('rejects a directory, in the home and outside it', async () => {
    await assert.rejects(connection.readFile({ path: WORK }), { code: 'EISDIR' });
    mkdirSync(sandbox.host('/opt/dir'), { recursive: true });
    await assert.rejects(connection.readFile({ path: '/opt/dir' }), { code: 'EISDIR' });
  });

  it('reads a file outside the home through a staged copy, and removes the copy', async () => {
    plant('/etc/eve-test.conf', 'outside');
    assert.equal(await readText(connection, '/etc/eve-test.conf'), 'outside');
    assert.deepEqual(stagingLeftovers(), []);
  });

  it('reads a file behind a link that leaves the home', async () => {
    plant('/opt/target.txt', 'linked');
    // On the fake's disk a link must name the host path standing for its sandbox target.
    symlinkSync(sandbox.host('/opt/target.txt'), sandbox.host(`${WORK}/link.txt`));
    assert.equal(await readText(connection, `${WORK}/link.txt`), 'linked');
    assert.deepEqual(stagingLeftovers(), []);
  });
});

function stagingLeftovers(): string[] {
  const dir = sandbox.host('/home/prized/.cache/prized-eve');
  return existsSync(dir) ? readdirSync(dir) : [];
}

describe('writeFiles', () => {
  it('writes every file, making parents and overwriting', async () => {
    plant(`${WORK}/old.txt`, 'old');
    await connection.writeFiles({
      files: [
        { path: `${WORK}/old.txt`, content: bytes('new') },
        { path: `${WORK}/deep/er/file.txt`, content: bytes('deep') },
        { path: '/home/prized/.agents/skills/s/SKILL.md', content: bytes('skill') },
      ],
    });
    assert.equal(readFileSync(sandbox.host(`${WORK}/old.txt`), 'utf8'), 'new');
    assert.equal(readFileSync(sandbox.host(`${WORK}/deep/er/file.txt`), 'utf8'), 'deep');
    assert.equal(readFileSync(sandbox.host('/home/prized/.agents/skills/s/SKILL.md'), 'utf8'), 'skill');
  });

  it('writes outside the home through a staged upload, keeping an existing mode, and cleans up', async () => {
    plant('/opt/tool.sh', 'old');
    chmodSync(sandbox.host('/opt/tool.sh'), 0o755);
    await connection.writeFiles({
      files: [
        { path: '/opt/tool.sh', content: bytes('echo new') },
        { path: '/tmp/eve/new/file.bin', content: new Uint8Array([0, 255, 10]) },
      ],
    });
    assert.equal(readFileSync(sandbox.host('/opt/tool.sh'), 'utf8'), 'echo new');
    assert.equal(statSync(sandbox.host('/opt/tool.sh')).mode & 0o777, 0o755);
    assert.deepEqual([...readFileSync(sandbox.host('/tmp/eve/new/file.bin'))], [0, 255, 10]);
    assert.deepEqual(stagingLeftovers(), []);
  });

  it('round-trips binary content', async () => {
    const all = new Uint8Array(256).map((_, i) => i);
    await connection.writeFiles({ files: [{ path: `${WORK}/all.bin`, content: all }] });
    assert.deepEqual(await connection.readFile({ path: `${WORK}/all.bin` }), all);
  });

  it('rejects writing over a directory', async () => {
    mkdirSync(sandbox.host(`${WORK}/dir`), { recursive: true });
    await assert.rejects(connection.writeFiles({ files: [{ path: `${WORK}/dir`, content: bytes('x') }] }), { code: 'EISDIR' });
    mkdirSync(sandbox.host('/opt/dir2'), { recursive: true });
    await assert.rejects(connection.writeFiles({ files: [{ path: '/opt/dir2', content: bytes('x') }] }), { code: 'EISDIR' });
  });

  it('writes many files', async () => {
    const files = Array.from({ length: 25 }, (_, i) => ({ path: `${WORK}/many/${i}.txt`, content: bytes(String(i)) }));
    await connection.writeFiles({ files });
    for (let i = 0; i < 25; i++) assert.equal(readFileSync(sandbox.host(`${WORK}/many/${i}.txt`), 'utf8'), String(i));
  });
});

describe('removePath', () => {
  it('removes a file; a missing path is an error unless force', async () => {
    plant(`${WORK}/a.txt`, 'a');
    await connection.removePath({ path: `${WORK}/a.txt` });
    assert.equal(existsSync(sandbox.host(`${WORK}/a.txt`)), false);
    await assert.rejects(connection.removePath({ path: `${WORK}/a.txt` }), { code: 'ENOENT' });
    await connection.removePath({ path: `${WORK}/a.txt`, force: true });
  });

  it('removes a non-empty directory only when recursive', async () => {
    plant(`${WORK}/d/x.txt`, 'x');
    await assert.rejects(connection.removePath({ path: `${WORK}/d` }), { code: 'ENOTEMPTY' });
    await connection.removePath({ path: `${WORK}/d`, recursive: true });
    assert.equal(existsSync(sandbox.host(`${WORK}/d`)), false);
    mkdirSync(sandbox.host(`${WORK}/empty`));
    await connection.removePath({ path: `${WORK}/empty` });
    assert.equal(existsSync(sandbox.host(`${WORK}/empty`)), false);
  });

  it('removes a link, not what it points to', async () => {
    plant(`${WORK}/target/keep.txt`, 'keep');
    symlinkSync(sandbox.host(`${WORK}/target`), sandbox.host(`${WORK}/link`));
    await connection.removePath({ path: `${WORK}/link`, recursive: true });
    assert.throws(() => lstatSync(sandbox.host(`${WORK}/link`)));
    assert.equal(readFileSync(sandbox.host(`${WORK}/target/keep.txt`), 'utf8'), 'keep');
  });
});

describe('a gone sandbox', () => {
  it('surfaces the SDK error from every call instead of reading as a missing file', async () => {
    await sandbox.delete();
    await assert.rejects(connection.readFile({ path: `${WORK}/a.txt` }), { code: 'not_found' });
    await assert.rejects(run(connection, 'true'), { code: 'not_found' });
    await assert.rejects(connection.writeFiles({ files: [{ path: `${WORK}/a`, content: bytes('a') }] }), { code: 'not_found' });
  });
});
