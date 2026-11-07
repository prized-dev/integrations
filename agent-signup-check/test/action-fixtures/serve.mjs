// Recorded answers of prized.dev's /api/agent-signup-check, served on 127.0.0.1 for the action's
// tests (test/action.test.mjs, and the action job in .github/workflows/ci.yml).
//
// Recorded on 2026-11-07 from the API route as it stands for launch (prized-dev/doppel
// 92e9b0a17), run locally, checking the real sites live:
//   prized-dev.json           prized.dev, listed: 5 of 7, nothing worse than its row
//   polar-sh-regression.json  polar.sh, listed, checked against its 2026-10-30 row (the board
//                             as it was before the 11-07 run): Docs in Markdown broke, pass to fail
//   val-town.json             val.town, not on the board: 1 of 7
//   rate-limited.json         the 5th fresh check of prized.dev in 10 minutes from one address: 429
//   api-not-on-domain.json    an api= on another domain: 400
// Any other domain answers like the API's own failure modes: busy.example 503, timeout.example
// 504, garbage.example a 200 that is not JSON, hang.example never answers, and the rest 404.
//
//   node test/action-fixtures/serve.mjs [port]   prints "PORT <n>" once listening
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const load = (f) => JSON.parse(fs.readFileSync(path.join(HERE, f), 'utf8'));

export const ANSWERS = {
  'prized.dev': { status: 200, body: () => load('prized-dev.json') },
  'polar.sh': { status: 200, body: () => load('polar-sh-regression.json') },
  'val.town': { status: 200, body: () => load('val-town.json') },
  'rate-limited.example': { status: 429, headers: { 'retry-after': '600' }, body: () => load('rate-limited.json') },
  'wrong-api.example': { status: 400, body: () => load('api-not-on-domain.json') },
  'busy.example': { status: 503, headers: { 'retry-after': '60' }, body: () => ({ error: 'busy', message: 'The checker is busy. Try again in a minute.' }) },
  'timeout.example': { status: 504, body: () => ({ error: 'timeout', message: 'The site took too long to answer.' }) },
  'garbage.example': { status: 200, raw: '<html>not the API</html>' },
};

/**
 * Serve the answers. `answers` overrides or adds domains (a test's own body), and every request
 * is pushed to `log` as { url, headers }. Resolves with the listening server.
 */
export function serve({ port = 0, answers = {}, log = [] } = {}) {
  const all = { ...ANSWERS, ...answers };
  const server = http.createServer((req, res) => {
    log.push({ url: req.url, headers: req.headers });
    const q = new URL(req.url, 'http://127.0.0.1').searchParams;
    const domain = q.get('domain') || '';
    if (domain === 'hang.example') return; // never answers; the client gives up
    const a = all[domain];
    if (!a) {
      res.writeHead(404, { 'content-type': 'application/json' });
      return res.end('{"error":"not_found"}');
    }
    if (a.raw !== undefined) {
      res.writeHead(a.status, { 'content-type': 'text/html' });
      return res.end(a.raw);
    }
    res.writeHead(a.status, { 'content-type': 'application/json', 'cache-control': 'no-store', ...(a.headers || {}) });
    res.end(JSON.stringify(typeof a.body === 'function' ? a.body() : a.body));
  });
  return new Promise((resolve) => server.listen(port, '127.0.0.1', () => resolve(server)));
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invoked) {
  const server = await serve({ port: Number(process.argv[2]) || 0 });
  process.stdout.write(`PORT ${server.address().port}\n`);
}
