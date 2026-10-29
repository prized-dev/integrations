#!/usr/bin/env node
// Copyright 2026 Prized. Licensed under the Apache License, Version 2.0.
//
// The weekly re-run: check every platform in targets.json again, publish the run, and write down
// what changed since the last one.
//
//   node agent-signup-check/weekly.mjs            # from the repo root, or any directory
//
// Writes, in this folder:
//   results/<date>.json   the run, ranked, every reason and request (build-index.mjs)
//   results/<date>.md     the same as a Markdown table
//   results.json          a copy of the newest results/<date>.json
//   diffs/<date>.md       who improved, who regressed, what else moved, since the previous run
//   diffs/<date>.json     the same, for scripts
//   evidence/             every response of this run, scrubbed (scrub.mjs); git keeps the old ones
// and prints a one-paragraph summary. It commits nothing.
//
// Options:
//   --date YYYY-MM-DD   name the run (default: today, UTC)
//   --client-ip ADDR    this machine's public address, to redact from evidence (repeatable).
//                       Default: asked once of https://checkip.amazonaws.com. Without one the run
//                       stops before writing anything, so evidence never goes out unscrubbed.
//   --out DIR           write somewhere else (default: this folder)
//   --replay DIR        answer every request from saved evidence instead of the network (tests)
//   --concurrency N     platforms at a time (default 4, at most 8)
//
// Exit codes: 0 written; 1 written, with warnings to read (replay mismatches, stale corrections);
// 2 nothing written (bad arguments, no client address, or too many platforms unreachable, which
// means this machine's network is the problem, not the platforms).

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { CHECKS, checkSite, guardedGet, isPublicAddress, saveEvidence, USER_AGENT, VERSION } from './agent-signup-check.mjs';
import { indexMarkdown, publish, rankOf } from './build-index.mjs';
import { loadSite, replay, siteDir } from './replay.mjs';
import { auditDir, scrubDir } from './scrub.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const MAX_UNREACHABLE = 10;
const NETWORK_ERROR = (e) => e && e !== 'request budget spent' && !/^not in saved evidence$/.test(e);

const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const writeJson = (p, v) => fs.writeFileSync(p, `${JSON.stringify(v, null, 1)}\n`);

/** The newest published run dated before `date`, or null. */
export function previousRun(outDir, date) {
  const dir = path.join(outDir, 'results');
  if (!fs.existsSync(dir)) return null;
  const dates = fs
    .readdirSync(dir)
    .map((f) => /^(\d{4}-\d{2}-\d{2})\.json$/.exec(f)?.[1])
    .filter((d) => d && d < date)
    .sort();
  return dates.length ? readJson(path.join(dir, `${dates.at(-1)}.json`)) : null;
}

const word = { pass: 'pass', partial: 'partly', fail: 'fail', skipped: 'not run' };

/** What moved between two published runs. */
export function diffRuns(prev, next) {
  const before = new Map((prev?.results ?? []).map((r) => [r.site, r]));
  const after = new Map(next.results.map((r) => [r.site, r]));
  const out = { from: prev?.date ?? null, to: next.date, checkerFrom: prev?.checker ?? null, checkerTo: next.checker, improved: [], regressed: [], changed: [], added: [], removed: [] };
  for (const r of next.results) {
    const p = before.get(r.site);
    if (!p) {
      out.added.push({ name: r.name, site: r.site, score: `${r.passed}/${r.total}` });
      continue;
    }
    const cells = [];
    for (const c of r.checks) {
      const pc = p.checks.find((x) => x.id === c.id);
      if (pc && pc.status !== c.status) cells.push({ check: c.id, name: c.name, from: pc.status, to: c.status, reason: c.reason });
    }
    if (!cells.length && p.total === r.total) continue;
    const entry = {
      name: r.name,
      site: r.site,
      from: `${p.passed}/${p.total}`,
      to: `${r.passed}/${r.total}`,
      rankFrom: rankOf(prev.results, p),
      rankTo: rankOf(next.results, r),
      cells,
    };
    if (r.passed > p.passed) out.improved.push(entry);
    else if (r.passed < p.passed) out.regressed.push(entry);
    else out.changed.push(entry);
  }
  for (const p of prev?.results ?? []) if (!after.has(p.site)) out.removed.push({ name: p.name, site: p.site });
  const byMove = (a, b) => Math.abs(Number(b.to.split('/')[0]) - Number(b.from.split('/')[0])) - Math.abs(Number(a.to.split('/')[0]) - Number(a.from.split('/')[0])) || a.name.localeCompare(b.name);
  out.improved.sort(byMove);
  out.regressed.sort(byMove);
  out.changed.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}

