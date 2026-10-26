/**
 * `sandbox.filesystem` against the real API: `PRIZED_LIVE=1 npm run test:live` with PRIZED_API_KEY
 * set. Uses one small sandbox (PRIZED_TEST_VCPU vCPU, default 2) labelled run=<this run>, deleted at
 * the end, failures included. Large-file checks use sparse files, so no 100 MiB is ever moved.
 */
import { afterAll, describe, expect, it } from 'vitest';
import {
  DirectoryNotEmptyError,
  FileExistsError,
  FileNotFoundError,
  IsDirectoryError,
  StaleFileError,
  WORKSPACE_TOOLS,
  Workspace,
  createWorkspaceTools,
} from '@mastra/core/workspace';

import { FileTooLargeError, MAX_FILE_BYTES, PrizedSandbox } from '../src/index.js';
import { LIVE, RUN, sweep, testLabels, testResources } from './live-helpers.js';

const W = '/home/prized/work/fs';
const T = '/tmp/mastra-fs';

describe.skipIf(!LIVE)('PrizedSandboxFilesystem against the real API', () => {
  const sandbox = new PrizedSandbox({
    id: `${RUN}-fs`,
    labels: testLabels(),
    resources: testResources(),
    maxRuntimeMin: 60,
    autoDeleteMin: 60,
  });
  const fs = sandbox.filesystem;
  const sh = async (script: string) => {
    const r = await sandbox.executeCommand!('sh', ['-c', script]);
    if (r.exitCode !== 0) throw new Error(`exit ${r.exitCode}: ${r.stderr}`);
    return r.stdout;
  };

  afterAll(async () => {
    await sandbox._destroy().catch(() => {});
    const left = await sweep();
    if (left.length > 0) console.log(`swept ${left.length} sandbox(es): ${left.join(', ')}`);
  }, 300_000);

  it('starts the sandbox on first use, and round-trips text and every byte value', async () => {
    const bytes = new Uint8Array(256).map((_, i) => i);
    await fs.writeFile(`${W}/bytes.bin`, bytes);
    expect(sandbox.status).toBe('running');
    expect(sandbox.sandboxId).toMatch(/^sbx_/);
    expect([...(await fs.readFile(`${W}/bytes.bin`))]).toEqual([...bytes]);
    await fs.writeFile('fs/hello.txt', 'héllo\n'); // relative: starts in /home/prized/work
    expect(await sh(`cat ${W}/hello.txt`)).toBe('héllo\n');
    expect(await fs.readFile(`${W}/hello.txt`, { encoding: 'utf8' })).toBe('héllo\n');
  });

  it('keeps a replaced 0755 script executable, in the home directory and outside it', async () => {
    await sh(`mkdir -p ${W} ${T} && printf 'echo v1\\n' > ${W}/run.sh && cp ${W}/run.sh ${T}/run.sh && chmod 755 ${W}/run.sh ${T}/run.sh`);
    await fs.writeFile(`${W}/run.sh`, 'echo v2\n');
    await fs.writeFile(`${T}/run.sh`, 'echo v3\n');
    expect(await sh(`stat -c %a ${W}/run.sh ${T}/run.sh`)).toBe('755\n755\n');
    expect(await sh(`${W}/run.sh && ${T}/run.sh`)).toBe('v2\nv3\n');
    // For the record: what the files API alone does to the same file on this sandbox's image
    // (images before r4 write it 0600, which is why every write here is staged).
    await sandbox.sandbox.files.write(`${W}/run.sh`, 'echo raw\n');
    console.log(`files API upload over a 0755 file on ${sandbox.sandboxId}: ${(await sh(`stat -c %a ${W}/run.sh`)).trim()}`);
  });

  it("gives a new file 0666 less the sandbox's umask, however it is written", async () => {
    const umask = parseInt((await sh('umask')).trim(), 8);
    const expected = (0o666 & ~umask).toString(8);
    await fs.writeFile(`${W}/new-direct.txt`, 'a');
    await fs.writeFile(`${W}/new-noclobber.txt`, 'b', { overwrite: false });
    await fs.appendFile(`${W}/new-append.txt`, 'c');
    await fs.writeFile(`${T}/new-staged.txt`, 'd');
    const modes = await sh(`stat -c %a ${W}/new-direct.txt ${W}/new-noclobber.txt ${W}/new-append.txt ${T}/new-staged.txt`);
    expect(modes.trim().split('\n')).toEqual([expected, expected, expected, expected]);
  });

  it('reads and writes outside /home/prized and through a symlink that leaves it, leaving no staging files', async () => {
    expect(await fs.readFile('/etc/os-release', { encoding: 'utf8' })).toMatch(/Ubuntu/);
    await sh(`printf target > ${T}/target.txt && ln -sfn ${T}/target.txt ${W}/link.txt`);
    expect(await fs.readFile(`${W}/link.txt`, { encoding: 'utf8' })).toBe('target');
    await fs.writeFile(`${W}/link.txt`, 'through');
    expect(await sh(`cat ${T}/target.txt; test -L ${W}/link.txt && echo link`)).toBe('throughlink\n');
    expect(await sh('ls -A /home/prized/.cache/prized-mastra 2>/dev/null | wc -l')).toBe('0\n');
  });

  it('maps missing files and directories to typed errors without replacing the sandbox', async () => {
    const id = sandbox.sandboxId;
    await expect(fs.readFile(`${W}/nope.txt`)).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(fs.readFile(`${T}/nope.txt`)).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(fs.readFile(W)).rejects.toBeInstanceOf(IsDirectoryError);
    await expect(fs.writeFile(W, 'x')).rejects.toBeInstanceOf(IsDirectoryError);
    expect(sandbox.sandboxId).toBe(id);
  });

  it('refuses a file over 100 MiB without moving it, inside and outside the home directory', async () => {
    await sh(`truncate -s ${MAX_FILE_BYTES + 1} ${W}/big.bin ${T}/big.bin`); // sparse
    const inside = await fs.readFile(`${W}/big.bin`).catch(e => e);
    expect(inside).toBeInstanceOf(FileTooLargeError);
    const outside = await fs.readFile(`${T}/big.bin`).catch(e => e);
    expect(outside).toBeInstanceOf(FileTooLargeError);
    expect(outside.sizeBytes).toBe(MAX_FILE_BYTES + 1);
    await sh(`rm ${W}/big.bin ${T}/big.bin`);
  });

  it('no-clobber writes, appends, copies, moves, mkdir, rmdir and delete', async () => {
    await expect(fs.writeFile(`${W}/hello.txt`, 'x', { overwrite: false })).rejects.toBeInstanceOf(FileExistsError);
    await fs.appendFile(`${W}/hello.txt`, 'more\n');
    expect(await sh(`cat ${W}/hello.txt`)).toBe('héllo\nmore\n');
    await fs.mkdir(`${W}/tree/sub`);
    await fs.writeFile(`${W}/tree/sub/a.ts`, 'A');
    await sh(`ln -s sub/a.ts ${W}/tree/link.ts`);
    await fs.copyFile(`${W}/tree`, `${W}/copy`, { recursive: true });
    expect(await sh(`cat ${W}/copy/sub/a.ts; readlink ${W}/copy/link.ts`)).toBe('Asub/a.ts\n');
    await fs.moveFile(`${W}/copy`, `${W}/moved/here`);
    expect(await fs.exists(`${W}/copy`)).toBe(false);
    expect(await fs.exists(`${W}/moved/here/sub/a.ts`)).toBe(true);
    await expect(fs.rmdir(`${W}/moved`)).rejects.toBeInstanceOf(DirectoryNotEmptyError);
    await fs.rmdir(`${W}/moved`, { recursive: true });
    await fs.deleteFile(`${W}/tree/link.ts`);
    expect(await sh(`ls -A ${W}/tree; test -e ${W}/moved || echo gone`)).toBe('sub\ngone\n');
  });

  it('lists, walks and stats like the sandbox sees it', async () => {
    await sh(`mkdir -p ${W}/ls/d ${W}/ls/.hidden && printf 12345 > ${W}/ls/f.txt && ln -sfn d ${W}/ls/dl && ln -sfn missing ${W}/ls/broken`);
    const entries = await fs.readdir(`${W}/ls`);
    expect(entries.map(e => [e.name, e.type])).toEqual([
      ['.hidden', 'directory'],
      ['d', 'directory'],
      ['dl', 'directory'],
      ['f.txt', 'file'],
    ]);
    expect(entries.find(e => e.name === 'f.txt')?.size).toBe(5);
    expect(entries.find(e => e.name === 'dl')).toMatchObject({ isSymlink: true, symlinkTarget: 'd' });
    expect((await fs.walk(`${W}/ls`)).map(e => e.path)).toEqual(['d', 'dl', 'f.txt']);
    const st = await fs.stat(`${W}/ls/f.txt`);
    const seconds = Number((await sh(`stat -c %Y ${W}/ls/f.txt`)).trim());
    expect(st).toMatchObject({ type: 'file', size: 5, path: `${W}/ls/f.txt` });
    expect(Math.floor(st.modifiedAt.getTime() / 1000)).toBe(seconds);
  });

  it('refuses a write over a file that changed since it was read', async () => {
    await fs.writeFile(`${W}/stale.txt`, 'v1');
    const { modifiedAt } = await fs.stat(`${W}/stale.txt`);
    await sh(`touch -d '@${Math.floor(modifiedAt.getTime() / 1000) + 5}' ${W}/stale.txt`);
    await expect(fs.writeFile(`${W}/stale.txt`, 'v2', { expectedMtime: modifiedAt })).rejects.toBeInstanceOf(StaleFileError);
    expect(await sh(`cat ${W}/stale.txt`)).toBe('v1');
  });

  it("runs Mastra's file tools on the sandbox: write, read, edit, list, grep", async () => {
    const workspace = new Workspace({ sandbox, filesystem: fs });
    const tools = await createWorkspaceTools(workspace);
    const run = (name: string, input: Record<string, unknown>) => (tools[name] as any).execute(input, { workspace });
    const F = WORKSPACE_TOOLS.FILESYSTEM;
    await run(F.WRITE_FILE, { path: 'fs/tools/app.py', content: 'def answer():\n    return 41\n' });
    await run(F.READ_FILE, { path: 'fs/tools/app.py' });
    await run(F.EDIT_FILE, { path: 'fs/tools/app.py', old_string: 'return 41', new_string: 'return 42' });
    expect(await sh(`python3 -c 'import sys; sys.path.insert(0, "${W}/tools"); import app; print(app.answer())'`)).toBe('42\n');
    expect(JSON.stringify(await run(F.LIST_FILES, { path: 'fs/tools' }))).toContain('app.py');
    expect(JSON.stringify(await run(F.GREP, { pattern: 'return 4\\d', path: 'fs/tools' }))).toContain('app.py');
  });

  it('works on a paused sandbox: the next operation resumes it', async () => {
    await sandbox.stop();
    expect(await fs.readFile(`${W}/hello.txt`, { encoding: 'utf8' })).toBe('héllo\nmore\n');
    expect(sandbox.status).toBe('running');
  });
});

describe.skipIf(LIVE)('live filesystem tests', () => {
  it.skip('need PRIZED_API_KEY and PRIZED_LIVE=1', () => {});
});
