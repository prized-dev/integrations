// Offline tests: no network, no keys. Run with `node --test agent-signup-check/test/`.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { checkSite, isPublicAddress, normalizeSite, saveEvidence, urlProblem } from '../agent-signup-check.mjs';
import { applyCorrections, publish } from '../build-index.mjs';
import { replay } from '../replay.mjs';
import { auditDir, bodyOf, HTML_KEEP, scrubEvidence } from '../scrub.mjs';
import { diffRuns, weekly } from '../weekly.mjs';

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'asc-test-'));

// ---------------------------------------------------------------------------------------
// The guards: what the checker refuses to fetch.

test('only public https on 443', () => {
  for (const bad of [
    'http://example.com/',
    'https://example.com:8443/',
    'https://user:pw@example.com/',
    'https://localhost/',
    'https://printer.local/',
    'https://10.0.0.1/',
    'https://127.0.0.1/',
    'https://169.254.169.254/latest/meta-data/',
    'https://[::1]/',
    'https://[fd00::1]/',
    'https://[::ffff:127.0.0.1]/',
    'https://intranet/',
  ]) {
    assert.ok(urlProblem(bad), `${bad} should be refused`);
  }
  assert.equal(urlProblem('https://example.com/auth.md'), null);
  assert.equal(isPublicAddress('8.8.8.8'), true);
  for (const a of ['10.1.2.3', '192.168.1.1', '100.64.0.1', '192.0.2.1', '::1', 'fe80::1', '64:ff9b::a9fe:a9fe']) {
    assert.equal(isPublicAddress(a), false, a);
  }
});

test('a domain is normalized, and a private one refused', () => {
  assert.equal(normalizeSite('https://Example.COM/some/path'), 'example.com');
  assert.equal(normalizeSite('example.com.'), 'example.com');
  assert.throws(() => normalizeSite('localhost'));
  assert.throws(() => normalizeSite('10.0.0.1'));
  assert.throws(() => normalizeSite(''));
});

// ---------------------------------------------------------------------------------------
// Fixture sites: a stub fetcher in place of the network.

const md = (body) => ({ status: 200, headers: { 'content-type': 'text/markdown; charset=utf-8' }, body });
const notFound = { status: 404, headers: { 'content-type': 'text/html' }, body: '<!doctype html><title>404</title>' };
function stub(pages) {
  return async (url) => {
    const page = pages[url] ?? notFound;
    return { url, finalUrl: url, hops: [], ...structuredClone(page) };
  };
}
const SITE_V1 = {
  'https://api.one.example/v1/me': { status: 401, headers: { 'content-type': 'application/json', 'www-authenticate': 'Bearer realm="one"' }, body: '{"error":"unauthorized"}' },
};
const SITE_V2 = { ...SITE_V1, 'https://one.example/llms.txt': md('# One\n\n> Docs for agents.\n\n- [API](https://one.example/docs/api.md)\n') };
const TARGETS = [{ name: 'One', category: 'Test', site: 'one.example', api: 'https://api.one.example/v1/me' }];

async function evidenceFor(pages, dir) {
  const r = await checkSite('one.example', { api: TARGETS[0].api, get: stub(pages) });
  saveEvidence(dir, r);
  return r;
}

test('replay re-scores saved evidence the same as the live check', async () => {
  const dir = tmp();
  const live = await evidenceFor(SITE_V2, dir);
  const [again] = await replay(TARGETS, dir);
  assert.deepEqual(
    again.checks.map((c) => [c.id, c.status, c.reason]),
    live.checks.map((c) => [c.id, c.status, c.reason]),
  );
  assert.equal(live.checks.find((c) => c.id === 'markdown').status, 'pass');
  assert.equal(live.checks.find((c) => c.id === 'challenge').status, 'pass');
});

// ---------------------------------------------------------------------------------------
// The scrubber.

const evidence = (headers, body, url = 'https://one.example/auth.md') =>
  [`GET ${url}`, 'user-agent: test', 'fetched: 2026-10-28T00:00:00.000Z', `final-url: ${url}`, '', 'HTTP 200', ...headers, '', body].join('\n');

test('cookies and address-echo headers are dropped', () => {
  const out = scrubEvidence(evidence(['content-type: text/plain', 'set-cookie: s=abc', 'true-client-ip: 8.8.4.4', 'x-forwarded-for: 8.8.4.4', 'server: x'], 'hello'));
  assert.doesNotMatch(out, /set-cookie|true-client-ip|x-forwarded-for|8\.8\.4\.4/);
  assert.match(out, /server: x/);
});

test("the client's own address is replaced everywhere, and only as a whole address", () => {
  const out = scrubEvidence(evidence(['content-type: text/plain'], 'you are 203.0.113.9 and 203.0.113.99 is someone else'), { clientIps: ['203.0.113.9'] });
  assert.match(out, /you are \[client-ip\] and 203\.0\.113\.99 is someone else/);
});

