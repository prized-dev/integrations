/**
 * The tools against a fake sandbox that runs the real git, tar and bash commands in a temp folder.
 */
import { existsSync, readFileSync, statSync } from 'node:fs';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { readConfig } from '../src/config.ts';
import { layoutFor } from '../src/paths.ts';
import { type Host, Sandboxes } from '../src/sandboxes.ts';
import { Tools } from '../src/tools.ts';
import { type FakeSandbox, fakeSdk, inSandbox, world } from './fake-sdk.ts';
import { makeCall, makeRepo } from './helpers.ts';

let repo: string;
let tools: Tools;
let sandboxes: Sandboxes;
const notes: string[] = [];

const host: Host = {
  rootOf: async (id) => id,
  inherited: async () => undefined,
  notify: (t) => notes.push(t),
};

beforeEach(() => {
  world.reset();
  notes.length = 0;
  repo = makeRepo();
  sandboxes = new Sandboxes(fakeSdk(), readConfig({ pauseSeconds: 0 }), { apiKey: 'dcp_000test' }, layoutFor(repo, repo), host);
  tools = new Tools(sandboxes);
});

afterEach(async () => {
  await sandboxes.dispose();
});

const sbx = (): FakeSandbox => [...world.sandboxes.values()][0] as FakeSandbox;

