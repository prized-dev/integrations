/**
 * Live test: the real Pi (the devDependency), this extension, the real Prized API. The model is a script
 * (a small OpenAI-compatible server below), so the test needs no model key: what it checks is Pi, the
 * extension and Prized, not a model's judgment.
 *
 *   PRIZED_API_KEY=dcp_... npm run test:live      (LIVE_DEBUG=1 also prints every event Pi emits)
 *
 * It creates up to two 1 vCPU sandboxes in your workspace, labelled created-by=pi-prized, and deletes
 * them at the end (failures included). Needs git and Node on this machine; python3 runs in the sandbox.
 */
import { type ChildProcess, execFileSync, spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { type IncomingMessage, type ServerResponse, createServer } from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { Sandbox } from '@prized/sdk';

const PKG = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const apiKey = process.env.PRIZED_API_KEY;
if (!apiKey) {
  console.error('Set PRIZED_API_KEY (a key for a workspace you do not mind creating two small sandboxes in).');
  process.exit(2);
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const t0 = Date.now();
const log = (...a: unknown[]) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1).padStart(6)}s]`, ...a);
let failures = 0;
const check = (name: string, ok: boolean, evidence = '') => {
  if (!ok) failures += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${evidence ? `  | ${evidence}` : ''}`);
};

// ---- the scripted model: the tag in the last user message picks a scenario, the tool results so far pick the step
type Step = ['text', string] | [string, Record<string, unknown>];
const COUNTER = `cat > counter.py <<'EOF'
import time
n = 0
while True:
    n += 1
    print(n, time.strftime('%H:%M:%S'), flush=True)
    time.sleep(1)
EOF
nohup python3 counter.py > counter.log 2>&1 &
echo started pid $!`;
const SCRIPTS: Record<string, Step[]> = {
  fix: [
    ['bash', { command: 'python3 test_calc.py 2>&1 | tail -3' }],
    ['read', { path: 'calc.py' }],
    ['edit', { path: 'calc.py', edits: [{ oldText: 'return a - b', newText: 'return a + b' }] }],
    ['bash', { command: 'python3 test_calc.py' }],
    ['text', 'Fixed add(): it subtracted instead of adding.'],
  ],
  counter: [['bash', { command: COUNTER }], ['text', 'Started a counter.']],
  check: [
    ['bash', { command: "echo pid=$(pgrep -f 'python3 counter.py' | head -1); tail -2 counter.log; wc -l < counter.log" }],
    ['text', 'Checked.'],
  ],
  write: [
    ['write', { path: 'notes/from-fork.txt', content: 'written after the fork\n' }],
    ['bash', { command: "echo pid=$(pgrep -f 'python3 counter.py' | head -1)" }],
    ['text', 'Wrote a note.'],
  ],
  stop: [
    ['bash', { command: "echo pid=$(pgrep -f 'python3 counter.py' | head -1); kill $(pgrep -f 'python3 counter.py' | head -1); rm -f counter.log counter.py; ls" }],
    ['text', 'Stopped the counter.'],
  ],
  slow: [['bash', { command: 'sleep 4; touch /home/prized/work/proj/too-late', timeout: 2 }], ['text', 'It timed out.']],
  hang: [['bash', { command: 'sleep 4; touch /home/prized/work/proj/aborted-late' }], ['text', 'unreachable']],
};
const textOf = (c: unknown): string =>
  typeof c === 'string' ? c : Array.isArray(c) ? c.map((p: { text?: string }) => p.text ?? '').join('') : '';

function pick(messages: Array<{ role: string; content: unknown }>): { step: Step; name: string; n: number } {
  const last = messages.map((m) => m.role).lastIndexOf('user');
  const tag = last >= 0 ? textOf(messages[last]?.content) : '';
  const n = messages.slice(last + 1).filter((m) => m.role === 'tool').length;
  for (const [name, script] of Object.entries(SCRIPTS)) {
    if (tag.includes(`[${name}]`)) return { step: script[Math.min(n, script.length - 1)] as Step, name, n };
  }
  return { step: ['text', 'No scripted scenario for this message.'], name: 'none', n };
}

