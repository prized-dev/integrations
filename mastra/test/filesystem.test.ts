/**
 * Offline tests for PrizedSandboxFilesystem. The SDK's Sandbox is the fake in fake-sdk.ts, and its
 * disk is a temp directory (fake-disk.ts): file transfers follow the files API's rules and
 * commands run in this machine's `sh`, so the shell scripts are tested for real.
 */
import { Buffer } from 'node:buffer';
import { execFileSync } from 'node:child_process';
import { chmod, lstat, mkdir, mkdtemp, readdir, readFile, readlink, realpath, rm, stat, symlink, truncate, utimes, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PrizedError } from '@prized/sdk';
import {
  DirectoryNotEmptyError,
  DirectoryNotFoundError,
  FileExistsError,
  FileNotFoundError,
  FilesystemError,
  IsDirectoryError,
  NotDirectoryError,
  PermissionError,
  StaleFileError,
  Workspace,
  WORKSPACE_TOOLS,
  createWorkspaceTools,
} from '@mastra/core/workspace';

import { FakeDisk } from './fake-disk.js';
import { FakeSandbox } from './fake-sdk.js';

vi.mock('@prized/sdk', async importOriginal => {
  const real = await importOriginal<typeof import('@prized/sdk')>();
  const { FakeSandbox } = await import('./fake-sdk.js');
  return { ...real, Sandbox: FakeSandbox };
});

const { PrizedSandbox, PrizedSandboxFilesystem, FileTooLargeError, MAX_FILE_BYTES } = await import('../src/index.js');
const { EXIT, linkFromStagingCommand, moveNoClobberCommand, replaceFromStagingCommand } = await import('../src/file-commands.js');

const KEY = 'dcp_0000000000000000000000000000';
const ROOT_USER = process.getuid?.() === 0;

let root: string;
let home: string;
let work: string;
let out: string;
let disk: FakeDisk;
let sandbox: InstanceType<typeof PrizedSandbox>;
let fs: InstanceType<typeof PrizedSandboxFilesystem>;

const at = (path: string) => join(work, path);
const text = (path: string) => readFile(path, 'utf8');
const modeOf = async (path: string) => (await lstat(path)).mode & 0o777;
const exists = (path: string) =>
  lstat(path).then(
    () => true,
    () => false,
  );

async function setup(umask = 0o022, legacy = false): Promise<void> {
  FakeSandbox.reset();
  root = await realpath(await mkdtemp(join(tmpdir(), 'prized-fs-')));
  home = join(root, 'home/prized');
  work = join(home, 'work');
  out = join(root, 'outside');
  await mkdir(work, { recursive: true });
  await mkdir(out, { recursive: true });
  disk = new FakeDisk(home, { umask, legacy });
  FakeSandbox.disk = disk;
  sandbox = new PrizedSandbox({ id: 'fs-test', apiKey: KEY, workingDirectory: work });
  fs = new PrizedSandboxFilesystem(sandbox, { home });
  await sandbox._start();
  disk.calls.length = 0;
  disk.commands.length = 0;
}

beforeEach(async () => {
  vi.unstubAllEnvs();
  vi.stubEnv('PRIZED_API_KEY', '');
  await setup();
});

afterEach(async () => {
  await rm(root, { recursive: true, force: true });
});

