// Offline tests: no network, no keys. Run with `scripts/test.sh agent-signup-check`, or
// `node --test agent-signup-check/test/*.test.mjs` from the repo root.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { checkSite, isPublicAddress, MAX_REQUESTS, normalizeSite, notAnswered, saveEvidence, urlProblem } from '../agent-signup-check.mjs';
import { applyCorrections, loadTargets, md as mdText } from '../build-index.mjs';
import { loadSite, replay } from '../replay.mjs';
import { auditDir, bodyOf, HTML_KEEP, redactClientIps, scrubEvidence } from '../scrub.mjs';
import { betterTry, diffRuns, unanswered, weekly } from '../weekly.mjs';

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

test('the address is found by plain text, beside backslashes and regex metacharacters, and is never a pattern', () => {
  const ip = '203.0.113.9';
  const text = String.raw`\203.0.113.9\ (203.0.113.9)+ [ec2-203-0-113-9.*] ^203.0.113.9$ 203x0x113x9 203.0.113.9.5 1203.0.113.9 ec2-203-0-113-90 .203.0.113.9 203.0.113.9|a`;
  assert.equal(
    redactClientIps(text, [ip]),
    String.raw`\[client-ip]\ ([client-ip])+ [ec2-[client-ip].*] ^[client-ip]$ 203x0x113x9 [client-ip].5 1203.0.113.9 ec2-203-0-113-90 .[client-ip] [client-ip]|a`,
  );
  // A value that is not an IP address is skipped before any search (net.isIP), so none of these
  // changes the text.
  for (const bad of ['203.0.113.9\\', '.*', '(', '\\', '[client-ip]', '203.0.113.*']) assert.equal(redactClientIps(text, [bad]), text, bad);
  // Two dotted copies one comma apart both go, and so does the dashed spelling after them.
  assert.equal(redactClientIps('203.0.113.9,203.0.113.9 ip-203-0-113-9', [ip]), '[client-ip],[client-ip] ip-[client-ip]');
});

test('an IPv4 client address goes in its other spellings too, and the audit finds each one', () => {
  const ip = ['198.51.100.23'];
  // Each spelling, written out by hand (198.51.100.23 = 3325256727 = 0xc6336417 = ::ffff:c633:6417).
  const spellings = {
    'after a dot': 'host.198.51.100.23 end',
    'after a letter': 'ip198.51.100.23 end',
    'dashed after a dot': 'x.198-51-100-23 end',
    'zero-padded': 'addr 198.051.100.023 end',
    'partly zero-padded': 'addr 198.51.100.023:443 end',
    'dots %-encoded': 'q=198%2E51%2e100%2E23&',
    'every character %-encoded': 'q=%31%39%38%2E%35%31%2E%31%30%30%2E%32%33&',
    'reverse DNS': 'ptr 23.100.51.198 end',
    'reverse DNS name': '23.100.51.198.in-addr.arpa',
    'one decimal number': 'ip=3325256727;',
    'one hex number': 'ip=0xC6336417;',
    'IPv4-mapped IPv6 in hex': 'from ::FFFF:C633:6417 end',
    'IPv4-mapped IPv6 with 4-digit groups': 'from ::ffff:c633:6417 end',
    'IPv4-mapped IPv6 written out': 'from 0000:0000:0000:0000:0000:FFFF:c633:6417 end',
    'IPv4-mapped IPv6 in brackets with a port': 'from [::ffff:c633:6417]:443 end',
    'IPv4-mapped IPv6 %-encoded': 'q=%3A%3Affff%3Ac633%3A6417&',
    'NAT64 IPv6': 'from 64:ff9b::c633:6417 end',
    'with a port after a dot': 'conn 198.51.100.23.443 > x',
    'zero-padded dashed': 'ip-198-051-100-023 end',
    'dots %-encoded twice': 'next=198%252E51%252E100%252E23&',
    'a literal % before it': 'load 100%198.51.100.23 end',
    'a literal % after it': 'x=198.51.100.23%31 end',
    'IPv4-mapped IPv6 after a colon': 'ip:::ffff:c633:6417 end',
  };
  const dir = tmp();
  fs.mkdirSync(path.join(dir, 'one.example'));
  for (const [name, text] of Object.entries(spellings)) {
    const out = redactClientIps(text, ip);
    assert.ok(out.includes('[client-ip]'), `${name}: ${out}`);
    for (const piece of ['198', '51.100', '3325256727', 'c633', 'C633', '%31%39%38']) assert.ok(!out.includes(piece), `${name}: ${out}`);
    const file = path.join(dir, 'one.example', '01-auth.md.http');
    fs.writeFileSync(file, evidence(['content-type: text/markdown'], text));
    assert.deepEqual(auditDir(dir, { clientIps: ip }), ['one.example/01-auth.md.http: client address'], name);
    fs.writeFileSync(file, scrubEvidence(fs.readFileSync(file, 'utf8'), { clientIps: ip }));
    assert.deepEqual(auditDir(dir, { clientIps: ip }), [], `${name}, scrubbed`);
  }
  // Neighbours stay: other addresses, longer numbers, and text around a replacement as it was.
  const near = '198.51.100.230 1198.51.100.23 33252567270 a3325256727 3325256727b 0xc63364170 ::ffff:c633:6418 198.51.100.24 %2E%41 l-198-51-100-23-4';
  assert.equal(redactClientIps(near, ip), near);
  assert.equal(redactClientIps('A%2Fb 198%2e51%2e100%2e23 C', ip), 'A%2Fb [client-ip] C');
  // 0.x.x.x is never public; its one-number and IPv6 forms (0, ::) would match anything, so they are not used.
  assert.equal(redactClientIps('::-webkit-x 0 0x0 ::1 ::', ['0.0.0.0']), '::-webkit-x 0 0x0 ::1 ::');
  // Copies chained by dots all go, and a second scrub changes nothing more.
  const chain = 'v=198.51.100.23.198.51.100.23.198.51.100.23 23.100.51.198.51.100.23 ip-198-51-100-23-198-51-100-23';
  const once = redactClientIps(chain, ip);
  assert.ok(!once.includes('198'), once);
  assert.equal(redactClientIps(once, ip), once);
});