function model(req: IncomingMessage, res: ServerResponse): void {
  let raw = '';
  req.on('data', (d) => (raw += d));
  req.on('end', () => {
    const { step, name, n } = pick(JSON.parse(raw).messages ?? []);
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    const chunk = (delta: object, finish: string | null = null) =>
      res.write(`data: ${JSON.stringify({ id: 'c', object: 'chat.completion.chunk', created: 0, model: 'scripted-1', choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`);
    chunk({ role: 'assistant', content: '' });
    if (step[0] === 'text') {
      chunk({ content: step[1] });
      chunk({}, 'stop');
    } else {
      chunk({ tool_calls: [{ index: 0, id: `call_${name}_${n}`, type: 'function', function: { name: step[0], arguments: JSON.stringify(step[1]) } }] });
      chunk({}, 'tool_calls');
    }
    res.write(`data: ${JSON.stringify({ id: 'c', object: 'chat.completion.chunk', created: 0, model: 'scripted-1', choices: [], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } })}\n\n`);
    res.end('data: [DONE]\n\n');
  });
}

// ---- driving Pi in RPC mode
interface Ev { type: string; [k: string]: any; _t: number }
function startPi(args: string[], env: NodeJS.ProcessEnv, cwd: string) {
  const pi: ChildProcess = spawn(path.join(PKG, 'node_modules', '.bin', 'pi'), ['--mode', 'rpc', '--offline', '-e', path.join(PKG, 'index.ts'), '--provider', 'scripted', '--model', 'scripted-1', ...args], { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] });
  const events: Ev[] = [];
  const waiters: Array<{ pred: (e: Ev) => boolean; resolve: (e: Ev) => void }> = [];
  const pending = new Map<string, (r: Ev) => void>();
  let buf = '';
  let seq = 1;
  const send = (cmd: Record<string, unknown>): Promise<Ev> => {
    const id = cmd.type === 'extension_ui_response' ? undefined : `c${seq++}`;
    pi.stdin?.write(`${JSON.stringify(id ? { id, ...cmd } : cmd)}\n`);
    return id ? new Promise((resolve) => pending.set(id, resolve)) : Promise.resolve({} as Ev);
  };
  pi.stderr?.on('data', (d) => process.stderr.write(`[pi stderr] ${d}`));
  pi.stdout?.on('data', (d) => {
    buf += d;
    for (let i = buf.indexOf('\n'); i >= 0; i = buf.indexOf('\n')) {
      const line = buf.slice(0, i).replace(/\r$/, '');
      buf = buf.slice(i + 1);
      if (!line.trim()) continue;
      const ev: Ev = { ...JSON.parse(line), _t: Date.now() };
      events.push(ev);
      if (process.env.LIVE_DEBUG) log('event', ev.type, ev.type === 'extension_ui_request' ? ev.method : '', ev.toolName ?? '', ev.aborted ?? '');
      if (ev.type === 'response' && pending.has(ev.id)) pending.get(ev.id)?.(ev);
      if (ev.type === 'extension_ui_request' && ev.method === 'confirm') {
        log('confirm:', ev.title, '|', String(ev.message).split('\n')[0]);
        void send({ type: 'extension_ui_response', id: ev.id, confirmed: true });
      }
      if (ev.type === 'extension_ui_request' && ev.method === 'notify') log('notify:', String(ev.message).replace(/\n/g, ' | '));
      for (const w of [...waiters]) if (w.pred(ev)) { waiters.splice(waiters.indexOf(w), 1); w.resolve(ev); }
    }
  });
  const exited = new Promise<number | null>((r) => pi.on('exit', (code) => r(code)));
  const waitFor = (pred: (e: Ev) => boolean, ms = 120_000, label = 'event') =>
    new Promise<Ev>((resolve, reject) => {
      const seen = events.find(pred); // it may have come already
      if (seen) return resolve(seen);
      const w = { pred, resolve };
      waiters.push(w);
      setTimeout(() => { if (waiters.includes(w)) { waiters.splice(waiters.indexOf(w), 1); reject(new Error(`timed out waiting for ${label}`)); } }, ms);
    });
  const notices = () => events.filter((e) => e.type === 'extension_ui_request' && e.method === 'notify').map((e) => String(e.message));
  async function turn(text: string) {
    const from = events.length;
    log('prompt:', text);
    const res = await send({ type: 'prompt', message: text });
    if (!res.success) throw new Error(`prompt failed: ${JSON.stringify(res)}`);
    if (res.data?.disposition === 'handled') { await sleep(1500); return { results: [], settledAt: Date.now(), firstToolAt: 0 }; }
    const settled = await waitFor((e) => events.indexOf(e) >= from && e.type === 'agent_settled', 120_000, 'agent_settled');
    const results = events.slice(from).filter((e) => e.type === 'tool_execution_end').map((e) => ({
      name: String(e.toolName), at: e._t, text: ((e.result?.content ?? []) as Array<{ text?: string }>).map((c) => c.text ?? '').join('').trim(),
    }));
    for (const r of results) log(`  ${r.name}:`, r.text.slice(0, 120).replace(/\n/g, ' | '));
    return { results, settledAt: settled._t, firstToolAt: results[0]?.at ?? 0 };
  }
  return { pi, events, send, waitFor, notices, turn, quit: () => { pi.stdin?.end(); return exited; } };
}

