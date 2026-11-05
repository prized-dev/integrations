// The live test for OpenCode 2 (@opencode/cli, $OPENCODE2_VERSION), same setup as e2e.mjs: no Prized key in
// this sandbox, a scripted model, the relay. `opencode run --standalone` runs a private server per call, so
// each run loads the plugin and its cleanup runs at exit.
//
//   1. a turn: upload, server started, edit, write, executables, read, glob, grep, .env not uploaded;
//   2. `--session` again: reconnect, the server still runs;
//   3. `--fork`: a copy of the sandbox with the server running; the parent untouched;
//   4. /prized-pull through OpenCode 2's command API: held back, then applied with --all.
//
// Writes $E2E/result-v2.json.
import { execFileSync, spawn } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';

const E2E = process.env.E2E ?? '/home/prized/e2e';
const PKG = process.env.PKG ?? '/home/prized/oc';
const VERSION = process.env.OPENCODE2_VERSION ?? '2.2.5';
const SDK_PORT = Number(process.env.SDK_PORT ?? 4021);
const MOCK_PORT = Number(process.env.MOCK_PORT ?? 4011);
const APP = `${E2E}/app`;
const checks = [];
const t0 = Date.now();
const note = (s) => process.stdout.write(`[${((Date.now() - t0) / 1000).toFixed(1)}s] ${s}\n`);
function check(name, ok, detail = '') {
  checks.push({ name, ok: !!ok, detail: String(detail).slice(0, 2000) });
  note(`${ok ? 'PASS' : 'FAIL'} ${name}${ok ? '' : `: ${String(detail).slice(0, 600)}`}`);
}
const sh = (cmd, cwd = E2E, env = process.env) => execFileSync('bash', ['-c', cmd], { cwd, env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });

function setup() {
  rmSync(APP, { recursive: true, force: true });
  mkdirSync(`${APP}/bin`, { recursive: true });
  mkdirSync(`${APP}/src`, { recursive: true });
  writeFileSync(`${APP}/app.py`, 'def greet():\n    print("Hi")\n\ngreet()\n');
  writeFileSync(`${APP}/bin/run`, '#!/bin/sh\necho run\n');
  chmodSync(`${APP}/bin/run`, 0o755);
  writeFileSync(`${APP}/src/util.py`, 'def add(a, b):\n    return a + b\n');
  writeFileSync(`${APP}/.gitignore`, '.env\n');
  writeFileSync(`${APP}/.env`, 'LOCAL_SECRET=must-not-upload\n');
  writeFileSync(
    `${APP}/opencode.json`,
    JSON.stringify(
      {
        $schema: 'https://opencode.ai/config.json',
        providers: {
          mock: {
            name: 'Mock',
            env: ['MOCK_API_KEY'],
            package: '@opencode/ai/providers/openai-compatible',
            settings: { baseURL: `http://127.0.0.1:${MOCK_PORT}/v1` },
            models: { scripted: { name: 'Scripted' } },
          },
        },
        model: 'mock/scripted',
        plugins: [{ package: `${E2E}/plugin/node_modules/opencode-prized`, options: { pauseSeconds: 4 } }],
      },
      null,
      2,
    ),
  );
  sh('git init -q -b main && git config user.name E2E && git config user.email e2e@example.com && git add -A && git commit -qm init', APP);
}

const env = {
  PATH: `${E2E}/cli2/node_modules/.bin:${process.env.PATH}`,
  HOME: `${E2E}/home2`,
  XDG_CONFIG_HOME: `${E2E}/home2/.config`,
  XDG_DATA_HOME: `${E2E}/home2/.local/share`,
  XDG_CACHE_HOME: `${E2E}/home2/.cache`,
  XDG_STATE_HOME: `${E2E}/home2/.local/state`,
  MOCK_API_KEY: 'not-a-key',
  // Not a key: the relay drops it, and the broker outside this sandbox adds the real one.
  PRIZED_API_KEY: 'dcp_relay_placeholder_not_a_key',
  PRIZED_BASE_URL: `http://127.0.0.1:${SDK_PORT}`,
  PRIZED_EDGE_URL: `http://127.0.0.1:${SDK_PORT}/__edge`,
  OPENCODE_DISABLE_AUTOUPDATE: '1',
  TERM: 'dumb',
};