describe('readFile', () => {
  it('returns a Buffer, or a string with an encoding, in one download and no command', async () => {
    await writeFile(at('a.txt'), 'héllo');
    const buffer = await fs.readFile(at('a.txt'));
    expect(Buffer.isBuffer(buffer)).toBe(true);
    expect(buffer.toString('utf8')).toBe('héllo');
    expect(await fs.readFile('a.txt', { encoding: 'utf8' })).toBe('héllo');
    expect(disk.calls).toEqual([`read ${at('a.txt')}`, `read ${at('a.txt')}`]);
    expect(disk.commands).toEqual([]);
  });

  it('resolves relative paths in the working directory, dropping . and empty segments', async () => {
    await mkdir(at('src'));
    await writeFile(at('src/x.ts'), 'X');
    expect(await fs.readFile('./src//x.ts', { encoding: 'utf8' })).toBe('X');
    expect(disk.calls).toEqual([`read ${at('src/x.ts')}`]);
  });

  it('reads a missing file as FileNotFoundError without taking the sandbox for gone', async () => {
    const lists = FakeSandbox.lists.length;
    await expect(fs.readFile(at('nope.txt'))).rejects.toBeInstanceOf(FileNotFoundError);
    // The download's not_found was checked against the sandbox (one refresh), which is still there.
    expect(FakeSandbox.refreshes).toBe(1);
    expect(FakeSandbox.creates).toHaveLength(1);
    expect(FakeSandbox.lists).toHaveLength(lists);
  });

  it('maps a directory to IsDirectoryError, and a broken link or a file on the way to FileNotFoundError', async () => {
    await mkdir(at('dir'));
    await symlink('missing', at('broken'));
    await writeFile(at('file.txt'), 'f');
    await expect(fs.readFile(at('dir'))).rejects.toBeInstanceOf(IsDirectoryError);
    await expect(fs.readFile(at('broken'))).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(fs.readFile(at('file.txt/x'))).rejects.toBeInstanceOf(FileNotFoundError);
  });

  it('does not report a missing file when it cannot tell whether the sandbox is still there', async () => {
    FakeSandbox.refreshErrors = [new PrizedError('network_error', null)];
    await expect(fs.readFile(at('nope.txt'))).rejects.toMatchObject({ name: 'PrizedError', code: 'network_error' });
  });

  it('refuses a file over 100 MiB with FileTooLargeError, moving no bytes', async () => {
    await writeFile(at('big.bin'), '');
    await truncate(at('big.bin'), MAX_FILE_BYTES + 1); // sparse: no disk used
    const error = await fs.readFile(at('big.bin')).catch(e => e);
    expect(error).toBeInstanceOf(FileTooLargeError);
    expect(error).toBeInstanceOf(FilesystemError);
    expect(error.code).toBe('EFBIG');
    expect(error.message).toMatch(/100 MiB.*head -c/);
  });

  it.skipIf(ROOT_USER)('maps an unreadable file to PermissionError', async () => {
    await writeFile(at('secret'), 's');
    await chmod(at('secret'), 0o000);
    await expect(fs.readFile(at('secret'))).rejects.toBeInstanceOf(PermissionError);
  });

  it('reads outside the home directory through a staged copy, and leaves no copy behind', async () => {
    await writeFile(join(out, 'hosts'), 'outside');
    expect(await fs.readFile(join(out, 'hosts'), { encoding: 'utf8' })).toBe('outside');
    // One command copies it under the root, one download fetches it, one command deletes the copy.
    expect(disk.commands).toHaveLength(2);
    expect(disk.calls).toHaveLength(1);
    expect(disk.calls[0]).toMatch(/^read .*\/\.cache\/prized-mastra\/down-/);
    expect(await disk.staged()).toEqual([]);
  });

  it('reads through a symlink that leaves the home directory', async () => {
    await writeFile(join(out, 'target.txt'), 'via link');
    await symlink(join(out, 'target.txt'), at('link.txt'));
    expect(await fs.readFile(at('link.txt'), { encoding: 'utf8' })).toBe('via link');
    expect(disk.calls[0]).toBe(`read ${at('link.txt')}`); // refused, then staged
    expect(await disk.staged()).toEqual([]);
  });

  it('lets the sandbox resolve .. after a symlink, as a command would', async () => {
    await mkdir(join(out, 'dir'));
    await writeFile(join(out, 'x.txt'), 'kernel');
    await writeFile(at('x.txt'), 'lexical');
    await symlink(join(out, 'dir'), at('link'));
    expect(await fs.readFile(`${work}/link/../x.txt`, { encoding: 'utf8' })).toBe('kernel');
    // The files API refuses `..`, so it isn't asked: only the staged copy is downloaded.
    expect(disk.calls).toEqual([expect.stringMatching(/\/\.cache\/prized-mastra\/down-/)]);
  });

  it.skipIf(ROOT_USER)('maps an unreadable file outside the home directory to PermissionError', async () => {
    await writeFile(join(out, 'secret'), 's');
    await chmod(join(out, 'secret'), 0o000);
    await expect(fs.readFile(join(out, 'secret'))).rejects.toBeInstanceOf(PermissionError);
    expect(await disk.staged()).toEqual([]);
  });

  it('maps staged-read failures: missing, directory, not regular, over 100 MiB', async () => {
    await mkdir(join(out, 'dir'));
    await writeFile(join(out, 'big'), '');
    await truncate(join(out, 'big'), MAX_FILE_BYTES + 5);
    await expect(fs.readFile(join(out, 'nope'))).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(fs.readFile(join(out, 'dir'))).rejects.toBeInstanceOf(IsDirectoryError);
    await expect(fs.readFile('/dev/null')).rejects.toThrow(/Not a regular file/);
    const big = await fs.readFile(join(out, 'big')).catch(e => e);
    expect(big).toBeInstanceOf(FileTooLargeError);
    expect(big.sizeBytes).toBe(MAX_FILE_BYTES + 5);
    expect(disk.calls).toEqual([]); // nothing was downloaded
    expect(await disk.staged()).toEqual([]);
  });
});

