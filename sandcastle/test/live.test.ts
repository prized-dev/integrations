/**
 * Against the real API: `PRIZED_LIVE=1 npm run test:live` with PRIZED_API_KEY set. Creates one small
 * sandbox (2 vCPU, 4 GB) labelled run=<this run>, runs the handle contract against it, and deletes it,
 * failures included. Billed to the key's workspace: under a minute of one sandbox.
 */
import { chmod, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { IsolatedSandboxHandle } from '@ai-hero/sandcastle';
import { PrizedError, Sandbox } from '@prized/sdk';

import { prized, transferLimits, WORKTREE_PATH } from '../src/index.js';

const LIVE = Boolean(process.env.PRIZED_API_KEY) && process.env.PRIZED_LIVE === '1';
const RUN = `sandcastle-test-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;

describe.skipIf(!LIVE)('prized() against the real API', () => {
  let handle: IsolatedSandboxHandle;
  let sandbox: Sandbox;
  let host: string;

  beforeAll(async () => {
    host = await mkdtemp(join(tmpdir(), 'psc-live-'));
    const provider = prized({ labels: { run: RUN }, resources: { vcpu: 2, memoryGb: 4 }, maxRuntimeMin: 30 }) as unknown as {
      create(o: { env: Record<string, string> }): Promise<IsolatedSandboxHandle>;
    };
    const started = Date.now();
    handle = await provider.create({ env: { SANDCASTLE_LIVE_MARK: 'from-create' } });
    console.log(`create: ${Date.now() - started} ms`);
    [sandbox] = await Sandbox.list({ labels: { run: RUN } });
    console.log(`sandbox ${sandbox!.id} (${sandbox!.template})`);
  });

  afterAll(async () => {
    await Sandbox.deleteByLabels({ run: RUN }).catch(() => {});
    await rm(host, { recursive: true, force: true });
  });

  it('runs in the worktree, with the run env and the agent CLIs of prized-agent-v1', async () => {
    let started = Date.now();
    const result = await handle.exec('pwd; echo "$SANDCASTLE_LIVE_MARK"; command -v claude codex git; echo oops >&2; exit 4');
    console.log(`buffered exec: ${Date.now() - started} ms`);
    expect(result.exitCode).toBe(4);
    expect(result.stderr).toBe('oops\n');
    const [pwd, mark, ...tools] = result.stdout.trim().split('\n');
    expect(pwd).toBe(WORKTREE_PATH);
    expect(mark).toBe('from-create');
    expect(tools).toHaveLength(3);
    started = Date.now();
    await handle.exec('true');
    console.log(`buffered exec (true): ${Date.now() - started} ms`);
  });

  it('streams stdout line by line while the command runs', async () => {
    const seen: { line: string; at: number }[] = [];
    const started = Date.now();
    const result = await handle.exec("printf 'one\\ntwo\\n'; sleep 3; printf 'three\\nlast'", {
      onLine: line => seen.push({ line, at: Date.now() - started }),
    });
    console.log(`streamed: ${JSON.stringify(seen)}`);
    expect(seen.map(s => s.line)).toEqual(['one', 'two', 'three', 'last']);
    expect(seen[2]!.at - seen[0]!.at).toBeGreaterThanOrEqual(2000);
    expect(result).toEqual({ stdout: 'one\ntwo\nthree\nlast', stderr: '', exitCode: 0 });
  });

  it('pipes a large stdin', async () => {
    const prompt = `it's "quoted" $HOME\n${'x'.repeat(300_000)}`;
    const result = await handle.exec('wc -c; ls -A /home/prized/.sandcastle-transfer | wc -l', { stdin: prompt, onLine: () => {} });
    expect(result.stdout.split('\n')).toEqual([String(Buffer.byteLength(prompt)), '0']);
  });

  it('copies a bundle into /tmp, a directory into the worktree, and files back out', async () => {
    await writeFile(join(host, 'repo.bundle'), 'bundle bytes');
    await chmod(join(host, 'repo.bundle'), 0o640);
    const sandboxTmp = (await handle.exec('mktemp -d -t sandcastle-XXXXXX')).stdout.trim();
    expect(sandboxTmp.startsWith('/tmp/')).toBe(true);
    await handle.copyIn(join(host, 'repo.bundle'), `${sandboxTmp}/repo.bundle`);
    expect((await handle.exec(`cat ${sandboxTmp}/repo.bundle; echo; stat -c %a ${sandboxTmp}/repo.bundle`)).stdout).toBe('bundle bytes\n640\n');

    const dir = join(host, 'dir');
    await mkdir(join(dir, 'a/b'), { recursive: true });
    await mkdir(join(dir, 'empty'));
    await writeFile(join(dir, 'a/b/deep.txt'), 'deep');
    await writeFile(join(dir, 'tool'), '#!/bin/sh\necho tool ran\n');
    await chmod(join(dir, 'tool'), 0o755);
    await symlink('a/b/deep.txt', join(dir, 'link'));
    await handle.copyIn(dir, `${WORKTREE_PATH}/copied`);
    const listing = await handle.exec('cat copied/link; echo; ./copied/tool; test -d copied/empty && echo empty-dir', {});
    expect(listing.stdout).toBe('deep\ntool ran\nempty-dir\n');

    await handle.exec(`printf patch > ${sandboxTmp}/0001.patch; printf note > note.txt`);
    await handle.copyFileOut(`${sandboxTmp}/0001.patch`, join(host, 'out/0001.patch'));
    await handle.copyFileOut(`${WORKTREE_PATH}/note.txt`, join(host, 'out/note.txt'));
    expect(await readFile(join(host, 'out/0001.patch'), 'utf8')).toBe('patch');
    expect(await readFile(join(host, 'out/note.txt'), 'utf8')).toBe('note');
    expect((await handle.exec('ls -A /home/prized/.sandcastle-transfer | wc -l')).stdout.trim()).toBe('0');
  });

  it('moves a file bigger than one transfer in parts, both ways', async () => {
    // The real threshold is 64 MiB; 1 MiB parts exercise the same split, cat and per-part download.
    const saved = transferLimits.chunkBytes;
    transferLimits.chunkBytes = 1024 * 1024;
    try {
      const bytes = Buffer.from(Array.from({ length: 3_500_000 }, (_, i) => (i * 7919) % 251));
      await writeFile(join(host, 'big.bin'), bytes);
      const sandboxTmp = (await handle.exec('mktemp -d')).stdout.trim();
      await handle.copyIn(join(host, 'big.bin'), `${sandboxTmp}/big.bin`);
      await handle.copyIn(join(host, 'big.bin'), `${WORKTREE_PATH}/big.bin`);
      const sums = (await handle.exec(`sha256sum ${sandboxTmp}/big.bin big.bin | cut -c1-64 | uniq | wc -l`)).stdout.trim();
      expect(sums).toBe('1');
      await handle.copyFileOut(`${sandboxTmp}/big.bin`, join(host, 'back/tmp.bin'));
      await handle.copyFileOut(`${WORKTREE_PATH}/big.bin`, join(host, 'back/home.bin'));
      expect(Buffer.compare(await readFile(join(host, 'back/tmp.bin')), bytes)).toBe(0);
      expect(Buffer.compare(await readFile(join(host, 'back/home.bin')), bytes)).toBe(0);
      expect((await handle.exec('ls -A /home/prized/.sandcastle-transfer | wc -l')).stdout.trim()).toBe('0');
    } finally {
      transferLimits.chunkBytes = saved;
    }
  });

  it("is why the transfer directory exists: the files API refuses paths outside the home", async () => {
    const error = await sandbox.files.write('/tmp/direct-write', 'x').then(() => null, (e: unknown) => e);
    console.log(`files.write('/tmp/...'): ${error instanceof PrizedError ? `${error.code} ${error.message}` : 'accepted'}`);
    expect(error).toBeInstanceOf(PrizedError);
  });

  it('refuses sudo', async () => {
    await expect(handle.exec('id', { sudo: true })).rejects.toThrow(/without root/);
  });

  it('deletes the sandbox on close', async () => {
    await handle.close();
    await handle.close();
    let state = 'present';
    for (let i = 0; i < 30 && state !== 'gone'; i++) {
      try {
        state = (await Sandbox.connect(sandbox.id)).state;
        if (state === 'deleted') state = 'gone';
      } catch (error) {
        if (error instanceof PrizedError && error.code === 'not_found') state = 'gone';
        else throw error;
      }
      if (state !== 'gone') await new Promise(resolve => setTimeout(resolve, 2000));
    }
    console.log(`after close: ${sandbox.id} ${state}`);
    expect(state).toBe('gone');
  });
});