test('labelled visitor addresses go from HTML pages, example addresses stay in agent documents', () => {
  const html = scrubEvidence(evidence(['content-type: text/html'], `<script>dataLayer=[{'visitorIP': '8.8.4.4'}]; x={"ip_address":"8.8.4.4"}</script>`));
  assert.doesNotMatch(html, /8\.8\.4\.4/);
  const doc = scrubEvidence(evidence(['content-type: text/markdown'], '{"IP": "8.8.4.4", "client_ip": "192.0.2.1"}'));
  assert.match(doc, /"IP": "8\.8\.4\.4"/);
  const privateOne = scrubEvidence(evidence(['content-type: text/html'], '<p>"ip": "10.0.0.1"</p>'));
  assert.match(privateOne, /10\.0\.0\.1/);
});

test('anti-forgery tokens are redacted', () => {
  const out = scrubEvidence(evidence(['content-type: text/html'], '<meta name="csrf-token" content="abcdefghijklmnop1234"><input type="hidden" value="QWERTYUIOPASDFGH" name="authenticity_token">'));
  assert.doesNotMatch(out, /abcdefghijklmnop1234|QWERTYUIOPASDFGH/);
  assert.match(out, /content="\[redacted\]"/);
});

test('a long HTML body is trimmed with its hash; Markdown is kept whole', () => {
  const long = `<!doctype html><title>Sign in</title>${'x'.repeat(HTML_KEEP * 2)}`;
  const out = scrubEvidence(evidence(['content-type: text/html'], long));
  assert.match(out, /\[body trimmed for publication: kept 16384 of \d+ characters; sha256 of the full body [0-9a-f]{64}\]$/);
  assert.equal(bodyOf(out), long.slice(0, HTML_KEEP));
  assert.equal(scrubEvidence(out), out, 'scrubbing twice changes nothing');
  const doc = `# Doc\n${'y'.repeat(HTML_KEEP * 2)}`;
  assert.equal(bodyOf(scrubEvidence(evidence(['content-type: text/markdown'], doc))), doc);
});

test('the audit catches what the scrubber removes', () => {
  const dir = tmp();
  fs.mkdirSync(path.join(dir, 'one.example'));
  fs.writeFileSync(path.join(dir, 'one.example', '01-auth.md.http'), evidence(['content-type: text/html', 'set-cookie: a=b'], "<b>'visitorIP': '8.8.4.4'</b>"));
  assert.equal(auditDir(dir).length, 2);
  fs.writeFileSync(path.join(dir, 'one.example', '01-auth.md.http'), scrubEvidence(fs.readFileSync(path.join(dir, 'one.example', '01-auth.md.http'), 'utf8')));
  assert.deepEqual(auditDir(dir), []);
});

// ---------------------------------------------------------------------------------------
// Corrections expire when the page they were made about changes.

function rowWith(status, match) {
  return {
    name: 'X',
    site: 'x.example',
    checks: [{ id: 'limits', name: 'Spend limits', status, match, url: 'https://x.example/llms.txt', reason: 'prose' }],
  };
}
const FIX = new Map([['x.example', { corrections: [{ check: 'limits', from: 'partial', match: 'Spending caps', url: 'https://x.example/llms.txt', status: 'fail', note: 'consumer page' }] }]]);

test('a correction applies while the checker still says what it was made against', () => {
  const rows = [rowWith('partial', 'Spending caps')];
  const { applied, stale } = applyCorrections(rows, FIX);
  assert.equal(applied.length, 1);
  assert.equal(stale.length, 0);
  assert.equal(rows[0].checks[0].status, 'fail');
  assert.equal(rows[0].checks[0].checkerStatus, 'partial');
  assert.match(rows[0].checks[0].reason, /^Corrected after review: consumer page\. The checker said partial: prose$/);
});

test('a correction goes stale when the verdict or the match changes', () => {
  for (const row of [rowWith('pass', 'Spending caps'), rowWith('partial', 'spend limit')]) {
    const rows = [row];
    const { applied, stale } = applyCorrections(rows, FIX);
    assert.equal(applied.length, 0);
    assert.equal(stale.length, 1);
    assert.notEqual(rows[0].checks[0].status, 'fail');
  }
});

// ---------------------------------------------------------------------------------------
// The diff.

const doc = (date, rows) => ({
  date,
  checker: 'agent-signup-check/test',
  results: rows.map(([site, passed, statuses]) => ({
    name: site,
    site,
    passed,
    total: 7,
    checks: statuses.map((s, i) => ({ id: `c${i}`, name: `Check ${i}`, status: s, reason: `r${i}` })),
  })),
});

