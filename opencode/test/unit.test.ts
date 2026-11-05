import { describe, expect, it } from 'vitest';

import { readConfig, readEnv } from '../src/config.ts';
import { replace } from '../src/edit.ts';
import { globMatcher } from '../src/glob.ts';
import { type Hunk, applyChunks, parsePatch } from '../src/patch.ts';
import { layoutFor, relative, toHost, toSandbox } from '../src/paths.ts';
import { parseRaw, risks } from '../src/workspace.ts';

describe('paths', () => {
  const l = layoutFor('/Users/me/app/src', '/Users/me/app');

  it('mirrors the checkout and the start folder', () => {
    expect(l).toMatchObject({ dir: '/home/prized/work/app', cwd: '/home/prized/work/app/src', hostDir: '/Users/me/app' });
    expect(layoutFor('/tmp/x y', undefined)).toMatchObject({ dir: '/home/prized/work/x_y', cwd: '/home/prized/work/x_y', hostRoot: undefined });
  });

  it('maps every kind of path to a sandbox path and never to the host', () => {
    expect(toSandbox('/Users/me/app/src/a.ts', l, '/Users/me')).toBe('/home/prized/work/app/src/a.ts');
    expect(toSandbox('/Users/me/app', l, '/Users/me')).toBe('/home/prized/work/app');
    expect(toSandbox('a.ts', l, '/Users/me')).toBe('/home/prized/work/app/src/a.ts');
    expect(toSandbox('../README.md', l, '/Users/me')).toBe('/home/prized/work/app/README.md');
    expect(toSandbox('/home/prized/work/app/x', l, '/Users/me')).toBe('/home/prized/work/app/x');
    expect(toSandbox('~/.bashrc', l, '/Users/me')).toBe('/home/prized/.bashrc');
    expect(toSandbox('/Users/me/.ssh/id_rsa', l, '/Users/me')).toBe('/home/prized/.ssh/id_rsa');
    expect(toSandbox('/etc/hosts', l, '/Users/me')).toBe('/etc/hosts');
    // A prefix that is not a folder boundary is not the project.
    expect(toSandbox('/Users/me/app2/x', l, '/Users/me')).toBe('/home/prized/app2/x');
    expect(toSandbox('/Users/me/app/../../../etc/passwd', l, '/Users/me')).toBe('/etc/passwd');
  });

  it('reads Windows paths', () => {
    const w = { dir: '/home/prized/work/app', cwd: '/home/prized/work/app', hostDir: 'C:\\Users\\me\\app', hostCwd: 'C:\\Users\\me\\app' };
    expect(toSandbox('C:\\Users\\me\\app\\src\\a.ts', w, 'C:\\Users\\me', 'win32')).toBe('/home/prized/work/app/src/a.ts');
    expect(toSandbox('C:\\Users\\me\\notes.txt', w, 'C:\\Users\\me', 'win32')).toBe('/home/prized/notes.txt');
    expect(toSandbox('src\\b.ts', w, 'C:\\Users\\me', 'win32')).toBe('/home/prized/work/app/src/b.ts');
  });

  it('shows sandbox paths under the project as host paths', () => {
    expect(toHost('/home/prized/work/app/src/a.ts', l, 'linux')).toBe('/Users/me/app/src/a.ts');
    expect(toHost('/tmp/x', l, 'linux')).toBe('/tmp/x');
    expect(relative('/home/prized/work/app/src/a.ts', l)).toBe('src/a.ts');
  });
});

describe('glob', () => {
  it('matches like ripgrep globs', () => {
    expect(globMatcher('*.ts')('src/deep/a.ts')).toBe(true);
    expect(globMatcher('*.ts')('src/a.tsx')).toBe(false);
    expect(globMatcher('**/*.ts')('a.ts')).toBe(true);
    expect(globMatcher('src/**/*.{ts,tsx}')('src/x/y.tsx')).toBe(true);
    expect(globMatcher('src/*.ts')('src/x/y.ts')).toBe(false);
    expect(globMatcher('test_?.py')('pkg/test_a.py')).toBe(true);
    expect(globMatcher('[ab].md')('b.md')).toBe(true);
    expect(globMatcher('a.b')('axb')).toBe(false);
  });
});

