/**
 * The lifecycle eve drives (prepare, start, resume, stop, onSessionEnd), against the fake Prized
 * API and the fake eve contexts. No network, no key.
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, it } from 'node:test';

import {
  BUILD_LABEL,
  SESSION_LABEL,
  TEMPLATE_LABEL,
  createPrizedImplementation,
  sessionLabel,
} from '../src/provider.ts';
import type { PrizedSandboxImplementation, PrizedSandboxOptions } from '../src/provider.ts';
import { EVE, prepareContext, readText, run, sessionContext } from './fake-eve.ts';
import { FakePrized } from './fake-prized.ts';

const KEY = 'dcp_0000000000000000000000000000';

let api: FakePrized;
beforeEach(() => {
  api = new FakePrized();
});
afterEach(() => api.close());

function provider(options: PrizedSandboxOptions = {}): PrizedSandboxImplementation {
  return createPrizedImplementation({ apiKey: KEY, ...options }, api, EVE);
}

describe('options', () => {
  it('refuses a relative workspaceRoot and a forkFrom that is not a sandbox id', () => {
    assert.throws(() => provider({ workspaceRoot: 'work' }), /workspaceRoot must be an absolute path/);
    assert.throws(() => provider({ forkFrom: 'my-template' }), /forkFrom must be a sandbox id/);
  });

  it('names PRIZED_API_KEY and the sign-up path when there is no key', async () => {
    const saved = process.env.PRIZED_API_KEY;
    delete process.env.PRIZED_API_KEY;
    try {
      const impl = createPrizedImplementation({}, api, EVE);
      await assert.rejects(impl.start(sessionContext('s1'), undefined, {}), /PRIZED_API_KEY.*via=eve/s);
      assert.equal(api.creates.length, 0);
    } finally {
      if (saved !== undefined) process.env.PRIZED_API_KEY = saved;
    }
  });
});

describe('prepare', () => {
  it('makes nothing for an agent with nothing to prepare', async () => {
    const ctx = prepareContext();
    assert.deepEqual(await provider().prepare(ctx), {});
    assert.equal(api.sandboxes.size, 0);
  });

  it('hands over forkFrom as the template when there is nothing to prepare', async () => {
    assert.deepEqual(await provider({ forkFrom: 'sbx_abc123' }).prepare(prepareContext()), { templateId: 'sbx_abc123' });
    assert.equal(api.sandboxes.size, 0);
  });

  it('builds a paused template with the workspace, the skills and the authored prepare, and deletes the build', async () => {
    const ctx = prepareContext({
      workspace: { 'README.md': '# hello', 'src/app.py': 'print(1)' },
      skills: { 'deploy/SKILL.md': '---\nname: deploy\n---' },
      prepare: async sandbox => {
        const result = await sandbox.run('echo prepared > prepared.txt && echo "$AI_AGENT" > agent.txt');
        assert.equal(result.exitCode, 0, result.stderr);
      },
    });
    const artifact = await provider({ labels: { app: 'demo' } }).prepare(ctx);
    assert.ok(artifact.templateId);
    const template = api.get(artifact.templateId);
    assert.equal(template.state, 'paused');
    assert.equal(template.labels[TEMPLATE_LABEL]?.length, 32);
    assert.equal(template.labels.app, 'demo');
    assert.equal(template.labels[BUILD_LABEL], undefined);
    const files = (path: string) => readFileSync(template.host(path), 'utf8');
    assert.equal(files('/home/prized/work/README.md'), '# hello');
    assert.equal(files('/home/prized/work/src/app.py'), 'print(1)');
    assert.equal(files('/home/prized/.agents/skills/deploy/SKILL.md'), '---\nname: deploy\n---');
    assert.equal(files('/home/prized/work/prepared.txt'), 'prepared\n');
    assert.equal(files('/home/prized/work/agent.txt'), 'eve\n');
    // The build sandbox is gone; only the template is left.
    assert.deepEqual(api.live().map(s => s.id), [template.id]);
    const build = [...api.sandboxes.values()].find(s => s.labels[BUILD_LABEL] !== undefined);
    assert.equal(build?.state, 'deleted');
    assert.equal(api.creates[0]?.autoPauseMin, 10);
    assert.deepEqual(api.creates[0]?.envVars, { AI_AGENT: 'eve' });
  });

  it('reuses the template a build with the same inputs published', async () => {
    const input = { workspace: { 'a.txt': 'a' } };
    const first = await provider().prepare(prepareContext(input));
    const before = api.sandboxes.size;
    const ctx = prepareContext(input);
    const second = await provider().prepare(ctx);
    assert.deepEqual(second, first);
    assert.equal(api.sandboxes.size, before, 'no sandbox was created');
    assert.equal(ctx.populated, 0);
    assert.match(ctx.logs.join('\n'), /reusing Prized sandbox template/);
  });

  it('builds a new template when the files or the options change', async () => {
    const first = await provider().prepare(prepareContext({ workspace: { 'a.txt': 'a' } }));
    const other = await provider().prepare(prepareContext({ workspace: { 'a.txt': 'b' } }));
    const shaped = await provider({ resources: { vcpu: 2 } }).prepare(prepareContext({ workspace: { 'a.txt': 'a' } }));
    assert.equal(new Set([first.templateId, other.templateId, shaped.templateId]).size, 3);
  });

  it('builds on forkFrom when it is given', async () => {
    const base = await api.create({ apiKey: KEY, labels: { role: 'base' } });
    await base.write('/home/prized/work/base.txt', 'from the base');
    await base.pause();
    const artifact = await provider({ forkFrom: base.id }).prepare(prepareContext({ workspace: { 'a.txt': 'a' } }));
    const template = api.get(artifact.templateId!);
    assert.equal(readFileSync(template.host('/home/prized/work/base.txt'), 'utf8'), 'from the base');
    assert.equal(readFileSync(template.host('/home/prized/work/a.txt'), 'utf8'), 'a');
    assert.equal(api.get(base.id).state, 'paused', 'the base is left as it was');
    assert.equal(api.creates.length, 1, 'only the base was created; the rest are forks');
  });

  it('deletes the build and says why when the authored prepare fails', async () => {
    const ctx = prepareContext({
      prepare: async () => {
        throw new Error('apt-get: not found');
      },
    });
    await assert.rejects(provider().prepare(ctx), /Failed to prepare the Prized sandbox template: apt-get: not found/);
    assert.deepEqual(api.live(), []);
  });

  it('deletes a paused build an earlier build abandoned over an hour ago', async () => {
    const input = { workspace: { 'a.txt': 'a' } };
    // Find the key the next build will use, then plant an abandoned build under it.
    const probe = prepareContext(input);
    await provider().prepare(probe);
    const key = api.live()[0]!.labels[TEMPLATE_LABEL]!;
    for (const s of api.live()) await s.delete();
    const abandoned = await api.create({ apiKey: KEY, labels: { [BUILD_LABEL]: key } });
    await abandoned.pause();
    abandoned.lastActiveAt = new Date(Date.now() - 2 * 60 * 60 * 1000);
    const fresh = await api.create({ apiKey: KEY, labels: { [BUILD_LABEL]: key } });
    await fresh.pause();
    await provider().prepare(prepareContext(input));
    assert.equal(api.get(abandoned.id).state, 'deleted');
    assert.equal(api.get(fresh.id).state, 'paused', 'a recent build may still be running');
  });
});

describe('start', () => {
  it('creates a labelled sandbox with AI_AGENT, auto-pause and the workspace root, when there is no template', async () => {
    const { connection, state } = await provider({ env: { FOO: 'bar' }, labels: { app: 'demo' }, maxRuntimeMin: 120 }).start(
      sessionContext('session-1'),
      { labels: { user: 'u1' } },
      {},
    );
    const sandbox = api.get(state.sandboxId);
    assert.deepEqual(state, { sandboxId: sandbox.id, version: 1 });
    assert.deepEqual(sandbox.labels, { app: 'demo', user: 'u1', [SESSION_LABEL]: sessionLabel('session-1') });
    assert.deepEqual(api.creates[0]?.envVars, { AI_AGENT: 'eve', FOO: 'bar' });
    assert.equal(api.creates[0]?.autoPauseMin, 10);
    assert.equal(api.creates[0]?.maxRuntimeMin, 120);
    assert.equal(connection.workspaceRoot, '/home/prized/work');
    assert.deepEqual(connection.extensions, { sandboxId: sandbox.id });
    const result = await run(connection, 'pwd && echo "$AI_AGENT $FOO"');
    assert.equal(result.stdout, '/home/prized/work\neve bar\n');
  });

  it('makes a custom workspace root before the first command', async () => {
    const { connection } = await provider({ workspaceRoot: '/home/prized/project' }).start(sessionContext('s'), undefined, {});
    assert.equal(connection.workspaceRoot, '/home/prized/project');
    assert.equal((await run(connection, 'pwd')).stdout, '/home/prized/project\n');
  });

  it('forks the template, with the session label in place of the template label', async () => {
    const artifact = await provider().prepare(prepareContext({ workspace: { 'hello.txt': 'hi' } }));
    const { connection, state } = await provider().start(sessionContext('s1'), undefined, artifact);
    const sandbox = api.get(state.sandboxId);
    assert.equal(sandbox.forkedFrom, artifact.templateId);
    assert.deepEqual(sandbox.labels, { [SESSION_LABEL]: sessionLabel('s1') });
    assert.equal(await readText(connection, '/home/prized/work/hello.txt'), 'hi');
    assert.equal(api.get(artifact.templateId!).state, 'paused', 'the template stays paused');
  });

  it('passes maxRuntimeMin to the fork', async () => {
    const artifact = await provider().prepare(prepareContext({ workspace: { 'a.txt': 'a' } }));
    const { state } = await provider({ maxRuntimeMin: 300 }).start(sessionContext('s1'), undefined, artifact);
    assert.equal(api.get(state.sandboxId).maxRuntimeMin, 300);
  });

  it('forks forkFrom directly for an agent with nothing to prepare', async () => {
    const base = await api.create({ apiKey: KEY });
    await base.write('/home/prized/work/tool.sh', 'echo tool');
    await base.pause();
    const impl = provider({ forkFrom: base.id });
    const artifact = await impl.prepare(prepareContext());
    const { connection, state } = await impl.start(sessionContext('s1'), undefined, artifact);
    assert.equal(api.get(state.sandboxId).forkedFrom, base.id);
    assert.equal((await run(connection, 'bash tool.sh')).stdout, 'tool\n');
  });

  it('returns the same sandbox to every start of a session', async () => {
    const impl = provider();
    const first = await impl.start(sessionContext('s1'), undefined, {});
    const again = await impl.start(sessionContext('s1'), { labels: { ignored: 'yes' } }, {});
    assert.equal(again.state.sandboxId, first.state.sandboxId);
    assert.equal(api.creates.length, 1);
    const other = await impl.start(sessionContext('s2'), undefined, {});
    assert.notEqual(other.state.sandboxId, first.state.sandboxId);
  });

  it('settles concurrent starts of one session on one sandbox and deletes the extras', async () => {
    const impl = provider();
    const results = await Promise.all([1, 2, 3, 4].map(() => impl.start(sessionContext('race'), undefined, {})));
    const ids = new Set(results.map(r => r.state.sandboxId));
    assert.equal(ids.size, 1, `all starts returned one sandbox, got ${[...ids]}`);
    assert.deepEqual(
      api.live({ [SESSION_LABEL]: sessionLabel('race') }).map(s => s.id),
      [...ids],
    );
  });

  it('adopts a paused session sandbox without waking it', async () => {
    const impl = provider();
    const first = await impl.start(sessionContext('s1'), undefined, {});
    await first.connection.stop();
    const again = await impl.start(sessionContext('s1'), undefined, {});
    assert.equal(again.state.sandboxId, first.state.sandboxId);
    assert.equal(api.get(first.state.sandboxId).state, 'paused');
  });

  it('reports a deleted template as not provisioned', async () => {
    const artifact = await provider().prepare(prepareContext({ workspace: { 'a.txt': 'a' } }));
    await api.get(artifact.templateId!).delete();
    await assert.rejects(provider().start(sessionContext('s1'), undefined, artifact), (error: Error & { templateKey?: string }) => {
      assert.equal(error.name, 'SandboxTemplateNotProvisionedError');
      assert.equal(error.templateKey, artifact.templateId);
      return true;
    });
    assert.deepEqual(api.live(), []);
  });

  it('passes other fork failures on unchanged', async () => {
    const artifact = await provider().prepare(prepareContext({ workspace: { 'a.txt': 'a' } }));
    api.forkError = Object.assign(new Error('fork_capacity'), { code: 'fork_capacity' });
    await assert.rejects(provider().start(sessionContext('s1'), undefined, artifact), /fork_capacity/);
  });

  it('refuses an artifact it did not write', async () => {
    await assert.rejects(
      provider().start(sessionContext('s1'), undefined, { templateId: 7 } as unknown as { templateId: string }),
      /run `eve build` again/,
    );
  });
});

describe('stop and resume', () => {
  it('stop pauses; resume reconnects without waking; the next command wakes it with its files', async () => {
    const impl = provider();
    const { connection, state } = await impl.start(sessionContext('s1'), undefined, {});
    await run(connection, 'echo kept > note.txt');
    await connection.stop();
    const sandbox = api.get(state.sandboxId);
    assert.equal(sandbox.state, 'paused');
    await connection.stop();
    assert.equal(sandbox.state, 'paused', 'a second stop is a no-op');

    const resumed = await impl.resume(sessionContext('s1'), {}, state);
    assert.equal(sandbox.state, 'paused', 'resume alone does not wake it');
    assert.equal(sandbox.wakes, 0);
    assert.equal((await run(resumed, 'cat note.txt')).stdout, 'kept\n');
    assert.equal(sandbox.state, 'running');
    assert.equal(sandbox.wakes, 1);
  });

  it('a stopped connection still works: its next command wakes the sandbox', async () => {
    const { connection, state } = await provider().start(sessionContext('s1'), undefined, {});
    await connection.stop();
    assert.equal((await run(connection, 'echo back')).stdout, 'back\n');
    assert.equal(api.get(state.sandboxId).state, 'running');
  });

  it('stop on a deleted sandbox does not throw', async () => {
    const { connection, state } = await provider().start(sessionContext('s1'), undefined, {});
    await api.get(state.sandboxId).delete();
    await connection.stop();
  });

  it('resume of a deleted sandbox throws SandboxSessionGoneError', async () => {
    const impl = provider();
    const { state } = await impl.start(sessionContext('s1'), undefined, {});
    await api.get(state.sandboxId).delete();
    await assert.rejects(impl.resume(sessionContext('s1'), {}, state), (error: Error) => {
      assert.equal(error.name, 'SandboxSessionGoneError');
      return true;
    });
  });

  it('resume of a failed sandbox deletes it and throws SandboxSessionGoneError', async () => {
    const impl = provider();
    const { state } = await impl.start(sessionContext('s1'), undefined, {});
    api.get(state.sandboxId).state = 'error';
    await assert.rejects(impl.resume(sessionContext('s1'), {}, state), { name: 'SandboxSessionGoneError' });
    assert.equal(api.get(state.sandboxId).state, 'deleted');
  });

  it('after a gone session eve starts again and gets a fresh sandbox', async () => {
    const impl = provider();
    const first = await impl.start(sessionContext('s1'), undefined, {});
    await api.get(first.state.sandboxId).delete();
    const second = await impl.start(sessionContext('s1'), undefined, {});
    assert.notEqual(second.state.sandboxId, first.state.sandboxId);
  });

  it('resume rejects state it did not write, without calling it gone', async () => {
    await assert.rejects(provider().resume(sessionContext('s1'), {}, { name: 'x' } as unknown as { sandboxId: string; version: 1 }), (error: Error) => {
      assert.notEqual(error.name, 'SandboxSessionGoneError');
      assert.match(error.message, /session state is not one this provider wrote/);
      return true;
    });
  });

  it('stop ends output polling of a running command, so the paused sandbox stays paused', async () => {
    const { connection, state } = await provider().start(sessionContext('s1'), undefined, {});
    const process = await connection.spawn({ command: 'sleep 30', env: {}, workingDirectory: '/home/prized/work' });
    await connection.stop();
    await assert.rejects(Promise.resolve(process.wait()), /stopped while a command was running/);
    const sandbox = api.get(state.sandboxId);
    assert.equal(sandbox.state, 'paused');
    assert.equal(sandbox.wakes, 0);
  });
});

describe('onSessionEnd', () => {
  it('deletes the session sandbox, and is safe to repeat', async () => {
    const impl = provider();
    const { state } = await impl.start(sessionContext('s1'), undefined, {});
    const other = await impl.start(sessionContext('s2'), undefined, {});
    await impl.onSessionEnd(sessionContext('s1'), state, { reason: 'completed' });
    assert.equal(api.get(state.sandboxId).state, 'deleted');
    assert.equal(api.get(other.state.sandboxId).state, 'running', 'another session is untouched');
    await impl.onSessionEnd(sessionContext('s1'), state, { reason: 'completed' });
  });

  it('finds the sandbox by the session id when eve saved no state', async () => {
    const impl = provider();
    const { state } = await impl.start(sessionContext('s1'), undefined, {});
    await impl.onSessionEnd(sessionContext('s1'), null, { reason: 'failed' });
    assert.equal(api.get(state.sandboxId).state, 'deleted');
  });

  it('succeeds when nothing exists', async () => {
    await provider().onSessionEnd(sessionContext('never-started'), null, { reason: 'expired' });
  });

  it('leaves the template alone', async () => {
    const impl = provider();
    const artifact = await impl.prepare(prepareContext({ workspace: { 'a.txt': 'a' } }));
    const { state } = await impl.start(sessionContext('s1'), undefined, artifact);
    await impl.onSessionEnd(sessionContext('s1'), state, { reason: 'deleted' });
    assert.equal(api.get(artifact.templateId!).state, 'paused');
    assert.ok(existsSync(api.get(artifact.templateId!).root));
  });
});