function opencode(args, { timeoutMs = 240_000 } = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn('opencode', ['run', '--standalone', '--format', 'json', '--dangerously-skip-permissions', ...args], { cwd: APP, env, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
    child.on('close', (code) => {
      clearTimeout(timer);
      const events = out.split('\n').filter((l) => l.startsWith('{')).map((l) => { try { return JSON.parse(l); } catch { return undefined; } }).filter(Boolean);
      const sessionID = events.map((e) => e.sessionID ?? e.data?.sessionID ?? e.properties?.sessionID ?? e.session?.id).find((x) => typeof x === 'string');
      resolve({ code, out, err, events, ms: Date.now() - started, sessionID });
    });
  });
}

function api(operation, params, data) {
  return new Promise((resolve) => {
    const child = spawn('opencode', ['api', operation, '--standalone', ...params.flatMap((p) => ['--param', p]), '-d', JSON.stringify(data)], { cwd: APP, env, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    const timer = setTimeout(() => child.kill('SIGKILL'), 120_000);
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ code, out, err });
    });
  });
}

/** The newest synthetic message the plugin posted into a session, from OpenCode 2's own database. */
function synthetic(sessionID) {
  // A woken session has it as a message; otherwise it waits in the inbox.
  const py = `import sqlite3,json,sys\nc=sqlite3.connect('file:${env.XDG_DATA_HOME}/opencode/opencode.db?mode=ro',uri=True)\nm=c.execute("select data,time_created from session_message where session_id=? and type='synthetic' order by seq desc limit 1",(sys.argv[1],)).fetchone()\ni=c.execute("select payload,time_created from session_inbox where session_id=? and type='synthetic' order by rowid desc limit 1",(sys.argv[1],)).fetchone()\nbest=max([x for x in (m,i) if x], key=lambda x:x[1], default=None)\nprint(json.loads(best[0]).get('text','') if best else '')`;
  try {
    return execFileSync('python3', ['-c', py, sessionID], { encoding: 'utf8' });
  } catch (err) {
    return `(no synthetic message: ${err})`;
  }
}

const mockLines = () => (existsSync('/tmp/mock.jsonl') ? readFileSync('/tmp/mock.jsonl', 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []);
function toolResults(marker) {
  const seen = new Map();
  for (const l of mockLines()) if (l.kind === 'tool_result' && l.marker === marker) seen.set(l.id, l.content);
  return [...seen.values()];
}
const relayCalls = () => (existsSync('/tmp/relay.jsonl') ? readFileSync('/tmp/relay.jsonl', 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)).filter((l) => l.kind === 'call') : []);

