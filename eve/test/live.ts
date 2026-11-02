/**
 * One live run of the provider's side against the real Prized API, with eve's side played by the
 * fakes in fake-eve.ts (eve itself is not loaded). It builds a template, starts a session as a fork
 * of it, runs commands and moves files, pauses and wakes it, ends the session, and deletes
 * everything it made, by its run label, whatever happens.
 *
 *   PRIZED_API_KEY=dcp_... npm run test:live
 *
 * It bills your workspace for about two minutes of three 1 vCPU, 1 GB sandboxes (well under $0.05),
 * and needs a workspace that has paid in (templates are forks; see the README).
 */
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';

import { Sandbox } from '@prized/sdk';

import { SESSION_LABEL, TEMPLATE_LABEL, createPrizedImplementation, sessionLabel } from '../src/provider.ts';
import { EVE, prepareContext, readText, run, sessionContext } from './fake-eve.ts';

const apiKey = process.env.PRIZED_API_KEY;
if (!apiKey) {
  console.error('Set PRIZED_API_KEY to run the live test.');
  process.exit(2);
}

const runLabel = `eve-live-${randomBytes(4).toString('hex')}`;
const labels: Record<string, string> = { 'eve-live': runLabel };
for (const pair of (process.env.EVE_LIVE_LABELS ?? '').split(',').filter(Boolean)) {
  const [name, value] = pair.split('=');
  if (name && value) labels[name] = value;
}
const seen = new Set<string>();
const step = (name: string, extra: Record<string, unknown> = {}) =>
  console.log(JSON.stringify({ t: new Date().toISOString(), step: name, ...extra }));

const impl = createPrizedImplementation(
  { apiKey, labels, resources: { vcpu: 1, memoryGb: 1 }, maxRuntimeMin: 15, autoPauseMin: 5 },
  Sandbox,
  EVE,
);