describe('writeFile', () => {
  it('writes strings and bytes with one upload and one command each, creating parents by default', async () => {
    await fs.writeFile(at('a/b/c.txt'), 'text');
    await fs.writeFile(at('bin'), new Uint8Array([0, 1, 2, 255]));
    expect(await text(at('a/b/c.txt'))).toBe('text');
    expect([...(await readFile(at('bin')))]).toEqual([0, 1, 2, 255]);
    expect(disk.calls).toEqual([expect.stringMatching(/prized-mastra\/up-.* 4$/), expect.stringMatching(/prized-mastra\/up-.* 4$/)]);
    expect(disk.commands).toHaveLength(2);
    expect(await disk.staged()).toEqual([]);
  });

  it('writes only the view of a Buffer that shares a larger pool', async () => {
    const pool = Buffer.from('xxHELLOxx');
    await fs.writeFile(at('view.txt'), pool.subarray(2, 7));
    expect(await text(at('view.txt'))).toBe('HELLO');
  });

  it('keeps a replaced 0755 script executable', async () => {
    await writeFile(at('run.sh'), 'echo v1\n');
    await chmod(at('run.sh'), 0o755);
    await fs.writeFile(at('run.sh'), 'echo v2\n');
    expect(await text(at('run.sh'))).toBe('echo v2\n');
    expect(await modeOf(at('run.sh'))).toBe(0o755);
  });

  it('with recursive: false, rejects a missing parent or a file in its place, creating nothing', async () => {
    await writeFile(at('file'), 'f');
    await expect(fs.writeFile(at('missing/x.txt'), 'x', { recursive: false })).rejects.toBeInstanceOf(DirectoryNotFoundError);
    await expect(fs.writeFile(at('file/x.txt'), 'x', { recursive: false })).rejects.toBeInstanceOf(NotDirectoryError);
    expect(await exists(at('missing'))).toBe(false);
    await mkdir(at('there'));
    await fs.writeFile(at('there/x.txt'), 'x', { recursive: false });
    expect(await text(at('there/x.txt'))).toBe('x');
  });

  it('maps writing onto a directory to IsDirectoryError, inside and outside the home directory', async () => {
    await mkdir(at('dir'));
    await mkdir(join(out, 'dir'));
    await expect(fs.writeFile(at('dir'), 'x')).rejects.toBeInstanceOf(IsDirectoryError);
    await expect(fs.writeFile(join(out, 'dir'), 'x')).rejects.toBeInstanceOf(IsDirectoryError);
    expect(await disk.staged()).toEqual([]);
  });

  it('refuses to replace or append to a FIFO, without blocking on it', async () => {
    execFileSync('mkfifo', [at('pipe')]);
    execFileSync('mkfifo', [join(out, 'pipe')]);
    await expect(fs.writeFile(at('pipe'), 'x')).rejects.toThrow(/Not a regular file/);
    await expect(fs.writeFile(join(out, 'pipe'), 'x')).rejects.toThrow(/Not a regular file/);
    await expect(fs.appendFile(at('pipe'), 'x')).rejects.toThrow(/Not a regular file/);
    expect((await lstat(at('pipe'))).isFIFO()).toBe(true);
    expect(await disk.staged()).toEqual([]);
  });

  it('touches nothing when the staged upload is gone by the time its command runs (the sandbox was replaced)', async () => {
    const realShell = disk.shell;
    disk.shell = async (command, cwd) => {
      for (const name of await disk.staged()) await rm(join(home, '.cache/prized-mastra', name));
      return realShell(command, cwd);
    };
    await expect(fs.writeFile(at('new/a.txt'), 'x')).rejects.toThrow(/staged upload is gone/);
    await expect(fs.writeFile(at('new/b.txt'), 'x', { overwrite: false })).rejects.toThrow(/staged upload is gone/);
    await expect(fs.appendFile(at('new/c.txt'), 'x')).rejects.toThrow(/staged upload is gone/);
    expect(await exists(at('new'))).toBe(false);
  });

  it('maps a file where a parent directory should be to NotDirectoryError, for every way of writing', async () => {
    await writeFile(at('file'), 'f');
    await writeFile(at('src.txt'), 's');
    await expect(fs.writeFile(at('file/x.txt'), 'x')).rejects.toBeInstanceOf(NotDirectoryError);
    await expect(fs.writeFile(at('file/sub/x.txt'), 'x', { overwrite: false })).rejects.toBeInstanceOf(NotDirectoryError);
    await expect(fs.appendFile(at('file/x.txt'), 'x')).rejects.toBeInstanceOf(NotDirectoryError);
    await expect(fs.copyFile(at('src.txt'), at('file/x.txt'))).rejects.toBeInstanceOf(NotDirectoryError);
    await expect(fs.moveFile(at('src.txt'), at('file/x.txt'))).rejects.toBeInstanceOf(NotDirectoryError);
    await expect(fs.moveFile(at('src.txt'), at('file/x.txt'), { overwrite: false })).rejects.toBeInstanceOf(NotDirectoryError);
    expect(await disk.staged()).toEqual([]);
  });

  it('refuses content over 100 MiB before sending anything', async () => {
    await expect(fs.writeFile(at('big'), Buffer.alloc(MAX_FILE_BYTES + 1))).rejects.toBeInstanceOf(FileTooLargeError);
    await expect(fs.appendFile(at('big'), Buffer.alloc(MAX_FILE_BYTES + 1))).rejects.toBeInstanceOf(FileTooLargeError);
    expect(disk.calls).toEqual([]);
    expect(disk.commands).toEqual([]);
  });

  it('with overwrite: false, refuses an existing file untouched and claims a free path', async () => {
    await writeFile(at('taken.txt'), 'old');
    await expect(fs.writeFile(at('taken.txt'), 'new', { overwrite: false })).rejects.toBeInstanceOf(FileExistsError);
    expect(await text(at('taken.txt'))).toBe('old');
    await symlink('nowhere', at('dangling'));
    await expect(fs.writeFile(at('dangling'), 'new', { overwrite: false })).rejects.toBeInstanceOf(FileExistsError);
    await fs.writeFile(at('fresh/new.txt'), 'new', { overwrite: false });
    expect(await text(at('fresh/new.txt'))).toBe('new');
    expect((await readdir(at('fresh'))).sort()).toEqual(['new.txt']); // no temp file left
    expect(await disk.staged()).toEqual([]);
  });

  it('lets exactly one of several racing no-overwrite writes win', async () => {
    const results = await Promise.allSettled(
      ['a', 'b', 'c', 'd'].map(v => fs.writeFile(at('race.txt'), v, { overwrite: false })),
    );
    expect(results.filter(r => r.status === 'fulfilled')).toHaveLength(1);
    for (const r of results.filter(r => r.status === 'rejected')) {
      expect((r as PromiseRejectedResult).reason).toBeInstanceOf(FileExistsError);
    }
    expect(['a', 'b', 'c', 'd']).toContain(await text(at('race.txt')));
    expect((await readdir(work)).sort()).toEqual(['race.txt']);
    expect(await disk.staged()).toEqual([]);
  });

  it('writes outside the home directory through a staged upload, keeping the replaced mode', async () => {
    const target = join(out, 'tool.sh');
    await writeFile(target, 'v1');
    await chmod(target, 0o750);
    await fs.writeFile(target, 'v2');
    expect(await text(target)).toBe('v2');
    expect(await modeOf(target)).toBe(0o750);
    await fs.writeFile(join(out, 'new/dir/file.txt'), 'n');
    expect(await text(join(out, 'new/dir/file.txt'))).toBe('n');
    expect((await readdir(out)).sort()).toEqual(['new', 'tool.sh']); // no temp file left
    expect(await disk.staged()).toEqual([]);
  });

  it('writes through a symlink that leaves the home directory: the target gets the bytes, the link stays', async () => {
    await writeFile(join(out, 'real.sh'), 'v1');
    await chmod(join(out, 'real.sh'), 0o755);
    await symlink(join(out, 'real.sh'), at('link.sh'));
    await fs.writeFile(at('link.sh'), 'v2');
    expect((await lstat(at('link.sh'))).isSymbolicLink()).toBe(true);
    expect(await text(join(out, 'real.sh'))).toBe('v2');
    expect(await modeOf(join(out, 'real.sh'))).toBe(0o755);
  });

  describe('expectedMtime', () => {
    it('writes when the file is as it was read', async () => {
      await writeFile(at('f.txt'), 'v1');
      const { modifiedAt } = await fs.stat(at('f.txt'));
      await fs.writeFile(at('f.txt'), 'v2', { expectedMtime: modifiedAt });
      expect(await text(at('f.txt'))).toBe('v2');
    });

    it('refuses with StaleFileError when the file changed since, leaving it alone', async () => {
      await writeFile(at('f.txt'), 'v1');
      const { modifiedAt } = await fs.stat(at('f.txt'));
      await utimes(at('f.txt'), new Date(), new Date(modifiedAt.getTime() + 5_000));
      const error = await fs.writeFile(at('f.txt'), 'v2', { expectedMtime: modifiedAt }).catch(e => e);
      expect(error).toBeInstanceOf(StaleFileError);
      expect(error.actualMtime.getTime()).toBe(modifiedAt.getTime() + 5_000);
      expect(await text(at('f.txt'))).toBe('v1');
    });

    it('writes a file that does not exist yet', async () => {
      await fs.writeFile(at('new.txt'), 'n', { expectedMtime: new Date(0) });
      expect(await text(at('new.txt'))).toBe('n');
    });
  });
});