test('an IPv6 client address goes when it is %-encoded too', () => {
  const ip = ['2001:db8:4:5::17'];
  for (const spelling of ['2001%3Adb8%3A4%3A5%3A%3A17', '[2001:DB8:4:5:0:0:0:17]', '2001:0db8:0004:0005:0000:0000:0000:0017', 'addr:2001:db8:4:5::17', 'q=ip%3A2001%3Adb8%3A4%3A5%3A%3A17', 'a%2001:db8:4:5::17', '2001:db8:4:5::17%41', 'at 2001:db8:4:5::17: then']) {
    const out = redactClientIps(`seen ${spelling} here`, ip);
    assert.ok(!/2001|db8/i.test(out), `${spelling}: ${out}`);
  }
  assert.equal(redactClientIps('2001:db8:4:5::18', ip), '2001:db8:4:5::18');
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
  for (const secret of ['Zx9yQwErTy12345678', '6f1c2b9e', 'AbCdEfGhIjKlMnOp', 'SxDVP_nD2Wc95bvBjmO', 'AmpDS2SkZGp2mlg']) assert.ok(!out.includes(secret), secret);
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

test('every correction in targets.json applies to the published evidence, none stale', async () => {
  const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
  const targets = loadTargets(path.join(root, 'targets.json'));
  const corrected = targets.filter((t) => t.corrections?.length);
  assert.deepEqual(corrected.map((t) => t.site).sort(), ['cdp.coinbase.com', 'stripe.com', 'x.ai']);
  // The checker's own verdicts, from the saved responses.
  const rows = await replay(corrected, path.join(root, 'evidence'));
  const before = new Map(rows.map((r) => [r.site, r.passed]));
  const { applied, stale } = applyCorrections(rows, new Map(targets.map((t) => [t.site, t])));
  assert.deepEqual(stale, []);
  assert.deepEqual(applied.map((a) => `${a.site} ${a.check} ${a.from}->${a.to}`).sort(), [
    'cdp.coinbase.com payment partial->fail',
    'stripe.com payment partial->fail',
    'x.ai limits partial->fail',
  ]);
  for (const r of rows) assert.equal(r.passed, before.get(r.site), `${r.site}: a correction here changes no score`);
  const coinbase = rows.find((r) => r.site === 'cdp.coinbase.com').checks.find((c) => c.id === 'payment');
  assert.equal(coinbase.checkerStatus, 'partial');
  assert.match(coinbase.reason, /^Corrected after review: .*not a way for an agent to pay Coinbase\. The checker said partial: llms\.txt mentions a payment path \(x402\)/);
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
  // No --client-ip, and the address lookup is a stand-in that finds none: it must stop, not publish.
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
  // The published evidence must replay to the published verdict in every cell. (While
  // results.json was the 2026-11-07 run, made with 0.2.2, a list of the cells 0.2.5 reads
  // differently applied; the 2026-11-12 run is made with 0.2.5, so there is none.)
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
    [PART2.default_spend_cap, { pricing_uri: undefined }, /pricing_uri/],
    [PART2.default_spend_cap, { version: undefined }, /version/],
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

test('a 402 whose billing sits in JSON:API meta passes payment handoff', async () => {
  const pages = billingSite(undefined);
  pages['https://api.two.example/v1/me'] = { status: 402, headers: { 'content-type': 'application/vnd.api+json' }, body: JSON.stringify({ errors: [{ code: 'insufficient_balance' }], meta: { billing: { reason: 'credit_exhausted', url: 'https://two.example/billing' } } }) };
  const r = await checkSite(BILLING_SITE, { api: 'https://api.two.example/v1/me', get: stub(pages) });
  assert.equal(r.checks.find((c) => c.id === 'payment').status, 'pass');
});

// ---------------------------------------------------------------------------------------
// 0.2.4: the API host is read for metadata even when the API itself is not probed, and
// authorization server metadata is looked for there too. Responses recorded on 2026-11-10
// (Railway's authorization server document is cut to the fields the checker reads).

const json = (body, type = 'application/json; charset=utf-8') => ({ status: 200, headers: { 'content-type': type }, body });
const RAILWAY_SKIP = 'GraphQL API that takes queries by POST only; this checker sends GET requests only';
const RAILWAY_NO_HTML = { status: 406, headers: { 'content-type': 'application/json' }, body: '{"error":"Only HTML requests are supported here"}' };
const RAILWAY = {
  'https://railway.com/llms.txt': md('# Railway\n\n> Deploy anything.\n'),
  'https://railway.com/.well-known/oauth-protected-resource': RAILWAY_NO_HTML,
  'https://railway.com/.well-known/oauth-authorization-server': RAILWAY_NO_HTML,
  'https://railway.com/.well-known/x402': RAILWAY_NO_HTML,
  'https://backboard.railway.com/.well-known/oauth-protected-resource': json(
    '{"resource":"https://backboard.railway.com","authorization_servers":["https://backboard.railway.com"],"scopes_supported":["workspace:member"],"bearer_methods_supported":["header"]}',
  ),
  'https://backboard.railway.com/.well-known/oauth-authorization-server': json(
    '{"issuer":"https://backboard.railway.com","authorization_endpoint":"https://backboard.railway.com/oauth/auth","token_endpoint":"https://backboard.railway.com/oauth/token","registration_endpoint":"https://backboard.railway.com/oauth/register","response_types_supported":["code"],"code_challenge_methods_supported":["S256"]}',
  ),
};
const RENDER = {
  'https://render.com/llms.txt': md('# Render\n\n- MCP OAuth 2.0 authorization server metadata: https://api.render.com/.well-known/oauth-authorization-server\n'),
  'https://api.render.com/v1/services': { status: 401, headers: { 'content-type': 'application/json; charset=utf-8' }, body: '{"message":"Unauthorized"}' },
  'https://api.render.com/.well-known/oauth-protected-resource': { status: 404, headers: { 'content-type': 'text/plain; charset=utf-8' }, body: '404 page not found\n' },
  'https://api.render.com/.well-known/oauth-authorization-server': json(
    '{"issuer":"https://api.render.com","authorization_endpoint":"https://api.render.com/v1/oauth/authorize","token_endpoint":"https://api.render.com/v1/oauth/token","revocation_endpoint":"https://api.render.com/v1/oauth/token/revoke","introspection_endpoint":"https://api.render.com/v1/oauth/introspect","response_types_supported":["code"],"grant_types_supported":["authorization_code","refresh_token"],"code_challenge_methods_supported":["S256"],"token_endpoint_auth_methods_supported":["none"],"revocation_endpoint_auth_methods_supported":["none"],"authorization_response_iss_parameter_supported":false}',
    'application/json',
  ),
};
const cell = (r, id) => r.checks.find((c) => c.id === id);
const asked = (r) => r.requests.map((q) => q.url);

test('a skipped API probe still has its host read for metadata (Railway)', async () => {
  const r = await checkSite('railway.com', { api: 'https://backboard.railway.com/graphql/v2', skipApi: RAILWAY_SKIP, get: stub(RAILWAY) });
  assert.equal(cell(r, 'prm').status, 'pass');
  assert.equal(cell(r, 'prm').url, 'https://backboard.railway.com/.well-known/oauth-protected-resource');
  assert.equal(cell(r, 'prm').via, 'api-well-known');
  // The authorization server that metadata names is read, and has no agent_auth block.
  assert.equal(cell(r, 'register').status, 'fail');
  assert.match(cell(r, 'register').reason, /^Authorization server metadata has no agent_auth block/);
  assert.equal(cell(r, 'register').url, 'https://backboard.railway.com/.well-known/oauth-authorization-server');
  // What skipApi is for stays: the API URL itself is never requested, and the check is not counted.
  assert.equal(cell(r, 'challenge').status, 'skipped');
  assert.equal(cell(r, 'challenge').reason, RAILWAY_SKIP);
  assert.equal(r.total, 6);
  assert.equal(r.passed, 2);
  assert.ok(!asked(r).some((u) => new URL(u).pathname.startsWith('/graphql')), 'the GraphQL endpoint is not requested');
});

test('a skipped probe with no API named reads no API host, and none is guessed', async () => {
  const r = await checkSite('railway.com', { skipApi: RAILWAY_SKIP, get: stub(RAILWAY) });
  assert.equal(cell(r, 'prm').status, 'fail');
  assert.equal(cell(r, 'prm').reason, 'Protected resource metadata answers HTTP 406');
  assert.ok(asked(r).every((u) => new URL(u).hostname === 'railway.com'), asked(r).join(' '));
});

test('a skipped probe on the site\'s own host adds no request', async () => {
  const r = await checkSite('railway.com', { api: 'https://railway.com/graphql', skipApi: RAILWAY_SKIP, get: stub(RAILWAY) });
  assert.ok(!r.requests.some((q) => /API host/.test(q.label)));
  assert.ok(!asked(r).some((u) => { const { hostname, pathname } = new URL(u); return hostname === 'railway.com' && pathname === '/graphql'; }), asked(r).join(' '));
});

test('authorization server metadata on the API host is read when the site has none (Render)', async () => {
  const r = await checkSite('render.com', { api: 'https://api.render.com/v1/services', get: stub(RENDER) });
  const reg = cell(r, 'register');
  assert.equal(reg.status, 'fail');
  assert.equal(reg.reason, 'Authorization server metadata has no agent_auth block, and no sign-up API is documented in auth.md or llms.txt');
  assert.equal(reg.url, 'https://api.render.com/.well-known/oauth-authorization-server');
  assert.deepEqual(
    r.requests.filter((q) => /^as-metadata/.test(q.label)).map((q) => [q.label, q.url, q.status]),
    [
      ['as-metadata', 'https://render.com/.well-known/oauth-authorization-server', 404],
      ['as-metadata (API host)', 'https://api.render.com/.well-known/oauth-authorization-server', 200],
    ],
  );
  // Protected resource metadata is on neither host: that cell is unchanged.
  assert.equal(cell(r, 'prm').reason, 'Protected resource metadata not found (HTTP 404)');
});

test('a miss on the API host leaves the site\'s answer as the reason', async () => {
  const { 'https://api.render.com/.well-known/oauth-authorization-server': _gone, ...pages } = RENDER;
  const r = await checkSite('render.com', { api: 'https://api.render.com/v1/services', get: stub(pages) });
  assert.equal(cell(r, 'register').reason, 'Authorization server metadata not found (HTTP 404); no sign-up API is documented in auth.md or llms.txt');
  assert.equal(cell(r, 'register').url, 'https://render.com/.well-known/oauth-authorization-server');
  // A request the saved evidence does not have (a replay of an older run) changes nothing either.
  const get = async (url) => (url === 'https://api.render.com/.well-known/oauth-authorization-server' ? { url, finalUrl: url, hops: [], error: 'not in saved evidence' } : stub(pages)(url));
  const again = await checkSite('render.com', { api: 'https://api.render.com/v1/services', get });
  assert.deepEqual(again.checks.map((c) => [c.id, c.status, c.reason]), r.checks.map((c) => [c.id, c.status, c.reason]));
});

test('JSON on the API host that is not authorization server metadata is not taken for it', async () => {
  const at = 'https://api.render.com/.well-known/oauth-authorization-server';
  for (const body of ['{}', '{"message":"Unauthorized"}', '{"issuer":""}', '{"issuer":42}']) {
    const r = await checkSite('render.com', { api: 'https://api.render.com/v1/services', get: stub({ ...RENDER, [at]: json(body) }) });
    assert.equal(cell(r, 'register').reason, 'Authorization server metadata not found (HTTP 404); no sign-up API is documented in auth.md or llms.txt', body);
    assert.equal(cell(r, 'register').url, 'https://render.com/.well-known/oauth-authorization-server');
  }
});

test('agent_auth on the API host counts only when its issuer is that host', async () => {
  const meta = (issuer) => json(JSON.stringify({ issuer, token_endpoint: 'https://api.render.com/v1/oauth/token', agent_auth: { identity_endpoint: 'https://api.render.com/v1/agent/identity', identity_types_supported: ['anonymous'] } }));
  const at = 'https://api.render.com/.well-known/oauth-authorization-server';
  const good = await checkSite('render.com', { api: 'https://api.render.com/v1/services', get: stub({ ...RENDER, [at]: meta('https://api.render.com') }) });
  assert.equal(cell(good, 'register').status, 'pass');
  const other = await checkSite('render.com', { api: 'https://api.render.com/v1/services', get: stub({ ...RENDER, [at]: meta('https://render.com') }) });
  assert.equal(cell(other, 'register').status, 'partial');
  assert.match(cell(other, 'register').reason, /is not the authorization server it was fetched for/);
});

test('the site\'s own authorization server metadata still comes first', async () => {
  const site = json('{"issuer":"https://render.com","token_endpoint":"https://render.com/token"}');
  const r = await checkSite('render.com', { api: 'https://api.render.com/v1/services', get: stub({ ...RENDER, 'https://render.com/.well-known/oauth-authorization-server': site }) });
  assert.ok(!r.requests.some((q) => q.label === 'as-metadata (API host)'));
  assert.equal(cell(r, 'register').url, 'https://render.com/.well-known/oauth-authorization-server');
});

test('a published row does not call an unprobed API probed', async () => {
  const { publish } = await import('../build-index.mjs');
  const r = await checkSite('railway.com', { api: 'https://backboard.railway.com/graphql/v2', skipApi: RAILWAY_SKIP, get: stub(RAILWAY) });
  const { _raw, ...clean } = r;
  const run = { checker: 'agent-signup-check/test', generatedAt: '2026-11-10T00:00:00.000Z', checks: [], results: [{ name: 'Railway', category: 'Test', ...clean }] };
  const { doc } = publish(run, [{ name: 'Railway', category: 'Test', site: 'railway.com', api: 'https://backboard.railway.com/graphql/v2', skipApi: RAILWAY_SKIP }]);
  assert.equal(doc.results[0].apiProbed, null);
  assert.equal(doc.results[0].apiNotProbed, RAILWAY_SKIP);
});

// ---------------------------------------------------------------------------------------
// 0.2.5: the request cap, the same-page rule for llms.txt, and "not read".

test('a site that redirects every path twice still has its last request, x402 discovery, answered', async () => {
  // The runs of 2026-11-11 with a cap of 16: cloudflare.com, hetzner.com and browserbase.com
  // needed 17 GETs and cdp.coinbase.com 19, so their x402 discovery was cut off ("the request
  // budget ran out"). The stub charges the budget the way guardedGet does: one GET per hop,
  // three for a path on the site. This is the widest shape among the 100: a landing page served
  // as Markdown for /auth.md (so the catch-all probe is sent) and a docs llms.txt on another host.
  const sent = [];
  const landing = md('# Welcome\n\nBuild with us.\n');
  const twoHops = async (url, { budget }) => {
    const cost = new URL(url).hostname === 'one.example' ? 3 : 1;
    if (budget.left < cost) { budget.left = 0; return { url, finalUrl: url, hops: [], error: 'request budget spent' }; }
    budget.left -= cost;
    sent.push(url);
    const page = url === 'https://api.one.example/v1/me' ? SITE_V1[url] : url === 'https://docs.one.example/llms.txt' ? md('# One\n') : url === 'https://one.example/auth.md' ? landing : notFound;
    return { url, finalUrl: url, hops: [], ...structuredClone(page) };
  };
  const r = await checkSite('one.example', { api: 'https://api.one.example/v1/me', docs: 'https://docs.one.example/llms.txt', get: twoHops });
  // auth.md 3, llms.txt 3, the API 1, the docs llms.txt 1, metadata on the site 3 + 3 and on the
  // API host 1 + 1, the catch-all probe 3, x402 3: 22 GETs in 10 requests.
  assert.equal(MAX_REQUESTS, 24);
  assert.equal(sent.length, 10);
  assert.ok(sent.includes('https://one.example/.well-known/x402'), sent.join(' '));
  assert.equal(r.requests.at(-1).label, 'x402 discovery');
  assert.equal(r.requests.at(-1).status, 404);
  assert.equal(cell(r, 'payment').status, 'fail');
  assert.doesNotMatch(cell(r, 'payment').reason, /request budget/);
});

test('a landing page served for both /llms.txt and /auth.md is not an llms.txt; the docs one is read (Coinbase)', async () => {
  // cdp.coinbase.com, every run to 2026-11-11: both paths redirect to the same landing page,
  // which is served as Markdown with an H1, and "llms.txt found" was its one pass.
  const landing = { ...md('---\ntitle: Developer Platform\n---\n\n# Trusted crypto infrastructure to power your business\n'), finalUrl: 'https://www.coinbase.example/developer-platform', hops: [{ url: 'x', status: 302 }] };
  const base = { 'https://cdp.coinbase.example/auth.md': landing, 'https://cdp.coinbase.example/llms.txt': landing, 'https://api.cdp.coinbase.example/v2': SITE_V1['https://api.one.example/v1/me'] };
  const get = (pages) => async (url) => ({ url, finalUrl: url, hops: [], ...structuredClone(pages[url] ?? notFound) });
  const opts = { api: 'https://api.cdp.coinbase.example/v2', docs: 'https://docs.cdp.coinbase.example/llms.txt' };
  const without = await checkSite('cdp.coinbase.example', { ...opts, get: get(base) });
  assert.ok(asked(without).includes(opts.docs), 'the docs llms.txt is asked for');
  assert.equal(cell(without, 'markdown').status, 'fail');
  assert.match(cell(without, 'markdown').reason, /answers with the same page as \/auth\.md/);
  const withDocs = await checkSite('cdp.coinbase.example', { ...opts, get: get({ ...base, [opts.docs]: md('# Coinbase Developer Platform\n\n- [x402](https://docs.cdp.coinbase.example/x402.md): pay per request with HTTP 402\n') }) });
  assert.equal(cell(withDocs, 'markdown').status, 'pass');
  assert.equal(cell(withDocs, 'markdown').reason, 'llms.txt found at a docs path');
  // Prose checks then read the real file, not the landing page.
  assert.equal(cell(withDocs, 'payment').status, 'partial');
  // A real llms.txt beside a real auth.md is untouched.
  const real = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: stub({ ...SITE_V2, 'https://one.example/auth.md': md('# auth.md\n\nAgents: POST https://api.one.example/agent/identity\n') }) });
  assert.equal(cell(real, 'markdown').reason, 'llms.txt found');
  // So is a real /llms.txt that /auth.md redirects to (a site that sends unknown paths to it),
  // and one whose body /auth.md also answers with: the rule is about /llms.txt being sent away.
  const llms = SITE_V2['https://one.example/llms.txt'];
  const toLlms = { ...llms, finalUrl: 'https://one.example/llms.txt', hops: [{ url: 'https://one.example/auth.md', status: 302 }] };
  for (const authmd of [toLlms, llms]) {
    const g = async (url) => ({ url, finalUrl: url, hops: [], ...structuredClone({ ...SITE_V2, 'https://one.example/auth.md': authmd }[url] ?? notFound) });
    const r = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: g });
    assert.equal(cell(r, 'markdown').status, 'pass');
    assert.equal(cell(r, 'markdown').reason, 'llms.txt found');
  }
});

