// The live test's driver, run inside a sandbox with no Prized key: the real OpenCode (opencode-ai, the
// version in $OPENCODE_VERSION) with this plugin installed from its packed tarball, a scripted model
// (mock-model.mjs) and the relay (relay.mjs) in place of a key. Drives OpenCode through:
//
//   1. a turn that uploads the project, starts a server, edits, writes, adds and edits executables, reads,
//      globs and greps in a second sandbox, and finds .env was not uploaded; OpenCode exits, the sandbox is
//      kept paused because it holds changes;
//   2. a second `opencode run --session`: the plugin reconnects and the server started in turn 1 still runs;
//   3. `opencode serve`: a turn, then the session goes idle and the sandbox pauses by itself; the next turn
//      resumes it and the server is still up;
//   4. `--fork`: the forked session gets a copy of the sandbox, server running; the parent is untouched;
//   5. /prized-pull: held back, listing the edited executable and the new one, with their diff;
//   6. /prized-pull --all: applied to the checkout; with nothing left to pull, the exit deletes the sandbox.
//
// Writes $E2E/result.json with every check. Needs: node, npm, git, and network for npm.
//
// E2E_KEY=scoped: no relay and no broker. PRIZED_API_KEY here is a short-lived scoped key that the machine
// holding the long-lived key minted for this run (scoped-run.mjs, src/scoped.ts), and the plugin talks to
// Prized itself. The checks that read the relay's log read the API instead, with that key.
import { execFileSync, spawn } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const E2E = process.env.E2E ?? '/home/prized/e2e';
const PKG = process.env.PKG ?? '/home/prized/oc';
const VERSION = process.env.OPENCODE_VERSION ?? '1.19.5';
const SDK_PORT = Number(process.env.SDK_PORT ?? 4021);
const MOCK_PORT = Number(process.env.MOCK_PORT ?? 4011);
const APP = `${E2E}/app`;
const SCOPED = process.env.E2E_KEY === 'scoped';
const API = (process.env.PRIZED_BASE_URL || 'https://api.prized.dev').replace(/\/+$/, '');
const checks = [];
const t0 = Date.now();
const note = (s) => process.stdout.write(`[${((Date.now() - t0) / 1000).toFixed(1)}s] ${s}\n`);
function check(name, ok, detail = '') {
  checks.push({ name, ok: !!ok, detail: String(detail).slice(0, 2000) });
  note(`${ok ? 'PASS' : 'FAIL'} ${name}${ok ? '' : `: ${String(detail).slice(0, 400)}`}`);
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
  sh('git init -q -b main && git config user.name E2E && git config user.email e2e@example.com && git add -A && git commit -qm init', APP);
  writeFileSync(
    `${APP}/opencode.json`,
    JSON.stringify(
      {
        $schema: 'https://opencode.ai/config.json',
        provider: {
          mock: {
            npm: '@ai-sdk/openai-compatible',
            name: 'Mock',
            options: { baseURL: `http://127.0.0.1:${MOCK_PORT}/v1`, apiKey: 'not-a-key' },
            models: { scripted: { name: 'Scripted', tool_call: true, limit: { context: 200000, output: 8192 } } },
          },
        },
        model: 'mock/scripted',
        small_model: 'mock/scripted',
        autoupdate: false,
        share: 'disabled',
        permission: { '*': 'allow' },
        plugin: [[`file://${E2E}/plugin/node_modules/opencode-prized`, { pauseSeconds: 4 }]],
      },
      null,
      2,
    ),
  );
  // The project config is part of the checkout the user has; commit it so pulls compare against it.
  sh('git add opencode.json && git commit -qm config', APP);
}

