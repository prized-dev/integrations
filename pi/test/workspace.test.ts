/** The project going up and the agent's changes coming back: real git and tar, a temp directory for the sandbox. */
import { execFileSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import type { Sandbox } from '@prized/sdk';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { capStat, changes, gitRoot, init, leftBehind, pack, parseRaw, pull, risks, run, shq, upload } from '../src/workspace.ts';
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
    const { files: n } = await upload(sandbox, host, DIR);
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
    expect((await upload(sandbox, empty, DIR)).files).toBe(0);
    expect(git(inSandbox(sb, DIR), 'log', '--format=%s').trim()).toBe('Empty workspace');
    rmSync(empty, { recursive: true, force: true });
  });

  it('refuses a project over the 100 MiB the sandbox accepts, and says what to do', async () => {
    writeFileSync(path.join(host, 'big.bin'), randomBytes(2 * 1024 * 1024)); // does not compress
    await expect(upload(sandbox, host, DIR, 1024 * 1024)).rejects.toThrow(/uploads stop there.*\.gitignore/);
    expect(existsSync(inSandbox(sb, DIR))).toBe(false);
  });
});

describe('what stays home', () => {
  it('leaves a submodule or nested repository at home, with its ignored files, and names it', async () => {
    const nested = path.join(host, 'vendor', 'lib');
    mkdirSync(nested, { recursive: true });
    git(nested, 'init', '-q');
    writeFileSync(path.join(nested, '.env'), 'NESTED_SECRET=1\n');
    writeFileSync(path.join(nested, 'code.js'), '1\n');
    const packed = await pack(host);
    expect(packed.skipped).toEqual(['vendor/lib/']);
    await upload(sandbox, host, DIR);
    expect(existsSync(inSandbox(sb, `${DIR}/vendor`))).toBe(false);
    expect(readFileSync(inSandbox(sb, `${DIR}/a.txt`), 'utf8')).toContain('one');
  });

  it('carries file names that are not UTF-8', async () => {
    const odd = Buffer.concat([Buffer.from('caf'), Buffer.from([0xe9]), Buffer.from('.txt')]); // latin1 e-acute
    writeFileSync(Buffer.concat([Buffer.from(`${host}/`), odd]), 'odd\n');
    const packed = await pack(host);
    expect(packed.files).toBeGreaterThanOrEqual(4);
    await upload(sandbox, host, DIR);
    const listing = execFileSync('ls', ['-1', inSandbox(sb, DIR)], { encoding: 'buffer' });
    expect(listing.includes(odd)).toBe(true);
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
    writeFileSync(path.join(host, 'a.txt'), 'one\ntwo\nthree\nedited\n');
    await expect(pull(sandbox, DIR, host, c, 'pi/abc')).rejects.toThrow(/commit or stash/);
    git(host, 'checkout', '--', 'a.txt');
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

describe('ignore rules cannot hide the agent\'s work or delete yours', () => {
  it('sees edits to a file git tracks although it is ignored', async () => {
    writeFileSync(path.join(host, 'dist.js'), 'v1\n');
    git(host, 'add', '-f', 'dist.js'); // tracked on purpose although .gitignore says otherwise below
    writeFileSync(path.join(host, '.gitignore'), '.env\nnode_modules/\ndist.js\n');
    git(host, 'add', '.gitignore');
    git(host, 'commit', '-q', '-m', 'track dist.js anyway');
    await upload(sandbox, host, DIR);
    writeFileSync(path.join(inSandbox(sb, DIR), 'dist.js'), 'v2\n');
    const c = await changes(sandbox, DIR);
    expect(c.stat).toMatch(/dist\.js/);
    await pull(sandbox, DIR, host, c);
    expect(readFileSync(path.join(host, 'dist.js'), 'utf8')).toBe('v2\n');
  });

  it('does not delete your files when the agent ignores a directory that was uploaded', async () => {
    mkdirSync(path.join(host, 'data'));
    writeFileSync(path.join(host, 'data', 'fixtures.json'), '{}\n');
    await upload(sandbox, host, DIR);
    const dir = inSandbox(sb, DIR);
    writeFileSync(path.join(dir, '.gitignore'), '.env\nnode_modules/\ndata/\n');
    const c = await changes(sandbox, DIR);
    expect(c.stat).toMatch(/\.gitignore/);
    expect(c.stat).not.toMatch(/fixtures/);
    await pull(sandbox, DIR, host, c);
    expect(existsSync(path.join(host, 'data', 'fixtures.json'))).toBe(true);
  });

  it('still leaves out new files the project ignores, such as node_modules', async () => {
    await upload(sandbox, host, DIR);
    const dir = inSandbox(sb, DIR);
    mkdirSync(path.join(dir, 'node_modules', 'x'), { recursive: true });
    writeFileSync(path.join(dir, 'node_modules', 'x', 'i.js'), '1\n');
    writeFileSync(path.join(dir, '.env'), 'NEW_SECRET=1\n');
    writeFileSync(path.join(dir, 'real.txt'), 'kept\n');
    const c = await changes(sandbox, DIR);
    expect(c.stat).toMatch(/real\.txt/);
    expect(c.stat).not.toMatch(/node_modules|\.env/);
  });
});

describe('pull --branch from a detached HEAD', () => {
  it('puts you back on the commit you were on when the changes do not apply', async () => {
    await upload(sandbox, host, DIR);
    writeFileSync(path.join(inSandbox(sb, DIR), 'a.txt'), 'one\nTWO\nthree\n');
    const c = await changes(sandbox, DIR);
    writeFileSync(path.join(host, 'a.txt'), 'one\nlocal\nthree\n');
    git(host, 'commit', '-q', '-am', 'local');
    const sha = git(host, 'rev-parse', 'HEAD').trim();
    git(host, 'checkout', '-q', '--detach');
    await expect(pull(sandbox, DIR, host, c, 'pi/dd')).rejects.toThrow(/do not apply/);
    expect(git(host, 'rev-parse', 'HEAD').trim()).toBe(sha);
    expect(git(host, 'rev-parse', '--abbrev-ref', 'HEAD').trim()).toBe('HEAD'); // still detached, not on a pi/ branch
    expect(git(host, 'branch', '--list', 'pi/dd').trim()).toBe('');
  });
});

describe('risks and capStat', () => {
  const filesOf = (dir: string = DIR) => changes(sandbox, dir).then((c) => c.files);

  it('flags symlinks, executables and files that run by themselves, at any depth, and nothing else', async () => {
    await upload(sandbox, host, DIR);
    const dir = inSandbox(sb, DIR);
    execFileSync('ln', ['-s', '/etc/passwd', path.join(dir, 'link')]);
    writeFileSync(path.join(dir, 'run.sh'), '#!/bin/sh\n', { mode: 0o755 });
    mkdirSync(path.join(dir, '.husky'));
    writeFileSync(path.join(dir, '.husky', 'pre-commit'), 'x\n');
    writeFileSync(path.join(dir, '.envrc'), 'export A=1\n');
    mkdirSync(path.join(dir, 'packages', 'app', '.pi', 'extensions', 'foo b'), { recursive: true }); // a space in the path
    writeFileSync(path.join(dir, 'packages', 'app', '.pi', 'extensions', 'foo b', 'index.ts'), 'x\n');
    mkdirSync(path.join(dir, '.claude'));
    writeFileSync(path.join(dir, '.claude', 'settings.json'), '{}\n');
    writeFileSync(path.join(dir, '.mcp.json'), '{}\n');
    writeFileSync(path.join(dir, 'plain.txt'), 'fine\n');
    writeFileSync(path.join(dir, 'a.txt'), 'one\nchanged\nthree\n');
    const found = risks(await filesOf());
    expect(found).toEqual(
      expect.arrayContaining([
        'a symlink: link',
        'an executable: run.sh',
        'a file your tools run on their own: .husky/pre-commit',
        'a file your tools run on their own: .envrc',
        'a file your tools run on their own: packages/app/.pi/extensions/foo b/index.ts',
        'a file your tools run on their own: .claude/settings.json',
        'a file your tools run on their own: .mcp.json',
      ]),
    );
    expect(found.join('\n')).not.toMatch(/plain\.txt|a\.txt/);
  });

  it('flags retargeting an existing symlink and editing an existing executable', async () => {
    writeFileSync(path.join(host, 'tool.sh'), '#!/bin/sh\necho 1\n', { mode: 0o755 });
    execFileSync('ln', ['-s', 'a.txt', path.join(host, 'ptr')]);
    git(host, 'add', '-A');
    git(host, 'commit', '-q', '-m', 'add an executable and a symlink');
    await upload(sandbox, host, DIR);
    const dir = inSandbox(sb, DIR);
    writeFileSync(path.join(dir, 'tool.sh'), '#!/bin/sh\necho 2\n', { mode: 0o755 });
    rmSync(path.join(dir, 'ptr'));
    execFileSync('ln', ['-s', '/etc/shadow', path.join(dir, 'ptr')]);
    expect(risks(await filesOf())).toEqual(expect.arrayContaining(['an executable: tool.sh', 'a symlink: ptr']));
  });

  it('does not flag a deleted hook, and finds nothing in an ordinary change', async () => {
    await upload(sandbox, host, DIR);
    writeFileSync(path.join(inSandbox(sb, DIR), 'a.txt'), 'one\nchanged\nthree\n');
    expect(risks(await filesOf())).toEqual([]);
  });

  it('reads git\'s raw list, names with newlines and non-ASCII included', () => {
    const entry = (mode: string, p: string) => `:100644 ${mode} 0000 1111 M\0${p}\0`;
    const files = parseRaw(Buffer.from(entry('120000', 'a\nb') + entry('100644', 'caf\u00e9.txt')));
    expect(files).toEqual([
      { oldMode: '100644', newMode: '120000', path: 'a\nb' },
      { oldMode: '100644', newMode: '100644', path: 'caf\u00e9.txt' },
    ]);
  });

  it('cuts a long stat to a few dozen files and a count, keeping the summary line', () => {
    const lines = Array.from({ length: 100 }, (_, i) => ` f${i}.txt | 1 +`);
    const stat = [...lines, ' 100 files changed, 100 insertions(+)'].join('\n');
    const cut = capStat(stat, 30);
    expect(cut.split('\n')).toHaveLength(32);
    expect(cut).toMatch(/ \.\.\. and 70 more files/);
    expect(cut.endsWith('100 files changed, 100 insertions(+)')).toBe(true);
    expect(capStat(' a | 1 +\n 1 file changed', 30)).toBe(' a | 1 +\n 1 file changed');
  });
});

describe('what the sandbox cannot hand back', () => {
  beforeEach(async () => {
    await upload(sandbox, host, DIR);
  });

  it('leaves a nested git repository the agent made out of the changes, names it, and still pulls the rest', async () => {
    const dir = inSandbox(sb, DIR);
    mkdirSync(path.join(dir, 'cloned'));
    git(path.join(dir, 'cloned'), 'init', '-q'); // no commit in it: git add would refuse it outright
    writeFileSync(path.join(dir, 'cloned', 'x.txt'), 'x\n');
    writeFileSync(path.join(dir, 'fine.txt'), 'fine\n');
    const c = await changes(sandbox, DIR);
    expect(c.nested).toEqual(['cloned']);
    expect(c.files.map((f) => f.path)).toEqual(['fine.txt']);
    expect(leftBehind(c)).toMatch(/Not included: cloned \(nested git repositories; delete their \.git folder/);
    await pull(sandbox, DIR, host, c);
    expect(readFileSync(path.join(host, 'fine.txt'), 'utf8')).toBe('fine\n');
    expect(existsSync(path.join(host, 'cloned'))).toBe(false);
    // Once its .git is gone, its files are ordinary files and come back.
    rmSync(path.join(dir, 'cloned', '.git'), { recursive: true });
    const again = await changes(sandbox, DIR);
    expect(again.nested).toEqual([]);
    await pull(sandbox, DIR, host, again);
    expect(readFileSync(path.join(host, 'cloned', 'x.txt'), 'utf8')).toBe('x\n');
  });

  it('also leaves out a nested repository that has commits, and one inside a folder with a space', async () => {
    const dir = inSandbox(sb, DIR);
    mkdirSync(path.join(dir, 'dep one'));
    git(path.join(dir, 'dep one'), 'init', '-q');
    git(path.join(dir, 'dep one'), 'config', 'user.name', 'T');
    git(path.join(dir, 'dep one'), 'config', 'user.email', 't@t');
    writeFileSync(path.join(dir, 'dep one', 'f'), '1\n');
    git(path.join(dir, 'dep one'), 'add', '-A');
    git(path.join(dir, 'dep one'), 'commit', '-q', '-m', 'c');
    mkdirSync(path.join(dir, 'node_modules', 'pkg'), { recursive: true });
    git(path.join(dir, 'node_modules', 'pkg'), 'init', '-q'); // ignored by the project anyway
    const c = await changes(sandbox, DIR);
    expect(c.nested).toEqual(['dep one']);
    expect(c.stat).toBe('');
  });

  it('shows names as they are, not git\'s quoted escapes', async () => {
    writeFileSync(path.join(inSandbox(sb, DIR), 'caf\u00e9.txt'), 'x\n');
    expect((await changes(sandbox, DIR)).stat).toContain('caf\u00e9.txt');
  });

  it('pulls to a branch although you have an untracked file, and not with an edited tracked one', async () => {
    writeFileSync(path.join(inSandbox(sb, DIR), 'made.txt'), 'new\n');
    const c = await changes(sandbox, DIR);
    writeFileSync(path.join(host, 'scratch.log'), 'untracked\n');
    await pull(sandbox, DIR, host, c, 'pi/abc12345');
    expect(git(host, 'show', '--stat', '--format=', 'HEAD')).toMatch(/made\.txt/);
    expect(existsSync(path.join(host, 'scratch.log'))).toBe(true);
  });
});

describe('run', () => {
  it('stops a command that prints more than it may, and keeps what it printed out of memory', async () => {
    await expect(run('sh', ['-c', 'yes | head -c 5000000'], { cwd: host, maxOut: 1000 })).rejects.toThrow(/more than 1000 bytes/);
  });

  it('accepts a non-zero exit only when told it is fine', async () => {
    await expect(run('sh', ['-c', 'echo partial; exit 1'], { cwd: host })).rejects.toThrow();
    expect((await run('sh', ['-c', 'echo partial; echo "x changed as we read it" >&2; exit 1'], { cwd: host, ok: (c, e) => c === 1 && /changed as we read it/.test(e) })).toString()).toBe('partial\n');
    await expect(run('sh', ['-c', 'echo bad >&2; exit 1'], { cwd: host, ok: (c, e) => c === 1 && /changed as we read it/.test(e) })).rejects.toThrow(/bad/);
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