// ---------------------------------------------------------------------------------------
// Not read: a request that got no answer is not a verdict about the platform.

const API_401 = { status: 401, headers: { 'content-type': 'application/json' }, body: '{"error":"unauthorized"}' };
const HTML_200 = { status: 200, headers: { 'content-type': 'text/html; charset=utf-8' }, body: '<!doctype html><title>Home</title>' };
const CF_CHALLENGE = { status: 403, headers: { 'content-type': 'text/html; charset=UTF-8', 'cf-mitigated': 'challenge', server: 'cloudflare' }, body: '<!DOCTYPE html><title>Just a moment...</title>' };
const statuses = (r) => Object.fromEntries(r.checks.map((c) => [c.id, c.status]));

test('a docs llms.txt that answers HTTP 500 is not read, not a fail (Deno Deploy, 2026-11-07 and 2026-11-11)', async () => {
  const pages = {
    'https://deno.com/auth.md': HTML_200,
    'https://deno.com/llms.txt': HTML_200,
    'https://api.deno.com/v2/apps': API_401,
    'https://docs.deno.com/llms.txt': { status: 500, headers: { 'content-type': 'text/plain;charset=UTF-8', server: 'deployd' }, body: 'Internal Server Error\n' },
  };
  const r = await checkSite('deno.com', { api: 'https://api.deno.com/v2/apps', docs: 'https://docs.deno.com/llms.txt', date: '2026-11-11', get: stub(pages) });
  const md5xx = cell(r, 'markdown');
  assert.equal(md5xx.status, 'unread');
  assert.equal(md5xx.reason, 'Not read on 2026-11-11: https://docs.deno.com/llms.txt answered with HTTP 500');
  assert.equal(md5xx.answered, 'HTTP 500');
  // The verdict it replaced is kept beside it.
  assert.equal(md5xx.was, 'fail');
  assert.match(md5xx.wasReason, /answers HTTP 500/);
  // The cells that were answered keep their verdicts, and the row is still out of seven.
  assert.equal(cell(r, 'authmd').status, 'fail');
  assert.equal(cell(r, 'challenge').status, 'partial');
  assert.equal(r.total, 7);
  assert.equal(r.passed, 0);
  // Only the check whose own file failed is not read. The others had every file they read
  // answered: they stay fails, and the three that also look in llms.txt prose say it was not read.
  assert.equal(r.unread, 1);
  assert.deepEqual([cell(r, 'prm').status, cell(r, 'register').status, cell(r, 'limits').status, cell(r, 'payment').status], ['fail', 'fail', 'fail', 'fail']);
  for (const id of ['register', 'limits', 'payment']) {
    assert.match(cell(r, id).reason, / \(not read on 2026-11-11: https:\/\/docs\.deno\.com\/llms\.txt answered with HTTP 500\)$/, id);
  }
  assert.doesNotMatch(cell(r, 'prm').reason, /not read/);
  // Without a date the reason carries the day of the check.
  const today = await checkSite('deno.com', { api: 'https://api.deno.com/v2/apps', docs: 'https://docs.deno.com/llms.txt', get: stub(pages) });
  assert.equal(cell(today, 'markdown').reason, `Not read on ${today.checkedAt.slice(0, 10)}: https://docs.deno.com/llms.txt answered with HTTP 500`);
});

