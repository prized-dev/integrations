/**
 * Against the real API: `PRIZED_LIVE=1 npm run test:live` with PRIZED_API_KEY set. Creates a few
 * small sandboxes (PRIZED_TEST_VCPU vCPU, default 2) labelled run=<this run>, and deletes every
 * one of them at the end, failures included.
 */
import { afterAll, describe, expect, it } from 'vitest';
import { PrizedError, Sandbox } from '@prized/sdk';
import { Workspace } from '@mastra/core/workspace';

import { PrizedSandbox, ID_LABEL, TIMEOUT_EXIT_CODE } from '../src/index.js';
import type { PrizedSandboxOptions } from '../src/index.js';
import { LIVE, RUN, sweep, testLabels, testResources } from './live-helpers.js';

let count = 0;
function make(overrides: PrizedSandboxOptions = {}): PrizedSandbox {
  return new PrizedSandbox({
    id: `${RUN}-${count++}`,
    labels: testLabels(),
    resources: testResources(),
    maxRuntimeMin: 60,
    ...overrides,
  });
}

async function stateOf(sandboxId: string): Promise<string> {
  try {
    return (await Sandbox.connect(sandboxId)).state;
  } catch (error) {
    if (error instanceof PrizedError && error.code === 'not_found') return 'gone';
    throw error;
  }
}