describe.each([
  [0o022, false],
  [0o077, false],
  [0o022, true],
  [0o077, true],
])('modes under umask %o (files API before r4: %s)', (umask, legacy) => {
  beforeEach(async () => {
    await rm(root, { recursive: true, force: true });
    await setup(umask, legacy);
  });

  const fileMode = 0o666 & ~umask;
  const dirMode = 0o777 & ~umask;

  it('a new file gets 0666 less the umask, whichever way it is written', async () => {
    await fs.writeFile(at('direct.txt'), 'd');
    await fs.writeFile(at('noclobber.txt'), 'n', { overwrite: false });
    await fs.appendFile(at('appended.txt'), 'a');
    await fs.writeFile(join(out, 'staged.txt'), 's');
    for (const path of [at('direct.txt'), at('noclobber.txt'), at('appended.txt'), join(out, 'staged.txt')]) {
      expect([path, await modeOf(path)]).toEqual([path, fileMode]);
    }
  });

  it('a new directory gets 0777 less the umask', async () => {
    await fs.mkdir(at('d1/d2'));
    await fs.writeFile(at('p1/p2/f.txt'), 'f');
    for (const path of [at('d1'), at('d1/d2'), at('p1'), at('p1/p2')]) {
      expect([path, await modeOf(path)]).toEqual([path, dirMode]);
    }
  });

  it('a write, an append and a copy keep the mode of the file they replace', async () => {
    for (const name of ['w.sh', 'a.sh', 'c.sh']) {
      await writeFile(at(name), 'v1\n');
      await chmod(at(name), 0o755);
    }
    await writeFile(join(out, 's.sh'), 'v1\n');
    await chmod(join(out, 's.sh'), 0o711);
    await writeFile(at('src.txt'), 'copied\n');
    await chmod(at('src.txt'), 0o600);
    await fs.writeFile(at('w.sh'), 'v2\n');
    await fs.appendFile(at('a.sh'), 'v2\n');
    await fs.copyFile(at('src.txt'), at('c.sh'));
    await fs.writeFile(join(out, 's.sh'), 'v2\n');
    expect(await modeOf(at('w.sh'))).toBe(0o755);
    expect(await modeOf(at('a.sh'))).toBe(0o755);
    expect(await modeOf(at('c.sh'))).toBe(0o755);
    expect(await modeOf(join(out, 's.sh'))).toBe(0o711);
    expect(await text(at('a.sh'))).toBe('v1\nv2\n');
    expect(await text(at('c.sh'))).toBe('copied\n');
  });

  it('setuid, setgid and sticky bits are not carried over by a staged write', async ({ skip }) => {
    const target = join(out, 'suid');
    await writeFile(target, 'v1');
    // Some systems (a Prized sandbox among them) refuse setuid on a user's own file: try the
    // sticky bit there instead, and skip only where neither can be set.
    let special = 0;
    for (const mode of [0o6755, 0o1755]) {
      try {
        await chmod(target, mode);
        special = (await lstat(target)).mode & 0o7000;
        if (special) break;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'EPERM') throw error;
      }
    }
    if (!special) skip('this system refuses setuid, setgid and sticky bits on a regular file');
    await fs.writeFile(target, 'v2');
    expect((await lstat(target)).mode & 0o7777).toBe(0o755);
  });
});

describe('appendFile', () => {
  it('appends to an existing file and creates a missing one, sending only the new bytes', async () => {
    await writeFile(at('log.txt'), 'one\n');
    await fs.appendFile(at('log.txt'), 'two\n');
    await fs.appendFile(at('new/log.txt'), Buffer.from('first\n'));
    expect(await text(at('log.txt'))).toBe('one\ntwo\n');
    expect(await text(at('new/log.txt'))).toBe('first\n');
    expect(disk.uploads.map(u => Buffer.from(u.data).toString())).toEqual(['two\n', 'first\n']);
    expect(disk.commands).toHaveLength(2);
    expect(await disk.staged()).toEqual([]);
  });

  it('appends outside the home directory', async () => {
    await writeFile(join(out, 'log'), 'a');
    await fs.appendFile(join(out, 'log'), 'b');
    expect(await text(join(out, 'log'))).toBe('ab');
  });

  it('rejects a directory and still deletes the staging file', async () => {
    await mkdir(at('dir'));
    await expect(fs.appendFile(at('dir'), 'x')).rejects.toBeInstanceOf(IsDirectoryError);
    expect(await disk.staged()).toEqual([]);
  });

  it('deletes the staging file when the command never answers', async () => {
    const realShell = disk.shell;
    let first = true;
    disk.shell = command => {
      if (first) {
        first = false;
        return Promise.reject(new PrizedError('sandbox_unreachable', null, { status: 502 }));
      }
      return realShell(command);
    };
    await expect(fs.appendFile(at('log'), 'x')).rejects.toMatchObject({ code: 'sandbox_unreachable' });
    expect(await disk.staged()).toEqual([]);
  });
});

describe('deleteFile', () => {
  it('deletes a file, and only the link for a symlink to a directory, in one command', async () => {
    await writeFile(at('a.txt'), 'a');
    await mkdir(at('dir'));
    await writeFile(at('dir/keep'), 'k');
    await symlink(at('dir'), at('dirlink'));
    await fs.deleteFile(at('a.txt'));
    await fs.deleteFile(at('dirlink'));
    expect(await exists(at('a.txt'))).toBe(false);
    expect(await exists(at('dirlink'))).toBe(false);
    expect(await text(at('dir/keep'))).toBe('k');
    expect(disk.commands).toHaveLength(2);
    expect(disk.calls).toEqual([]);
  });

  it('deletes a broken link', async () => {
    await symlink('nowhere', at('broken'));
    await fs.deleteFile(at('broken'));
    expect(await exists(at('broken'))).toBe(false);
  });

  it('rejects a directory, and a missing file unless force is set', async () => {
    await mkdir(at('dir'));
    await expect(fs.deleteFile(at('dir'))).rejects.toBeInstanceOf(IsDirectoryError);
    await expect(fs.deleteFile(at('nope'))).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(fs.deleteFile(at('nope'), { force: true })).resolves.toBeUndefined();
    expect(await exists(at('dir'))).toBe(true);
  });
});