/** The diff as Markdown. */
export function diffMarkdown(d, { stale = [], mismatches = [], retried = [], errors = [] } = {}) {
  const L = [];
  L.push(`# Agent sign-up check: what changed, ${d.from ?? 'first run'} to ${d.to}`, '');
  L.push(`Checker \`${d.checkerTo}\`${d.checkerFrom && d.checkerFrom !== d.checkerTo ? ` (the previous run used \`${d.checkerFrom}\`; a checker change can move rows on its own)` : ''}. Full results: [results/${d.to}.md](../results/${d.to}.md).`, '');
  const cell = (c) => `${c.name} ${word[c.from] ?? c.from} to ${word[c.to] ?? c.to} (${c.reason})`;
  const row = (e) => `- **${e.name}** (${e.site}): ${e.from} to ${e.to}, rank ${e.rankFrom} to ${e.rankTo}.${e.cells.length ? ` ${e.cells.map(cell).join('; ')}.` : ''}`;
  const section = (title, list, fmt) => {
    L.push(`## ${title} (${list.length})`, '');
    if (list.length) L.push(...list.map(fmt));
    else L.push('None.');
    L.push('');
  };
  section('Improved', d.improved, row);
  section('Regressed', d.regressed, row);
  section('Other changes (same number of passes)', d.changed, row);
  if (d.added.length) section('Added', d.added, (a) => `- ${a.name} (${a.site}): ${a.score}`);
  if (d.removed.length) section('Removed', d.removed, (a) => `- ${a.name} (${a.site})`);
  if (stale.length) section('Corrections that no longer apply (review them)', stale, (s) => `- ${s.name} ${s.check}: the correction expected the checker to say ${word[s.expected] ?? s.expected}; it now says ${word[s.now] ?? s.now}. Its note: ${s.note}.`);
  if (retried.length) section('Checked twice (a request failed the first time)', retried, (r) => `- ${r.name} (${r.site}): ${r.kept}`);
  if (errors.length) section('Requests that failed in the published result', errors, (e) => `- ${e.name} (${e.site}): ${e.label} ${e.url}: ${e.error}`);
  if (mismatches.length) section('Replay mismatches (scrubbed evidence scores differently from the live run; look before publishing)', mismatches, (m) => `- ${m}`);
  return L.join('\n');
}

/** Ask once what this machine's public IPv4 address is. */
async function learnClientIp() {
  const r = await guardedGet('https://checkip.amazonaws.com/', { accept: 'text/plain', timeoutMs: 5000 });
  const ip = (r.body || '').trim();
  return r.status === 200 && /^\d{1,3}(?:\.\d{1,3}){3}$/.test(ip) && isPublicAddress(ip) ? ip : null;
}

async function runAll(targets, { getFor, concurrency, pauseMs }) {
  const out = new Array(targets.length);
  let next = 0;
  const one = async (t) => {
    try {
      return await checkSite(t.site, { api: t.api || undefined, apiAccept: t.apiAccept || undefined, docs: t.docs || undefined, skipApi: t.skipApi || undefined, pauseMs, get: getFor(t) });
    } catch (e) {
      return { site: t.site, error: String((e && e.message) || e).slice(0, 200) };
    }
  };
  const worker = async () => {
    while (next < targets.length) {
      const i = next++;
      out[i] = await one(targets[i]);
    }
  };
  await Promise.all(Array.from({ length: concurrency }, worker));
  return { out, one };
}

const failedRequests = (r) => (r._raw || []).filter((q) => NETWORK_ERROR(q.error));
const unreachable = (r) => r.error || ((r._raw || []).length > 0 && (r._raw || []).every((q) => NETWORK_ERROR(q.error)));