describe.skipIf(!LIVE)('PrizedSandbox against the real API', () => {
  const main = make();

  afterAll(async () => {
    await main._destroy().catch(() => {});
    const left = await sweep();
    if (left.length > 0) console.log(`swept ${left.length} sandbox(es): ${left.join(', ')}`);
  }, 300_000);

  it('creates a labelled sandbox and runs commands in it', async () => {
    await expect(main._start()).resolves.toEqual({ outcome: 'created' });
    expect(main.sandboxId).toMatch(/^sbx_/);
    const listed = await Sandbox.list({ labels: { [ID_LABEL]: main.id } });
    expect(listed.map(s => s.id)).toEqual([main.sandboxId]);

    const echo = await main.executeCommand!('echo', ['hello world', "it's"]);
    expect(echo).toMatchObject({ success: true, exitCode: 0, stdout: "hello world it's\n" });
    const pwd = await main.executeCommand!('pwd');
    expect(pwd.stdout.trim()).toBe('/home/prized/work');
    const tmp = await main.executeCommand!('pwd', [], { cwd: '/tmp' });
    expect(tmp.stdout.trim()).toBe('/tmp');
    const fail = await main.executeCommand!('sh', ['-c', 'echo out; echo err >&2; exit 42']);
    expect(fail).toMatchObject({ success: false, exitCode: 42, stdout: 'out\n', stderr: 'err\n' });
    const stdin = await main.executeCommand!('cat', [], { timeout: 10_000 });
    expect(stdin).toMatchObject({ exitCode: 0, stdout: '' });
    const info = await main.getInfo();
    expect(info).toMatchObject({ status: 'running', metadata: { sandboxId: main.sandboxId, state: 'running' } });
  });

  it('merges the sandbox env, setEnv and per-command env', async () => {
    main.setEnv(env => ({ ...env, FROM_SANDBOX: 'a', BOTH: 'sandbox' }));
    const r = await main.executeCommand!('sh', ['-c', 'echo "$FROM_SANDBOX $BOTH $FROM_CALL"'], {
      env: { BOTH: 'call', FROM_CALL: 'c' },
    });
    expect(r.stdout.trim()).toBe('a call c');
    main.setEnv(() => ({}));
  });

  it('streams output, times out, and aborts', async () => {
    const chunks: string[] = [];
    const streamed = await main.executeCommand!('sh', ['-c', 'for i in 1 2 3; do echo "chunk $i"; sleep 0.3; done'], {
      onStdout: c => chunks.push(c),
    });
    expect(streamed.stdout).toBe('chunk 1\nchunk 2\nchunk 3\n');
    expect(chunks.length).toBeGreaterThan(1);

    const slow = await main.executeCommand!('sh', ['-c', 'echo started; sleep 30'], { timeout: 2000 });
    expect(slow).toMatchObject({ success: false, exitCode: TIMEOUT_EXIT_CODE, timedOut: true });
    expect(slow.stdout).toContain('started');

    const controller = new AbortController();
    const t0 = Date.now();
    const aborted = await main.executeCommand!('sh', ['-c', 'echo before; sleep 30'], {
      abortSignal: controller.signal,
      onStdout: () => controller.abort(),
    });
    expect(aborted.success).toBe(false);
    expect(aborted.stdout).toContain('before');
    expect(Date.now() - t0).toBeLessThan(10_000);

    // Mastra's own conformance suite allows 2 s for a command whose signal had already fired.
    const fired = new AbortController();
    fired.abort();
    const t1 = Date.now();
    const pre = await main.executeCommand!('sleep', ['10'], { abortSignal: fired.signal });
    expect(pre.success).toBe(false);
    expect(Date.now() - t1).toBeLessThan(2000);
  });

  it('runs commands concurrently', async () => {
    const results = await Promise.all(
      ['1', '2', '3'].map(v => main.executeCommand!('sh', ['-c', 'sleep 1; echo $V'], { env: { V: v } })),
    );
    expect(results.map(r => r.stdout.trim())).toEqual(['1', '2', '3']);
  });

  it('runs, reads and kills a background process', async () => {
    const handle = await main.processes.spawn('i=0; while true; do i=$((i+1)); echo "tick $i"; sleep 0.2; done');
    await expect.poll(() => handle.stdout, { timeout: 15_000 }).toContain('tick 2');
    expect((await main.processes.list()).find(p => p.pid === handle.pid)?.running).toBe(true);
    expect(await main.processes.kill(handle.pid)).toBe(true);
    const result = await handle.wait();
    expect(result).toMatchObject({ success: false, killed: true });
    expect(await handle.kill()).toBe(false);
  });

  it('writes files with modes', async () => {
    await main.writeFiles([
      { path: '/home/prized/work/run.sh', content: '#!/bin/sh\necho ran\n', mode: 0o755 },
      { path: '/home/prized/work/data/blob.bin', content: Buffer.from([0, 1, 2, 255]) },
    ]);
    const r = await main.executeCommand!('sh', ['-c', './run.sh && stat -c %a run.sh && od -An -tu1 data/blob.bin']);
    expect(r.stdout.split('\n').map(l => l.trim().replace(/\s+/g, ' '))).toEqual(['ran', '755', '0 1 2 255', '']);
  });

  it('stop pauses the sandbox and keeps files and processes; start resumes the same one', async () => {
    await main.executeCommand!('sh', ['-c', 'echo kept > /home/prized/work/state.txt']);
    const bg = await main.executeCommand!('sh', ['-c', 'nohup sleep 3600 >/dev/null 2>&1 & echo $!']);
    const pid = bg.stdout.trim();
    const id = main.sandboxId!;

    await main._stop();
    expect(main.status).toBe('stopped');
    expect(await stateOf(id)).toBe('paused');

    await expect(main._start()).resolves.toEqual({ outcome: 'connected' });
    expect(main.sandboxId).toBe(id);
    const after = await main.executeCommand!('sh', ['-c', `cat /home/prized/work/state.txt && kill -0 ${pid} && echo alive`]);
    expect(after.stdout).toBe('kept\nalive\n');
  });

  it('a second instance with the same id, or the sandboxId, reattaches to the same sandbox', async () => {
    const byId = new PrizedSandbox({ id: main.id });
    await expect(byId._start()).resolves.toEqual({ outcome: 'connected' });
    expect(byId.sandboxId).toBe(main.sandboxId);
    expect((await byId.executeCommand!('cat', ['/home/prized/work/state.txt'])).stdout).toBe('kept\n');

    const bySandboxId = new PrizedSandbox({ sandboxId: main.sandboxId! });
    await expect(bySandboxId._start()).resolves.toEqual({ outcome: 'connected' });
    expect(bySandboxId.sandboxId).toBe(main.sandboxId);
  });

  it('starts a new sandbox under the same id when the old one is deleted elsewhere', async () => {
    const old = main.sandboxId!;
    await main.sandbox.delete({ wait: true });
    const r = await main.executeCommand!('echo', ['recovered']);
    expect(r.stdout).toBe('recovered\n');
    expect(main.sandboxId).not.toBe(old);
    expect(main.status).toBe('running');
    expect(await stateOf(old)).toBe('gone');
  });

  it('a Workspace starts and destroys its sandbox', async () => {
    const sandbox = make();
    const workspace = new Workspace({ sandbox });
    await workspace.init();
    expect(sandbox.status).toBe('running');
    const r = await workspace.sandbox!.executeCommand!('uname', ['-s']);
    expect(r.stdout.trim()).toBe('Linux');
    const id = sandbox.sandboxId!;
    await workspace.destroy();
    expect(sandbox.status).toBe('destroyed');
    expect(await stateOf(id)).toMatch(/^(gone|deleting|deleted)$/);
  });

  it('a new instance with the id deletes a paused sandbox without resuming it', async () => {
    const first = make();
    await first._start();
    const id = first.sandboxId!;
    await first._stop();
    expect(await stateOf(id)).toBe('paused');
    await new PrizedSandbox({ id: first.id }).destroy();
    expect(await stateOf(id)).toMatch(/^(gone|deleting|deleted)$/);
  });

  it('an unknown template fails start cleanly', async () => {
    const bad = make({ template: 'no-such-template' });
    await expect(bad._start()).rejects.toMatchObject({ code: 'template_not_found' });
    expect(bad.status).toBe('error');
  });

  it('destroy deletes the sandbox', async () => {
    const id = main.sandboxId!;
    await main._destroy();
    expect(main.status).toBe('destroyed');
    expect(await stateOf(id)).toMatch(/^(gone|deleting|deleted)$/);
    const left = await Sandbox.list({ labels: { [ID_LABEL]: main.id } });
    expect(left.filter(s => s.state !== 'deleting' && s.state !== 'deleted')).toEqual([]);
  });
});

describe.skipIf(LIVE)('live tests', () => {
  it.skip('need PRIZED_API_KEY and PRIZED_LIVE=1', () => {});
});