describe('copyFile', () => {
  it('copies a file in one command, creating the parent and moving no bytes over the network', async () => {
    await writeFile(at('a.txt'), 'A');
    await fs.copyFile(at('a.txt'), at('deep/er/b.txt'));
    expect(await text(at('deep/er/b.txt'))).toBe('A');
    expect(disk.commands).toHaveLength(1);
    expect(disk.calls).toEqual([]);
  });

  it('refuses to overwrite when asked, and overwrites by default', async () => {
    await writeFile(at('a.txt'), 'A');
    await writeFile(at('b.txt'), 'B');
    await expect(fs.copyFile(at('a.txt'), at('b.txt'), { overwrite: false })).rejects.toBeInstanceOf(FileExistsError);
    expect(await text(at('b.txt'))).toBe('B');
    await fs.copyFile(at('a.txt'), at('b.txt'));
    expect(await text(at('b.txt'))).toBe('A');
    await fs.copyFile(at('a.txt'), at('c.txt'), { overwrite: false });
    expect((await readdir(work)).sort()).toEqual(['a.txt', 'b.txt', 'c.txt']);
  });

  it('copies a tree only with recursive, keeping symlinks inside it as links', async () => {
    await mkdir(at('src/nested'), { recursive: true });
    await writeFile(at('src/nested/c.ts'), 'C');
    await symlink('nested/c.ts', at('src/link.ts'));
    await expect(fs.copyFile(at('src'), at('dst'))).rejects.toBeInstanceOf(IsDirectoryError);
    await fs.copyFile(at('src'), at('dst'), { recursive: true });
    expect(await text(at('dst/nested/c.ts'))).toBe('C');
    expect(await readlink(at('dst/link.ts'))).toBe('nested/c.ts');
  });

  it('rejects a missing source and a file copied onto a directory', async () => {
    await writeFile(at('a.txt'), 'A');
    await mkdir(at('dir'));
    await expect(fs.copyFile(at('nope'), at('x'))).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(fs.copyFile(at('a.txt'), at('dir'))).rejects.toBeInstanceOf(IsDirectoryError);
  });
});

describe('moveFile', () => {
  it('renames in one command, creating the destination parent and replacing a file', async () => {
    await writeFile(at('a.txt'), 'A');
    await writeFile(at('b.txt'), 'B');
    await fs.moveFile(at('a.txt'), at('x/y/a.txt'));
    expect(await text(at('x/y/a.txt'))).toBe('A');
    await fs.moveFile(at('x/y/a.txt'), at('b.txt'));
    expect(await text(at('b.txt'))).toBe('A');
    expect(await exists(at('x/y/a.txt'))).toBe(false);
    expect(disk.commands).toHaveLength(2);
    expect(disk.calls).toEqual([]);
  });

  it('moves a directory, and never into a directory that is at the destination', async () => {
    await mkdir(at('d1'));
    await writeFile(at('d1/f'), 'f');
    await fs.moveFile(at('d1'), at('d2'));
    expect(await text(at('d2/f'))).toBe('f');
    // Like rename(2): a directory replaces an empty one rather than moving inside it.
    await mkdir(at('d3'));
    await fs.moveFile(at('d2'), at('d3'));
    expect(await text(at('d3/f'))).toBe('f');
    expect(await exists(at('d3/d2'))).toBe(false);
    await fs.moveFile(at('d3'), at('d2'));
    await writeFile(at('file'), 'x');
    await expect(fs.moveFile(at('file'), at('d2'))).rejects.toBeInstanceOf(IsDirectoryError);
    expect(await exists(at('d2/file'))).toBe(false);
  });

  it('rejects a missing source, and an existing destination with overwrite: false', async () => {
    await writeFile(at('a.txt'), 'A');
    await writeFile(at('b.txt'), 'B');
    await expect(fs.moveFile(at('nope'), at('x'))).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(fs.moveFile(at('a.txt'), at('b.txt'), { overwrite: false })).rejects.toBeInstanceOf(FileExistsError);
    expect(await text(at('a.txt'))).toBe('A');
    expect(await text(at('b.txt'))).toBe('B');
    await fs.moveFile(at('a.txt'), at('c.txt'), { overwrite: false });
    expect(await text(at('c.txt'))).toBe('A');
  });
});

describe('mkdir', () => {
  it('creates nested directories by default and is a no-op on an existing one', async () => {
    await fs.mkdir(at('a/b/c'));
    expect((await stat(at('a/b/c'))).isDirectory()).toBe(true);
    await expect(fs.mkdir(at('a/b/c'))).resolves.toBeUndefined();
  });

  it('with recursive: false, requires the parent', async () => {
    await expect(fs.mkdir(at('x/y'), { recursive: false })).rejects.toBeInstanceOf(DirectoryNotFoundError);
    await fs.mkdir(at('x'), { recursive: false });
    await fs.mkdir(at('x/y'), { recursive: false });
    expect((await stat(at('x/y'))).isDirectory()).toBe(true);
  });

  it('maps a file or a broken link in the way to FileExistsError', async () => {
    await writeFile(at('file'), 'f');
    await symlink('nowhere', at('broken'));
    await expect(fs.mkdir(at('file'))).rejects.toBeInstanceOf(FileExistsError);
    await expect(fs.mkdir(at('broken'))).rejects.toBeInstanceOf(FileExistsError);
  });
});

