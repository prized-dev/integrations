/**
 * Against the real Prized API: one small sandbox, a few minutes, deleted at the end.
 *
 *   PRIZED_LIVE=1 PRIZED_API_KEY=dcp_... npm run test:live
 *
 * It drives the sandbox the way an Electric runtime does: a persistent, keyed `prizedSandbox()`
 * for one wake (every Sandbox method, stdin, a timeout, `fetch()` from inside the VM), dispose
 * (kept), a pause, a second wake that finds it by key and resumes it with its files, and
 * finally a profile lease that never ran a tool disposed with `reclaim`, which deletes it by
 * label. It then waits for the API to answer 404 for the sandbox.
 */
import { createHash, randomBytes } from 'node:crypto';

import { Sandbox as PrizedSdkSandbox } from '@prized/sdk';
import { describe, expect, it } from 'vitest';

import { PRIZED_SANDBOX_NAME, prizedSandbox, prizedSandboxProfile } from '../src/index.js';

const live = process.env.PRIZED_LIVE === '1' && !!process.env.PRIZED_API_KEY;
const run = process.env.PRIZED_LIVE_RUN ?? `electric-live-${randomBytes(4).toString('hex')}`;

describe.skipIf(!live)('live: Prized API', () => {
  it('runs a keyed persistent workspace across two wakes, then reclaims it', { timeout: 600_000 }, async () => {
    const key = `https://agents.example/live/${run}`;
    const common = {
      resources: { vcpu: 1, memoryMb: 2048, diskGb: 10 },
      maxRuntimeMin: 15,
      labels: { run },
      log: (m: string) => console.log(`[log] ${m}`),
    };
    let sandboxId: string | undefined;
    try {
      // Wake 1: create.
      const t0 = Date.now();
      const first = await prizedSandbox({ ...common, sandboxKey: key, persistent: true });
      expect(first.name).toBe(PRIZED_SANDBOX_NAME);
      const [listed] = await PrizedSdkSandbox.list({ labels: { run } });
      sandboxId = listed?.id;
      console.log(`created ${sandboxId} in ${Date.now() - t0} ms: autoPauseMin=${listed?.info.autoPauseMin} autoDeleteMin=${listed?.info.autoDeleteMin} labels=${JSON.stringify(listed?.labels)}`);
      expect(sandboxId).toMatch(/^sbx_/);
      expect(listed!.info.autoPauseMin).toBe(2);
      expect(listed!.info.autoDeleteMin).toBe(10_080);

      const hello = await first.exec({ command: 'echo "$(whoami) in $(pwd)"; uname -s' });
      expect(hello.stdout.toString()).toBe('prized in /home/prized/work\nLinux\n');
      expect(hello.exitCode).toBe(0);

      const piped = await first.exec({ command: 'sha256sum | cut -c1-16', stdin: Buffer.from([0, 1, 2, 255]) });
      const expectedHash = createHash('sha256').update(Buffer.from([0, 1, 2, 255])).digest('hex').slice(0, 16);
      expect(piped.stdout.toString().trim()).toBe(expectedHash);

      const bytes = randomBytes(300_000);
      await first.writeFile('data/blob.bin', bytes);
      expect(Buffer.compare(await first.readFile('data/blob.bin'), bytes)).toBe(0);
      expect(await first.stat('data/blob.bin')).toMatchObject({ type: 'file', size: 300_000 });
      await first.mkdir('a/b/c', { recursive: true });
      await first.writeFile('a/b/c/note.txt', 'kept across wakes');
      expect([...(await first.readdir('.'))].map(e => `${e.name}:${e.type}`).sort()).toEqual(['a:directory', 'data:directory']);
      expect(await first.exists('a/b/c/note.txt')).toBe(true);
      await first.remove('data', { recursive: true });
      expect(await first.exists('data')).toBe(false);
      await expect(first.remove('data')).rejects.toThrow();
      await expect(first.readFile('missing.txt')).rejects.toMatchObject({ code: 'ENOENT' });

      const osRelease = (await first.readFile('/etc/os-release')).toString();
      expect(osRelease).toMatch(/Ubuntu/);

      const slow = await first.exec({ command: 'echo begun; sleep 30', timeoutMs: 3_000 });
      expect(slow).toMatchObject({ timedOut: true, exitCode: null });
      expect(slow.stdout.toString()).toBe('begun\n');

      const page = await first.fetch('https://example.com/');
      expect(page.status).toBe(200);
      expect(await page.text()).toMatch(/Example Domain/);

      await first.dispose();
      const kept = await PrizedSdkSandbox.connect(sandboxId!);
      expect(kept.state).toBe('running');

      // Between wakes the sandbox idles out and pauses (autoPauseMin); pause it now instead of waiting.
      const p0 = Date.now();
      await kept.pause();
      console.log(`paused in ${Date.now() - p0} ms`);

      // Wake 2: another lease for the same key finds it, resumes it, and sees the files.
      const r0 = Date.now();
      const second = await prizedSandbox({ ...common, sandboxKey: key, persistent: true });
      console.log(`reattached and resumed in ${Date.now() - r0} ms`);
      expect((await second.readFile('a/b/c/note.txt')).toString()).toBe('kept across wakes');
      const again = await PrizedSdkSandbox.list({ labels: { run } });
      expect(again.map(s => s.id)).toEqual([sandboxId]);
      await second.dispose();

      // The entity ends: a fresh profile lease that never ran a tool reclaims the workspace by label.
      const profile = prizedSandboxProfile({ ...common });
      const lease = await profile.factory({ sandboxKey: key, persistent: true, owner: true, entityUrl: key, entityType: 'live', args: {} });
      await lease.dispose({ reclaim: true });
      // The reclaim itself deleted it (checked before the safety net below runs).
      const reclaimed = await PrizedSdkSandbox.connect(sandboxId!).then(
        s => s.state,
        (err: { status?: number }) => err.status,
      );
      expect(['deleting', 'deleted', 404]).toContain(reclaimed);
    } finally {
      // Whatever happened above, nothing from this run is left behind.
      const leftover = await PrizedSdkSandbox.deleteByLabels({ run });
      if (leftover.length > 0) console.log(`cleanup deleted ${leftover.join(', ')}`);
    }

    const deadline = Date.now() + 120_000;
    let gone = false;
    while (!gone && Date.now() < deadline) {
      try {
        const s = await PrizedSdkSandbox.connect(sandboxId!);
        console.log(`after reclaim: ${s.state}`);
        await new Promise(r => setTimeout(r, 3_000));
      } catch (err) {
        const e = err as { code?: string; status?: number };
        console.log(`after reclaim: ${e.status} ${e.code}`);
        gone = e.status === 404;
        if (!gone) throw err;
      }
    }
    expect(gone).toBe(true);
    console.log(`sandbox ${sandboxId} answers 404`);
  });
});