async function main(): Promise<void> {
  // prepare: a template with a workspace file, a skill and an authored prepare step.
  const files = {
    workspace: { 'README.md': '# live\n', 'src/hello.py': 'print("hello from the template")\n' },
    skills: { 'live/SKILL.md': '---\nname: live\n---\n' },
    revision: runLabel,
  };
  const ctx = prepareContext({
    ...files,
    prepare: async sandbox => {
      const result = await sandbox.run('python3 -m venv ~/.venv && echo "$AI_AGENT" > prepared.txt');
      assert.equal(result.exitCode, 0, result.stderr);
    },
  });
  let t = Date.now();
  const artifact = await impl.prepare(ctx);
  assert.ok(artifact.templateId);
  seen.add(artifact.templateId);
  const template = await Sandbox.connect(artifact.templateId, { apiKey });
  assert.equal(template.state, 'paused');
  assert.ok(template.labels[TEMPLATE_LABEL]);
  step('prepare', { ms: Date.now() - t, templateId: artifact.templateId, logs: ctx.logs });

  // A second build with the same inputs reuses it and creates nothing.
  t = Date.now();
  const again = await impl.prepare(prepareContext({ ...files, prepare: async () => {} }));
  assert.equal(again.templateId, artifact.templateId);
  step('prepare-again', { ms: Date.now() - t, reused: true });

  // start: two concurrent starts of one session settle on one fork of the template.
  const session = sessionContext(`live-session-${runLabel}`);
  t = Date.now();
  const [first, second] = await Promise.all([
    impl.start(session, { labels: { user: 'live' } }, artifact),
    impl.start(session, { labels: { user: 'live' } }, artifact),
  ]);
  seen.add(first.state.sandboxId);
  seen.add(second.state.sandboxId);
  assert.equal(first.state.sandboxId, second.state.sandboxId);
  const live = await Sandbox.list({ apiKey, labels: { [SESSION_LABEL]: sessionLabel(session.session.id) } });
  for (const s of live) seen.add(s.id);
  step('start', { ms: Date.now() - t, sandboxId: first.state.sandboxId, sessionSandboxes: live.map(s => `${s.id}:${s.state}`) });
  const connection = first.connection;

  // Commands and files.
  t = Date.now();
  const where = await run(connection, 'pwd; echo "$AI_AGENT"; cat prepared.txt; python3 src/hello.py; ls ~/.agents/skills/live');
  assert.equal(where.exitCode, 0, where.stderr);
  assert.equal(where.stdout, '/home/prized/work\neve\neve\nhello from the template\nSKILL.md\n');
  const failing = await run(connection, 'echo oops >&2; exit 7');
  assert.deepEqual(failing, { exitCode: 7, stdout: '', stderr: 'oops\n' });
  const enc = new TextEncoder();
  await connection.writeFiles({
    files: [
      { path: '/home/prized/work/notes/a.txt', content: enc.encode('in the home') },
      { path: '/tmp/eve-live/b.txt', content: enc.encode('outside the home') },
    ],
  });
  assert.equal(await readText(connection, '/home/prized/work/notes/a.txt'), 'in the home');
  assert.equal(await readText(connection, '/tmp/eve-live/b.txt'), 'outside the home');
  assert.equal(await connection.readFile({ path: '/home/prized/work/missing.txt' }), null);
  assert.equal(await connection.readFile({ path: '/tmp/eve-live/missing.txt' }), null);
  await assert.rejects(connection.readFile({ path: '/home/prized/work/notes' }), { code: 'EISDIR' });
  await connection.removePath({ path: '/tmp/eve-live', recursive: true });
  assert.equal(await connection.readFile({ path: '/tmp/eve-live/b.txt' }), null);
  const killed = await connection.spawn({ command: 'sleep 60', env: {}, workingDirectory: '/home/prized/work' });
  await killed.kill();
  const { exitCode } = await killed.wait();
  assert.notEqual(exitCode, 0);
  step('commands-and-files', { ms: Date.now() - t, killedExit: exitCode });

  // stop pauses; resume reconnects without waking; the next command wakes it with its files.
  t = Date.now();
  await connection.stop();
  const paused = await Sandbox.connect(first.state.sandboxId, { apiKey });
  assert.equal(paused.state, 'paused');
  const resumed = await impl.resume(session, artifact, first.state);
  assert.equal((await Sandbox.connect(first.state.sandboxId, { apiKey })).state, 'paused');
  const woke = Date.now();
  assert.equal(await readText(resumed, '/home/prized/work/notes/a.txt'), 'in the home');
  step('stop-resume-wake', { ms: Date.now() - t, wakeMs: Date.now() - woke });

  // onSessionEnd deletes it; a later resume says the session is gone.
  t = Date.now();
  await impl.onSessionEnd(session, first.state, { reason: 'completed' });
  await impl.onSessionEnd(session, first.state, { reason: 'completed' });
  await assert.rejects(impl.resume(session, artifact, first.state), { name: 'SandboxSessionGoneError' });
  step('session-end', { ms: Date.now() - t });
}

async function cleanup(): Promise<void> {
  for (const s of await Sandbox.list({ apiKey, labels: { 'eve-live': runLabel } })) seen.add(s.id);
  await Sandbox.deleteByLabels({ 'eve-live': runLabel }, { apiKey });
  const left: string[] = [];
  for (let i = 0; i < 40; i++) {
    left.length = 0;
    for (const id of seen) {
      try {
        const s = await Sandbox.connect(id, { apiKey });
        left.push(`${id}:${s.state}`);
      } catch (error) {
        if ((error as { status?: number }).status !== 404) throw error;
      }
    }
    if (left.length === 0) break;
    await new Promise(resolve => setTimeout(resolve, 3000));
  }
  step('cleanup', { ids: [...seen], notYet404: left });
}

let failed = false;
try {
  await main();
} catch (error) {
  failed = true;
  console.error(error);
} finally {
  await cleanup().catch(error => {
    failed = true;
    console.error('cleanup failed:', error);
  });
}
step(failed ? 'FAILED' : 'PASSED');
process.exit(failed ? 1 : 0);