const env = {
  PATH: `${E2E}/cli/node_modules/.bin:${process.env.PATH}`,
  HOME: `${E2E}/home`,
  XDG_CONFIG_HOME: `${E2E}/home/.config`,
  XDG_DATA_HOME: `${E2E}/home/.local/share`,
  XDG_CACHE_HOME: `${E2E}/home/.cache`,
  XDG_STATE_HOME: `${E2E}/home/.local/state`,
  ...(SCOPED
    ? // The scoped key this sandbox was started with, and the API it came from.
      Object.fromEntries(['PRIZED_API_KEY', 'PRIZED_BASE_URL', 'PRIZED_EDGE_URL'].filter((k) => process.env[k]).map((k) => [k, process.env[k]]))
    : {
        // Not a key: the relay drops it, and the broker outside this sandbox adds the real one.
        PRIZED_API_KEY: 'dcp_relay_placeholder_not_a_key',
        PRIZED_BASE_URL: `http://127.0.0.1:${SDK_PORT}`,
        PRIZED_EDGE_URL: `http://127.0.0.1:${SDK_PORT}/__edge`,
      }),
  OPENCODE_DISABLE_AUTOUPDATE: '1',
  OPENCODE_DISABLE_DEFAULT_PLUGINS: '1',
  OPENCODE_DISABLE_LSP_DOWNLOAD: '1',
  OPENCODE_DISABLE_CLAUDE_CODE: '1',
  TERM: 'dumb',
};

function opencode(args, { timeoutMs = 240_000 } = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn('opencode', args, { cwd: APP, env, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
    child.on('close', (code) => {
      clearTimeout(timer);
      const events = out.split('\n').filter((l) => l.startsWith('{')).map((l) => { try { return JSON.parse(l); } catch { return undefined; } }).filter(Boolean);
      resolve({ code, out, err, events, ms: Date.now() - started, sessionID: events.find((e) => e.sessionID)?.sessionID });
    });
  });
}

function toolResults(marker) {
  const lines = existsSync('/tmp/mock.jsonl') ? readFileSync('/tmp/mock.jsonl', 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)) : [];
  const seen = new Map();
  for (const l of lines) if (l.kind === 'tool_result' && l.marker === marker) seen.set(l.id, l.content);
  return [...seen.values()];
}

/** The text OpenCode sent the model for the newest user message that contains `needle`. */
function userText(needle) {
  const lines = existsSync('/tmp/mock.jsonl') ? readFileSync('/tmp/mock.jsonl', 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)) : [];
  return lines.filter((l) => l.kind === 'request' && typeof l.user === 'string' && l.user.includes(needle)).at(-1)?.user ?? '';
}

const relayCalls = () => (existsSync('/tmp/relay.jsonl') ? readFileSync('/tmp/relay.jsonl', 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)).filter((l) => l.kind === 'call') : []);

/** E2E_KEY=scoped: the API, with this sandbox's scoped key. */
async function api(method, path) {
  const res = await fetch(`${API}/api/v1${path}`, { method, headers: { authorization: `Bearer ${process.env.PRIZED_API_KEY}` }, signal: AbortSignal.timeout(20_000) });
  return { status: res.status, json: await res.json().catch(() => null) };
}
/** The live sandboxes of a session, as the scoped key sees them; null when the API did not answer 200. */
async function sessionSandboxes(session) {
  const res = await api('GET', `/sandboxes?label=${encodeURIComponent(`opencode-session=${session}`)}`);
  return res.status === 200 && Array.isArray(res.json?.sandboxes) ? res.json.sandboxes.filter((s) => s.state !== 'deleted') : null;
}

