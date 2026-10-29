// Offline tests: no network, no keys. Run with `scripts/test.sh agent-signup-check`, or
// `node --test agent-signup-check/test/*.test.mjs` from the repo root.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { checkSite, isPublicAddress, normalizeSite, saveEvidence, urlProblem } from '../agent-signup-check.mjs';
import { applyCorrections, loadTargets, md as mdText } from '../build-index.mjs';
import { loadSite, replay } from '../replay.mjs';
import { auditDir, bodyOf, HTML_KEEP, redactClientIps, scrubEvidence } from '../scrub.mjs';
import { diffRuns, weekly } from '../weekly.mjs';

const made = [];
const tmp = () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'asc-test-'));
  made.push(d);
  return d;
};
test.after(() => {
  for (const d of made) fs.rmSync(d, { recursive: true, force: true });
});

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
  assert.match(out, /\[body trimmed for publication: kept 16384 of \d+ characters; sha256 of the published body [0-9a-f]{64}\]$/);
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

test('an IPv4 client address goes in every spelling, and only as a whole address', () => {
  const ip = ['54.201.10.20'];
  const cases = {
    '54.201.10.20:8080': '[client-ip]:8080',
    'x-client-addr: 54.201.10.20:443': 'x-client-addr: [client-ip]:443',
    '::ffff:54.201.10.20': '::ffff:[client-ip]',
    'ec2-54-201-10-20.us-west-2.compute.amazonaws.com': 'ec2-[client-ip].us-west-2.compute.amazonaws.com',
    '154.201.10.20 54.201.10.201 54.201.10.2': '154.201.10.20 54.201.10.201 54.201.10.2',
  };
  for (const [input, want] of Object.entries(cases)) assert.equal(redactClientIps(input, ip), want, input);
});

test('an IPv6 client address goes however it is written', () => {
  const ip = ['2600:1f14:abc:de00::5'];
  for (const spelling of ['2600:1f14:abc:de00::5', '2600:1f14:abc:de00:0:0:0:5', '2600:1F14:0ABC:DE00:0000:0000:0000:0005', '[2600:1f14:abc:de00::5]:443']) {
    assert.doesNotMatch(redactClientIps(`seen ${spelling} here`, ip), /2600/i, spelling);
  }
  assert.equal(redactClientIps('2600:1f14:abc:de00::6', ip), '2600:1f14:abc:de00::6');
});

test('per-visitor tokens go from HTML pages: session ids, csrf in JS, Cloudflare challenge tokens', () => {
  const page = [
    `<script>window.csrfToken = "Zx9yQwErTy12345678"; var cfg = {'anonymous_session_id': '6f1c2b9e-1111-2222-3333-444455556666'};</script>`,
    `<input type="hidden" name="csrfmiddlewaretoken" value="AbCdEfGhIjKlMnOp">`,
    `<script>window._cf_chl_opt = {cH: 'AbCdEfGhIjKlMnOpQrSt', md: 'SxDVP_nD2Wc95bvBjmO_3Qp2', cUPMDTk:"/auth.md?__cf_chl_tk=AmpDS2SkZGp2mlg_KR9P"};</script>`,
  ].join('\n');
  const out = scrubEvidence(evidence(['content-type: text/html', 'x-csrf-token: Zx9yQwErTy12345678'], page));
  for (const secret of ['Zx9yQwErTy12345678', '6f1c2b9e', 'AbCdEfGhIjKlMnOp', 'SxDVP_nD2Wc95bvBjmO', 'AmpDS2SkZGp2mlg']) assert.doesNotMatch(out, new RegExp(secret), secret);
  assert.match(out, /_cf_chl_opt/, 'the markers a bot-wall check reads stay');
});

test('JSON served as text/html is a document: kept whole and unscrubbed', () => {
  const json = JSON.stringify({ x402Version: 2, accepts: [{ note: 'x'.repeat(HTML_KEEP * 2) }], session_id: 'keep-this-value-1234' });
  const out = scrubEvidence(evidence(['content-type: text/html'], json));
  assert.equal(bodyOf(out), json);
});