test('a site behind a bot challenge is not read; what its API host answered still counts (CodeSandbox)', async () => {
  const wall = Object.fromEntries(['auth.md', 'llms.txt', 'docs/llms.txt', '.well-known/oauth-protected-resource', '.well-known/oauth-authorization-server', '.well-known/x402'].map((p) => [`https://codesandbox.io/${p}`, CF_CHALLENGE]));
  const r = await checkSite('codesandbox.io', { api: 'https://api.codesandbox.io/sandbox', date: '2026-11-11', get: stub({ ...wall, 'https://api.codesandbox.io/sandbox': API_401 }) });
  assert.deepEqual(statuses(r), { authmd: 'unread', prm: 'unread', register: 'unread', challenge: 'partial', limits: 'unread', payment: 'unread', markdown: 'unread' });
  assert.equal(cell(r, 'authmd').reason, 'Not read on 2026-11-11: https://codesandbox.io/auth.md answered with a Cloudflare bot challenge');
  // Never "1 of 1": a row with unread cells is still out of seven and cannot pass every check.
  assert.equal(r.total, 7);
  assert.equal(r.passed, 0);
  assert.equal(r.unread, 6);
  // The same site with every path answering 404 is six plain fails: a real fail is not hidden.
  const open = await checkSite('codesandbox.io', { api: 'https://api.codesandbox.io/sandbox', get: stub({ 'https://api.codesandbox.io/sandbox': API_401 }) });
  assert.deepEqual(statuses(open), { authmd: 'fail', prm: 'fail', register: 'fail', challenge: 'partial', limits: 'fail', payment: 'fail', markdown: 'fail' });
  assert.equal(open.unread, 0);
});