describe('edit', () => {
  it('replaces one exact match, all with replaceAll, and keeps CRLF', () => {
    expect(replace('a\nb\nc\n', 'b', 'B')).toBe('a\nB\nc\n');
    expect(replace('x x x', 'x', 'y', true)).toBe('y y y');
    expect(replace('a\r\nb\r\n', 'a\nb', 'A\nB')).toBe('A\r\nB\r\n');
  });

  it('tolerates indentation the model got wrong', () => {
    expect(replace('def f():\n    return 1\n', 'def f():\n  return 1', 'def f():\n    return 2')).toBe('def f():\n    return 2\n');
  });

  it("says what OpenCode's edit says", () => {
    expect(() => replace('a', 'zzz', 'y')).toThrow(/Could not find oldString/);
    expect(() => replace('a a', 'a', 'b')).toThrow(/Found multiple matches/);
    expect(() => replace('a', 'a', 'a')).toThrow(/identical/);
    expect(() => replace('a', '', 'b')).toThrow(/oldString cannot be empty/);
  });
});

describe('apply_patch', () => {
  it('parses add, update with move, and delete', () => {
    const hunks = parsePatch(
      '*** Begin Patch\n*** Add File: hello.txt\n+Hello\n*** Update File: src/app.py\n*** Move to: src/main.py\n@@ def greet():\n-    print("Hi")\n+    print("Hello")\n*** Delete File: old.txt\n*** End Patch',
    );
    expect(hunks.map((h) => h.type)).toEqual(['add', 'update', 'delete']);
    expect(hunks[0]).toMatchObject({ path: 'hello.txt', contents: 'Hello\n' });
    expect(hunks[1]).toMatchObject({ path: 'src/app.py', movePath: 'src/main.py' });
  });

  it('applies chunks after their context line', () => {
    const before = 'def a():\n    print("Hi")\n\ndef greet():\n    print("Hi")\n';
    const [h] = parsePatch('*** Begin Patch\n*** Update File: x.py\n@@ def greet():\n-    print("Hi")\n+    print("Hello")\n*** End Patch');
    expect(applyChunks('x.py', before, (h as Extract<Hunk, { type: 'update' }>).chunks)).toBe('def a():\n    print("Hi")\n\ndef greet():\n    print("Hello")\n');
  });

  it('refuses what it cannot place', () => {
    expect(() => parsePatch('*** Update File: x')).toThrow(/Begin Patch/);
    const [h] = parsePatch('*** Begin Patch\n*** Update File: x\n-nope\n+yes\n*** End Patch');
    expect(() => applyChunks('x', 'a\n', (h as Extract<Hunk, { type: 'update' }>).chunks)).toThrow(/failed to find expected lines/);
  });
});

describe('config', () => {
  it('reads options and refuses unknown or out of range ones', () => {
    expect(readConfig(undefined)).toMatchObject({ pauseSecs: 20, idleMin: 10, keep: false });
    expect(readConfig({ vcpu: 4, memoryGb: 8, pauseSeconds: 0, keep: true, template: 'prized-agent-v1' })).toMatchObject({
      resources: { vcpu: 4, memoryGb: 8 },
      pauseSecs: 0,
      keep: true,
      template: 'prized-agent-v1',
    });
    expect(() => readConfig({ vcpu: 64 })).toThrow(/vcpu/);
    expect(() => readConfig({ typo: 1 })).toThrow(/no option "typo"/);
  });

  it('takes the key and the endpoints from the environment only', () => {
    expect(() => readConfig({ apiKey: 'dcp_x' })).toThrow(/environment/);
    expect(() => readConfig({ baseUrl: 'https://evil.example' })).toThrow(/environment/);
    expect(readEnv({ PRIZED_API_KEY: ' dcp_000 ', PRIZED_BASE_URL: 'http://127.0.0.1:9' })).toEqual({ apiKey: 'dcp_000', baseUrl: 'http://127.0.0.1:9', edgeUrl: undefined });
  });
});

describe('pull review', () => {
  it('names symlinks, executables (new, made, or edited) and files tools run on their own', () => {
    const raw = Buffer.from(
      [
        ':000000 100755 0 0 A\0bin/new.sh\0',
        ':100644 100755 0 0 M\0tools/run\0',
        ':100755 100755 0 0 M\0scripts/deploy\0',
        ':000000 120000 0 0 A\0link\0',
        ':100644 100644 0 0 M\0.opencode/plugins/x.ts\0',
        ':100644 100644 0 0 M\0src/app.ts\0',
      ].join(''),
    );
    expect(risks(parseRaw(raw))).toEqual([
      'an executable: bin/new.sh',
      'made executable: tools/run',
      'a change to an executable file: scripts/deploy (read the diff)',
      'a symlink: link',
      'a file your tools run on their own: .opencode/plugins/x.ts',
    ]);
  });
});
