/** The project going up and the agent's changes coming back: real git and tar, a temp directory for the sandbox. */
import { execFileSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import type { Sandbox } from '@prized/sdk';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { changes, gitRoot, init, pull, shq, upload } from '../src/workspace.ts';
import { FakeSandbox, inSandbox, world } from './fake-sdk.ts';

const git = (cwd: string, ...args: string[]) => execFileSync('git', args, { cwd, encoding: 'utf8' });

let host: string;
let sb: FakeSandbox;
let sandbox: Sandbox;
const DIR = '/home/prized/work/proj';

beforeEach(async () => {
  world.reset();
  host = mkdtempSync(path.join(tmpdir(), 'host-'));
  git(host, 'init', '-q', '-b', 'main');
  git(host, 'config', 'user.name', 'Host User');
  git(host, 'config', 'user.email', 'host@example.com');
  writeFileSync(path.join(host, 'a.txt'), 'one\ntwo\nthree\n');
  mkdirSync(path.join(host, 'src'));
  writeFileSync(path.join(host, 'src', 'main.py'), 'print("hi")\n');
  writeFileSync(path.join(host, '.gitignore'), '.env\nnode_modules/\n');
  writeFileSync(path.join(host, '.env'), 'SECRET=1\n');
  mkdirSync(path.join(host, 'node_modules'));
  writeFileSync(path.join(host, 'node_modules', 'big.js'), 'x');
  git(host, 'add', '-A');
  git(host, 'commit', '-q', '-m', 'init');
  sandbox = await FakeSandbox.create({});
  sb = world.sandboxes.get(sandbox.id) as FakeSandbox;
});

afterEach(() => {
  rmSync(host, { recursive: true, force: true });
  world.reset();
});

describe('upload', () => {
  it('sends tracked and untracked files, keeps ignored ones home, and commits a baseline', async () => {
    writeFileSync(path.join(host, 'new.txt'), 'untracked\n');
    writeFileSync(path.join(host, 'a.txt'), 'one\ntwo\nthree\nlocal edit\n');
    const n = await upload(sandbox, host, DIR);
    expect(n).toBe(4); // .gitignore, a.txt, new.txt, src/main.py
    expect(readFileSync(inSandbox(sb, `${DIR}/a.txt`), 'utf8')).toContain('local edit');
    expect(readFileSync(inSandbox(sb, `${DIR}/new.txt`), 'utf8')).toBe('untracked\n');
    expect(existsSync(inSandbox(sb, `${DIR}/.env`))).toBe(false);
    expect(existsSync(inSandbox(sb, `${DIR}/node_modules`))).toBe(false);
    const dir = inSandbox(sb, DIR);
    expect(git(dir, 'rev-parse', '--abbrev-ref', 'HEAD').trim()).toBe('main');
    expect(git(dir, 'log', '--format=%s').trim()).toBe('Your working tree when the session started');
    expect(git(dir, 'config', 'user.name').trim()).toBe('Pi agent'); // the agent's own commits work
    expect(git(dir, 'status', '--porcelain').trim()).toBe('');
  });

  it('skips files deleted from the working tree and keeps dangling symlinks', async () => {
    git(host, 'rm', '-q', '--cached', 'a.txt');
    git(host, 'add', 'a.txt');
    rmSync(path.join(host, 'a.txt'));
    symlinkSync('missing-target', path.join(host, 'dangling'));
    await upload(sandbox, host, DIR);
    expect(existsSync(inSandbox(sb, `${DIR}/a.txt`))).toBe(false);
    expect(readFileSync(inSandbox(sb, `${DIR}/src/main.py`), 'utf8')).toContain('hi');
    expect(git(inSandbox(sb, DIR), 'ls-files').split('\n')).toContain('dangling');
  });

  it('handles file names with spaces, quotes and non-ASCII characters', async () => {
    const odd = "it's a name é.txt";
    writeFileSync(path.join(host, odd), 'odd\n');
    await upload(sandbox, host, DIR);
    expect(readFileSync(inSandbox(sb, `${DIR}/${odd}`), 'utf8')).toBe('odd\n');
  });

  it('starts an empty workspace when git lists nothing', async () => {
    const empty = mkdtempSync(path.join(tmpdir(), 'empty-'));
    git(empty, 'init', '-q');
    expect(await upload(sandbox, empty, DIR)).toBe(0);
    expect(git(inSandbox(sb, DIR), 'log', '--format=%s').trim()).toBe('Empty workspace');
    rmSync(empty, { recursive: true, force: true });
  });

  it('refuses a project over the 100 MiB the sandbox accepts, and says what to do', async () => {
    writeFileSync(path.join(host, 'big.bin'), randomBytes(2 * 1024 * 1024)); // does not compress
    await expect(upload(sandbox, host, DIR, 1024 * 1024)).rejects.toThrow(/uploads stop at 100 MiB.*\.gitignore/);
    expect(existsSync(inSandbox(sb, DIR))).toBe(false);
  });
});

describe('init', () => {
  it('makes an empty repository for a folder that is not a checkout', async () => {
    await init(sandbox, DIR);
    expect(git(inSandbox(sb, DIR), 'log', '--format=%s').trim()).toBe('Empty workspace');
  });
});

describe('changes and pull', () => {
  beforeEach(async () => {
    await upload(sandbox, host, DIR);
  });

  it('sees nothing before the agent works', async () => {
    const c = await changes(sandbox, DIR);
    expect(c.stat).toBe('');
    expect(c.patch.length).toBe(0);
  });

  it('diffs against the baseline whatever the agent did with git, and applies to the checkout', async () => {
    const dir = inSandbox(sb, DIR);
    writeFileSync(path.join(dir, 'a.txt'), 'one\nTWO\nthree\n');
    writeFileSync(path.join(dir, 'made.txt'), 'new file\n');
    rmSync(path.join(dir, 'src', 'main.py'));
    git(dir, 'add', '-A');
    git(dir, 'commit', '-q', '-m', 'the agent committed some of it');
    writeFileSync(path.join(dir, 'later.txt'), 'uncommitted\n');
    const c = await changes(sandbox, DIR);
    expect(c.stat).toMatch(/a\.txt/);
    expect(c.stat).toMatch(/made\.txt/);
    expect(c.stat).toMatch(/later\.txt/);
    expect(c.stat).toMatch(/src\/main\.py/);
    await pull(sandbox, DIR, host, c);
    expect(readFileSync(path.join(host, 'a.txt'), 'utf8')).toBe('one\nTWO\nthree\n');
    expect(readFileSync(path.join(host, 'made.txt'), 'utf8')).toBe('new file\n');
    expect(existsSync(path.join(host, 'src', 'main.py'))).toBe(false);
    expect(readFileSync(path.join(host, '.env'), 'utf8')).toBe('SECRET=1\n'); // never touched
    // The baseline moved: nothing left to pull, and the agent's index is untouched.
    expect((await changes(sandbox, DIR)).stat).toBe('');
    // A later edit comes back alone.
    writeFileSync(path.join(dir, 'later.txt'), 'uncommitted\nmore\n');
    const next = await changes(sandbox, DIR);
    expect(next.stat).toMatch(/later\.txt/);
    expect(next.stat).not.toMatch(/made\.txt/);
  });

  it('carries binary files and non-UTF-8 bytes byte for byte', async () => {
    const dir = inSandbox(sb, DIR);
    const bytes = Buffer.from([0x00, 0xff, 0xfe, 0x80, 0x81, 0x0a, 0xc3, 0x28]);
    writeFileSync(path.join(dir, 'blob.bin'), bytes);
    writeFileSync(path.join(dir, 'latin1.txt'), Buffer.from('caf\xe9\n', 'latin1'));
    const c = await changes(sandbox, DIR);
    await pull(sandbox, DIR, host, c);
    expect(readFileSync(path.join(host, 'blob.bin')).equals(bytes)).toBe(true);
    expect(readFileSync(path.join(host, 'latin1.txt')).equals(Buffer.from('caf\xe9\n', 'latin1'))).toBe(true);
  });

  it('keeps file modes', async () => {
    const dir = inSandbox(sb, DIR);
    writeFileSync(path.join(dir, 'run.sh'), '#!/bin/sh\necho hi\n', { mode: 0o755 });
    await pull(sandbox, DIR, host, await changes(sandbox, DIR));
    expect(statSync(path.join(host, 'run.sh')).mode & 0o100).toBe(0o100); // executable
  });

  it('applies nothing when your files changed in a conflicting way', async () => {
    const dir = inSandbox(sb, DIR);
    writeFileSync(path.join(dir, 'a.txt'), 'one\nTWO\nthree\n');
    writeFileSync(path.join(dir, 'made.txt'), 'new file\n');
    const c = await changes(sandbox, DIR);
    writeFileSync(path.join(host, 'a.txt'), 'one\nlocal two\nthree\n');
    await expect(pull(sandbox, DIR, host, c)).rejects.toThrow(/conflicts, so nothing was applied/);
    expect(readFileSync(path.join(host, 'a.txt'), 'utf8')).toBe('one\nlocal two\nthree\n');
    expect(existsSync(path.join(host, 'made.txt'))).toBe(false);
    // The baseline did not move, so the change can still be pulled after you sort your side out.
    expect((await changes(sandbox, DIR)).stat).toMatch(/made\.txt/);
  });

  it('commits on a local branch with --branch, and leaves your branch alone when it cannot', async () => {
    const dir = inSandbox(sb, DIR);
    writeFileSync(path.join(dir, 'made.txt'), 'new file\n');
    const c = await changes(sandbox, DIR);
    writeFileSync(path.join(host, 'dirty.txt'), 'uncommitted\n');
    await expect(pull(sandbox, DIR, host, c, 'pi/abc')).rejects.toThrow(/commit or stash/);
    rmSync(path.join(host, 'dirty.txt'));
    await pull(sandbox, DIR, host, c, 'pi/abc');
    expect(git(host, 'rev-parse', '--abbrev-ref', 'HEAD').trim()).toBe('pi/abc');
    expect(git(host, 'log', '-1', '--format=%s')).toMatch(/Changes from a Pi session/);
    expect(git(host, 'show', '--stat', '--format=', 'HEAD')).toMatch(/made\.txt/);
    expect(git(host, 'show', 'main:a.txt')).toBe('one\ntwo\nthree\n');
  });

  it('puts you back on your branch when a branch pull does not apply', async () => {
    git(host, 'switch', '-q', '-c', 'work');
    const dir = inSandbox(sb, DIR);
    writeFileSync(path.join(dir, 'a.txt'), 'one\nTWO\nthree\n');
    const c = await changes(sandbox, DIR);
    writeFileSync(path.join(host, 'a.txt'), 'one\nlocal two\nthree\n');
    git(host, 'commit', '-q', '-am', 'local');
    await expect(pull(sandbox, DIR, host, c, 'pi/zzz')).rejects.toThrow(/do not apply to pi\/zzz/);
    expect(git(host, 'rev-parse', '--abbrev-ref', 'HEAD').trim()).toBe('work');
    expect(git(host, 'branch', '--list', 'pi/zzz').trim()).toBe('');
  });
});

describe('gitRoot and shq', () => {
  it('finds the root from a subdirectory, and nothing outside a checkout', async () => {
    expect(await gitRoot(path.join(host, 'src'))).toBe(host);
    const plain = mkdtempSync(path.join(tmpdir(), 'plain-'));
    expect(await gitRoot(plain)).toBeUndefined();
    rmSync(plain, { recursive: true, force: true });
  });

  it('quotes for a POSIX shell', () => {
    expect(shq("a b'c")).toBe(`'a b'\\''c'`);
  });
});