test('a challenge met only after the platform redirected the path elsewhere is an answer (GitLab)', async () => {
  const signIn = { ...CF_CHALLENGE, finalUrl: 'https://gitlab.com/users/sign_in', hops: [{ url: 'https://gitlab.com/auth.md', status: 302 }] };
  const pages = { 'https://gitlab.com/auth.md': signIn, 'https://gitlab.com/llms.txt': md('# GitLab\n'), 'https://gitlab.com/api/v4/user': API_401 };
  const get = async (url) => ({ url, finalUrl: url, hops: [], ...structuredClone(pages[url] ?? notFound) });
  const r = await checkSite('gitlab.com', { api: 'https://gitlab.com/api/v4/user', get });
  assert.equal(cell(r, 'authmd').status, 'fail');
  assert.match(cell(r, 'authmd').reason, /after a redirect to https:\/\/gitlab\.com\/users\/sign_in/);
  // The same challenge on the path itself, after a redirect to www only, hides the file (Perplexity).
  const www = { ...CF_CHALLENGE, finalUrl: 'https://www.one.example/auth.md', hops: [{ url: 'https://one.example/auth.md', status: 301 }] };
  const get2 = async (url) => ({ url, finalUrl: url, hops: [], ...structuredClone({ 'https://one.example/auth.md': www, 'https://api.one.example/v1/me': API_401 }[url] ?? notFound) });
  const r2 = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: get2 });
  assert.equal(cell(r2, 'authmd').status, 'unread');
});

test('a bot wall in front of the API is the 401 check\'s own answer; a server error there is not', async () => {
  const walled = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: stub({ 'https://api.one.example/v1/me': CF_CHALLENGE }) });
  assert.equal(cell(walled, 'challenge').status, 'fail');
  assert.match(cell(walled, 'challenge').reason, /^The API answers with a Cloudflare bot challenge/);
  const down = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: stub({ 'https://api.one.example/v1/me': { status: 503, headers: {}, body: 'upstream connect error' } }) });
  assert.equal(cell(down, 'challenge').status, 'unread');
  assert.equal(cell(down, 'payment').status, 'unread');
});

test('one metadata URL that answers 503 leaves unread only the checks that read it', async () => {
  // /auth.md is a plain 404, the API answers 401, x402 answers 404, the site has no
  // authorization server metadata; only the protected resource metadata answers 503.
  const down = { status: 503, headers: {}, body: '' };
  const pages = { 'https://one.example/llms.txt': md('# One\n'), 'https://api.one.example/v1/me': API_401, 'https://one.example/.well-known/oauth-protected-resource': down };
  const r = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: stub(pages) });
  // Not read: the metadata itself, and the two checks that needed it to find anything
  // (no authorization server metadata was read at all, and a cap can be declared in it).
  assert.deepEqual(statuses(r), { authmd: 'fail', prm: 'unread', register: 'unread', challenge: 'partial', limits: 'unread', payment: 'fail', markdown: 'pass' });
  // /auth.md answered 404 and x402 answered 404: real fails, each from its own URL, not hidden.
  assert.equal(cell(r, 'authmd').reason, '/auth.md not found (HTTP 404)');
  assert.match(cell(r, 'payment').reason, /^No 402, payment challenge or billing metadata/);

  // The metadata a 401 names answers 503, but the site's own is usable and its authorization
  // server answered with no agent_auth: sign-up by API and spend limits are fails that were read.
  const named = 'https://api.one.example/.well-known/oauth-protected-resource/v1';
  const usable = {
    ...pages,
    'https://api.one.example/v1/me': { status: 401, headers: { 'content-type': 'application/json', 'www-authenticate': `Bearer resource_metadata="${named}"` }, body: '{}' },
    [named]: down,
    'https://one.example/.well-known/oauth-protected-resource': json('{"resource":"https://one.example","authorization_servers":["https://auth.one.example"]}'),
    'https://auth.one.example/.well-known/oauth-authorization-server': json('{"issuer":"https://auth.one.example","token_endpoint":"https://auth.one.example/token"}'),
  };
  const r2 = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: stub(usable) });
  assert.deepEqual([cell(r2, 'prm').status, cell(r2, 'register').status, cell(r2, 'limits').status], ['pass', 'fail', 'fail']);
  assert.match(cell(r2, 'register').reason, /^Authorization server metadata has no agent_auth block/);
});