describe('rmdir', () => {
  it('removes an empty directory, and a non-empty one only with recursive', async () => {
    await mkdir(at('empty'));
    await mkdir(at('full/sub'), { recursive: true });
    await fs.rmdir(at('empty'));
    expect(await exists(at('empty'))).toBe(false);
    await expect(fs.rmdir(at('full'))).rejects.toBeInstanceOf(DirectoryNotEmptyError);
    await fs.rmdir(at('full'), { recursive: true });
    expect(await exists(at('full'))).toBe(false);
  });

  it('rejects a file, a symlink to a directory without recursive, and a missing path unless force', async () => {
    await writeFile(at('file'), 'f');
    await mkdir(at('dir'));
    await symlink(at('dir'), at('dirlink'));
    await expect(fs.rmdir(at('file'))).rejects.toBeInstanceOf(NotDirectoryError);
    await expect(fs.rmdir(at('file'), { recursive: true })).rejects.toBeInstanceOf(NotDirectoryError);
    await expect(fs.rmdir(at('dirlink'))).rejects.toBeInstanceOf(NotDirectoryError);
    await expect(fs.rmdir(at('nope'))).rejects.toBeInstanceOf(DirectoryNotFoundError);
    await expect(fs.rmdir(at('nope'), { force: true })).resolves.toBeUndefined();
  });

  it('removes only the link when recursively removing a symlink to a directory', async () => {
    await mkdir(at('dir'));
    await writeFile(at('dir/keep'), 'k');
    await symlink(at('dir'), at('dirlink'));
    await fs.rmdir(at('dirlink'), { recursive: true });
    expect(await exists(at('dirlink'))).toBe(false);
    expect(await text(at('dir/keep'))).toBe('k');
  });
});

describe('readdir', () => {
  beforeEach(async () => {
    await mkdir(at('src/nested'), { recursive: true });
    await mkdir(at('empty'));
    await writeFile(at('a.txt'), 'hello');
    await writeFile(at('src/b.ts'), 'B');
    await writeFile(at('src/nested/c.ts'), 'C');
    await writeFile(at('src/nested/d.md'), 'D');
    await symlink(at('src'), at('dirlink'));
    await symlink('a.txt', at('filelink'));
    await symlink('missing', at('broken'));
    disk.commands.length = 0;
  });

  it('lists names in one command, typing links by their target and dropping broken ones', async () => {
    const entries = await fs.readdir(work);
    expect(entries.map(e => [e.name, e.type])).toEqual([
      ['a.txt', 'file'],
      ['dirlink', 'directory'],
      ['empty', 'directory'],
      ['filelink', 'file'],
      ['src', 'directory'],
    ]);
    expect(entries.find(e => e.name === 'a.txt')).toEqual({ name: 'a.txt', type: 'file', size: 5 });
    expect(entries.find(e => e.name === 'dirlink')).toEqual({ name: 'dirlink', type: 'directory', isSymlink: true, symlinkTarget: at('src') });
    expect(entries.find(e => e.name === 'filelink')).toEqual({ name: 'filelink', type: 'file', isSymlink: true, symlinkTarget: 'a.txt' });
    expect(disk.commands).toHaveLength(1);
    expect(disk.calls).toEqual([]);
  });

  it('lists the working directory for "." and relative paths', async () => {
    expect((await fs.readdir('.')).map(e => e.name)).toContain('a.txt');
    expect((await fs.readdir('src')).map(e => e.name)).toEqual(['b.ts', 'nested']);
  });

  it('lists recursively with a/b-style names, honoring maxDepth and filtering only files by extension', async () => {
    const all = await fs.readdir(at('src'), { recursive: true });
    expect(all.map(e => e.name)).toEqual(['b.ts', 'nested', 'nested/c.ts', 'nested/d.md']);
    const shallow = await fs.readdir(at('src'), { recursive: true, maxDepth: 0 });
    expect(shallow.map(e => e.name)).toEqual(['b.ts', 'nested']);
    const two = await fs.readdir(work, { recursive: true, maxDepth: 1 });
    expect(two.map(e => e.name)).toContain('src/nested');
    expect(two.map(e => e.name)).not.toContain('src/nested/c.ts');
    const ts = await fs.readdir(at('src'), { recursive: true, extension: '.ts' });
    expect(ts.map(e => e.name)).toEqual(['b.ts', 'nested', 'nested/c.ts']);
    const md = await fs.readdir(at('src'), { recursive: true, extension: ['md'] });
    expect(md.map(e => e.name)).toEqual(['nested', 'nested/d.md']);
  });

  it('lists through a symlinked directory, and does not descend into one when recursive', async () => {
    expect((await fs.readdir(at('dirlink'))).map(e => e.name)).toEqual(['b.ts', 'nested']);
    const all = await fs.readdir(work, { recursive: true });
    expect(all.map(e => e.name)).not.toContain('dirlink/b.ts');
  });

  it('keeps names with spaces, tabs, newlines and quotes intact', async () => {
    const odd = ["it's here", 'tab\there', 'new\nline', 'dollar $HOME', 'back\\slash'];
    await mkdir(at('odd'));
    for (const name of odd) await writeFile(join(at('odd'), name), name);
    expect((await fs.readdir(at('odd'))).map(e => e.name)).toEqual([...odd].sort());
    for (const name of odd) expect(await fs.readFile(join(at('odd'), name), { encoding: 'utf8' })).toBe(name);
  });

  it('maps a missing path to DirectoryNotFoundError and a file to NotDirectoryError', async () => {
    await expect(fs.readdir(at('nope'))).rejects.toBeInstanceOf(DirectoryNotFoundError);
    await expect(fs.readdir(at('a.txt'))).rejects.toBeInstanceOf(NotDirectoryError);
  });

  it.skipIf(ROOT_USER)('leaves out a subdirectory it cannot read, and refuses an unreadable directory itself', async () => {
    await mkdir(at('locked/inner'), { recursive: true });
    await chmod(at('locked'), 0o000);
    try {
      const names = (await fs.readdir(work, { recursive: true })).map(e => e.name);
      expect(names).toContain('locked');
      expect(names).toContain('src/nested/c.ts');
      expect((await fs.walk(work)).map(e => e.path)).toContain('src/nested/c.ts');
      await expect(fs.readdir(at('locked'))).rejects.toBeInstanceOf(PermissionError);
    } finally {
      await chmod(at('locked'), 0o755);
    }
  });

  it('refuses rather than return part of a listing past the output limit', async () => {
    disk.maxOutputBytes = 20;
    const error = await fs.readdir(work).catch(e => e);
    expect(error).toBeInstanceOf(FilesystemError);
    expect(error.message).toMatch(/Too many entries/);
  });
});