// ---- the run
const root = mkdtempSync(path.join(tmpdir(), 'prized-pi-live-'));
const proj = path.join(root, 'proj');
const agentDir = path.join(root, 'agent');
const home = path.join(root, 'home');
for (const d of [proj, agentDir, home]) mkdirSync(d, { recursive: true });
const git = (...args: string[]) => execFileSync('git', args, { cwd: proj, encoding: 'utf8' });
git('init', '-q', '-b', 'main');
git('config', 'user.name', 'Live Test');
git('config', 'user.email', 'live@example.com');
writeFileSync(path.join(proj, 'calc.py'), 'def add(a, b):\n    return a - b\n');
writeFileSync(path.join(proj, 'test_calc.py'), 'from calc import add\nassert add(2, 3) == 5, "add is broken"\nprint("all good")\n');
writeFileSync(path.join(proj, '.gitignore'), '__pycache__/\n.env\n');
writeFileSync(path.join(proj, '.env'), 'TOPSECRET=1\n');
git('add', '-A');
git('commit', '-q', '-m', 'init');

const server = createServer(model);
await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
const port = (server.address() as { port: number }).port;
writeFileSync(
  path.join(agentDir, 'models.json'),
  JSON.stringify({ providers: { scripted: { baseUrl: `http://127.0.0.1:${port}/v1`, api: 'openai-completions', apiKey: 'none', models: [{ id: 'scripted-1', name: 'Scripted', contextWindow: 32000, maxTokens: 4000, reasoning: false, input: ['text'] }] } } }),
);
const env: NodeJS.ProcessEnv = { PATH: process.env.PATH, HOME: home, PI_CODING_AGENT_DIR: agentDir, PRIZED_API_KEY: apiKey, PI_SKIP_VERSION_CHECK: '1' };
const FLAGS = ['--prized', '--prized-vcpu', '1', '--prized-memory', '1', '--prized-pause', '4', '--prized-idle', '5'];
const ids: string[] = [];
const sessions: string[] = [];
const idFrom = (notices: string[], re: RegExp) => notices.map((n) => n.match(re)).filter(Boolean).at(-1)?.[1];
const stateOf = (id: string) => Sandbox.connect(id, { apiKey });
async function untilState(id: string, want: string, ms = 60_000): Promise<boolean> {
  const t = Date.now();
  while (Date.now() - t < ms) {
    if ((await stateOf(id)).state === want) return true;
    await sleep(500);
  }
  return false;
}
const alive = async (id: string) => {
  try { return !['deleted', 'deleting'].includes((await stateOf(id)).state); } catch (e) { return !['not_found', 'sandbox_deleted'].includes((e as { code?: string }).code ?? ''); }
};
let current: ReturnType<typeof startPi> | undefined;