describe('tools in the sandbox', () => {
  it('uploads the working tree on first use, without ignored files', async () => {
    const r = await tools.bash({ command: 'pwd && ls -a' }, makeCall());
    expect(r.output).toContain('/home/prized/work/');
    expect(r.output).toContain('app.py');
    expect(r.output).not.toContain('.env\n');
    expect(existsSync(inSandbox(sbx(), `work/${repo.split('/').at(-1)}/.env`))).toBe(false);
    expect(world.creates[0]?.labels).toMatchObject({ 'created-by': 'opencode-prized', 'opencode-session': 'ses_root' });
    expect(r.metadata.prized).toEqual({ sandboxId: sbx().id, owner: 'ses_root' });
  });

  it('runs bash with OpenCode permission, exit codes, timeouts and workdir, and no host env', async () => {
    process.env.OC_PRIZED_TEST_SECRET = 'host-only';
    const call = makeCall();
    const r = await tools.bash({ command: 'echo "[$OC_PRIZED_TEST_SECRET]"; exit 3', workdir: `${repo}/src` }, call);
    expect(r.output).toContain('[]');
    expect(r.output).toContain('exit code 3');
    expect(call.asks[0]).toEqual({ permission: 'bash', patterns: ['echo "[$OC_PRIZED_TEST_SECRET]"', 'exit 3'] });
    const where = await tools.bash({ command: 'pwd', workdir: `${repo}/src` }, makeCall());
    expect(where.output.trim()).toMatch(/\/home\/prized\/work\/.+\/src$/);
    const slow = await tools.bash({ command: 'sleep 5', timeout: 300 }, makeCall());
    expect(slow.output).toContain('exceeding timeout 300 ms');
  });

  it('does nothing when bash is denied', async () => {
    await expect(tools.bash({ command: 'touch /home/prized/x' }, makeCall('ses_root', 'bash'))).rejects.toThrow(/rejected/);
    expect(world.creates).toHaveLength(0);
  });

  it('reads with line numbers, lists folders, and refuses binaries', async () => {
    const r = await tools.read({ filePath: `${repo}/app.py` }, makeCall());
    expect(r.output).toContain(`<path>${repo}/app.py</path>`);
    expect(r.output).toContain('1: def greet():');
    expect(r.output).toContain('(End of file - total 2 lines)');
    const part = await tools.read({ filePath: 'app.py', offset: 2, limit: 1 }, makeCall());
    expect(part.output).toContain('2:     print("Hi")');
    const dir = await tools.read({ filePath: repo }, makeCall());
    expect(dir.output).toContain('<type>directory</type>');
    expect(dir.output).toContain('bin/');
    await tools.bash({ command: 'printf "a\\0b" > blob.bin' }, makeCall());
    await expect(tools.read({ filePath: `${repo}/blob.bin` }, makeCall())).rejects.toThrow(/binary/);
    await expect(tools.read({ filePath: `${repo}/nope.txt` }, makeCall())).rejects.toThrow(/not found/);
  });

  it('writes and edits in the sandbox, never in your checkout', async () => {
    const call = makeCall();
    await tools.write({ filePath: `${repo}/notes/todo.md`, content: '# todo\n' }, call);
    const e = await tools.edit({ filePath: `${repo}/app.py`, oldString: 'print("Hi")', newString: 'print("Hello")' }, call);
    expect(e.metadata.diff).toContain('+    print("Hello")');
    expect(call.asks.map((a) => a.permission)).toEqual(['edit', 'edit']);
    const name = repo.split('/').at(-1);
    expect(readFileSync(inSandbox(sbx(), `work/${name}/app.py`), 'utf8')).toContain('Hello');
    expect(readFileSync(inSandbox(sbx(), `work/${name}/notes/todo.md`), 'utf8')).toBe('# todo\n');
    expect(readFileSync(`${repo}/app.py`, 'utf8')).toContain('"Hi"');
    expect(existsSync(`${repo}/notes`)).toBe(false);
    await expect(tools.edit({ filePath: `${repo}/app.py`, oldString: 'missing', newString: 'x' }, makeCall())).rejects.toThrow(/Could not find/);
  });

  it('finds files with glob and text with grep, as host paths, skipping ignored files', async () => {
    await tools.bash({ command: 'mkdir -p node_modules/x && echo "add" > node_modules/x/i.ts && echo "const add2 = 1" > src/more.ts' }, makeCall());
    const g = await tools.glob({ pattern: '**/*.ts' }, makeCall());
    expect(g.output.split('\n').sort()).toEqual([`${repo}/src/more.ts`, `${repo}/src/util.ts`]);
    const none = await tools.glob({ pattern: '*.rs' }, makeCall());
    expect(none.output).toBe('No files found');
    const m = await tools.grep({ pattern: 'add\\d?\\b', include: '*.{ts,tsx}' }, makeCall());
    expect(m.output).toContain(`${repo}/src/util.ts:`);
    expect(m.output).toContain('Line 1: export const add');
    expect(m.output).toContain(`${repo}/src/more.ts:`);
    expect(m.output).not.toContain('node_modules');
    const zero = await tools.grep({ pattern: 'definitely-not-here' }, makeCall());
    expect(zero.output).toBe('No files found');
  });

  it('runs a background command detached and returns at once', async () => {
    const started = Date.now();
    const r = await tools.bash({ command: 'sleep 30; echo late', background: true }, makeCall());
    expect(Date.now() - started).toBeLessThan(5000);
    expect(r.output).toMatch(/Started in the background in the sandbox \(pid \d+\)/);
  });

  it('reads and writes outside /home/prized through a copy, and refuses to edit non-UTF-8 files', async () => {
    const out = `/tmp/oc-prized-test-${process.pid}.txt`;
    await tools.write({ filePath: out, content: 'outside\n' }, makeCall());
    const r = await tools.read({ filePath: out }, makeCall());
    expect(r.output).toContain('1: outside');
    await tools.bash({ command: "printf 'caf\\351\\n' > latin1.txt" }, makeCall());
    await expect(tools.edit({ filePath: `${repo}/latin1.txt`, oldString: 'caf', newString: 'cafe' }, makeCall())).rejects.toThrow(/not UTF-8/);
  });

  it('greps literally and case-insensitively when asked, and caps results', async () => {
    await tools.bash({ command: 'printf "foo(1)\\nFOO(2)\\n" > src/f.txt' }, makeCall());
    const lit = await tools.grep({ pattern: 'foo(', literal: true }, makeCall());
    expect(lit.output).toContain('Line 1: foo(1)');
    const ci = await tools.grep({ pattern: 'foo\\(', caseSensitive: false }, makeCall());
    expect(ci.output).toContain('Found 2 matches');
    const capped = await tools.grep({ pattern: 'o', limit: 1 }, makeCall());
    expect(capped.output).toMatch(/showing first 1/);
  });

  it('reads the default number of lines when limit is 0', async () => {
    const r = await tools.read({ filePath: `${repo}/app.py`, limit: 0 }, makeCall());
    expect(r.output).toContain('2:     print("Hi")');
  });

  it('applies a patch: add, update with move, delete', async () => {
    const r = await tools.applyPatch(
      {
        patchText:
          '*** Begin Patch\n*** Add File: hello.txt\n+Hello\n*** Update File: app.py\n*** Move to: main.py\n@@ def greet():\n-    print("Hi")\n+    print("Hello")\n*** Delete File: src/util.ts\n*** End Patch',
      },
      makeCall(),
    );
    expect(r.output).toBe('Success. Updated the following files:\nA hello.txt\nM main.py\nD src/util.ts');
    const name = repo.split('/').at(-1);
    expect(readFileSync(inSandbox(sbx(), `work/${name}/main.py`), 'utf8')).toContain('Hello');
    expect(existsSync(inSandbox(sbx(), `work/${name}/app.py`))).toBe(false);
    expect(existsSync(inSandbox(sbx(), `work/${name}/src/util.ts`))).toBe(false);
    expect(statSync(`${repo}/app.py`).isFile()).toBe(true);
  });
});