describe('walk', () => {
  beforeEach(async () => {
    await mkdir(at('src/deep/er'), { recursive: true });
    await mkdir(at('node_modules/pkg'), { recursive: true });
    await mkdir(at('.git'), { recursive: true });
    await writeFile(at('src/a.ts'), 'A');
    await writeFile(at('src/deep/er/b.ts'), 'B');
    await writeFile(at('node_modules/pkg/index.js'), 'x');
    await writeFile(at('.git/HEAD'), 'ref');
    await writeFile(at('.env'), 'S=1');
    await writeFile(at('.gitignore'), 'dist\n');
    await symlink(at('src'), at('srclink'));
    disk.commands.length = 0;
  });

  it('returns the tree in one command with paths relative to the root, pruning hidden entries', async () => {
    const entries = await fs.walk(work);
    expect(entries.map(e => e.path)).toEqual([
      'node_modules',
      'node_modules/pkg',
      'node_modules/pkg/index.js',
      'src',
      'src/a.ts',
      'src/deep',
      'src/deep/er',
      'src/deep/er/b.ts',
      'srclink',
    ]);
    expect(entries.find(e => e.path === 'src/deep/er/b.ts')).toEqual({ name: 'b.ts', type: 'file', size: 1, path: 'src/deep/er/b.ts' });
    expect(entries.find(e => e.path === 'srclink')).toMatchObject({ name: 'srclink', type: 'directory', isSymlink: true });
    expect(disk.commands).toHaveLength(1);
  });

  it('honours includeHidden, includeGitignoreFiles, skipDirectoryNames and maxDepth', async () => {
    const hidden = (await fs.walk(work, { includeHidden: true })).map(e => e.path);
    expect(hidden).toEqual(expect.arrayContaining(['.env', '.git', '.git/HEAD', '.gitignore']));
    const gitignore = (await fs.walk(work, { includeGitignoreFiles: true })).map(e => e.path);
    expect(gitignore).toContain('.gitignore');
    expect(gitignore).not.toContain('.env');
    const skipped = (await fs.walk(work, { skipDirectoryNames: ['node_modules'] })).map(e => e.path);
    expect(skipped.some(p => p.startsWith('node_modules'))).toBe(false);
    const shallow = (await fs.walk(work, { maxDepth: 1 })).map(e => e.path);
    expect(shallow).toEqual(['node_modules', 'src', 'srclink']);
  });

  it('maps a missing root to DirectoryNotFoundError and a file to NotDirectoryError', async () => {
    await expect(fs.walk(at('nope'))).rejects.toBeInstanceOf(DirectoryNotFoundError);
    await expect(fs.walk(at('src/a.ts'))).rejects.toBeInstanceOf(NotDirectoryError);
  });
});

describe('exists and stat', () => {
  it('exists is true for files, directories and live links, false for missing paths and broken links', async () => {
    await writeFile(at('f'), 'f');
    await mkdir(at('d'));
    await symlink('f', at('live'));
    await symlink('nowhere', at('broken'));
    expect(await fs.exists(at('f'))).toBe(true);
    expect(await fs.exists(at('d'))).toBe(true);
    expect(await fs.exists(at('live'))).toBe(true);
    expect(await fs.exists(at('broken'))).toBe(false);
    expect(await fs.exists(at('nope'))).toBe(false);
    expect(await fs.exists(join(out, 'nope'))).toBe(false);
  });

  it('reports a file and a directory with absolute paths, a directory size of 0, and the mtime to the ms', async () => {
    await writeFile(at('f.txt'), 'hello');
    await utimes(at('f.txt'), new Date(), new Date('2026-10-26T10:00:00.123Z'));
    await mkdir(at('d'));
    const file = await fs.stat('f.txt');
    expect(file).toEqual({
      name: 'f.txt',
      path: at('f.txt'),
      type: 'file',
      size: 5,
      createdAt: new Date('2026-10-26T10:00:00.123Z'),
      modifiedAt: new Date('2026-10-26T10:00:00.123Z'),
    });
    expect(await fs.stat(at('d'))).toMatchObject({ name: 'd', type: 'directory', size: 0 });
  });

  it("follows symlinks to the target's type and size", async () => {
    await writeFile(at('f.txt'), 'hello');
    await mkdir(at('d'));
    await symlink('f.txt', at('fl'));
    await symlink('d', at('dl'));
    expect(await fs.stat(at('fl'))).toMatchObject({ name: 'fl', type: 'file', size: 5 });
    expect(await fs.stat(at('dl'))).toMatchObject({ name: 'dl', type: 'directory', size: 0 });
  });

  it('maps a missing path and a broken link to FileNotFoundError', async () => {
    await symlink('nowhere', at('broken'));
    await expect(fs.stat(at('nope'))).rejects.toBeInstanceOf(FileNotFoundError);
    await expect(fs.stat(at('broken'))).rejects.toBeInstanceOf(FileNotFoundError);
  });
});