async function main() {
  note(`OpenCode ${VERSION}`);
  if (!existsSync(`${E2E}/cli2/node_modules/.bin/opencode`)) sh(`npm install --prefix ${E2E}/cli2 --no-audit --no-fund --loglevel=error @opencode/cli@${VERSION}`);
  const tgz = sh(`npm pack --silent --pack-destination ${E2E} ${PKG}`).trim().split('\n').at(-1);
  rmSync(`${E2E}/plugin`, { recursive: true, force: true });
  sh(`npm install --prefix ${E2E}/plugin --no-audit --no-fund --loglevel=error ${E2E}/${tgz}`);
  setup();
  mkdirSync(env.HOME, { recursive: true });
  note(sh('opencode --version', APP, env).trim());

  const r1 = await opencode(['E2E-TURN-1 work in the sandbox']);
  writeFileSync(`${E2E}/v2-turn1.log`, `${r1.out}\n----\n${r1.err}`);
  const S = r1.sessionID;
  note(`turn 1: exit ${r1.code} in ${r1.ms} ms, session ${S}`);
  const req = mockLines().find((l) => l.kind === 'request' && l.tools?.length);
  check('OpenCode 2 offers its own tool names (shell, read, write, edit, glob, grep), once each', req && ['shell', 'read', 'glob', 'grep'].every((n) => req.tools.filter((t) => t.name === n).length === 1), req?.tools?.map((t) => t.name).join(','));
  const t1 = toolResults('E2E-TURN-1');
  check('turn 1 ran 9 tools', t1.length === 9, `${t1.length}: ${r1.err.slice(-1500)}`);
  check('shell ran in the sandbox, in the mirrored project folder', /\/home\/prized\/work\/app/.test(t1[0] ?? ''), t1[0]);
  check("the user's environment is not forwarded", /^0$/m.test(t1[0] ?? ''), t1[0]);
  check('edit and write applied in the sandbox', /Edit applied/.test(t1[2] ?? '') && /Wrote file/.test(t1[3] ?? ''), `${t1[2]} | ${t1[3]}`);
  check('read shows the edit with a host path', (t1[5] ?? '').includes(`<path>${APP}/app.py</path>`) && /Hello from the sandbox/.test(t1[5] ?? ''), t1[5]);
  check('glob and grep run in the sandbox', (t1[6] ?? '').includes(`${APP}/src/util.py`) && /Line 2: .*Hello/.test(t1[7] ?? ''), `${t1[6]} | ${t1[7]}`);
  check('.env was not uploaded', /File not found/.test(t1[8] ?? ''), t1[8]);
  check('the checkout is untouched', readFileSync(`${APP}/app.py`, 'utf8').includes('"Hi"'));

  const r2 = await opencode(['--session', S, 'E2E-TURN-2 check the server']);
  const t2 = toolResults('E2E-TURN-2');
  check('a new OpenCode process reconnects; the server from turn 1 still runs', /SERVER-ALIVE/.test(t2[0] ?? '') && /http=200/.test(t2[0] ?? ''), `${t2[0]} ${r2.err.slice(-800)}`);

  const r3 = await opencode(['--session', S, '--fork', 'E2E-FORK in a fork']);
  const tF = toolResults('E2E-FORK');
  check('a forked session gets a copy of the sandbox, server and all', /FORK-SERVER-ALIVE/.test(tF[0] ?? '') && relayCalls().some((c) => /\/fork/.test(c.path) && c.status < 300), `${tF[0]} ${r3.err.slice(-800)}`);
  const r4 = await opencode(['--session', S, 'E2E-TURN-3 parent check']);
  const t3 = toolResults('E2E-TURN-3');
  check('the parent does not see what the fork wrote', /PARENT-CLEAN/.test(t3[0] ?? ''), `${t3[0]} ${r4.err.slice(-400)}`);

  // Commands go through the API (`opencode api session.command`): `run` sends its message to the model.
  const r5 = await api('session.command', [`sessionID=${S}`], { name: 'prized-pull', text: '' });
  const held = synthetic(S);
  writeFileSync(`${E2E}/v2-pull1.log`, `${held}\n----\n${r5.out}${r5.err}`);
  check('/prized-pull (run by the plugin, not the model) holds back the edited executable and shows its diff', /a change to an executable file: bin\/run/.test(held) && /\+echo extra/.test(held) && readFileSync(`${APP}/app.py`, 'utf8').includes('"Hi"'), `${held.slice(-1200)} ${r5.err.slice(-600)}`);
  const r6 = await api('session.command', [`sessionID=${S}`], { name: 'prized-pull', text: '--all' });
  const applied = synthetic(S);
  writeFileSync(`${E2E}/v2-pull2.log`, `${applied}\n----\n${r6.out}${r6.err}`);
  check('/prized-pull --all applies the changes for git diff', /review with git diff/.test(applied) && readFileSync(`${APP}/app.py`, 'utf8').includes('Hello from the sandbox') && existsSync(`${APP}/deploy.sh`) && (statSync(`${APP}/deploy.sh`).mode & 0o111) !== 0, `${applied.slice(0, 600)} ${sh('git status --porcelain', APP)}`);

  const calls = relayCalls();
  const result = { opencode: VERSION, session: S, passed: checks.filter((c) => c.ok).length, failed: checks.filter((c) => !c.ok).length, checks, timing: { turn1Ms: r1.ms, turn2Ms: r2.ms, forkMs: r3.ms }, relay: { calls: calls.length, denied: calls.filter((c) => c.status === 403).length } };
  writeFileSync(`${E2E}/result-v2.json`, JSON.stringify(result, null, 2));
  note(`${result.passed} passed, ${result.failed} failed`);
}

main().catch((err) => {
  note(`driver error: ${err.stack ?? err}`);
  writeFileSync(`${E2E}/result-v2.json`, JSON.stringify({ error: String(err.stack ?? err), checks }, null, 2));
  process.exit(1);
});
