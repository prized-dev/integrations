// Runs on a machine that holds a Prized key (BROKER_PRIZED_KEY in its environment), never inside a
// sandbox. It long-polls the relay in the keyless sandbox through that sandbox's preview URL, runs each
// request the plugin's SDK made against Prized with the real key, and posts the answer back. Only what
// the plugin needs is allowed, and only for sandboxes this run created:
//
// - create: labels must say created-by=opencode-prized; the run label (oc-e2e=$RUN) and a 2 vCPU / 4 GB /
//   2 hour cap are added;
// - list: narrowed to the run label;
// - get, pause, resume, fork (copies get the run label), delete, and the sandbox edge (exec, files): only
//   for sandbox ids this run created or forked;
// - anything else: 403.
//
//   BROKER_PRIZED_KEY=... RELAY_URL=https://4022-sbx_....prized.dev RUN=e2e-123 node broker.mjs
import { appendFileSync } from 'node:fs';

const KEY = process.env.BROKER_PRIZED_KEY;
const RELAY = process.env.RELAY_URL?.replace(/\/+$/, '');
const RUN = process.env.RUN;
const API = process.env.BROKER_API ?? 'https://api.prized.dev';
const EDGE = process.env.BROKER_EDGE ?? 'https://term.prized.dev';
const LOG = process.env.BROKER_LOG ?? './broker.jsonl';
const POLLERS = 3;
if (!KEY || !RELAY || !RUN) {
  console.error('need BROKER_PRIZED_KEY, RELAY_URL and RUN');
  process.exit(2);
}
const log = (o) => appendFileSync(LOG, `${JSON.stringify({ t: Date.now(), ...o })}\n`);
const relayHeaders = { 'Prized-Key': KEY, 'Prized-Auto-Resume': 'false' };

/** Sandbox ids this run made, with where their edge is. */
const ours = new Map();
const EXTRA = { 'oc-e2e': RUN, agent: 'agent-xp', task: 'opencode-prized-1105' };

function remember(json) {
  const visit = (v) => {
    if (!v || typeof v !== 'object') return;
    if (Array.isArray(v)) return v.forEach(visit);
    if (typeof v.id === 'string' && v.id.startsWith('sbx_') && v.labels?.['oc-e2e'] === RUN) {
      ours.set(v.id, typeof v.toolboxBase === 'string' ? new URL(v.toolboxBase).origin : EDGE);
    }
    Object.values(v).forEach(visit);
  };
  visit(json);
}

const deny = (why) => ({ status: 403, headers: { 'content-type': 'application/json' }, body: Buffer.from(JSON.stringify({ error: { code: 'forbidden_by_broker', message: why } })) });

/** Decide and rewrite: returns { url, body, auth } to send, or a ready answer. */
function plan(job) {
  const u = new URL(job.path, 'http://relay');
  const body = Buffer.from(job.body ?? '', 'base64');
  if (u.pathname === '/__edge/healthz' || u.pathname === '/healthz') return { url: `${EDGE}/healthz`, body: undefined, auth: false };
  const edge = /^\/__edge\/v1\/sandbox\/(sbx_[\w]+)(\/.*)$/.exec(u.pathname);
  if (edge) {
    const origin = ours.get(edge[1]);
    if (!origin) return { answer: deny(`edge call for a sandbox this run did not make: ${edge[1]}`) };
    return { url: `${origin}/v1/sandbox/${edge[1]}${edge[2]}${u.search}`, body, auth: true };
  }
  if (u.pathname === '/api/v1/sandboxes' && job.method === 'POST') {
    const doc = JSON.parse(body.toString() || '{}');
    if (doc.labels?.['created-by'] !== 'opencode-prized') return { answer: deny('create without created-by=opencode-prized') };
    doc.labels = { ...doc.labels, ...EXTRA };
    doc.resources = { ...(doc.resources ?? {}), vcpu: Math.min(doc.resources?.vcpu ?? 2, 2) };
    if (doc.resources.memoryGb !== undefined) doc.resources.memoryGb = Math.min(doc.resources.memoryGb, 4);
    else doc.resources.memoryMb = Math.min(doc.resources.memoryMb ?? 4096, 4096);
    doc.maxRuntimeMin = Math.min(doc.maxRuntimeMin ?? 120, 120);
    return { url: `${API}${u.pathname}${u.search}`, body: Buffer.from(JSON.stringify(doc)), auth: true };
  }
  if (u.pathname === '/api/v1/sandboxes' && job.method === 'GET') {
    u.searchParams.append('label', `oc-e2e=${RUN}`);
    return { url: `${API}${u.pathname}?${u.searchParams}`, body: undefined, auth: true };
  }
  const one = /^\/api\/v1\/sandboxes\/(sbx_[\w]+)(\/(pause|resume|fork))?$/.exec(u.pathname);
  if (one) {
    if (!ours.has(one[1])) return { answer: deny(`a sandbox this run did not make: ${one[1]}`) };
    const verb = one[3];
    if (verb === undefined && !['GET', 'DELETE'].includes(job.method)) return { answer: deny(`${job.method} on a sandbox`) };
    if (verb && job.method !== 'POST') return { answer: deny(`${job.method} ${verb}`) };
    let b = job.method === 'GET' || job.method === 'DELETE' ? undefined : body;
    if (verb === 'fork') {
      const doc = JSON.parse(body.toString() || '{}');
      doc.labels = { ...(doc.labels ?? {}), ...EXTRA };
      doc.count = Math.min(doc.count ?? 1, 1);
      b = Buffer.from(JSON.stringify(doc));
    }
    return { url: `${API}${u.pathname}${u.search}`, body: b, auth: true };
  }
  return { answer: deny(`not a call the plugin needs: ${job.method} ${u.pathname}`) };
}