test('a partly stays a partly when another file went unread, and says which', async () => {
  // owner_limits_uri was read from the authorization server; the API host's resource metadata
  // answered 429. What was read is kept; the reason names what was not.
  const pages = {
    'https://one.example/llms.txt': md('# One\n'),
    'https://api.one.example/v1/me': API_401,
    'https://api.one.example/.well-known/oauth-protected-resource': { status: 429, headers: {}, body: '' },
    'https://one.example/.well-known/oauth-authorization-server': json(
      '{"issuer":"https://one.example","agent_auth":{"identity_endpoint":"https://one.example/agent/identity","identity_types_supported":["service_auth"],"billing":{"version":"0.1","owner_limits_uri":"https://one.example/limits"}}}',
    ),
  };
  const r = await checkSite('one.example', { api: 'https://api.one.example/v1/me', date: '2026-11-11', get: stub(pages) });
  assert.equal(cell(r, 'limits').status, 'partial');
  assert.match(cell(r, 'limits').reason, /^agent_auth\.billing names owner_limits_uri.* \(not read on 2026-11-11: https:\/\/api\.one\.example\/\.well-known\/oauth-protected-resource answered with HTTP 429\)$/);
  assert.equal(cell(r, 'register').status, 'pass');
  assert.equal(r.unread, 1, 'only the resource metadata check itself');
  assert.equal(cell(r, 'prm').status, 'unread');
});

test('a host that does not exist, or has a bad certificate, is an answer: nothing goes unread', async () => {
  // The default guess for an API (https://api.<domain>/) on a site with no such host.
  for (const error of ['ENOTFOUND', 'ERR_TLS_CERT_ALTNAME_INVALID', 'CERT_HAS_EXPIRED', 'ECONNREFUSED']) {
    const get = async (url) => (new URL(url).hostname === 'api.one.example' ? { url, finalUrl: url, hops: [], error } : { url, finalUrl: url, hops: [], ...structuredClone(notFound) });
    const r = await checkSite('one.example', { get });
    assert.deepEqual(statuses(r), { authmd: 'fail', prm: 'fail', register: 'fail', challenge: 'fail', limits: 'fail', payment: 'fail', markdown: 'fail' }, error);
    assert.equal(cell(r, 'challenge').reason, `API: ${error}`);
    assert.equal(r.unread, 0);
  }
  // A timeout on the same host could pass: the 401 check is then not read.
  const get = async (url) => (new URL(url).hostname === 'api.one.example' ? { url, finalUrl: url, hops: [], error: 'timeout' } : { url, finalUrl: url, hops: [], ...structuredClone(notFound) });
  assert.equal(cell(await checkSite('one.example', { get }), 'challenge').status, 'unread');
});

test('when /auth.md goes unread, checks that read its prose do not claim it says nothing', async () => {
  // Read, auth.md documents a sign-up API, a spend limit, and where the x402 document is.
  const authmd = md('# auth.md\n\nAgents register with POST https://api.one.example/agent/identity (no browser needed).\n\nEach key has a spend limit.\n\nPayment: https://one.example/pay/.well-known/x402\n');
  const x402 = json('{"x402Version":2,"resources":[]}');
  const base = { 'https://one.example/llms.txt': md('# One\n'), 'https://api.one.example/v1/me': API_401, 'https://one.example/pay/.well-known/x402': x402 };
  const read = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: stub({ ...base, 'https://one.example/auth.md': authmd }) });
  assert.deepEqual([cell(read, 'register').status, cell(read, 'limits').status, cell(read, 'payment').status], ['partial', 'partial', 'pass']);
  // Unread, only the auth.md check itself is not read. The three others had their own files
  // answered (the API, the standard x402 path, the metadata), so they are fails that were
  // read; each says auth.md was not, since its prose could have changed the verdict.
  const unreadRun = await checkSite('one.example', { api: 'https://api.one.example/v1/me', date: '2026-11-11', get: stub({ ...base, 'https://one.example/auth.md': { status: 503, headers: {}, body: '' } }) });
  assert.equal(cell(unreadRun, 'authmd').status, 'unread');
  assert.equal(unreadRun.unread, 1);
  for (const id of ['register', 'limits', 'payment']) {
    assert.equal(cell(unreadRun, id).status, 'fail');
    assert.match(cell(unreadRun, id).reason, / \(not read on 2026-11-11: https:\/\/one\.example\/auth\.md answered with HTTP 503\)$/, id);
  }
  // The root llms.txt behind a challenge with the docs one read: nothing was missed, no note.
  const docsRead = await checkSite('one.example', { api: 'https://api.one.example/v1/me', docs: 'https://docs.one.example/llms.txt', get: stub({ 'https://one.example/llms.txt': CF_CHALLENGE, 'https://docs.one.example/llms.txt': md('# One\n'), 'https://api.one.example/v1/me': API_401 }) });
  assert.equal(cell(docsRead, 'payment').reason, 'No 402, payment challenge or billing metadata, and no payment path the checker recognizes in auth.md or llms.txt');
});

test('an answer is an answer: 404, a plain 403, HTML at 200 and a 401 stay verdicts', async () => {
  const s3 = { status: 403, headers: { 'content-type': 'application/xml', server: 'cloudflare' }, body: '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>' };
  const pages = {
    'https://one.example/auth.md': HTML_200,
    'https://one.example/llms.txt': md('# One\n'),
    'https://api.one.example/v1/me': API_401,
    'https://one.example/.well-known/oauth-protected-resource': s3,
    'https://one.example/.well-known/oauth-authorization-server': s3,
    'https://one.example/.well-known/x402': s3,
  };
  const r = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: stub(pages) });
  assert.deepEqual(statuses(r), { authmd: 'fail', prm: 'fail', register: 'fail', challenge: 'partial', limits: 'fail', payment: 'fail', markdown: 'pass' });
  assert.equal(r.total, 7);
  assert.equal(r.unread, 0);
  for (const c of r.checks) assert.doesNotMatch(c.reason, /not read/i, c.id);
});

test('a pass is never taken away by a request that got no answer', async () => {
  // The root llms.txt is behind a challenge, the docs one is read (OpenAI, 2026-11-11).
  const pages = { 'https://one.example/llms.txt': CF_CHALLENGE, 'https://docs.one.example/llms.txt': md('# One\n'), 'https://api.one.example/v1/me': API_401 };
  const r = await checkSite('one.example', { api: 'https://api.one.example/v1/me', docs: 'https://docs.one.example/llms.txt', get: stub(pages) });
  assert.equal(cell(r, 'markdown').status, 'pass');
  assert.equal(r.total, 7);
});