async function main() {
  note(`OpenCode ${VERSION}`);
  mkdirSync(E2E, { recursive: true });
  if (!existsSync(`${E2E}/cli/node_modules/.bin/opencode`)) sh(`npm install --prefix ${E2E}/cli --no-audit --no-fund --loglevel=error opencode-ai@${VERSION}`);
  const tgz = sh(`npm pack --silent --pack-destination ${E2E} ${PKG}`).trim().split('\n').at(-1);
  rmSync(`${E2E}/plugin`, { recursive: true, force: true });
  sh(`npm install --prefix ${E2E}/plugin --no-audit --no-fund --loglevel=error ${E2E}/${tgz}`);
  check('the packed plugin installs with its dependencies only', existsSync(`${E2E}/plugin/node_modules/opencode-prized/index.ts`) && existsSync(`${E2E}/plugin/node_modules/@prized/sdk`) && !existsSync(`${E2E}/plugin/node_modules/@opencode-ai/plugin`));
  if (SCOPED) {
    const me = await api('GET', '/me');
    check('the only Prized key in this sandbox is a scoped one', me.json?.key?.kind === 'scoped' && !Object.entries(process.env).some(([k, v]) => /^dcp_/.test(v ?? '') && k !== 'PRIZED_API_KEY'), JSON.stringify(me.json?.key ?? me.status));
    const outside = await Promise.all([api('GET', '/boxes'), api('GET', '/secrets'), api('POST', '/api-keys/scoped')]);
    check('it reaches nothing outside its scope (boxes, secrets, minting: 403)', outside.every((r) => r.status === 403), outside.map((r) => r.status).join(','));
    const seen = await api('GET', '/sandboxes');
    check('it sees no sandbox it did not make, not even this one', seen.status === 200 && seen.json?.sandboxes?.length === 0, JSON.stringify(seen.json).slice(0, 400));
  } else {
    check('no Prized key in this sandbox', !Object.entries(process.env).some(([k, v]) => /^dcp_/.test(v ?? '') && k !== 'PRIZED_API_KEY') && !/^dcp_(?!relay_placeholder)/.test(process.env.PRIZED_API_KEY ?? ''));
  }
  setup();
  mkdirSync(env.HOME, { recursive: true });
  sh(`opencode --version`, APP, env);

  // 1. First turn.
  const r1 = await opencode(['run', '--format', 'json', 'E2E-TURN-1 work in the sandbox']);
  const S = r1.sessionID;
  note(`turn 1: exit ${r1.code} in ${r1.ms} ms, session ${S}`);
  writeFileSync(`${E2E}/turn1.stderr.log`, r1.err);
  const t1 = toolResults('E2E-TURN-1');
  check('turn 1 ran 9 tools through the plugin', t1.length === 9, `${t1.length} results; stderr: ${r1.err.slice(-1500)}`);
  check('bash ran in the sandbox, in the mirrored project folder', /\/home\/prized\/work\/app/.test(t1[0] ?? '') && /print\("Hi"\)/.test(t1[0] ?? ''), t1[0]);
  check("the user's environment is not forwarded (no PRIZED_ variables in the sandbox)", /^0$/m.test(t1[0] ?? ''), t1[0]);
  check('a background server started', /started \d+/.test(t1[1] ?? ''), t1[1]);
  check('edit applied', /Edit applied successfully/.test(t1[2] ?? ''), t1[2]);
  check('write applied', /Wrote file successfully/.test(t1[3] ?? ''), t1[3]);
  check('read shows the edit, with a host path', (t1[5] ?? '').includes(`<path>${APP}/app.py</path>`) && /2:\s+print\("Hello from the sandbox"\)/.test(t1[5] ?? ''), t1[5]);
  check('glob lists host paths', (t1[6] ?? '').includes(`${APP}/app.py`) && (t1[6] ?? '').includes(`${APP}/src/util.py`), t1[6]);
  check('grep finds the edit', (t1[7] ?? '').includes(`${APP}/app.py:`) && /Line 2: .*Hello/.test(t1[7] ?? ''), t1[7]);
  check('.env (git ignored) was not uploaded', /File not found/.test(t1[8] ?? ''), t1[8]);
  check('the checkout is untouched before a pull', readFileSync(`${APP}/app.py`, 'utf8').includes('"Hi"') && !existsSync(`${APP}/notes`));
  check('on exit, a sandbox with changes is kept and OpenCode says how to get them', /is paused with changes you have not pulled/.test(r1.err), r1.err.slice(-800));
  const req1 = readFileSync('/tmp/mock.jsonl', 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)).find((l) => l.kind === 'request' && l.tools?.length);
  const bashTool = req1?.tools.find((t) => t.name === 'bash');
  check('the model gets the plugin tools with OpenCode\'s parameter schema (only command required)', JSON.stringify(bashTool?.parameters?.required) === '["command"]' && bashTool?.parameters?.properties?.timeout, JSON.stringify(bashTool?.parameters));
  check('the built-in tools are not offered beside them (one bash, one read)', req1?.tools.filter((t) => t.name === 'bash').length === 1 && req1?.tools.filter((t) => t.name === 'read').length === 1, req1?.tools.map((t) => t.name).join(','));

  // 2. OpenCode starts again: same sandbox, server still running.
  const r2 = await opencode(['run', '--format', 'json', '--session', S, 'E2E-TURN-2 check the server']);
  const t2 = toolResults('E2E-TURN-2');
  check('after OpenCode restarts, the plugin reconnects and the server from turn 1 still runs', /SERVER-ALIVE/.test(t2[0] ?? '') && /http=200/.test(t2[0] ?? ''), `${t2[0]} ${r2.err.slice(-600)}`);

  // 3. A long-running OpenCode: the sandbox pauses by itself after the turn, and resumes with the next tool call.
  const port = 4031;
  const srv = spawn('opencode', ['serve', '--port', String(port), '--hostname', '127.0.0.1'], { cwd: APP, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let srvErr = '';
  srv.stderr.on('data', (d) => (srvErr += d));
  srv.stdout.on('data', (d) => (srvErr += d));
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 60; i += 1) {
    const ok = await fetch(`${base}/session`, { signal: AbortSignal.timeout(5000) }).then((r) => r.ok).catch(() => false);
    if (ok) break;
    await new Promise((r) => setTimeout(r, 500));
  }
  const prompt = (text) => fetch(`${base}/session/${S}/message`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ parts: [{ type: 'text', text }] }), signal: AbortSignal.timeout(180_000) }).then((r) => r.json()).catch((err) => ({ error: String(err) }));
  const before = relayCalls().length;
  await prompt('E2E-IDLE a short turn');
  const pauseWait = Date.now();
  let paused = false;
  for (let i = 0; i < 40 && !paused; i += 1) {
    await new Promise((r) => setTimeout(r, 500));
    paused = SCOPED
      ? (await sessionSandboxes(S))?.[0]?.state === 'paused'
      : relayCalls().slice(before).some((c) => c.method === 'POST' && /\/pause/.test(c.path) && c.status < 300);
  }
  check('in a long-running OpenCode, the sandbox pauses by itself once the session is idle (pauseSeconds 4)', paused, `waited ${Date.now() - pauseWait} ms`);
  const resumeAt = relayCalls().length;
  await prompt('E2E-AFTER-PAUSE is it still up');
  const tA = toolResults('E2E-AFTER-PAUSE');
  const resumed = SCOPED ? (await sessionSandboxes(S))?.[0]?.state === 'running' : relayCalls().slice(resumeAt).some((c) => /\/resume/.test(c.path));
  check('the next turn resumes it and the server is still running', /ALIVE-AFTER-PAUSE/.test(tA[0] ?? '') && resumed, tA[0]);
  srv.kill('SIGTERM');
  await new Promise((r) => setTimeout(r, 4000));

  // 4. Fork: the copy has the parent's processes; the parent does not see the fork's files.
  const r6 = await opencode(['run', '--format', 'json', '--session', S, '--fork', 'E2E-FORK in a fork']);
  const tF = toolResults('E2E-FORK');
  check('a forked session gets a copy of the sandbox, server and all', /FORK-SERVER-ALIVE/.test(tF[0] ?? '') && r6.sessionID && r6.sessionID !== S, `${tF[0]} ${r6.err.slice(-800)}`);
  const forkedFrom = SCOPED ? [(await sessionSandboxes(S))?.[0]?.id, (await sessionSandboxes(r6.sessionID))?.[0]?.id] : [];
  check(
    'the fork came from a sandbox fork',
    SCOPED ? forkedFrom.every(Boolean) && forkedFrom[0] !== forkedFrom[1] : relayCalls().some((c) => /\/fork/.test(c.path) && c.status < 300),
    forkedFrom.join(' '),
  );
  const r7 = await opencode(['run', '--format', 'json', '--session', S, 'E2E-TURN-3 parent check']);
  const t3 = toolResults('E2E-TURN-3');
  check('the parent sandbox does not see what the fork wrote', /PARENT-CLEAN/.test(t3[0] ?? ''), `${t3[0]} ${r7.err.slice(-400)}`);

  // 5. Pull, held back.
  const r3 = await opencode(['run', '--format', 'json', '--session', S, '--command', 'prized-pull']);
  const held = userText('The user ran /prized-pull,');
  writeFileSync(`${E2E}/pull1.log`, held);
  check('/prized-pull holds back an edited executable and names it', /a change to an executable file: bin\/run \(read the diff\)/.test(held), held.slice(-1500));
  check('/prized-pull names the new executable and shows its diff', /an executable: deploy\.sh/.test(held) && /\+echo extra/.test(held), held.slice(-1500));
  check('nothing was applied yet', readFileSync(`${APP}/app.py`, 'utf8').includes('"Hi"'));

  // 6. Pull everything.
  const r4 = await opencode(['run', '--format', 'json', '--session', S, '--command', 'prized-pull', '--', '--all']);
  const applied = userText('The user ran /prized-pull --all');
  writeFileSync(`${E2E}/pull2.log`, `${applied}\n----\n${r4.out}${r4.err}`);
  check('/prized-pull --all says where the changes went', /Pulled into .*review with git diff/.test(applied), applied.slice(0, 1500));
  const status = sh('git status --porcelain', APP);
  check('/prized-pull --all applies the changes to the checkout', readFileSync(`${APP}/app.py`, 'utf8').includes('Hello from the sandbox') && existsSync(`${APP}/notes/sandbox.md`), status);
  check('the new executable arrives executable, the edit to bin/run arrives', existsSync(`${APP}/deploy.sh`) && (statSync(`${APP}/deploy.sh`).mode & 0o111) !== 0 && readFileSync(`${APP}/bin/run`, 'utf8').includes('echo extra'), status);
  check('the changes land unstaged, for git diff', / M app\.py/.test(status) && /\?\? deploy\.sh/.test(status), status);

  // The parent's sandbox, by the id seen before the exit: gone (404) or on its way.
  const gone = SCOPED && forkedFrom[0] ? await api('GET', `/sandboxes/${forkedFrom[0]}`) : null;
  check(
    'on exit, a sandbox with nothing left to pull is deleted',
    SCOPED
      ? gone !== null && (gone.status === 404 || ['deleting', 'deleted'].includes(gone.json?.sandbox?.state))
      : relayCalls().some((c) => c.method === 'DELETE' && c.status < 300),
    gone === null ? 'no parent sandbox id' : `${gone.status} ${gone.json?.sandbox?.state ?? ''}`,
  );

  const calls = relayCalls();
  const result = {
    opencode: VERSION,
    session: S,
    fork: r6.sessionID,
    passed: checks.filter((c) => c.ok).length,
    failed: checks.filter((c) => !c.ok).length,
    checks,
    timing: { turn1Ms: r1.ms, turn2Ms: r2.ms, pullMs: r4.ms, forkMs: r6.ms, forkTurnMs: r6.ms },
    key: SCOPED ? 'scoped' : 'relay',
    relay: SCOPED ? null : { calls: calls.length, medianMs: calls.map((c) => c.ms).sort((a, b) => a - b)[Math.floor(calls.length / 2)], denied: calls.filter((c) => c.status === 403).length },
  };
  writeFileSync(`${E2E}/result.json`, JSON.stringify(result, null, 2));
  writeFileSync(`${E2E}/serve.log`, srvErr);
  note(`${result.passed} passed, ${result.failed} failed`);
}

main().catch((err) => {
  note(`driver error: ${err.stack ?? err}`);
  writeFileSync(`${E2E}/result.json`, JSON.stringify({ error: String(err.stack ?? err), checks }, null, 2));
  process.exit(1);
});