describe('PrizedSandbox.filesystem', () => {
  it('is memoized, named after the sandbox, and needs no request to create', async () => {
    const fresh = new PrizedSandbox({ id: 'memo', apiKey: KEY });
    expect(fresh.filesystem).toBe(fresh.filesystem);
    expect(fresh.filesystem).toBeInstanceOf(PrizedSandboxFilesystem);
    expect(fresh.filesystem.id).toBe('memo-filesystem');
    expect(fresh.filesystem.provider).toBe('prized');
    expect(fresh.filesystem.getInstructions()).toMatch(/relative paths start in \/home\/prized\/work/);
    expect(FakeSandbox.creates).toHaveLength(1); // only the sandbox from setup()
  });

  it('starts the sandbox before the first operation', async () => {
    const lazy = new PrizedSandbox({ id: 'lazy', apiKey: KEY, workingDirectory: work });
    const lazyFs = new PrizedSandboxFilesystem(lazy, { home });
    await writeFile(at('a.txt'), 'A');
    expect(lazy.status).toBe('pending');
    expect(await lazyFs.readFile('a.txt', { encoding: 'utf8' })).toBe('A');
    expect(lazy.status).toBe('running');
    expect(FakeSandbox.creates).toHaveLength(2);
  });

  it('starts a new sandbox when this one was deleted, and runs the failed call once more', async () => {
    // The fake's disk outlives its sandboxes, so the retried read finds the file; a real new
    // sandbox starts empty (see the staged-upload-gone test for a write split across the two).
    await writeFile(at('a.txt'), 'A');
    const first = sandbox.sandboxId!;
    FakeSandbox.store.delete(first); // deleted server-side
    expect(await fs.readFile(at('a.txt'), { encoding: 'utf8' })).toBe('A');
    expect(sandbox.sandboxId).not.toBe(first);
    expect(FakeSandbox.creates).toHaveLength(2);
    // Commands recover the same way.
    FakeSandbox.store.delete(sandbox.sandboxId!);
    expect(await fs.exists(at('a.txt'))).toBe(true);
    expect(FakeSandbox.creates).toHaveLength(3);
  });

  it('keeps working after the agent removes the working directory', async () => {
    await rm(work, { recursive: true });
    expect(await fs.exists(work)).toBe(false);
    await fs.mkdir(work);
    await fs.writeFile(at('back.txt'), 'b');
    expect(await text(at('back.txt'))).toBe('b');
  });

  it('passes errors that do not mean the sandbox is gone through unchanged, without retrying', async () => {
    await writeFile(at('a.txt'), 'A');
    disk.failNext = [new PrizedError('rate_limited', null, { status: 429 })];
    await expect(fs.readFile(at('a.txt'))).rejects.toMatchObject({ name: 'PrizedError', code: 'rate_limited' });
    expect(disk.calls).toHaveLength(1);
    expect(FakeSandbox.creates).toHaveLength(1);
  });

  it('surfaces an unknown command failure with its exit code and stderr', async () => {
    await writeFile(at('a.txt'), 'A');
    disk.shell = async () => ({ stdout: '', stderr: 'disk on fire\n', exitCode: 3, durationMs: 1, timedOut: false, truncated: false });
    await expect(fs.deleteFile(at('a.txt'))).rejects.toThrow(/deleteFile .*a\.txt failed \(exit 3\): disk on fire/);
  });
});

describe('the file commands on their own', () => {
  const stage = async (content: string) => {
    const staging = join(home, '.cache/prized-mastra/up-test');
    await mkdir(join(home, '.cache/prized-mastra'), { recursive: true });
    await writeFile(staging, content);
    return staging;
  };

  it('a no-clobber publish undoes a link that landed in a directory which appeared after the check', async () => {
    // Drop the check to reach the race path: ln then creates the link inside the directory.
    await mkdir(at('dest'));
    const staging = await stage('new');
    const lines = linkFromStagingCommand(staging, at('dest')).split('\n');
    const script = lines.filter(line => !line.startsWith('{ [ -e "$dest" ]')).join('\n');
    expect(script.split('\n')).toHaveLength(lines.length - 1);
    expect((await disk.shell(script)).exitCode).toBe(EXIT.exists);
    expect(await readdir(at('dest'))).toEqual([]);
    expect(await readdir(work)).toEqual(['dest']);
    expect(await disk.staged()).toEqual([]);
  });

  it('a no-clobber move does not move into a directory which appeared after the check', async () => {
    await writeFile(at('src.txt'), 's');
    await mkdir(at('dir'));
    const lines = moveNoClobberCommand(at('src.txt'), at('dir')).split('\n');
    const script = lines.filter(line => !line.startsWith('{ [ -e "$dest" ]')).join('\n');
    expect(script.split('\n')).toHaveLength(lines.length - 1);
    expect((await disk.shell(script)).exitCode).toBe(EXIT.exists);
    expect(await readdir(at('dir'))).toEqual([]);
    expect(await text(at('src.txt'))).toBe('s');
  });

  it.skipIf(ROOT_USER)('a staged replace that fails part-way leaves the old file and no temp file', async () => {
    await writeFile(at('f.txt'), 'old');
    const staging = await stage('new');
    await chmod(staging, 0o000); // the copy into the temp file fails
    const result = await disk.shell(replaceFromStagingCommand(staging, at('f.txt')));
    expect(result.exitCode).not.toBe(0);
    expect(await text(at('f.txt'))).toBe('old');
    expect(await readdir(work)).toEqual(['f.txt']);
    expect(await disk.staged()).toEqual([]);
  });
});

describe('a Workspace with the sandbox as its filesystem', () => {
  it("runs Mastra's file tools on the sandbox's disk: write, read, edit, list, grep", async () => {
    const workspace = new Workspace({ sandbox, filesystem: fs });
    await workspace.init();
    const tools = await createWorkspaceTools(workspace);
    const run = (name: string, input: Record<string, unknown>) => (tools[name] as any).execute(input, { workspace });
    const F = WORKSPACE_TOOLS.FILESYSTEM;

    await writeFile(at('run.sh'), 'echo one\n');
    await chmod(at('run.sh'), 0o755);
    await run(F.READ_FILE, { path: 'run.sh' });
    await run(F.EDIT_FILE, { path: 'run.sh', old_string: 'echo one', new_string: 'echo two' });
    expect(await text(at('run.sh'))).toBe('echo two\n');
    expect(await modeOf(at('run.sh'))).toBe(0o755);

    await run(F.WRITE_FILE, { path: 'src/app.ts', content: 'export const answer = 42;\n' });
    expect(await text(at('src/app.ts'))).toBe('export const answer = 42;\n');
    const read = await run(F.READ_FILE, { path: 'src/app.ts' });
    expect(JSON.stringify(read)).toContain('answer = 42');

    await mkdir(at('src/a/b'), { recursive: true });
    const before = disk.commands.length;
    const listed = await run(F.LIST_FILES, { path: '.', maxDepth: 5 });
    expect(JSON.stringify(listed)).toMatch(/app\.ts/);
    // One walk for the whole tree, not a listing per directory.
    expect(disk.commands.slice(before).filter(c => c.includes('-mindepth 1'))).toHaveLength(1);
    const found = await run(F.GREP, { pattern: 'answer', path: '.' });
    expect(JSON.stringify(found)).toMatch(/src\/app\.ts/);

    await workspace.destroy();
  });
});
