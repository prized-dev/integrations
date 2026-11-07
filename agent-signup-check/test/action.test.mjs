// The GitHub Action (action/): action/run.mjs run as the action runs it, against answers recorded
// from prized.dev's API (test/action-fixtures/serve.mjs) on 127.0.0.1. Offline: no request leaves
// the machine.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { evaluate, md, oneLine, parseFailOn, requestUrl } from '../action/run.mjs';
import { serve } from './action-fixtures/serve.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const RUN = path.join(HERE, '..', 'action', 'run.mjs');
const fixture = (f) => JSON.parse(fs.readFileSync(path.join(HERE, 'action-fixtures', f), 'utf8'));

const made = [];
test.after(() => {
  for (const d of made) fs.rmSync(d, { recursive: true, force: true });
});

/** Run the step the way action.yml does, against a fixture server; resolves with what it left. */
async function step(inputs, { answers = {}, url, env = {}, script = RUN, nodeArgs = [] } = {}) {
  const log = [];
  const server = url ? null : await serve({ answers, log });
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'asc-action-'));
  made.push(dir);
  const summary = path.join(dir, 'summary.md');
  const output = path.join(dir, 'output');
  const childEnv = {
    PATH: process.env.PATH,
    HOME: dir,
    RUNNER_TEMP: dir,
    GITHUB_STEP_SUMMARY: summary,
    GITHUB_OUTPUT: output,
    AGENT_SIGNUP_CHECK_URL: url || `http://127.0.0.1:${server.address().port}/api/agent-signup-check`,
    ...Object.fromEntries(Object.entries(inputs).map(([k, v]) => [`INPUT_${k.toUpperCase().replace(/-/g, '_')}`, v])),
    ...env,
  };
  try {
    const { code, stdout } = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [...nodeArgs, script], { env: childEnv });
      let out = '';
      child.stdout.on('data', (d) => (out += d));
      child.stderr.on('data', (d) => (out += d));
      child.on('error', reject);
      child.on('close', (c) => resolve({ code: c, stdout: out }));
    });
    const md = fs.existsSync(summary) ? fs.readFileSync(summary, 'utf8') : '';
    return {
      code,
      stdout,
      log,
      dir,
      summary: md,
      text: decode(md),
      outputs: fs.existsSync(output) ? parseOutputs(fs.readFileSync(output, 'utf8')) : {},
    };
  } finally {
    if (server) {
      server.closeAllConnections?.();
      await new Promise((r) => server.close(r));
    }
  }
}

/** The summary as it reads once rendered: entities decoded. */
const decode = (s) =>
  s.replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n))).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

/** GITHUB_OUTPUT's name<<DELIM ... DELIM records. */
function parseOutputs(text) {
  const out = {};
  const re = /^([\w-]+)<<(\S+)\n([\s\S]*?)\n\2\n/gm;
  for (let m; (m = re.exec(text)); ) out[m[1]] = m[3];
  return out;
}

/** The workflow commands a log would run: every line that starts with :: outside a stop-commands block. */
function commandsIn(stdout) {
  const live = [];
  let stop = null;
  for (const raw of stdout.split(/\r?\n/)) {
    const line = raw.trimStart();
    if (stop) {
      if (line === `::${stop}::`) stop = null;
      continue;
    }
    const m = /^::stop-commands::(.+)$/.exec(line);
    if (m) stop = m[1];
    else if (line.startsWith('::') || line.includes('##[')) live.push(line);
  }
  return live;
}

// ---------------------------------------------------------------------------------------
// Recorded answers, through the whole step.