try {
  let s = (current = startPi(FLAGS, env, proj));
  const fix = await s.turn('[fix] test_calc.py fails; fix calc.py');
  check('tool calls ran in order: bash,read,edit,bash', fix.results.map((r) => r.name).join(',') === 'bash,read,edit,bash');
  check('the test failed, then passed, in the sandbox', /add is broken/.test(fix.results[0]?.text ?? '') && /all good/.test(fix.results[3]?.text ?? ''));
  check('your checkout is untouched until you pull', readFileSync(path.join(proj, 'calc.py'), 'utf8').includes('a - b'));
  const B = idFrom(s.notices(), /Prized sandbox (sbx_\w+) ready/) ?? '';
  if (B) ids.push(B);
  check('a sandbox was created and announced', !!B, B);
  const sb = await stateOf(B);
  sessions.push(sb.labels['pi-session'] ?? '');
  check('size per create: 1 vCPU, 1 GB', sb.resources.vcpu === 1 && sb.resources.memoryMb === 1024, JSON.stringify(sb.resources));
  check('labelled, with auto-pause 5 min and auto-delete 7 days', sb.labels['created-by'] === 'pi-prized' && sb.info.autoPauseMin === 5 && sb.info.autoDeleteMin === 10080);
  const files = (await sb.exec('cd ~/work/proj && ls -a')).stdout.split('\n');
  check('the project went up and the ignored .env stayed home', files.includes('calc.py') && !files.includes('.env'), files.filter(Boolean).join(' '));

  const counter = await s.turn('[counter] start a background counter');
  const pid = counter.results[0]?.text.match(/started pid (\d+)/)?.[1];
  check('a background process started and the turn returned at once', !!pid, `pid ${pid}`);
  check('the sandbox paused by itself after the turn (--prized-pause 4)', await untilState(B, 'paused'));
  const pausedAt = Date.now();
  await sleep(10_000);
  const chk = await s.turn('[check] how is the counter doing?');
  const out = chk.results[0]?.text ?? '';
  // The counter prints "<n> <hh:mm:ss>" every second. If it was frozen, line n+1 is stamped after the pause, not a second after line n.
  const [, a = '', b = ''] = out.split('\n');
  const [n1, t1 = '0:0:0'] = a.split(' ');
  const [n2, t2 = '0:0:0'] = b.split(' ');
  const secs = (t: string) => t.split(':').reduce((acc, v) => acc * 60 + Number(v), 0);
  const gap = (secs(t2) - secs(t1) + 86400) % 86400;
  const paused = (Date.now() - pausedAt) / 1000;
  check('the process kept its pid across the pause', out.includes(`pid=${pid}`));
  check('and did not tick while paused (consecutive counts, a pause-sized gap between them)', Number(n2) === Number(n1) + 1 && gap >= paused - 4, `line ${n1} at ${t1}, line ${n2} at ${t2}: ${gap}s apart after ${paused.toFixed(0)}s of pause`);
  check('the next turn resumed the sandbox', (await stateOf(B)).state === 'running');

  await s.turn('/prized pull');
  await sleep(1500);
  check('/prized pull brought the fix home', readFileSync(path.join(proj, 'calc.py'), 'utf8').includes('a + b'));
  check('.env was never touched', readFileSync(path.join(proj, '.env'), 'utf8') === 'TOPSECRET=1\n');

  // A timeout and an abort reach the command in the sandbox (the SDK stops it and the process group), as Pi expects.
  const slow = await s.turn('[slow] a command with a timeout');
  check("Pi's tool timeout is reported as Pi reports it", /timed out after 2 seconds/i.test(slow.results[0]?.text ?? ''), slow.results[0]?.text.slice(0, 80));
  const from = s.events.length;
  const hangTurn = s.send({ type: 'prompt', message: '[hang] a command I will abort' });
  await s.waitFor((e) => s.events.indexOf(e) >= from && e.type === 'tool_execution_start', 60_000, 'the hanging command to start');
  const aborted = Date.now();
  await s.send({ type: 'abort' });
  await hangTurn;
  await s.waitFor((e) => s.events.indexOf(e) >= from && e.type === 'agent_settled', 60_000, 'the aborted run to settle');
  check('an abort settles the turn at once', Date.now() - aborted < 15_000, `${Date.now() - aborted}ms`);
  await sleep(6000);
  const late = await sb.exec('ls ~/work/proj | grep -c -E "too-late|aborted-late" || true');
  check('and both commands were stopped in the sandbox, not left to finish', late.stdout.trim() === '0', `markers: ${late.stdout.trim()}`);

  check('Pi cloned the session', Boolean((await s.send({ type: 'clone' })).success));
  const w = await s.turn('[write] write a note in the fork');
  const F = idFrom(s.notices(), /Prized sandbox (sbx_\w+) forked from/) ?? '';
  if (F) ids.push(F);
  check('the clone got its own sandbox by forking the first', !!F && F !== B, `${B} -> ${F}`);
  check('the parent was paused when the session moved on', (await stateOf(B)).state === 'paused');
  const fsb = await stateOf(F);
  sessions.push(fsb.labels['pi-session'] ?? '');
  check("the fork has the parent's running counter (same pid)", w.results[1]?.text.includes(`pid=${pid}`) === true);
  check('the note exists only in the fork', (await fsb.exec('cat ~/work/proj/notes/from-fork.txt')).stdout.trim() === 'written after the fork' && (await (await stateOf(B)).exec('ls ~/work/proj/notes 2>&1 | head -1')).stdout.includes('No such file'));

  await s.quit();
  await sleep(1500);
  check('quitting with an unpulled note keeps the fork, paused', (await stateOf(F)).state === 'paused');

  s = current = startPi([...FLAGS, '--continue'], env, proj);
  const stop = await s.turn('[stop] stop the counter');
  check('the counter is still the same process after quitting and coming back with pi -c', stop.results[0]?.text.includes(`pid=${pid}`) === true, stop.results[0]?.text.split('\n')[0]);
  const resumed = s.notices().find((n) => /Prized sandbox sbx_/.test(n));
  check('pi -c reconnected to the same sandbox, not a new one', Boolean(resumed?.includes(F) && /reconnected/.test(resumed)), resumed);
  await s.turn('/prized pull');
  await sleep(1500);
  check('pull brought the note home and removed the counter files', readFileSync(path.join(proj, 'notes', 'from-fork.txt'), 'utf8') === 'written after the fork\n' && !existsSync(path.join(proj, 'counter.py')));
  await s.quit();
  await sleep(2500);
  check('leaving with nothing left to pull deleted the sandbox', !(await alive(F)));

  // Print mode, with nobody there: the task on the command line, the changes brought home on exit, the sandbox deleted.
  git('checkout', '--', '.');
  git('clean', '-fdq');
  const started = new Date();
  const pr = spawn(path.join(PKG, 'node_modules', '.bin', 'pi'), ['--offline', '-e', path.join(PKG, 'index.ts'), '--provider', 'scripted', '--model', 'scripted-1', '-p', '[fix] test_calc.py fails; fix calc.py', '--prized', '--prized-vcpu', '1', '--prized-memory', '1', '--prized-pull'], { cwd: proj, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let printed = '';
  let printedErr = '';
  pr.stdout?.on('data', (d) => (printed += d));
  pr.stderr?.on('data', (d) => (printedErr += d));
  const code = await new Promise<number | null>((r) => pr.on('exit', r));
  log('print mode exited', code, printed.trim().slice(0, 80));
  check('print mode: pi -p "task" --prized --prized-pull ran the task and exited cleanly', code === 0 && /Fixed add/.test(printed), `exit ${code}`);
  check('print mode: the fix is in your checkout, and the exit note says so', readFileSync(path.join(proj, 'calc.py'), 'utf8').includes('a + b') && /Prized pulled the sandbox's changes into .* and deleted sandbox sbx_/.test(printedErr), printedErr.trim().slice(0, 120));
  const freshOnes = async () => (await Sandbox.list({ apiKey, labels: { 'created-by': 'pi-prized' } })).filter((x) => x.createdAt && x.createdAt >= started);
  let fresh = await freshOnes();
  for (let i = 0; i < 20 && fresh.length > 0; i += 1) {
    await sleep(1000); // a sandbox that was just deleted is listed as deleting for a moment
    fresh = await freshOnes();
  }
  check('print mode: its sandbox is gone', fresh.length === 0, fresh.map((x) => `${x.id} ${x.state}`).join(' '));
  for (const x of fresh) ids.push(x.id);
} catch (err) {
  failures += 1;
  console.log('FAIL  test error:', err instanceof Error ? err.stack : err);
} finally {
  try { current?.pi.kill('SIGKILL'); } catch { /* gone already */ }
  for (const id of ids) {
    try { await (await stateOf(id)).delete({ wait: true }); } catch { /* already gone */ }
  }
  const left = (await Sandbox.list({ apiKey, labels: { 'created-by': 'pi-prized' } })).filter((x) => sessions.includes(x.labels['pi-session'] ?? '---'));
  check('no sandbox of this run is left', left.length === 0, left.map((x) => x.id).join(' '));
  server.close();
  rmSync(root, { recursive: true, force: true });
}
console.log(failures === 0 ? 'ALL PASS' : `${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
