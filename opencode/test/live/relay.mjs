// Runs in the sandbox that runs OpenCode, which holds no Prized key. The plugin's SDK talks to
// http://127.0.0.1:$SDK_PORT (PRIZED_BASE_URL, and PRIZED_EDGE_URL = .../__edge) with a dummy key. Each
// request waits here until the broker, on a machine that holds the key, collects it through this
// sandbox's own preview URL (GET /next on $BROKER_PORT), runs it against Prized and posts the answer
// back (POST /reply/<id>). The Authorization header the SDK sends is dropped here, so not even the dummy
// leaves the sandbox; the key is added by the broker.
import { appendFileSync } from 'node:fs';
import http from 'node:http';

const SDK_PORT = Number(process.env.SDK_PORT ?? 4021);
const BROKER_PORT = Number(process.env.BROKER_PORT ?? 4022);
const LOG = process.env.RELAY_LOG ?? '/tmp/relay.jsonl';
const log = (o) => appendFileSync(LOG, `${JSON.stringify({ t: Date.now(), ...o })}\n`);

let seq = 0;
/** Requests waiting for the broker, and the broker's long polls waiting for requests. */
const queue = [];
const polls = [];
const pending = new Map();
/** Headers the broker's requests arrived with, for the check that the edge strips Prized-Key. */
const seenBrokerHeaders = new Set();

/** Jobs handed to the broker and not answered yet. One unanswered after 12 s goes out again: the broker
 *  runs each job id once and answers a repeat with the same reply, so a hand-off lost between here and
 *  the broker delays a request instead of losing it. */
const sent = new Map();
setInterval(() => {
  for (const [id, s] of sent) {
    if (!pending.has(id)) sent.delete(id);
    else if (Date.now() - s.at > 12_000) {
      sent.delete(id);
      log({ kind: 'resend', id });
      queue.unshift(s.job);
    }
  }
  deliver();
}, 2000).unref();

/** Hand queued jobs to live polls; a poll whose connection is gone is dropped. */
function deliver() {
  while (queue.length && polls.length) {
    const poll = polls.shift();
    clearTimeout(poll.timer);
    if (poll.res.destroyed || poll.res.writableEnded || poll.res.socket?.destroyed) continue;
    const job = queue.shift();
    sent.set(job.id, { job, at: Date.now() });
    poll.res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(job));
  }
}

const sdk = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (d) => chunks.push(d));
  req.on('end', () => {
    const id = `r${(seq += 1)}`;
    const headers = { ...req.headers };
    delete headers.authorization;
    delete headers.host;
    delete headers.connection;
    delete headers['content-length'];
    const job = { id, method: req.method, path: req.url, headers, body: Buffer.concat(chunks).toString('base64') };
    pending.set(id, { res, started: Date.now(), method: req.method, path: req.url });
    queue.push(job);
    deliver();
  });
});

const broker = http.createServer((req, res) => {
  for (const k of Object.keys(req.headers)) seenBrokerHeaders.add(k.toLowerCase());
  if (req.method === 'GET' && req.url === '/next') {
    const drop = () => {
      const i = polls.indexOf(poll);
      if (i >= 0) polls.splice(i, 1);
    };
    const poll = { res, timer: setTimeout(() => {
      drop();
      if (!res.writableEnded) res.writeHead(204).end();
    }, 8_000) };
    res.on('close', () => {
      clearTimeout(poll.timer);
      drop();
    });
    polls.push(poll);
    deliver();
    return;
  }
  if (req.method === 'GET' && req.url === '/headers') {
    // What reached this app from the broker's side: Prized-Key must not be among them.
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify([...seenBrokerHeaders].sort()));
    return;
  }
  const m = /^\/reply\/(r\d+)$/.exec(req.url ?? '');
  if (req.method === 'POST' && m) {
    const chunks = [];
    req.on('data', (d) => chunks.push(d));
    req.on('end', () => {
      const p = pending.get(m[1]);
      pending.delete(m[1]);
      res.writeHead(204).end();
      if (!p) return;
      const r = JSON.parse(Buffer.concat(chunks).toString());
      const h = { ...r.headers };
      delete h['content-length'];
      delete h['content-encoding'];
      delete h['transfer-encoding'];
      delete h.connection;
      log({ kind: 'call', method: p.method, path: p.path.replace(/path=[^&]*/, 'path=…'), status: r.status, ms: Date.now() - p.started });
      p.res.writeHead(r.status, h).end(Buffer.from(r.body ?? '', 'base64'));
    });
    return;
  }
  res.writeHead(404).end();
});

sdk.listen(SDK_PORT, '127.0.0.1', () => log({ kind: 'sdk-listening', port: SDK_PORT }));
broker.listen(BROKER_PORT, '127.0.0.1', () => log({ kind: 'broker-listening', port: BROKER_PORT }));