/** Each job id runs once: a repeat (the relay resends what it has no answer for) gets the same reply. */
const jobs = new Map();

async function handle(job) {
  const prior = jobs.get(job.id);
  if (prior === 'running') return;
  if (prior) return send(job.id, prior);
  jobs.set(job.id, 'running');
  const started = Date.now();
  const p = plan(job);
  let answer = p.answer;
  if (!answer) {
    const headers = { ...job.headers };
    for (const k of Object.keys(headers)) if (/^(authorization|prized-key|cookie|host)$/i.test(k)) delete headers[k];
    if (p.auth) headers.authorization = `Bearer ${KEY}`;
    try {
      const body = job.method === 'GET' || job.method === 'HEAD' || !p.body?.length ? undefined : p.body;
      const r = await fetch(p.url, { method: job.method, headers, body, redirect: 'manual' });
      const buf = Buffer.from(await r.arrayBuffer());
      if ((r.headers.get('content-type') ?? '').includes('json')) {
        try {
          remember(JSON.parse(buf.toString()));
        } catch {
          // not JSON after all
        }
      }
      answer = { status: r.status, headers: Object.fromEntries(r.headers), body: buf };
    } catch (err) {
      answer = { status: 502, headers: { 'content-type': 'application/json' }, body: Buffer.from(JSON.stringify({ error: { code: 'broker_upstream', message: String(err) } })) };
    }
  }
  log({ kind: 'call', method: job.method, path: new URL(job.path, 'http://r').pathname, status: answer.status, ms: Date.now() - started, denied: !!p.answer });
  const payload = JSON.stringify({ status: answer.status, headers: answer.headers, body: answer.body.toString('base64') });
  jobs.set(job.id, payload);
  await send(job.id, payload);
}

/** A reply that does not arrive leaves the plugin's request hanging: try again (the edge rate-limits bursts). */
async function send(id, payload) {
  for (let attempt = 0; attempt < 12; attempt += 1) {
    const r = await fetch(`${RELAY}/reply/${id}`, { method: 'POST', headers: { ...relayHeaders, 'content-type': 'application/json' }, body: payload, redirect: 'manual' }).catch((err) => ({ status: 0, err }));
    if (r.status >= 200 && r.status < 300) return;
    log({ kind: 'reply-retry', id, status: r.status, attempt });
    await new Promise((s) => setTimeout(s, Math.min(250 * 2 ** attempt, 4000)));
  }
  log({ kind: 'reply-failed', id });
}

async function poller(n) {
  for (;;) {
    try {
      const r = await fetch(`${RELAY}/next`, { headers: relayHeaders, redirect: 'manual' });
      if (r.status === 204) continue;
      if (r.status !== 200) {
        log({ kind: 'poll', n, status: r.status });
        await new Promise((s) => setTimeout(s, 1000));
        continue;
      }
      void handle(await r.json());
    } catch (err) {
      log({ kind: 'poll-error', n, err: String(err) });
      await new Promise((s) => setTimeout(s, 1000));
    }
  }
}

log({ kind: 'start', run: RUN });
for (let i = 0; i < POLLERS; i += 1) void poller(i);
