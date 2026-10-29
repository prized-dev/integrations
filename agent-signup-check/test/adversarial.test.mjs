// Hostile bodies (1 MB of what the parsers look for) through checkSite with a stub fetcher:
// every case must finish quickly, so no pattern in the checker backtracks badly.
import assert from 'node:assert/strict';
import test from 'node:test';
import { checkSite } from '../agent-signup-check.mjs';

const MB = 1024 * 1024;
const rep = (s, n) => s.repeat(Math.ceil(n / s.length)).slice(0, n);
const CASES = {
  'comment opens': rep('<!--', MB),
  'curl lines': rep('curl https://a ', MB),
  'curl one line no newline': 'curl -X POST ' + rep('https://a ', MB),
  'https repeated': rep('https://', MB),
  'dollar spaces': '$1' + rep(' ', MB),
  'agent signup brackets': rep('[agent signup ', MB),
  'open brackets': rep('[ ', MB),
  'anonymous not': rep('anonymous not ', MB),
  fences: rep('```', MB),
  'POST spaces': rep('POST ', MB),
  'link opens': rep('](https://', MB),
  'x402 refs': rep('https://a/.well-known/x402', MB),
  'does not support': rep('does not support agent ', MB),
  'type colon': rep('type      ', MB),
  'hash lines': rep('#\n', MB),
  'front matter': '---' + rep('\n', MB),
  'quoted header': null,
};
const hostileJson = JSON.stringify({
  issuer: rep('/', MB / 2),
  resource: 'https://x.example/' + rep('/', 1000),
  authorization_servers: ['https://a.example/' + rep('/', 5000)],
  agent_auth: { identity_types_supported: [{ toString: 1 }, 'anonymous'], identity_endpoint: 'https://a.example/' + rep('a', 100000) },
});

for (const [name, body] of Object.entries(CASES)) {
  test(`hostile body: ${name}`, async () => {
    const get = async (url) => {
      if (url.includes('/.well-known/') || url.includes('api.')) {
        const challenge = name === 'quoted header' ? `Bearer realm="${rep('\\"', 30000)}"` : 'Bearer realm="x"';
        return { url, finalUrl: url, hops: [], status: url.includes('api.') ? 401 : 200, headers: { 'content-type': 'application/json', 'www-authenticate': challenge }, body: hostileJson };
      }
      return { url, finalUrl: url, hops: [], status: 200, headers: { 'content-type': 'text/markdown' }, body: body ?? '# auth.md' };
    };
    const t0 = Date.now();
    const out = await checkSite('example.com', { get, api: 'https://api.example.com/v1/me' });
    assert.ok(Date.now() - t0 < 3000, `took ${Date.now() - t0} ms`);
    assert.equal(out.checks.length, 7);
    assert.ok(JSON.stringify({ ...out, _raw: undefined }).length < 64 * 1024, 'the result stays small');
  });
}