test('the diff sorts platforms into improved, regressed and other changes', () => {
  const before = doc('2026-10-28', [
    ['a.example', 1, ['pass', 'fail']],
    ['b.example', 2, ['pass', 'pass']],
    ['c.example', 1, ['pass', 'fail']],
    ['gone.example', 0, ['fail', 'fail']],
  ]);
  const after = doc('2026-11-04', [
    ['a.example', 2, ['pass', 'pass']],
    ['b.example', 1, ['pass', 'fail']],
    ['c.example', 1, ['pass', 'partial']],
    ['new.example', 0, ['fail', 'fail']],
  ]);
  const d = diffRuns(before, after);
  assert.deepEqual(d.improved.map((e) => [e.site, e.from, e.to]), [['a.example', '1/7', '2/7']]);
  assert.deepEqual(d.regressed.map((e) => [e.site, e.from, e.to]), [['b.example', '2/7', '1/7']]);
  assert.deepEqual(d.changed.map((e) => e.site), ['c.example']);
  assert.deepEqual(d.added.map((e) => e.site), ['new.example']);
  assert.deepEqual(d.removed.map((e) => e.site), ['gone.example']);
});

// ---------------------------------------------------------------------------------------
// The weekly run, end to end, from saved evidence.

test('weekly publishes a run, a diff and scrubbed evidence', async () => {
  const root = tmp();
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(TARGETS));
  const ev1 = tmp();
  const ev2 = tmp();
  await evidenceFor(SITE_V1, ev1);
  await evidenceFor(SITE_V2, ev2);
  const quiet = () => {};
  await weekly({ root, out: root, replay: ev1, date: '2026-10-28', log: quiet });
  const second = await weekly({ root, out: root, replay: ev2, date: '2026-11-04', log: quiet });
  assert.deepEqual(second.diff.improved.map((e) => [e.site, e.cells.map((c) => `${c.check}:${c.from}->${c.to}`)]), [['one.example', ['markdown:fail->pass']]]);
  for (const f of ['results/2026-10-28.json', 'results/2026-11-04.json', 'results/2026-11-04.md', 'results.json', 'diffs/2026-11-04.md', 'diffs/2026-11-04.json']) {
    assert.ok(fs.existsSync(path.join(root, f)), f);
  }
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'results.json'), 'utf8')).date, '2026-11-04');
  assert.match(fs.readFileSync(path.join(root, 'diffs/2026-11-04.md'), 'utf8'), /## Improved \(1\)\n\n- \*\*One\*\* \(one\.example\)/);
  assert.ok(fs.existsSync(path.join(root, 'evidence', 'one.example', 'result.json')));
  assert.deepEqual(auditDir(path.join(root, 'evidence')), []);
});

test('weekly writes nothing when most platforms are unreachable', async () => {
  const root = tmp();
  const many = Array.from({ length: 12 }, (_, i) => ({ name: `S${i}`, category: 'Test', site: `s${i}.example`, api: `https://api.s${i}.example/v1` }));
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(many));
  const down = () => async (url) => ({ url, finalUrl: url, hops: [], error: 'getaddrinfo ENOTFOUND' });
  await assert.rejects(weekly({ root, out: root, getFor: down, clientIps: ['203.0.113.9'], date: '2026-11-04', log: () => {} }), /unreachable/);
  assert.equal(fs.existsSync(path.join(root, 'results.json')), false);
  assert.equal(fs.existsSync(path.join(root, 'evidence')), false);
});

test('weekly refuses to publish without knowing the address to scrub', async () => {
  const root = tmp();
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(TARGETS));
  // No --client-ip, and the address lookup itself is offline here: it must stop, not publish.
  await assert.rejects(weekly({ root, out: root, date: '2026-11-04', log: () => {}, learnClientIp: async () => null }), /public address/);
  assert.equal(fs.existsSync(path.join(root, 'results.json')), false);
});

// ---------------------------------------------------------------------------------------
// What is published agrees with itself.

test('the published evidence scores the same as the published results', async () => {
  const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
  const targets = JSON.parse(fs.readFileSync(path.join(root, 'targets.json'), 'utf8'));
  const published = JSON.parse(fs.readFileSync(path.join(root, 'results.json'), 'utf8'));
  const replayed = new Map((await replay(targets, path.join(root, 'evidence'))).map((r) => [r.site, r]));
  assert.equal(published.results.length, targets.length);
  for (const row of published.results) {
    const again = replayed.get(row.site);
    // A hand-corrected cell keeps the checker's own verdict as checkerStatus.
    const expected = row.checks.map((c) => `${c.id}:${c.checkerStatus ?? c.status}`).join(' ');
    assert.equal(again.checks.map((c) => `${c.id}:${c.status}`).join(' '), expected, row.site);
  }
  assert.deepEqual(auditDir(path.join(root, 'evidence')), []);
});

test('results.json is the newest dated run', () => {
  const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
  const dates = fs.readdirSync(path.join(root, 'results')).filter((f) => /^\d{4}-\d{2}-\d{2}\.json$/.test(f)).sort();
  assert.deepEqual(fs.readFileSync(path.join(root, 'results.json'), 'utf8'), fs.readFileSync(path.join(root, 'results', dates.at(-1)), 'utf8'));
});