test('replay keeps an error that came with a status (a body cut off part way)', () => {
  const dir = tmp();
  const text = [`GET https://one.example/auth.md`, 'user-agent: test', 'final-url: https://one.example/auth.md', '', 'error: incomplete body (ECONNRESET)', 'HTTP 200', 'content-type: text/markdown', '', '# auth.md\n'].join('\n');
  fs.writeFileSync(path.join(dir, '01-auth.md.http'), text);
  const r = loadSite(dir).get('https://one.example/auth.md');
  assert.equal(r.status, 200);
  assert.equal(r.error, 'incomplete body (ECONNRESET)');
});

test('targets are named the way the checker names them', () => {
  const dir = tmp();
  fs.writeFileSync(path.join(dir, 'targets.json'), JSON.stringify([{ name: 'S', site: 'https://S1.Example/' }]));
  assert.equal(loadTargets(path.join(dir, 'targets.json'))[0].site, 's1.example');
});

test('platform text is inert in the published Markdown', () => {
  assert.equal(mdText('a|b [x](https://e.example) <img> @someone\nnext'), 'a\\|b \\[x\\](https://e.example) \\<img\\> \\@someone next');
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
  const down = () => async (url) => ({ url, finalUrl: url, hops: [], error: 'ENOTFOUND' });
  await assert.rejects(weekly({ root, out: root, getFor: down, clientIps: ['203.0.113.9'], date: '2026-11-04', log: () => {} }), /unreachable/);
  assert.equal(fs.existsSync(path.join(root, 'results.json')), false);
  assert.equal(fs.existsSync(path.join(root, 'evidence')), false);
});

test('weekly refuses to publish without knowing the address to scrub', async () => {
  const root = tmp();
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(TARGETS));
  // No --client-ip, and the address lookup itself is offline here: it must stop, not publish.
  await assert.rejects(weekly({ root, out: root, date: '2026-11-04', log: () => {}, learnClientIps: async () => [] }), /public address/);
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

test('a platform that cannot be reached keeps its last row, marked, instead of regressing', async () => {
  const root = tmp();
  const two = [...TARGETS, { name: 'Two', category: 'Test', site: 'two.example', api: 'https://api.two.example/v1' }];
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(two));
  const quiet = () => {};
  const live = (t) => (t.site === 'one.example' ? stub(SITE_V2) : stub({}));
  await weekly({ root, out: root, getFor: live, clientIps: ['203.0.113.9'], date: '2026-10-28', log: quiet });
  const down = (t) => (t.site === 'one.example' ? async (url) => ({ url, finalUrl: url, hops: [], error: 'ETIMEDOUT' }) : stub({}));
  const second = await weekly({ root, out: root, getFor: down, clientIps: ['203.0.113.9'], date: '2026-11-04', log: quiet });
  assert.deepEqual(second.diff.regressed, []);
  assert.deepEqual(second.notChecked.map((n) => [n.site, n.carried]), [['one.example', '2026-10-28']]);
  const row = second.doc.results.find((r) => r.site === 'one.example');
  assert.equal(row.notChecked.keptFrom, '2026-10-28');
  assert.equal(row.checks.find((c) => c.id === 'markdown').status, 'pass');
  assert.ok(second.warnings >= 1, 'a kept row is a warning');
  assert.match(fs.readFileSync(path.join(root, 'diffs/2026-11-04.md'), 'utf8'), /## Not checked this run/);
  assert.ok(fs.existsSync(path.join(root, 'evidence', 'one.example')), 'its evidence is kept too');
});

test('a backfill with an older date leaves results.json on the newest run', async () => {
  const root = tmp();
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(TARGETS));
  const opts = { root, out: root, getFor: () => stub(SITE_V2), clientIps: ['203.0.113.9'], log: () => {} };
  await weekly({ ...opts, date: '2026-11-04' });
  await weekly({ ...opts, date: '2026-10-21' });
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'results.json'), 'utf8')).date, '2026-11-04');
});

