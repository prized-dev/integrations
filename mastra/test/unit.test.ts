/** Offline tests: the Prized SDK's Sandbox is replaced by the in-memory fake in fake-sdk.ts. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PrizedError } from '@prized/sdk';
import { RequestContext } from '@mastra/core/request-context';
import { UnsupportedStdinCloseError, Workspace } from '@mastra/core/workspace';

import { FakeSandbox } from './fake-sdk.js';

vi.mock('@prized/sdk', async importOriginal => {
  const real = await importOriginal<typeof import('@prized/sdk')>();
  const { FakeSandbox } = await import('./fake-sdk.js');
  return { ...real, Sandbox: FakeSandbox };
});

const { PrizedSandbox, prizedSandboxProvider, ID_LABEL, DEFAULT_TIMEOUT_MS, KILLED_EXIT_CODE, TIMEOUT_EXIT_CODE } =
  await import('../src/index.js');

const KEY = 'dcp_0000000000000000000000000000';

beforeEach(() => {
  FakeSandbox.reset();
  vi.unstubAllEnvs();
  vi.stubEnv('PRIZED_API_KEY', '');
});

function only<T>(items: T[]): T {
  expect(items).toHaveLength(1);
  return items[0]!;
}

describe('construction', () => {
  it('generates an id, defaults the directory and timeout, and makes no request', () => {
    const sandbox = new PrizedSandbox({ apiKey: KEY });
    expect(sandbox.id).toMatch(/^mastra-/);
    expect(sandbox.status).toBe('pending');
    expect(sandbox.workingDirectory).toBe('/home/prized/work');
    expect(sandbox.provider).toBe('prized');
    expect(sandbox.sandboxId).toBeUndefined();
    expect(FakeSandbox.creates).toHaveLength(0);
    expect(FakeSandbox.lists).toHaveLength(0);
  });

  it('two instances get different ids', () => {
    expect(new PrizedSandbox().id).not.toBe(new PrizedSandbox().id);
  });

  it('reports the sandboxId it was given before start', () => {
    expect(new PrizedSandbox({ sandboxId: 'sbx_0123456789abcdef' }).sandboxId).toBe('sbx_0123456789abcdef');
  });

  it('fails start with a clear message when there is no key', async () => {
    const sandbox = new PrizedSandbox({ id: 'no-key' });
    await expect(sandbox._start()).rejects.toThrow(/Missing Prized API key/);
    expect(sandbox.status).toBe('error');
    // An agent with no key learns it can sign its person up.
    await expect(new PrizedSandbox({ id: 'no-key-2' })._start()).rejects.toThrow('https://prized.dev/auth.md?via=mastra');
  });

  it('reads the key from PRIZED_API_KEY', async () => {
    vi.stubEnv('PRIZED_API_KEY', KEY);
    const sandbox = new PrizedSandbox({ id: 'env-key' });
    await sandbox._start();
    expect(only(FakeSandbox.creates).apiKey).toBe(KEY);
  });
});

describe('start', () => {
  it('creates a labelled sandbox when none exists', async () => {
    const sandbox = new PrizedSandbox({
      id: 'proj-1',
      apiKey: KEY,
      template: 'prized-agent-v1',
      resources: { vcpu: 4, memoryGb: 8 },
      labels: { team: 'a' },
      maxRuntimeMin: 120,
    });
    await expect(sandbox._start()).resolves.toEqual({ outcome: 'created' });
    expect(sandbox.status).toBe('running');
    const create = only(FakeSandbox.creates);
    expect(create).toMatchObject({
      apiKey: KEY,
      template: 'prized-agent-v1',
      resources: { vcpu: 4, memoryGb: 8 },
      labels: { team: 'a', [ID_LABEL]: 'proj-1' },
      autoPauseMin: 10,
      maxRuntimeMin: 120,
    });
    expect(create).not.toHaveProperty('autoDeleteMin');
    expect(only(FakeSandbox.lists).labels).toEqual({ [ID_LABEL]: 'proj-1' });
    expect(sandbox.sandboxId).toMatch(/^sbx_/);
  });

  it('passes autoDeleteMin', async () => {
    await new PrizedSandbox({ id: 'proj-ad', apiKey: KEY, autoDeleteMin: 60 })._start();
    expect(only(FakeSandbox.creates).autoDeleteMin).toBe(60);
  });

  it('a user label cannot replace the mastra-id label', async () => {
    const sandbox = new PrizedSandbox({ id: 'mine', apiKey: KEY, labels: { [ID_LABEL]: 'theirs' } });
    await sandbox._start();
    expect(only(FakeSandbox.creates).labels?.[ID_LABEL]).toBe('mine');
  });

  it('labels a long id with its hash', async () => {
    const id = 'x'.repeat(300);
    const sandbox = new PrizedSandbox({ id, apiKey: KEY });
    await sandbox._start();
    const label = only(FakeSandbox.creates).labels?.[ID_LABEL] ?? '';
    expect(label).toMatch(/^sha256-[0-9a-f]{64}$/);
    expect(sandbox.id).toBe(id);
  });

  it('resumes the paused sandbox created under the same id', async () => {
    const existing = FakeSandbox.seed('paused', { [ID_LABEL]: 'proj-2' });
    FakeSandbox.seed('running', { [ID_LABEL]: 'other' });
    const sandbox = new PrizedSandbox({ id: 'proj-2', apiKey: KEY });
    await expect(sandbox._start()).resolves.toEqual({ outcome: 'connected' });
    expect(FakeSandbox.creates).toHaveLength(0);
    expect(existing.resumes).toBe(1);
    expect(sandbox.sandboxId).toBe(existing.id);
  });

  it('skips sandboxes in error and creates a new one', async () => {
    FakeSandbox.seed('error', { [ID_LABEL]: 'proj-3' });
    const sandbox = new PrizedSandbox({ id: 'proj-3', apiKey: KEY });
    await expect(sandbox._start()).resolves.toEqual({ outcome: 'created' });
  });

  it('attaches to the sandboxId it was given', async () => {
    const existing = FakeSandbox.seed('running');
    const sandbox = new PrizedSandbox({ sandboxId: existing.id, apiKey: KEY });
    await expect(sandbox._start()).resolves.toEqual({ outcome: 'connected' });
    expect(FakeSandbox.connects).toEqual([existing.id]);
    expect(FakeSandbox.lists).toHaveLength(0);
    expect(sandbox.sandboxId).toBe(existing.id);
  });

  it('falls back to creating when the sandboxId is gone', async () => {
    const sandbox = new PrizedSandbox({ id: 'proj-4', sandboxId: 'sbx_00000000deadbeef', apiKey: KEY });
    await expect(sandbox._start()).resolves.toEqual({ outcome: 'created' });
    expect(sandbox.sandboxId).not.toBe('sbx_00000000deadbeef');
  });

  it('a failed create leaves the sandbox in error, and a later start can succeed', async () => {
    FakeSandbox.createErrors.push(new PrizedError('sandbox_quota_exceeded', 'full', { status: 429 }));
    const sandbox = new PrizedSandbox({ id: 'proj-5', apiKey: KEY });
    await expect(sandbox._start()).rejects.toMatchObject({ code: 'sandbox_quota_exceeded' });
    expect(sandbox.status).toBe('error');
    await expect(sandbox._start()).resolves.toEqual({ outcome: 'created' });
  });

  it('an unknown template rejects start', async () => {
    const sandbox = new PrizedSandbox({ apiKey: KEY, template: 'no-such-template' });
    await expect(sandbox._start()).rejects.toMatchObject({ code: 'template_not_found' });
  });

  it('concurrent starts create one sandbox', async () => {
    const sandbox = new PrizedSandbox({ id: 'proj-6', apiKey: KEY });
    await Promise.all([sandbox._start(), sandbox._start(), sandbox._start()]);
    expect(FakeSandbox.creates).toHaveLength(1);
  });
});

describe('stop and destroy', () => {
  it('stop pauses the sandbox and start resumes the same one', async () => {
    const sandbox = new PrizedSandbox({ id: 'life-1', apiKey: KEY });
    await sandbox._start();
    const fake = FakeSandbox.store.get(sandbox.sandboxId!)!;
    await sandbox._stop();
    expect(sandbox.status).toBe('stopped');
    expect(fake.state).toBe('paused');
    await expect(sandbox._start()).resolves.toEqual({ outcome: 'connected' });
    expect(fake.resumes).toBe(1);
    expect(sandbox.sandboxId).toBe(fake.id);
  });

  it('stop kills the background processes this instance started', async () => {
    FakeSandbox.behave = () => ({ hang: true });
    const sandbox = new PrizedSandbox({ id: 'life-2', apiKey: KEY });
    const handle = await sandbox.processes.spawn('sleep 600');
    await sandbox._stop();
    expect(only(FakeSandbox.runs).killedWith).toBe('SIGKILL');
    const result = await handle.wait();
    expect(result.exitCode).toBe(KILLED_EXIT_CODE);
    expect(await sandbox.processes.list()).toEqual([]);
  });

  it('destroy deletes the sandbox', async () => {
    const sandbox = new PrizedSandbox({ id: 'life-3', apiKey: KEY });
    await sandbox._start();
    const id = sandbox.sandboxId!;
    await sandbox._destroy();
    expect(sandbox.status).toBe('destroyed');
    expect(FakeSandbox.store.has(id)).toBe(false);
  });

  it('destroy() on a new instance deletes the sandbox created under the id, without resuming it', async () => {
    // How a later process cleans up a sandbox by id. (The base class's _destroy() skips a
    // never-started instance, so this calls destroy() itself.)
    const existing = FakeSandbox.seed('paused', { [ID_LABEL]: 'life-4' });
    await new PrizedSandbox({ id: 'life-4', apiKey: KEY }).destroy();
    expect(FakeSandbox.store.has(existing.id)).toBe(false);
    expect(existing.resumes).toBe(0);
  });

  it('destroy of a sandbox deleted elsewhere succeeds', async () => {
    const sandbox = new PrizedSandbox({ id: 'life-5', apiKey: KEY });
    await sandbox._start();
    FakeSandbox.store.delete(sandbox.sandboxId!);
    await expect(sandbox._destroy()).resolves.toBeUndefined();
  });

  it('destroy() deletes every sandbox labelled with the id, duplicates and failed ones included', async () => {
    const a = FakeSandbox.seed('running', { [ID_LABEL]: 'life-7' });
    const b = FakeSandbox.seed('error', { [ID_LABEL]: 'life-7' });
    const other = FakeSandbox.seed('running', { [ID_LABEL]: 'life-other' });
    await new PrizedSandbox({ id: 'life-7', apiKey: KEY }).destroy();
    expect([FakeSandbox.store.has(a.id), FakeSandbox.store.has(b.id), FakeSandbox.store.has(other.id)]).toEqual([false, false, true]);
  });

  it('destroy() reports a failure instead of resolving', async () => {
    FakeSandbox.seed('paused', { [ID_LABEL]: 'life-8' });
    FakeSandbox.listError = new PrizedError('network_error', 'unreachable');
    await expect(new PrizedSandbox({ id: 'life-8', apiKey: KEY }).destroy()).rejects.toMatchObject({ code: 'network_error' });
    await expect(new PrizedSandbox({ id: 'life-8' }).destroy()).rejects.toThrow(/Missing Prized API key/);
  });

  it('stop() on a sandbox deleted elsewhere succeeds', async () => {
    const sandbox = new PrizedSandbox({ id: 'life-9', apiKey: KEY });
    await sandbox._start();
    FakeSandbox.store.delete(sandbox.sandboxId!);
    await sandbox._stop();
    expect(sandbox.status).toBe('stopped');
  });

  it('start waits for a sandbox that is still starting', async () => {
    const starting = FakeSandbox.seed('starting', { [ID_LABEL]: 'life-10' });
    starting.nextStates = ['running'];
    const sandbox = new PrizedSandbox({ id: 'life-10', apiKey: KEY });
    await expect(sandbox._start()).resolves.toEqual({ outcome: 'connected' });
    expect(sandbox.sandboxId).toBe(starting.id);
  });

  it('destroy with nothing to delete makes no create', async () => {
    await new PrizedSandbox({ id: 'life-6', apiKey: KEY }).destroy();
    expect(FakeSandbox.creates).toHaveLength(0);
  });
});

describe('executeCommand', () => {
  it('starts the sandbox, quotes args, and runs in the working directory with the default timeout', async () => {
    FakeSandbox.behave = () => ({ stdout: ['hello world\n'] });
    const sandbox = new PrizedSandbox({ id: 'cmd-1', apiKey: KEY });
    const result = await sandbox.executeCommand!('echo', ['hello world', "it's"]);
    expect(result).toMatchObject({ success: true, exitCode: 0, stdout: 'hello world\n' });
    const call = only(FakeSandbox.execs);
    expect(call.command).toBe(`echo 'hello world' 'it'\\''s'`);
    expect(call.options).toMatchObject({ background: true, cwd: '/home/prized/work', timeoutMs: DEFAULT_TIMEOUT_MS });
    expect(sandbox.status).toBe('running');
  });

  it('passes cwd, a per-command timeout and merged env', async () => {
    const sandbox = new PrizedSandbox({ id: 'cmd-2', apiKey: KEY, env: { A: 'sandbox', B: 'kept' } });
    await sandbox.executeCommand!('env', [], { cwd: '/tmp', timeout: 5000, env: { A: 'call', C: 'added' } });
    expect(only(FakeSandbox.execs).options).toMatchObject({
      cwd: '/tmp',
      timeoutMs: 5000,
      env: { A: 'call', B: 'kept', C: 'added' },
    });
  });

  it('setEnv reaches the next command', async () => {
    const sandbox = new PrizedSandbox({ id: 'cmd-3', apiKey: KEY });
    sandbox.setEnv(env => ({ ...env, TOKEN: 't1' }));
    await sandbox.executeCommand!('true');
    expect(only(FakeSandbox.execs).options.env).toEqual({ TOKEN: 't1' });
  });

  it('a timeout of 0 means no limit, and a configured default applies', async () => {
    const none = new PrizedSandbox({ id: 'cmd-4', apiKey: KEY, timeout: 0 });
    await none.executeCommand!('true');
    expect(FakeSandbox.execs.at(-1)!.options.timeoutMs).toBeUndefined();
    const short = new PrizedSandbox({ id: 'cmd-5', apiKey: KEY, timeout: 1000 });
    await short.executeCommand!('true');
    expect(FakeSandbox.execs.at(-1)!.options.timeoutMs).toBe(1000);
  });

  it('streams output and keeps stderr and the exit code', async () => {
    FakeSandbox.behave = () => ({ stdout: ['a\n', 'b\n'], stderr: ['oops\n'], exitCode: 42 });
    const sandbox = new PrizedSandbox({ id: 'cmd-6', apiKey: KEY });
    const chunks: string[] = [];
    const result = await sandbox.executeCommand!('x', [], { onStdout: c => chunks.push(c) });
    expect(chunks).toEqual(['a\n', 'b\n']);
    expect(result).toMatchObject({ success: false, exitCode: 42, stdout: 'a\nb\n', stderr: 'oops\n' });
  });

  it('reports a timed-out command with exit code 124 and timedOut', async () => {
    FakeSandbox.behave = () => ({ stdout: ['partial\n'], hang: true });
    const sandbox = new PrizedSandbox({ id: 'cmd-7', apiKey: KEY });
    const result = await sandbox.executeCommand!('sleep', ['10'], { timeout: 20 });
    expect(result).toMatchObject({ success: false, exitCode: TIMEOUT_EXIT_CODE, timedOut: true, stdout: 'partial\n' });
  });

  it('an aborted command is killed', async () => {
    FakeSandbox.behave = () => ({ hang: true });
    const sandbox = new PrizedSandbox({ id: 'cmd-8', apiKey: KEY });
    const controller = new AbortController();
    controller.abort();
    const result = await sandbox.executeCommand!('sleep', ['60'], { abortSignal: controller.signal });
    expect(result.success).toBe(false);
    expect(only(FakeSandbox.runs).killedWith).toBe('SIGKILL');
  });

  it('starts a new sandbox under the same id when the old one is gone, and runs the command once there', async () => {
    const sandbox = new PrizedSandbox({ id: 'cmd-9', apiKey: KEY });
    await sandbox._start();
    const first = sandbox.sandboxId!;
    FakeSandbox.store.delete(first); // deleted outside this process
    FakeSandbox.behave = () => ({ stdout: ['recovered\n'] });
    const result = await sandbox.executeCommand!('echo', ['recovered']);
    expect(result.stdout).toBe('recovered\n');
    expect(sandbox.sandboxId).not.toBe(first);
    expect(FakeSandbox.creates).toHaveLength(2);
    expect(only(FakeSandbox.execs).sandboxId).toBe(sandbox.sandboxId);
    expect(sandbox.status).toBe('running');
  });

  it('treats a sandbox being deleted as gone', async () => {
    const sandbox = new PrizedSandbox({ id: 'cmd-11', apiKey: KEY });
    await sandbox._start();
    const old = FakeSandbox.store.get(sandbox.sandboxId!)!;
    old.state = 'deleting';
    const result = await sandbox.executeCommand!('true');
    expect(result.success).toBe(true);
    expect(sandbox.sandboxId).not.toBe(old.id);
  });

  it('concurrent commands on a deleted sandbox share one new sandbox', async () => {
    const sandbox = new PrizedSandbox({ id: 'cmd-12', apiKey: KEY });
    await sandbox._start();
    FakeSandbox.store.delete(sandbox.sandboxId!);
    const results = await Promise.all([1, 2, 3].map(() => sandbox.executeCommand!('true')));
    expect(results.every(r => r.success)).toBe(true);
    expect(FakeSandbox.creates).toHaveLength(2);
  });

  it('never sends the API key into the sandbox', async () => {
    vi.stubEnv('PRIZED_API_KEY', KEY);
    const sandbox = new PrizedSandbox({ id: 'cmd-13', env: { A: '1' } });
    await sandbox.executeCommand!('env');
    await sandbox.processes.spawn('env');
    await sandbox.writeFiles([{ path: '/home/prized/work/x', content: 'x', mode: 0o600 }]);
    expect(JSON.stringify([FakeSandbox.execs, FakeSandbox.writes])).not.toContain(KEY);
    expect(FakeSandbox.creates[0]).not.toHaveProperty('envVars');
  });

  it('does not start a new sandbox for a call that fails after destroy', async () => {
    const sandbox = new PrizedSandbox({ id: 'cmd-14', apiKey: KEY });
    await sandbox._start();
    await sandbox._destroy();
    const gone = new PrizedError('not_found', 'gone', { status: 404 });
    await expect(sandbox.retryOnDead(() => Promise.reject(gone))).rejects.toBe(gone);
    expect(FakeSandbox.creates).toHaveLength(1);
    expect(sandbox.status).toBe('destroyed');
  });

  it('does not retry other errors', async () => {
    FakeSandbox.behave = () => ({ startError: new PrizedError('rate_limited', 'slow down', { status: 429 }) });
    const sandbox = new PrizedSandbox({ id: 'cmd-10', apiKey: KEY });
    await expect(sandbox.executeCommand!('true')).rejects.toMatchObject({ code: 'rate_limited' });
    expect(FakeSandbox.creates).toHaveLength(1);
  });
});

describe('processes', () => {
  it('tracks, reads, lists and kills a background process', async () => {
    FakeSandbox.behave = () => ({ stdout: ['start', 'ed\n'], hang: true });
    const sandbox = new PrizedSandbox({ id: 'proc-1', apiKey: KEY });
    const handle = await sandbox.processes.spawn('node server.js');
    expect(handle.pid).toMatch(/^run_/);
    expect(only(FakeSandbox.execs).options.timeoutMs).toBeUndefined(); // background spawns have no default limit
    const got = await sandbox.processes.get(handle.pid);
    expect(got).toBe(handle);
    expect(handle.stdout).toBe('started\n');
    expect(await sandbox.processes.list()).toEqual([{ pid: handle.pid, command: 'node server.js', running: true }]);
    expect(await sandbox.processes.kill(handle.pid)).toBe(true);
    expect(await sandbox.processes.get(handle.pid)).toBeUndefined();
    const result = await handle.wait();
    expect(result).toMatchObject({ killed: true, exitCode: KILLED_EXIT_CODE, stdout: 'started\n' }); // replayed output not doubled
  });

  it('does not read a process nobody follows, so the sandbox can idle', async () => {
    FakeSandbox.behave = () => ({ stdout: ['tick\n'], hang: true });
    const sandbox = new PrizedSandbox({ id: 'proc-lazy', apiKey: KEY });
    await sandbox.processes.spawn('dev-server');
    await new Promise(resolve => setTimeout(resolve, 20));
    const run = only(FakeSandbox.runs);
    expect([run.waits, run.statusCalls, run.logsCalls]).toEqual([0, 0, 0]);
  });

  it('follows a process whose output was asked for at spawn', async () => {
    FakeSandbox.behave = () => ({ stdout: ['a\n'], hang: true });
    const sandbox = new PrizedSandbox({ id: 'proc-follow', apiKey: KEY });
    const chunks: string[] = [];
    const handle = await sandbox.processes.spawn('server', { onStdout: c => chunks.push(c) });
    await vi.waitFor(() => expect(chunks).toEqual(['a\n']));
    expect(only(FakeSandbox.runs).waits).toBe(1);
    expect(handle.exitCode).toBeUndefined();
    await handle.kill();
    expect((await handle.wait()).exitCode).toBe(KILLED_EXIT_CODE);
  });

  it('get() settles a process that exited while nobody followed it', async () => {
    FakeSandbox.behave = () => ({ stdout: ['done\n'], hang: true });
    const sandbox = new PrizedSandbox({ id: 'proc-exit', apiKey: KEY });
    const handle = await sandbox.processes.spawn('job');
    only(FakeSandbox.runs).exit();
    const got = await sandbox.processes.get(handle.pid);
    expect(got?.exitCode).toBe(0);
    expect(got?.stdout).toBe('done\n');
    expect(await sandbox.processes.get(handle.pid)).toBeUndefined(); // read once after exit, then pruned
  });

  it('a background process spawned with an abort signal is not followed, and the abort kills it', async () => {
    FakeSandbox.behave = () => ({ hang: true });
    const sandbox = new PrizedSandbox({ id: 'proc-abort', apiKey: KEY });
    const controller = new AbortController();
    const handle = await sandbox.processes.spawn('dev-server', { abortSignal: controller.signal });
    await new Promise(resolve => setTimeout(resolve, 20));
    const run = only(FakeSandbox.runs);
    expect(run.waits).toBe(0);
    controller.abort();
    await vi.waitFor(() => expect(run.killedWith).toBe('SIGKILL'));
    expect((await handle.wait()).exitCode).toBe(KILLED_EXIT_CODE);
    expect(handle.killedByAbort).toBe(true);
  });

  it('list() still answers after the sandbox was deleted elsewhere, and settles the old process', async () => {
    FakeSandbox.behave = () => ({ hang: true });
    const sandbox = new PrizedSandbox({ id: 'proc-gone', apiKey: KEY });
    const handle = await sandbox.processes.spawn('sleep 600');
    FakeSandbox.store.delete(sandbox.sandboxId!);
    const listed = await sandbox.processes.list();
    expect(listed).toEqual([{ pid: handle.pid, command: 'sleep 600', running: false, exitCode: 1 }]);
  });

  it('get() passes on an error other than gone', async () => {
    FakeSandbox.behave = () => ({ hang: true, statusError: new PrizedError('rate_limited', 'slow down', { status: 429 }) });
    const sandbox = new PrizedSandbox({ id: 'proc-get-err', apiKey: KEY });
    const handle = await sandbox.processes.spawn('sleep 600');
    await expect(sandbox.processes.get(handle.pid)).rejects.toMatchObject({ code: 'rate_limited' });
    expect(handle.exitCode).toBeUndefined();
  });

  it('stop() reports the real exit code of a process that exited unfollowed', async () => {
    FakeSandbox.behave = () => ({ hang: true, exitCode: 0 });
    const sandbox = new PrizedSandbox({ id: 'proc-stop-exit', apiKey: KEY });
    const handle = await sandbox.processes.spawn('job');
    only(FakeSandbox.runs).exit();
    await sandbox._stop();
    expect((await handle.wait()).exitCode).toBe(0);
  });

  it('a failed kill throws and leaves the process running; a gone one returns false', async () => {
    FakeSandbox.behave = () => ({ hang: true, killError: new PrizedError('rate_limited', 'slow down', { status: 429 }) });
    const sandbox = new PrizedSandbox({ id: 'proc-killfail', apiKey: KEY });
    const handle = await sandbox.processes.spawn('sleep 600');
    await expect(handle.kill()).rejects.toMatchObject({ code: 'rate_limited' });
    expect(handle.exitCode).toBeUndefined();

    FakeSandbox.behave = () => ({ hang: true, killError: new PrizedError('not_found', 'gone', { status: 404 }) });
    const gone = await sandbox.processes.spawn('sleep 600');
    expect(await gone.kill()).toBe(false);
    expect(gone.exitCode).toBe(1);
  });

  it('kill after exit returns false and keeps the real exit code', async () => {
    FakeSandbox.behave = () => ({ exitCode: 3 });
    const sandbox = new PrizedSandbox({ id: 'proc-2', apiKey: KEY });
    const handle = await sandbox.processes.spawn('exit 3');
    expect((await handle.wait()).exitCode).toBe(3);
    expect(await handle.kill()).toBe(false);
    expect(handle.exitCode).toBe(3);
  });

  it('wait is idempotent', async () => {
    FakeSandbox.behave = () => ({ stdout: ['x\n'] });
    const sandbox = new PrizedSandbox({ id: 'proc-3', apiKey: KEY });
    const handle = await sandbox.processes.spawn('echo x');
    const [a, b] = [await handle.wait(), await handle.wait()];
    expect(a.stdout).toBe(b.stdout);
    expect(a.exitCode).toBe(0);
  });

  it('has no stdin', async () => {
    const sandbox = new PrizedSandbox({ id: 'proc-4', apiKey: KEY });
    const handle = await sandbox.processes.spawn('cat');
    await expect(handle.sendStdin('x')).rejects.toThrow(/no stdin/);
    await expect(handle.closeStdin()).rejects.toBeInstanceOf(UnsupportedStdinCloseError);
  });

  it('reports a run that could not be followed as failed, with the reason', async () => {
    const sandbox = new PrizedSandbox({ id: 'proc-5', apiKey: KEY });
    FakeSandbox.behave = () => ({ hang: true });
    const handle = await sandbox.processes.spawn('sleep 600');
    // The handle stops following without a kill: wait still settles.
    (handle as unknown as { detach(): void }).detach();
    const result = await handle.wait();
    expect(result.success).toBe(false);
    expect(result.stderr).toMatch(/stopped following/);
  });
});

describe('writeFiles', () => {
  it('writes through the file API and applies modes with one chmod', async () => {
    const sandbox = new PrizedSandbox({ id: 'files-1', apiKey: KEY });
    await sandbox.writeFiles([
      { path: '/home/prized/work/a.sh', content: 'echo a', mode: 0o755 },
      { path: '/home/prized/work/b.bin', content: Buffer.from([1, 2, 3]) },
    ]);
    expect(FakeSandbox.writes.map(w => w.path)).toEqual(['/home/prized/work/a.sh', '/home/prized/work/b.bin']);
    expect(FakeSandbox.writes[1]!.data).toBeInstanceOf(Uint8Array);
    expect(only(FakeSandbox.execs).command).toBe(`chmod 755 -- '/home/prized/work/a.sh'`);
  });

  it('rejects an invalid mode before writing anything', async () => {
    const sandbox = new PrizedSandbox({ id: 'files-2', apiKey: KEY });
    await expect(sandbox.writeFiles([{ path: '/home/prized/x', content: 'x', mode: 0 }])).rejects.toThrow();
    expect(FakeSandbox.writes).toHaveLength(0);
    expect(FakeSandbox.creates).toHaveLength(0);
  });
});

describe('clone, info and instructions', () => {
  it('clone keeps the settings, applies overrides, and makes no request', () => {
    const base = new PrizedSandbox({ id: 'base', apiKey: KEY, template: 'prized-agent-v1', env: { A: '1' }, timeout: 9000 });
    const copy = base.clone({ id: 'copy', env: { B: '2' }, idleTimeoutMinutes: 3, workingDirectory: '/tmp' });
    expect(copy).toBeInstanceOf(PrizedSandbox);
    expect(copy.id).toBe('copy');
    expect(copy.getEnv()).toEqual({ B: '2' });
    expect(copy.workingDirectory).toBe('/tmp');
    expect(copy.getInstructions()).toContain('9s');
    expect(FakeSandbox.creates).toHaveLength(0);
  });

  it('clone starts its own sandbox with the base template and idle timeout', async () => {
    const base = new PrizedSandbox({ id: 'base2', apiKey: KEY, template: 'prized-agent-v1' });
    await base.clone({ id: 'copy2', idleTimeoutMinutes: 3 })._start();
    expect(only(FakeSandbox.creates)).toMatchObject({
      template: 'prized-agent-v1',
      autoPauseMin: 3,
      labels: { [ID_LABEL]: 'copy2' },
    });
  });

  it('getInfo reports status, the Prized id and the shape', async () => {
    const sandbox = new PrizedSandbox({ id: 'info-1', apiKey: KEY });
    await sandbox._start();
    const info = await sandbox.getInfo();
    expect(info).toMatchObject({
      id: 'info-1',
      name: 'PrizedSandbox',
      provider: 'prized',
      status: 'running',
      resources: { cpuCores: 2, memoryMB: 8192, diskMB: 10240 },
      metadata: { sandboxId: sandbox.sandboxId, state: 'running', template: 'prized-base-v1' },
    });
    expect(info.timeoutAt).toBeInstanceOf(Date);
  });

  it('instructions describe the sandbox and can be replaced or extended', () => {
    expect(new PrizedSandbox().getInstructions()).toMatch(/user `prized`.*no sudo/);
    expect(new PrizedSandbox({ instructions: 'custom' }).getInstructions()).toBe('custom');
    const extended = new PrizedSandbox({ instructions: ({ defaultInstructions }) => `${defaultInstructions} More.` });
    expect(extended.getInstructions()).toMatch(/Prized sandbox.* More\.$/);
  });
});

describe('Workspace', () => {
  it('a workspace runs commands in the sandbox and destroys it', async () => {
    FakeSandbox.behave = () => ({ stdout: ['ok\n'] });
    const sandbox = new PrizedSandbox({ id: 'ws-1', apiKey: KEY });
    const workspace = new Workspace({ sandbox });
    await workspace.init();
    expect(sandbox.status).toBe('running');
    const result = await workspace.sandbox!.executeCommand!('echo', ['ok']);
    expect(result.stdout).toBe('ok\n');
    const id = sandbox.sandboxId!;
    await workspace.destroy();
    expect(FakeSandbox.store.has(id)).toBe(false);
  });
});

describe('a sandbox per thread (resolver)', () => {
  it('resolves one sandbox per thread, reuses it, and a later instance destroys it by id', async () => {
    const workspace = new Workspace({
      sandbox: async ({ requestContext }) => {
        const sandbox = new PrizedSandbox({ id: `thread-${requestContext.get('thread')}`, apiKey: KEY });
        await sandbox.start();
        return sandbox;
      },
      sandboxCacheKey: ({ requestContext }) => requestContext.get('thread') as string,
    });
    const context = (thread: string) => {
      const rc = new RequestContext();
      rc.set('thread', thread);
      return rc;
    };
    const a1 = await workspace.resolveSandbox({ requestContext: context('a') });
    const a2 = await workspace.resolveSandbox({ requestContext: context('a') });
    const b = await workspace.resolveSandbox({ requestContext: context('b') });
    expect(a2).toBe(a1);
    expect(b).not.toBe(a1);
    expect(FakeSandbox.creates.map(c => c.labels?.[ID_LABEL])).toEqual(['thread-a', 'thread-b']);

    // The thread ends: delete its sandbox, from this process or any other.
    await new PrizedSandbox({ id: 'thread-a', apiKey: KEY }).destroy();
    workspace.clearSandboxCache('a');
    expect(FakeSandbox.store.has((a1 as InstanceType<typeof PrizedSandbox>).sandboxId!)).toBe(false);
  });
});

describe('editor provider', () => {
  it('takes no API key in stored config and passes only known fields', async () => {
    expect(Object.keys((prizedSandboxProvider.configSchema as { properties: object }).properties)).not.toContain('apiKey');
    vi.stubEnv('PRIZED_API_KEY', KEY);
    const sandbox = (await prizedSandboxProvider.createSandbox({
      id: 'ed-1',
      template: 'prized-agent-v1',
      vcpu: 4,
      env: { A: '1' },
      apiKey: 'dcp_stored',
      baseUrl: 'https://elsewhere.example',
    } as never)) as InstanceType<typeof PrizedSandbox>;
    await sandbox._start();
    const create = only(FakeSandbox.creates);
    expect(create.apiKey).toBe(KEY);
    expect(create).toMatchObject({ template: 'prized-agent-v1', resources: { vcpu: 4 } });
    expect(create).not.toHaveProperty('baseUrl');
  });
});