test('pass: prized.dev, listed, nothing worse than its row', async () => {
  const r = await step({ domain: 'prized.dev' });
  assert.equal(r.code, 0, r.stdout);
  assert.equal(r.log.length, 1);
  const q = new URL(r.log[0].url, 'http://x').searchParams;
  assert.equal(q.get('domain'), 'prized.dev');
  assert.equal(q.get('fresh'), '1');
  assert.equal(q.get('api'), null);
  assert.match(r.log[0].headers['user-agent'], /^agent-signup-check-action\/\d/);
  assert.deepEqual(r.outputs, {
    status: 'checked',
    score: '5/7',
    passed: '5',
    total: '7',
    failed: 'limits',
    regressions: '',
    row: 'https://prized.dev/agent-signup-check?via=github-action#prized.dev',
    result: r.outputs.result,
  });
  assert.equal(JSON.parse(fs.readFileSync(r.outputs.result, 'utf8')).site, 'prized.dev');
  // The summary: one row per check, and the link to the row on prized.dev.
  assert.match(r.text, /^### Agent sign-up check: prized\.dev, 5 of 7 \(1 partly\)$/m);
  assert.equal(r.summary.split('\n').filter((l) => /^\| (?!Check|---)/.test(l)).length, 7);
  assert.match(r.text, /\| Spend limits \| Fail \| Same \| No spend cap/);
  assert.match(r.summary, /\(https:\/\/prized\.dev\/agent-signup-check\?via=github-action#prized\.dev\)/);
  assert.match(r.text, /Prized wrote this check/);
  assert.deepEqual(commandsIn(r.stdout).map((l) => l.split('::')[1].split(' ')[0]), ['notice']);
});

test('regression: polar.sh broke a check its row passes, and the step fails', async () => {
  const r = await step({ domain: 'polar.sh' });
  assert.equal(r.code, 1, r.stdout);
  assert.equal(r.outputs.status, 'checked');
  assert.equal(r.outputs.score, '4/7');
  assert.equal(r.outputs.regressions, 'markdown');
  assert.equal(r.outputs.failed, 'limits,payment,markdown');
  assert.match(r.summary, /\*\*Failed\.\*\*/);
  assert.match(r.text, /\| Docs in Markdown \| Fail \| \*\*Broke \(was Pass\)\*\* \|/);
  assert.match(r.text, /Since the Oct 30, 2026 row/);
  const cmds = commandsIn(r.stdout);
  assert.equal(cmds.length, 1);
  assert.match(cmds[0], /^::error title=Agent sign-up check::polar\.sh: 1 check is worse than its row on prized\.dev \(Oct 30, 2026 run\): Docs in Markdown \(was Pass, now Fail\)\./);
});

test('regression with fail-on never, or a score above below-score N: a warning, not a failure', async () => {
  for (const failOn of ['never', 'below-score 4']) {
    const r = await step({ domain: 'polar.sh', 'fail-on': failOn });
    assert.equal(r.code, 0, `${failOn}: ${r.stdout}`);
    assert.equal(r.outputs.regressions, 'markdown');
    assert.match(commandsIn(r.stdout)[0], /^::warning /);
  }
  const r = await step({ domain: 'polar.sh', 'fail-on': 'below-score 5' });
  assert.equal(r.code, 1);
  assert.match(commandsIn(r.stdout)[0], /^::error .*4 of 7 checks pass, below the 5 this step asks for/);
});

test('a regression scored by a newer checker than the row warns: it may be a rule change', async () => {
  const body = fixture('polar-sh-regression.json');
  body.checker = 'agent-signup-check/0.3.0';
  const r = await step({ domain: 'polar.sh' }, { answers: { 'polar.sh': { status: 200, body } } });
  assert.equal(r.code, 0, r.stdout);
  assert.match(commandsIn(r.stdout)[0], /^::warning .*may be the checker's rules/);
});

test('429: a warning, never a failure, and one request only', async () => {
  for (const failOn of ['any-regression', 'below-score 7']) {
    const r = await step({ domain: 'rate-limited.example', 'fail-on': failOn });
    assert.equal(r.code, 0, r.stdout);
    assert.equal(r.log.length, 1, 'no retry');
    assert.equal(r.outputs.status, 'rate-limited');
    assert.equal(r.outputs.score, '');
    assert.equal(r.outputs.row, 'https://prized.dev/agent-signup-check?domain=rate-limited.example&via=github-action');
    const cmds = commandsIn(r.stdout);
    assert.equal(cmds.length, 1);
    assert.match(cmds[0], /^::warning title=Agent sign-up check::prized\.dev rate-limited this check \(HTTP 429\).*Try again in 10 minutes\./);
    assert.match(r.text, /not checked/);
  }
});

test('network error: connection refused, or no answer in time, is a warning', async () => {
  // A port that was just free: nothing listens there.
  const s = await serve();
  const port = s.address().port;
  await new Promise((r) => s.close(r));
  const refused = await step({ domain: 'prized.dev' }, { url: `http://127.0.0.1:${port}/api/agent-signup-check` });
  assert.equal(refused.code, 0, refused.stdout);
  assert.equal(refused.outputs.status, 'unavailable');
  assert.match(commandsIn(refused.stdout)[0], /^::warning .*Could not reach prized\.dev \(ECONNREFUSED/);

  const hung = await step({ domain: 'hang.example' }, { env: { AGENT_SIGNUP_CHECK_TIMEOUT_MS: '500' } });
  assert.equal(hung.code, 0, hung.stdout);
  assert.equal(hung.log.length, 1);
  assert.equal(hung.outputs.status, 'unavailable');
  assert.match(commandsIn(hung.stdout)[0], /^::warning .*Could not reach prized\.dev \(no answer in 1 s\)/);
});

test('the API failing (503, 504, 404, not JSON) is a warning', async () => {
  for (const domain of ['busy.example', 'timeout.example', 'missing.example', 'garbage.example']) {
    const r = await step({ domain, 'fail-on': 'below-score 7' });
    assert.equal(r.code, 0, `${domain}: ${r.stdout}`);
    assert.equal(r.outputs.status, 'unavailable');
    assert.match(commandsIn(r.stdout)[0], /^::warning .*so nothing was checked/);
  }
});

test('not on the board: any-regression has no row to compare with and says how to pin the score', async () => {
  const r = await step({ domain: 'val.town' });
  assert.equal(r.code, 0, r.stdout);
  assert.equal(r.outputs.score, '1/7');
  assert.equal(r.outputs.row, 'https://prized.dev/agent-signup-check?domain=val.town&via=github-action');
  assert.match(commandsIn(r.stdout)[0], /^::notice .*set fail-on: below-score 1\./);
  assert.match(r.text, /^\| Check \| Now \| Why \|$/m);
  assert.match(r.text, /isn't on the prized\.dev board/);
  const low = await step({ domain: 'val.town', 'fail-on': 'below-score 2' });
  assert.equal(low.code, 1);
});

test('inputs: a refused api= fails, a bad fail-on fails before any request, fresh=false asks for a cached answer', async () => {
  const refused = await step({ domain: 'wrong-api.example', api: 'https://evil.example/x', 'fail-on': 'never' });
  assert.equal(refused.code, 1);
  assert.equal(refused.outputs.status, 'invalid');
  assert.match(commandsIn(refused.stdout)[0], /^::error .*refused the inputs \(HTTP 400\)\. The API URL must be on/);
  assert.equal(new URL(refused.log[0].url, 'http://x').searchParams.get('api'), 'https://evil.example/x');

  const bad = await step({ domain: 'prized.dev', 'fail-on': 'sometimes' });
  assert.equal(bad.code, 1);
  assert.equal(bad.log.length, 0);
  assert.match(bad.stdout, /^::error title=Agent sign-up check::fail-on must be/m);

  const none = await step({ domain: '' });
  assert.equal(none.code, 1);
  assert.equal(none.log.length, 0);

  const cached = await step({ domain: 'prized.dev', fresh: 'false' });
  assert.equal(cached.code, 0);
  assert.equal(new URL(cached.log[0].url, 'http://x').searchParams.get('fresh'), null);
});

test("the checked site's own text cannot run workflow commands, set outputs or inject Markdown", async () => {
  const body = fixture('prized-dev.json');
  body.checks[0].reason = 'ok\n::set-output name=score::7/7\n::add-mask::x\r\n##[error]boom | <img src=x onerror=alert(1)> [click](https://evil.example) ![i](https://evil.example/p.png)';
  body.checks[1].name = 'Name\u2028::warning::x';
  body.checks[0].reason += ' www.evil.example/phish $x^2$ team@evil.example';
  body.checks[6].reason = '&'.repeat(5000);
  body.checks[2].status = 'constructor';
  const r = await step({ domain: 'prized.dev' }, { answers: { 'prized.dev': { status: 200, body } } });
  assert.equal(r.code, 0, r.stdout);
  assert.deepEqual(commandsIn(r.stdout).map((l) => l.split('::')[1].split(' ')[0]), ['notice']);
  assert.equal(r.outputs.score, '5/7');
  assert.ok(!/native code/.test(r.stdout + r.summary));
  assert.ok(!r.summary.includes('@'), 'an email would be linked');
  assert.ok(!/<img|\]\(https:\/\/evil|!\[/.test(r.summary), r.summary);
  // No autolink (www. or a scheme), no math, and the cut never splits an escape.
  assert.ok(!/www\.|https:\/\/evil|\$/.test(r.summary.split('\n').find((l) => l.startsWith('| auth'))), r.summary);
  assert.ok(!/&#?\w*\s*\|$/.test(r.summary.split('\n').find((l) => l.startsWith('| Docs'))));
  // Each check is still one table row.
  assert.equal(r.summary.split('\n').filter((l) => /^\| (?!Check|---)/.test(l)).length, 7);
});

test('a listed platform checked with an api of its own: a regression only warns, the row probed another URL', async () => {
  const body = fixture('polar-sh-regression.json');
  body.probedAsListed = false;
  const r = await step({ domain: 'polar.sh', api: 'https://api.polar.sh/v1/orders/' }, { answers: { 'polar.sh': { status: 200, body } } });
  assert.equal(r.code, 0, r.stdout);
  assert.equal(r.outputs.regressions, 'markdown');
  assert.match(commandsIn(r.stdout)[0], /^::warning .*probed the api you gave, not the API URL the row probed/);
});

test('below-score N above the checks a domain runs says so', async () => {
  const body = fixture('prized-dev.json');
  body.checks = body.checks.map((c) => (c.id === 'challenge' ? { ...c, status: 'skipped', reason: 'Not probed' } : c));
  body.passed = 4;
  body.total = 6;
  body.listed.changes = [{ id: 'challenge', name: 'Key works without a browser', was: 'pass', now: 'skipped', change: 'changed' }];
  const r = await step({ domain: 'prized.dev', 'fail-on': 'below-score 7' }, { answers: { 'prized.dev': { status: 200, body } } });
  assert.equal(r.code, 1);
  assert.equal(r.outputs.total, '6');
  assert.equal(r.outputs.regressions, '', 'skipped is a change, not a regression');
  assert.match(commandsIn(r.stdout)[0], /4 of 6 checks pass, below the 7 this step asks for \(only 6 checks run for this domain\)/);
  assert.match(r.text, /\| Key works without a browser \| Skipped \| Changed \(was Pass\) \|/);
});

test("a 400 that isn't the API's own (a proxy's) is a warning, not refused inputs", async () => {
  const r = await step({ domain: 'proxy.example' }, { answers: { 'proxy.example': { status: 400, raw: 'Bad Request' } } });
  assert.equal(r.code, 0, r.stdout);
  assert.equal(r.outputs.status, 'unavailable');
});

test('off the board with an api: the link opens the same check', async () => {
  const body = fixture('val-town.json');
  const r = await step({ domain: 'val.town', api: 'https://api.val.town/v1/me' }, { answers: { 'val.town': { status: 200, body } } });
  assert.equal(r.outputs.row, 'https://prized.dev/agent-signup-check?domain=val.town&api=https%3A%2F%2Fapi.val.town%2Fv1%2Fme&via=github-action');
});

test('runs through a symlinked path, as on a runner whose work folder is a link', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'asc-link-'));
  made.push(dir);
  const link = path.join(dir, 'run.mjs');
  fs.symlinkSync(RUN, link);
  const r = await step({ domain: '' }, { script: link });
  assert.equal(r.code, 1, r.stdout);
  assert.match(r.stdout, /^::error title=Agent sign-up check::domain is required/m);
  const ok = await step({ domain: 'prized.dev' }, { script: link });
  assert.equal(ok.code, 0, ok.stdout);
  assert.equal(ok.outputs.score, '5/7');
});

test('no fetch (Node before 18) fails the step instead of passing as a network error', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'asc-nofetch-'));
  made.push(dir);
  const pre = path.join(dir, 'no-fetch.cjs');
  fs.writeFileSync(pre, 'delete globalThis.fetch;\n');
  const r = await step({ domain: 'prized.dev' }, { nodeArgs: ['--require', pre] });
  assert.equal(r.code, 1, r.stdout);
  assert.equal(r.log.length, 0);
  assert.equal(r.outputs.status, 'invalid');
  assert.match(r.stdout, /^::error title=Agent sign-up check::This step needs Node 18 or later/m);
  assert.match(r.text, /not run/);
});

test('two runs for one site in a job keep their own result files', async () => {
  const a = await step({ domain: 'prized.dev' });
  const b = await step({ domain: 'prized.dev' }, { env: { RUNNER_TEMP: a.dir } });
  assert.notEqual(a.outputs.result, b.outputs.result);
  assert.ok(fs.existsSync(a.outputs.result) && fs.existsSync(b.outputs.result));
});

// ---------------------------------------------------------------------------------------
// The pieces.

test('fail-on parses the three modes and refuses the rest', () => {
  assert.deepEqual(parseFailOn(''), { mode: 'any-regression' });
  assert.deepEqual(parseFailOn(' Any-Regression '), { mode: 'any-regression' });
  assert.deepEqual(parseFailOn('never'), { mode: 'never' });
  assert.deepEqual(parseFailOn('below-score 5'), { mode: 'below-score', min: 5 });
  assert.deepEqual(parseFailOn('below-score: 0'), { mode: 'below-score', min: 0 });
  for (const bad of ['below-score 8', 'below-score', 'below-score -1', 'always', 'below-score 5.5']) assert.throws(() => parseFailOn(bad), /fail-on must be/);
});

test('the request: domain, api and fresh as the API reads them', () => {
  assert.equal(
    requestUrl('https://prized.dev/api/agent-signup-check', { domain: 'example.com', api: 'https://api.example.com/v1/me?a=1&b=2', fresh: true }),
    'https://prized.dev/api/agent-signup-check?domain=example.com&api=https%3A%2F%2Fapi.example.com%2Fv1%2Fme%3Fa%3D1%26b%3D2&fresh=1',
  );
});

test('a listed answer with no row data falls back to the page', () => {
  const body = fixture('prized-dev.json');
  body.listed = { site: 'prized.dev' };
  const v = evaluate({ status: 200, body }, { failOn: { mode: 'any-regression' }, domain: 'prized.dev' });
  assert.equal(v.listed, null);
  assert.equal(v.fail, false);
  assert.equal(v.row, 'https://prized.dev/agent-signup-check?domain=prized.dev&via=github-action');
});

test('md and oneLine', () => {
  assert.equal(oneLine('a\r\nb\u2028c\u0085d'), 'a b c d');
  assert.equal(md('a|b <i>[x](y)'), 'a&#124;b &lt;i&gt;&#91;x&#93;&#40;y&#41;');
});
