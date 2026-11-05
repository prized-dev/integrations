// The live test with a scoped key and no broker, run on the machine that holds the long-lived key:
//
//   PRIZED_API_KEY=dcp_... node test/live/scoped-run.mjs      (V2=1 runs e2e-v2.mjs after e2e.mjs)
//
// Node 22.18+ (it imports src/scoped.ts as it is) and an @prized/sdk with Sandbox.scopedKey under node_modules.
//
// 1. Mints a scoped key for this run with src/scoped.ts. It fails closed: if the API or the SDK has no scoped
//    keys, it says why and stops before anything is created (exit 3).
// 2. Creates the runner sandbox with only that key in its environment (the runner itself is made with the
//    long-lived key and carries no label the scoped key selects, so the scoped key cannot reach it), copies
//    in this folder's files that git tracks or would track (never an ignored .env), refusing to if any holds
//    the long-lived key, starts the scripted model and runs e2e.mjs there with E2E_KEY=scoped.
// 3. Brings the results back to $RESULTS_DIR (default: a folder under the temp dir), deletes what the scoped key made (with that key), revokes it,
//    checks it is refused, and deletes the runner. The key is never printed.
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { PrizedError, Sandbox } from '@prized/sdk';

import { mintSandboxKey } from '../../src/scoped.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const pkg = path.resolve(here, '../..');
const RUN = `scoped-${new Date().toISOString().replace(/[-:]/g, '').slice(0, 15)}`;
const baseUrl = process.env.PRIZED_BASE_URL?.trim() || undefined;
const results = process.env.RESULTS_DIR || path.join(tmpdir(), 'opencode-prized-live');
mkdirSync(results, { recursive: true });
const log = (s) => process.stdout.write(`[${RUN}] ${s}\n`);

const parentKey = process.env.PRIZED_API_KEY?.trim() ?? '';
// The files that go to the runner: what git tracks or would track here, so an ignored .env stays behind.
const files = execFileSync('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '.'], { cwd: pkg });
const tar = execFileSync('tar', ['-cf', '-', '--null', '-T', '-'], { cwd: pkg, input: files, maxBuffer: 256 << 20 });
if (parentKey === '' || tar.includes(Buffer.from(parentKey))) {
  log(parentKey === '' ? 'no PRIZED_API_KEY here' : 'a file here holds your PRIZED_API_KEY, so nothing was minted or copied');
  process.exit(2);
}
const key = await mintSandboxKey({
  // Long enough for both scripts at their 30-minute bound.
  ttlSeconds: process.env.V2 ? 7200 : 3600,
  spendCapUsd: Number(process.env.SPEND_CAP_USD || 2),
  name: `opencode-prized live ${RUN}`,
});
if (!key.available) {
  log(`no scoped key, so nothing was created: ${key.reason}`);
  process.exit(3);
}
log(`minted ${key} (ops: every one the plugin uses)`);

const cleanup = [];
let runner;
try {
  runner = await Sandbox.create({
    baseUrl,
    template: 'prized-base-v1',
    resources: { vcpu: 2, memoryGb: 4 },
    envVars: key.env,
    labels: { 'oc-e2e': RUN, role: 'runner' },
    maxRuntimeMin: 90,
    autoPauseMin: 20,
  });
  log(`runner ${runner.id}`);
  await runner.files.write('/home/prized/oc-src.tgz', gzipSync(tar));
  await runner.exec('mkdir -p /home/prized/oc && tar -xzf /home/prized/oc-src.tgz -C /home/prized/oc && rm /home/prized/oc-src.tgz');
  await runner.exec('cd /home/prized/oc/test/live && (setsid nohup node mock-model.mjs > /tmp/mock.out 2>&1 &) && sleep 1 && echo started');
  for (const [script, out] of [['e2e.mjs', 'result.json'], ...(process.env.V2 ? [['e2e-v2.mjs', 'result-v2.json']] : [])]) {
    log(`running ${script} in the runner`);
    const run = await runner.exec(`cd /home/prized/oc/test/live && E2E_KEY=scoped node ${script} > /tmp/${script}.out 2>&1; tail -n 5 /tmp/${script}.out`, { timeoutMs: 30 * 60_000 });
    log(run.stdout.trim());
    const result = await runner.files.read(`/home/prized/e2e/${out}`).catch((err) => JSON.stringify({ error: String(err) }));
    const file = path.join(results, `${out.replace('.json', '')}-${RUN}.json`);
    writeFileSync(file, result);
    const parsed = JSON.parse(result);
    log(`${script}: ${parsed.passed ?? 0} passed, ${parsed.failed ?? '?'} failed -> ${file}`);
  }
} finally {
  // What the scoped key made, by that key: its list is only its own.
  const scoped = { apiKey: key.env.PRIZED_API_KEY, baseUrl };
  const made = await Sandbox.list(scoped).catch((err) => err);
  if (Array.isArray(made)) {
    const failed = [];
    for (const sb of made) await sb.delete().catch(() => failed.push(sb.id));
    cleanup.push({ name: `deleted what the scoped key made (${made.length})`, ok: failed.length === 0, detail: failed.join(' ') });
  } else {
    cleanup.push({ name: 'list what the scoped key made', ok: false, detail: made?.code ?? String(made) });
  }
  cleanup.push({ name: 'the host revokes the scoped key', ok: await key.revoke().catch(() => false) });
  const after = await Sandbox.list(scoped).then(() => null, (err) => err);
  cleanup.push({ name: 'the revoked key is refused (401)', ok: after instanceof PrizedError && after.status === 401, detail: after?.code });
  if (runner) await runner.delete().catch((err) => cleanup.push({ name: 'delete the runner', ok: false, detail: String(err) }));
  for (const c of cleanup) log(`${c.ok ? 'PASS' : 'FAIL'} ${c.name}${c.detail && !c.ok ? `: ${c.detail}` : ''}`);
  writeFileSync(path.join(results, `cleanup-${RUN}.json`), JSON.stringify(cleanup, null, 2));
}