test('a request the budget stopped is not read, whether or not it was started', async () => {
  // Every path on the site costs five GETs (four redirects, the most the checker follows).
  const hops = (pages, cost) => async (url, { budget }) => {
    const n = new URL(url).hostname === 'one.example' ? cost : 1;
    if (budget.left < n) { budget.left = 0; return { url, finalUrl: url, hops: [], error: 'request budget spent' }; }
    budget.left -= n;
    return { url, finalUrl: url, hops: [], ...structuredClone(pages[url] ?? notFound) };
  };
  const r = await checkSite('one.example', { api: 'https://api.one.example/v1/me', get: hops({ 'https://api.one.example/v1/me': API_401 }, 5) });
  assert.equal(cell(r, 'payment').status, 'unread');
  assert.match(cell(r, 'payment').reason, /^Not read on \d{4}-\d\d-\d\d: https:\/\/one\.example\/\.well-known\/.* answered with nothing: the request budget ran out first$/);
  // The request that was never started is listed with the rest, so evidence and a replay show it.
  assert.equal(r.requests.at(-1).label, 'x402 discovery');
  assert.equal(r.requests.at(-1).error, 'request budget spent');
});

test('what counts as no answer: network failures, 5xx, 429, a challenge on the file itself, the budget; nothing else', () => {
  const at = (q) => notAnswered({ url: 'https://one.example/auth.md', finalUrl: 'https://one.example/auth.md', hops: [], ...q });
  assert.equal(at({ status: 500, headers: {}, body: '' }), 'HTTP 500');
  assert.equal(at({ status: 503, headers: {}, body: 'Service Unavailable' }), 'HTTP 503');
  assert.equal(at({ status: 429, headers: {}, body: '' }), 'HTTP 429');
  assert.equal(at(CF_CHALLENGE), 'a Cloudflare bot challenge');
  assert.equal(at({ ...CF_CHALLENGE, finalUrl: 'https://www.one.example/auth.md' }), 'a Cloudflare bot challenge');
  assert.equal(at({ ...CF_CHALLENGE, finalUrl: 'https://one.example/en/auth.md' }), 'a Cloudflare bot challenge');
  assert.equal(at({ error: 'timeout' }), 'a network failure (timeout)');
  assert.equal(at({ error: 'ECONNRESET' }), 'a network failure (ECONNRESET)');
  assert.equal(at({ error: 'EAI_AGAIN' }), 'a network failure (EAI_AGAIN)');
  assert.equal(at({ error: 'incomplete body (ECONNRESET)' }), 'a network failure (incomplete body (ECONNRESET))');
  assert.equal(at({ error: 'request budget spent' }), 'nothing: the request budget ran out first');
  // A long run of slashes in a redirect target is trimmed in linear time.
  const slashes = `https://one.example/auth.md${'/'.repeat(100000)}`;
  const t0 = Date.now();
  assert.equal(at({ ...CF_CHALLENGE, finalUrl: slashes }), 'a Cloudflare bot challenge');
  assert.ok(Date.now() - t0 < 3000, `took ${Date.now() - t0} ms`);
  // Answers, each scored as before.
  for (const answer of [
    notFound,
    { status: 410, headers: {}, body: '' },
    { status: 403, headers: { server: 'nginx' }, body: '<html><title>403 Forbidden</title></html>' },
    { status: 401, headers: {}, body: 'Unauthorized' },
    { status: 406, headers: {}, body: '{"error":"Only HTML requests are supported here"}' },
    HTML_200,
    { ...CF_CHALLENGE, finalUrl: 'https://one.example/users/sign_in' },
    { error: 'only https URLs are fetched' },
    { error: 'EBLOCKED' },
    { error: 'too many redirects' },
    { error: 'not in saved evidence' },
    // Facts about the host, the same on every try.
    { error: 'ENOTFOUND' },
    { error: 'ECONNREFUSED' },
    { error: 'CERT_HAS_EXPIRED' },
    { error: 'ERR_TLS_CERT_ALTNAME_INVALID' },
    { error: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE' },
    { error: 'DEPTH_ZERO_SELF_SIGNED_CERT' },
  ]) assert.equal(at(answer), null, JSON.stringify(answer).slice(0, 80));
  assert.equal(notAnswered(null), null);
  assert.equal(notAnswered({ url: 'not a url', finalUrl: '::', status: 500 }), 'HTTP 500');
});

test('a weekly run checks a platform again after a server error, a 429 or a bot challenge, not after its own budget', () => {
  const raw = (q) => ({ _raw: [{ label: 'auth.md', url: 'https://one.example/auth.md', ...q }] });
  assert.equal(unanswered(raw({ status: 500, headers: {}, body: 'Internal Server Error' })).length, 1);
  assert.equal(unanswered(raw({ status: 429, headers: {}, body: '' })).length, 1);
  assert.equal(unanswered(raw(CF_CHALLENGE)).length, 1);
  assert.equal(unanswered(raw({ error: 'ETIMEDOUT' })).length, 1);
  // A name that did not resolve is asked for once more too, as before 0.2.5 (and scored as an
  // answer if it fails again).
  assert.equal(unanswered(raw({ error: 'ENOTFOUND' })).length, 1);
  assert.equal(unanswered(raw({ error: 'request budget spent' })).length, 0);
  assert.equal(unanswered(raw({ error: 'only https URLs are fetched' })).length, 0);
  assert.equal(unanswered(raw(notFound)).length, 0);
  assert.equal(unanswered(raw(API_401)).length, 0);
});

test('of two checks of one platform, the one that read more is kept, and a pass is never dropped', () => {
  const tryOf = (statuses, raw = []) => ({ checks: statuses.map((status, i) => ({ id: `c${i}`, status })), _raw: raw });
  const s503 = { label: 'prm', status: 503, headers: {}, body: '' };
  const s429 = { label: 'auth.md', status: 429, headers: {}, body: '' };
  // The first read auth.md (a pass) with two metadata URLs at 503; the second got 429 on auth.md only.
  const first = tryOf(['pass', 'unread', 'unread'], [s503, s503]);
  const second = tryOf(['unread', 'fail', 'fail'], [s429]);
  assert.equal(betterTry(first, second), first, 'fewer unanswered requests does not beat a pass that was read');
  // Everything answered the second time: the second is kept.
  const answered = tryOf(['fail', 'fail']);
  assert.equal(betterTry(tryOf(['fail', 'unread'], [s503]), answered), answered);
  const healed = tryOf(['pass', 'fail']);
  assert.equal(betterTry(tryOf(['unread', 'fail'], [s429]), healed), healed);
  // No better: the first stays. A second check that threw never wins.
  const same = tryOf(['fail', 'unread'], [s503]);
  assert.equal(betterTry(same, tryOf(['fail', 'unread'], [s503])), same);
  assert.equal(betterTry(same, { site: 'one.example', error: 'boom' }), same);
});

test('a platform that answers 429 to everything, twice, keeps its last row; it is not a regression to zero', async () => {
  const root = tmp();
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(TARGETS));
  await weekly({ root, out: root, getFor: () => stub(SITE_V2), clientIps: ['203.0.113.9'], date: '2026-11-05', log: () => {} });
  const limited = () => async (url) => ({ url, finalUrl: url, hops: [], status: 429, headers: { 'retry-after': '60' }, body: 'Too Many Requests' });
  const second = await weekly({ root, out: root, getFor: limited, retry: true, clientIps: ['203.0.113.9'], date: '2026-11-12', log: () => {} });
  assert.deepEqual(second.diff.regressed, []);
  assert.deepEqual(second.notChecked.map((n) => [n.site, n.carried]), [['one.example', '2026-11-05']]);
  assert.match(second.notChecked[0].why, /^every request went unanswered \(auth\.md: HTTP 429\)$/);
  const row = second.doc.results[0];
  assert.equal(row.notChecked.keptFrom, '2026-11-05');
  assert.equal(row.checks.find((c) => c.id === 'markdown').status, 'pass');
  assert.ok(second.warnings >= 1);
  // Kept a second week, the row still says which run last read it.
  const third = await weekly({ root, out: root, getFor: limited, retry: true, clientIps: ['203.0.113.9'], date: '2026-11-19', log: () => {} });
  assert.deepEqual([third.doc.results[0].notChecked.date, third.doc.results[0].notChecked.keptFrom], ['2026-11-19', '2026-11-05']);

  // With no earlier row to keep and nothing answered, the run still writes (the platform is
  // listed under errors), it does not throw.
  const fresh = tmp();
  fs.writeFileSync(path.join(fresh, 'targets.json'), JSON.stringify(TARGETS));
  const none = await weekly({ root: fresh, out: fresh, getFor: limited, retry: true, clientIps: ['203.0.113.9'], date: '2026-11-12', log: () => {} });
  assert.deepEqual(none.notChecked.map((n) => [n.site, n.carried]), [['one.example', null]]);
});