export async function weekly(opts) {
  const root = opts.root ?? HERE;
  const outDir = path.resolve(opts.out ?? root);
  const date = opts.date ?? new Date().toISOString().slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw Object.assign(new Error(`bad --date ${date}`), { exit: 2 });
  const targets = readJson(path.join(root, 'targets.json'));
  const concurrency = Math.max(1, Math.min(8, Number(opts.concurrency) || 4));
  const log = opts.log ?? ((s) => process.stderr.write(`${s}\n`));

  // The network by default (the checker's guarded GET); saved evidence with --replay.
  let getFor = opts.getFor ?? (() => undefined);
  let clientIps = opts.clientIps ?? [];
  if (opts.replay) {
    getFor = (t) => {
      const saved = loadSite(path.join(opts.replay, siteDir(t.site)));
      return async (url) => saved.get(url) || { url, finalUrl: url, hops: [], error: 'not in saved evidence' };
    };
  } else if (!clientIps.length) {
    const ip = await (opts.learnClientIp ?? learnClientIp)();
    if (!ip) throw Object.assign(new Error('could not learn this machine\'s public address; pass --client-ip so evidence can be scrubbed'), { exit: 2 });
    clientIps = [ip];
  }

  // 1. The run.
  const startedAt = new Date().toISOString();
  log(`checking ${targets.length} platforms, ${concurrency} at a time`);
  const { out, one } = await runAll(targets, { getFor, concurrency, pauseMs: opts.replay || opts.getFor ? 0 : 300 });
  const down = targets.filter((_, i) => unreachable(out[i]));
  if (down.length > MAX_UNREACHABLE) {
    throw Object.assign(new Error(`${down.length} platforms unreachable (${down.slice(0, 8).map((t) => t.site).join(', ')}...): this machine's network looks down; nothing written`), { exit: 2 });
  }

  // 2. Once more, alone, for any platform where a request failed on the network.
  const retried = [];
  for (let i = 0; i < targets.length; i++) {
    if (opts.replay || opts.getFor || out[i].error || !failedRequests(out[i]).length) continue;
    const again = await one(targets[i]);
    const better = !again.error && failedRequests(again).length < failedRequests(out[i]).length;
    retried.push({ name: targets[i].name, site: targets[i].site, kept: better ? 'kept the second check' : 'kept the first check (the second was no better)' });
    if (better) out[i] = again;
  }

  // 3. Evidence, saved raw to a private temp dir, scrubbed into a second one, then copied into place.
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'asc-weekly-'));
  try {
    const raw = path.join(tmp, 'raw');
    const staged = path.join(tmp, 'scrubbed');
    for (const r of out) if (!r.error) saveEvidence(raw, r);
    const counts = scrubDir(raw, staged, { clientIps });
    const problems = auditDir(staged, { clientIps });
    if (problems.length) {
      throw Object.assign(new Error(`scrubbed evidence still has: ${problems.slice(0, 5).join('; ')}; nothing written`), { exit: 2 });
    }
    log(`evidence: ${counts.files} files, ${counts.trimmed} HTML bodies trimmed, ${counts.redacted} redacted`);

    // 4. The published run.
    const results = out.map((r, i) => {
      const t = targets[i];
      if (r.error) return { name: t.name || t.site, category: t.category || null, site: t.site, error: r.error };
      const { _raw, ...clean } = r;
      return { name: t.name || t.site, category: t.category || null, ...clean };
    });
    const run = { checker: `agent-signup-check/${VERSION}`, userAgent: USER_AGENT, generatedAt: startedAt, checks: CHECKS, results };
    const { doc, applied, stale } = publish(run, targets);
    doc.date = date;

    // 5. The scrubbed evidence must score the same as the live run.
    const mismatches = [];
    const replayed = await replay(targets.filter((_, i) => !out[i].error), staged);
    for (const rr of replayed) {
      const live = results.find((x) => x.site === rr.site);
      const a = live.checks.map((c) => `${c.id}:${c.status}`).join(' ');
      const b = rr.checks.map((c) => `${c.id}:${c.status}`).join(' ');
      if (a !== b) mismatches.push(`${rr.site}: live ${a}; replay ${b}`);
    }

    // 6. The diff against the newest earlier run.
    const prev = previousRun(outDir, date);
    const d = diffRuns(prev, doc);
    const errors = doc.results.flatMap((r) => r.requests.filter((q) => NETWORK_ERROR(q.error)).map((q) => ({ name: r.name, site: r.site, label: q.label, url: q.url, error: q.error })));
    const diffRel = `../diffs/${date}.md`;

    // 7. Write.
    for (const dir of ['results', 'diffs']) fs.mkdirSync(path.join(outDir, dir), { recursive: true });
    writeJson(path.join(outDir, 'results', `${date}.json`), doc);
    fs.writeFileSync(path.join(outDir, 'results', `${date}.md`), indexMarkdown(doc, { applied, stale, diffPath: diffRel }));
    writeJson(path.join(outDir, 'results.json'), doc);
    fs.writeFileSync(path.join(outDir, 'diffs', `${date}.md`), `${diffMarkdown(d, { stale, mismatches, retried, errors })}\n`);
    writeJson(path.join(outDir, 'diffs', `${date}.json`), { ...d, stale, mismatches, retried, errors });
    const evidence = path.join(outDir, 'evidence');
    fs.rmSync(evidence, { recursive: true, force: true });
    fs.cpSync(staged, evidence, { recursive: true });

    const summary =
      `${date}: ${doc.results.length} platforms, best ${doc.results[0].name} ${doc.results[0].passed}/7. ` +
      `Since ${d.from ?? 'nothing'}: ${d.improved.length} improved${d.improved.length ? ` (${d.improved.map((e) => `${e.name} ${e.from} to ${e.to}`).join(', ')})` : ''}, ` +
      `${d.regressed.length} regressed${d.regressed.length ? ` (${d.regressed.map((e) => `${e.name} ${e.from} to ${e.to}`).join(', ')})` : ''}, ` +
      `${d.changed.length} other changes, ${d.added.length} added, ${d.removed.length} removed. ` +
      `${applied.length} corrected, ${stale.length} stale corrections, ${mismatches.length} replay mismatches, ${errors.length} failed requests.`;
    return { summary, warnings: stale.length + mismatches.length, doc, diff: d };
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invoked) {
  const argv = process.argv.slice(2);
  const opts = { clientIps: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--client-ip') opts.clientIps.push(argv[++i]);
    else if (['--date', '--out', '--replay', '--concurrency'].includes(a)) opts[a.slice(2)] = argv[++i];
    else {
      console.error(`unknown argument ${a}\nusage: node weekly.mjs [--date YYYY-MM-DD] [--client-ip ADDR] [--out DIR] [--replay DIR] [--concurrency N]`);
      process.exit(2);
    }
  }
  weekly(opts).then(
    ({ summary, warnings }) => {
      console.log(summary);
      process.exit(warnings ? 1 : 0);
    },
    (e) => {
      console.error(e.message || e);
      process.exit(e.exit ?? 2);
    },
  );
}