test('weekly refuses a --client-ip that is not an address', async () => {
  const root = tmp();
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(TARGETS));
  await assert.rejects(weekly({ root, out: root, clientIps: ['not-an-ip'], date: '2026-11-04', log: () => {} }), /needs an IP address/);
});

// ---------------------------------------------------------------------------------------
// Spend limits and payment handoff read agent_auth.billing (auth-md-spend-limits section 1).

const BILLING_SITE = 'two.example';
const billingSite = (billing) => {
  const json = (o, status = 200, headers = {}) => ({ status, headers: { 'content-type': 'application/json', ...headers }, body: JSON.stringify(o) });
  return {
    'https://api.two.example/v1/me': json({ error: 'invalid_token' }, 401, { 'www-authenticate': 'Bearer resource_metadata="https://api.two.example/.well-known/oauth-protected-resource"' }),
    'https://api.two.example/.well-known/oauth-protected-resource': json({ resource: 'https://api.two.example', authorization_servers: ['https://auth.two.example'] }),
    'https://auth.two.example/.well-known/oauth-authorization-server': json({
      issuer: 'https://auth.two.example',
      agent_auth: { identity_endpoint: 'https://auth.two.example/agent/identity', identity_types_supported: ['service_auth'], ...(billing === undefined ? {} : { billing }) },
    }),
  };
};
const verdicts = async (billing) => {
  const r = await checkSite(BILLING_SITE, { api: 'https://api.two.example/v1/me', get: stub(billingSite(billing)) });
  const by = Object.fromEntries(r.checks.map((c) => [c.id, c]));
  return { limits: by.limits.status, payment: by.payment.status, why: by.limits.reason };
};
const PART1 = { version: '0.1', pricing_uri: 'https://two.example/pricing.md' };
const PART2 = {
  ...PART1,
  default_spend_cap: { amount: { currency: 'USD', value: '20.00' }, period: 'month' },
  limits_endpoint: 'https://api.two.example/agent/limits',
  manage_uri: 'https://two.example/settings/agents',
};

test("the extension's part 1 and part 2 examples score as the spec says", async () => {
  assert.deepEqual(await verdicts(undefined).then(({ limits, payment }) => [limits, payment]), ['fail', 'fail']);
  assert.deepEqual(await verdicts(PART1).then(({ limits, payment }) => [limits, payment]), ['fail', 'pass']);
  assert.deepEqual(await verdicts(PART2).then(({ limits, payment }) => [limits, payment]), ['pass', 'pass']);
});

test('a default_spend_cap out of shape, or without its two URLs, passes only partly', async () => {
  for (const [cap, extra, why] of [
    [true, {}, /not an object/],
    [null, {}, /not an object/],
    [{ amount: null, period: 'month' }, {}, /currency/],
    [{ amount: { currency: 'usd', value: '20.00' }, period: 'month' }, {}, /currency/],
    [{ amount: { currency: 'USD', value: 20 }, period: 'month' }, {}, /decimal string/],
    [{ amount: { currency: 'USD', value: '-5' }, period: 'month' }, {}, /decimal string/],
    [{ amount: { currency: 'USD', value: '20.00' }, period: 'week' }, {}, /period/],
    [PART2.default_spend_cap, { manage_uri: undefined }, /manage_uri/],
    [PART2.default_spend_cap, { limits_endpoint: 'http://api.two.example/agent/limits' }, /limits_endpoint/],
  ]) {
    const v = await verdicts({ ...PART2, default_spend_cap: cap, ...extra });
    assert.equal(v.limits, 'partial', JSON.stringify(cap));
    assert.match(v.why, why);
  }
});

test('owner_limits_uri without a default cap passes spend limits partly', async () => {
  const v = await verdicts({ ...PART1, owner_limits_uri: 'https://two.example/settings/limits' });
  assert.equal(v.limits, 'partial');
  assert.match(v.why, /owner_limits_uri/);
  assert.equal(v.payment, 'pass');
});