test('a platform that puts a bot wall in front of everything, its API too, is checked, not carried', async () => {
  // A wall is not "nothing came back": the row shows what was seen instead of keeping last
  // week's verdicts for as long as the wall stands.
  const root = tmp();
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(TARGETS));
  await weekly({ root, out: root, getFor: () => stub(SITE_V2), clientIps: ['203.0.113.9'], date: '2026-11-05', log: () => {} });
  const walled = () => async (url) => ({ url, finalUrl: url, hops: [], ...structuredClone(CF_CHALLENGE) });
  const second = await weekly({ root, out: root, getFor: walled, retry: true, clientIps: ['203.0.113.9'], date: '2026-11-12', log: () => {} });
  assert.deepEqual(second.notChecked, []);
  const row = second.doc.results[0];
  assert.equal(row.notChecked, undefined);
  // The 401 check fails on the wall in front of the API (its own question); the files are not read.
  assert.equal(row.checks.find((c) => c.id === 'challenge').status, 'fail');
  assert.match(row.checks.find((c) => c.id === 'challenge').reason, /^The API answers with a Cloudflare bot challenge/);
  assert.equal(row.unread, 6);
  assert.equal(row.total, 7);
});

test('a weekly run tries a platform once more when a request got no answer, and records both tries', async () => {
  const root = tmp();
  fs.writeFileSync(path.join(root, 'targets.json'), JSON.stringify(TARGETS));
  // The docs host answers 503 the first time it is asked and the file the second time.
  let asked503 = 0;
  const flaky = () => async (url) => {
    if (url === 'https://one.example/llms.txt' && asked503++ === 0) return { url, finalUrl: url, hops: [], status: 503, headers: {}, body: 'Service Unavailable' };
    return stub(SITE_V2)(url);
  };
  const healed = await weekly({ root, out: root, getFor: flaky, retry: true, clientIps: ['203.0.113.9'], date: '2026-11-12', log: () => {} });
  const row = healed.doc.results[0];
  assert.equal(row.checks.find((c) => c.id === 'markdown').status, 'pass');
  assert.equal(healed.unread.length, 0);
  assert.equal(healed.retried.length, 1);
  assert.match(healed.retried[0].kept, /^kept the second check\. First: llms\.txt 503\. Second: every request answered$/);
  assert.match(fs.readFileSync(path.join(root, 'diffs/2026-11-12.md'), 'utf8'), /## Checked twice \(a request got no answer the first time\) \(1\)/);

  // Still unanswered the second time: published as not read, dated with the day it was asked.
  const root2 = tmp();
  fs.writeFileSync(path.join(root2, 'targets.json'), JSON.stringify(TARGETS));
  let tries = 0;
  const down = () => async (url) => {
    if (url === 'https://one.example/llms.txt') { tries++; return { url, finalUrl: url, hops: [], status: 503, headers: {}, body: 'Service Unavailable' }; }
    return stub(SITE_V1)(url);
  };
  const still = await weekly({ root: root2, out: root2, getFor: down, retry: true, clientIps: ['203.0.113.9'], date: '2026-11-12', log: () => {} });
  assert.equal(tries, 2, 'asked twice, not more');
  const cellMd = still.doc.results[0].checks.find((c) => c.id === 'markdown');
  assert.equal(cellMd.status, 'unread');
  // The day the request was made, whatever --date names the run.
  const asked = still.doc.results[0].checkedAt.slice(0, 10);
  assert.equal(cellMd.reason, `Not read on ${asked}: https://one.example/llms.txt answered with HTTP 503`);
  assert.equal(still.doc.results[0].unread, 1);
  assert.deepEqual(still.unread.map((u) => [u.site, u.check, u.was]), [['one.example', 'Docs in Markdown', 'fail']]);
  assert.match(still.summary, /not read: 1 check\(s\) on 1 platform\(s\)\.$/);
  const mdOut = fs.readFileSync(path.join(root2, 'results/2026-11-12.md'), 'utf8');
  assert.match(mdOut, /- Not read: 1 check\(s\) on 1 platform\(s\) \(One\)/);
  assert.match(mdOut, /\| One \(one\.example\) \| Test \| 1\/7 1\? \|/);
  // The saved responses score the same when replayed, and the reason keeps the day they were fetched.
  const again = await replay(TARGETS, path.join(root2, 'evidence'));
  assert.equal(again[0].checks.find((c) => c.id === 'markdown').status, 'unread');
  assert.equal(again[0].checks.find((c) => c.id === 'markdown').reason, cellMd.reason);
  // A replay of older responses says the day they were fetched, not the day of the replay.
  const dir = tmp();
  const old = await checkSite('one.example', { api: TARGETS[0].api, get: down() });
  old.checkedAt = '2026-11-07T00:18:00.000Z';
  saveEvidence(dir, old);
  const [replayed] = await replay(TARGETS, dir);
  assert.equal(replayed.checks.find((c) => c.id === 'markdown').reason, 'Not read on 2026-11-07: https://one.example/llms.txt answered with HTTP 503');
  const out = tmp();
  fs.writeFileSync(path.join(out, 'targets.json'), JSON.stringify(TARGETS));
  const viaWeekly = await weekly({ root: out, out, replay: dir, date: '2026-11-12', log: () => {} });
  assert.equal(viaWeekly.doc.results[0].checks.find((c) => c.id === 'markdown').reason, 'Not read on 2026-11-07: https://one.example/llms.txt answered with HTTP 503');
});

