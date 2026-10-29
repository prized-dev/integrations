#!/usr/bin/env node
// Copyright 2026 Prized. Licensed under the Apache License, Version 2.0.
//
// Turn one run of the checker into the published results: the ranked JSON and a Markdown index.
//
// usage: node build-index.mjs RUN.json targets.json OUT_DIR
//   writes OUT_DIR/<date>.json and OUT_DIR/<date>.md
//
// RUN.json is what `agent-signup-check.mjs --targets targets.json --out RUN.json` writes.
//
// Corrections. When a reviewer finds a cell the checker gets wrong (prose that matched a pattern
// but means something else), targets.json can carry a correction for that cell:
//   { "check": "limits", "from": "partial", "match": "Spending caps", "url": "https://...",
//     "status": "fail", "note": "why" }
// It applies only while the checker still says `from` on the same match and URL, so a correction
// never outlives the page it was made about: once the platform changes, the checker's verdict
// stands and the correction is reported as stale until someone looks again. Every corrected cell
// says so in its reason and keeps the checker's own verdict beside it.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { normalizeSite } from './agent-signup-check.mjs';

export const TITLE = 'Agent sign-up check: developer platforms';

/** targets.json, with every domain normalized the way the checker names it (lower case, no scheme). */
export function loadTargets(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8')).map((t) => ({ ...t, site: normalizeSite(t.site) }));
}

/** Text a platform controls (a heading, an endpoint, a reason quoting them), made inert in Markdown. */
export const md = (v) =>
  String(v ?? '')
    .replace(/\s+/g, ' ')
    .replace(/[\\`*_[\]<>|#@]/g, (c) => `\\${c}`);
const MARK = { pass: 'P', partial: '~', fail: '.', skipped: '-' };

const byName = (a, b) => {
  const x = a.name.toLowerCase();
  const y = b.name.toLowerCase();
  return x < y ? -1 : x > y ? 1 : 0;
};

/** Ranked by checks passed only; ties share a rank and are listed by name. */
export const rankSort = (rows) => rows.sort((a, b) => b.passed - a.passed || byName(a, b));
export const rankOf = (rows, row) => 1 + rows.filter((r) => r.passed > row.passed).length;

/** Apply targets.json corrections in place. Returns { applied, stale }. */
export function applyCorrections(rows, targetsBySite) {
  const applied = [];
  const stale = [];
  for (const r of rows) {
    for (const fix of targetsBySite.get(r.site)?.corrections ?? []) {
      const c = r.checks.find((x) => x.id === fix.check);
      if (!c) continue;
      const still =
        (!fix.from || c.status === fix.from) && (!fix.match || c.match === fix.match) && (!fix.url || c.url === fix.url);
      if (!still) {
        stale.push({ name: r.name, site: r.site, check: fix.check, expected: fix.from, now: c.status, note: fix.note });
        continue;
      }
      if (c.status === fix.status) continue;
      c.checkerStatus = c.status;
      c.checkerReason = c.reason;
      c.status = fix.status;
      c.reason = `Corrected after review: ${fix.note}. The checker said ${c.checkerStatus}: ${c.checkerReason}`;
      applied.push({ name: r.name, site: r.site, check: fix.check, from: c.checkerStatus, to: fix.status });
    }
    r.passed = r.checks.filter((c) => c.status === 'pass').length;
    r.partial = r.checks.filter((c) => c.status === 'partial').length;
  }
  return { applied, stale };
}

/** The published document for one run. */
export function publish(run, targets) {
  const targetsBySite = new Map(targets.map((t) => [t.site, t]));
  const rows = structuredClone(run.results.filter((r) => !r.error));
  const errors = run.results.filter((r) => r.error).map((r) => ({ name: r.name, site: r.site, error: r.error }));
  const { applied, stale } = applyCorrections(rows, targetsBySite);
  rankSort(rows);
  const doc = {
    title: TITLE.replace('developer platforms', `${rows.length} developer platforms`),
    date: run.generatedAt.slice(0, 10),
    checker: run.checker,
    userAgent: run.userAgent ?? null,
    generatedAt: run.generatedAt,
    checks: run.checks,
    results: rows.map((r) => {
      const t = targetsBySite.get(r.site) ?? {};
      return {
        name: r.name,
        category: r.category,
        site: r.site,
        apiProbed: t.api ?? null,
        apiNotProbed: t.skipApi ?? null,
        passed: r.passed,
        partial: r.partial,
        total: r.total,
        whoActs: r.whoActs ?? null,
        checkedAt: r.checkedAt,
        checks: r.checks,
        requests: r.requests,
      };
    }),
  };
  if (errors.length) doc.errors = errors;
  return { doc, applied, stale };
}

const count = (rows, id, status) => rows.filter((r) => r.checks.find((c) => c.id === id)?.status === status).length;

/** The Markdown index: top line, the checks, the ranked table, the method. Every number is computed. */
export function indexMarkdown(doc, { applied = [], stale = [], diffPath = null } = {}) {
  const rows = doc.results;
  const n = rows.length;
  const L = [];
  const best = rows[0];
  const full = rows.filter((r) => r.passed === r.total);
  const dist = new Map();
  for (const r of rows) dist.set(r.passed, (dist.get(r.passed) ?? 0) + 1);
  L.push(`# Agent sign-up check: ${n} developer platforms (${doc.date})`, '');
  L.push(
    'Can an AI agent sign up for, fund and use a developer platform on its own? Seven checks, each read from public discovery documents with plain GET requests. The checker is one dependency-free Node file; anyone can run it on their own domain.',
    '',
  );
  L.push(`Run: ${doc.generatedAt} UTC, checker \`${doc.checker}\`, User-Agent \`${doc.userAgent}\`.`, '');
  if (diffPath) L.push(`What changed since the last run: [${path.basename(diffPath)}](${diffPath}).`, '');
  L.push('## Top line', '');
  const tops = rows.filter((r) => r.passed === best.passed).map((r) => r.name);
  L.push(
    `- Platforms checked: ${n}. ${full.length ? `${full.map((r) => r.name).join(', ')} pass every check they were given.` : 'None passes all seven checks.'} The best score is ${best.passed} of ${doc.checks.length} (${tops.join(', ')}).`,
  );
  for (const c of doc.checks) {
    const skipped = count(rows, c.id, 'skipped');
    L.push(
      `- ${c.name}: ${count(rows, c.id, 'pass')} pass, ${count(rows, c.id, 'partial')} partly, ${count(rows, c.id, 'fail')} fail${skipped ? ` (not run on ${skipped})` : ''}, of ${n - skipped}.`,
    );
  }
  L.push(`- Score distribution (checks passed: platforms): ${[...dist.keys()].sort((a, b) => a - b).map((k) => `${k}: ${dist.get(k)}`).join(', ')}`);
  const prized = rows.find((r) => r.site === 'prized.dev');
  if (prized) {
    const tied = rows.filter((r) => r.passed === prized.passed).length;
    const fails = prized.checks.filter((c) => c.status === 'fail').map((c) => c.name);
    const parts = prized.checks.filter((c) => c.status === 'partial').map((c) => c.name);
    L.push(
      `- Prized, scored by the same checker: ${prized.passed} of ${prized.total}, ${tied > 1 ? `tied for rank ${rankOf(rows, prized)} with ${tied - 1} others` : `rank ${rankOf(rows, prized)}`} of ${n}. It fails: ${fails.join(', ') || 'none'}; partly: ${parts.join(', ') || 'none'}.`,
    );
  }
  L.push('', '## The checks', '');
  L.push(
    'Each check is pass, partly or fail. "Partly" means the platform has the thing in prose an agent can read, or in a shape the spec says a client must not use, but not in the machine-readable form the spec defines.',
    '',
  );
  doc.checks.forEach((c, i) => L.push(`${i + 1}. **${c.name}** (\`${c.id}\`): ${c.question} Spec: [${c.spec}](${c.specUrl}).`));
  L.push('', '## Results', '');
  L.push(
    'P pass, ~ partly, . fail, - not run. Ranked by checks passed; ties share a rank and are listed by name ("partly" never breaks a tie). Every cell has a reason and the URL it came from in the JSON file of the same date.',
    '',
  );
  L.push(`| # | Platform | Category | Score | ${doc.checks.map((c) => c.name).join(' | ')} | Who acts at sign-up |`);
  L.push(`|---|---|---|---|${'---|'.repeat(doc.checks.length)}---|`);
  for (const r of rows) {
    const score = `${r.passed}/${r.total}${r.partial ? ` +${r.partial}~` : ''}`;
    const kept = r.notChecked ? ` (not checked ${r.notChecked.date}; from ${r.notChecked.keptFrom})` : '';
    L.push(`| ${rankOf(rows, r)} | ${md(r.name)} (${md(r.site)})${kept} | ${md(r.category)} | ${score} | ${r.checks.map((c) => MARK[c.status] ?? c.status).join(' | ')} | ${md(r.whoActs)} |`);
  }
  L.push('', '## Method', '');
  L.push(`- One run of \`agent-signup-check.mjs\` over the ${n} platforms in \`targets.json\`, 4 platforms at a time, 300 ms between requests to the same platform. The full method is in [METHOD.md](../METHOD.md).`);
  L.push('- Read-only: GET requests only, to public documents meant for agents and to one documented API URL per platform without a key. No sign-ups, no POSTs, no forms, no credentials. At most 16 GETs per platform, redirect hops included.');
  L.push(
    `- ${applied.length} cell(s) corrected by hand after review, each marked "Corrected after review" in the JSON with the checker's own verdict beside it${applied.length ? `: ${applied.map((a) => `${a.name} ${a.check} (${a.from} to ${a.to})`).join('; ')}` : ''}.`,
  );
  if (stale.length) {
    L.push(`- ${stale.length} correction(s) no longer apply, because the checker's verdict changed: ${stale.map((s) => `${s.name} ${s.check} (was ${s.expected}, now ${s.now})`).join('; ')}. The checker's verdict stands until someone reviews them.`);
  }
  if (doc.errors?.length) L.push(`- Not checked, and no earlier row to keep: ${doc.errors.map((e) => `${md(e.name)} (${md(e.error)})`).join(', ')}.`);
  const kept = rows.filter((r) => r.notChecked);
  if (kept.length) L.push(`- Not checked this run, so their last row is kept: ${kept.map((r) => `${md(r.name)} (${md(r.notChecked.reason)})`).join(', ')}.`);
  L.push('- A check sees what a platform publishes, not what its sign-up does after a POST. Results change when platforms change their files; this is one dated run.');
  L.push('');
  return L.join('\n');
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invoked) {
  const [runPath, targetsPath, outDir] = process.argv.slice(2);
  if (!runPath || !targetsPath || !outDir) {
    console.error('usage: node build-index.mjs RUN.json targets.json OUT_DIR');
    process.exit(2);
  }
  const run = JSON.parse(fs.readFileSync(runPath, 'utf8'));
  const targets = loadTargets(targetsPath);
  const { doc, applied, stale } = publish(run, targets);
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, `${doc.date}.json`), `${JSON.stringify(doc, null, 1)}\n`);
  fs.writeFileSync(path.join(outDir, `${doc.date}.md`), indexMarkdown(doc, { applied, stale }));
  console.log(`${doc.results.length} rows; best ${doc.results[0].name} ${doc.results[0].passed}/7; ${applied.length} corrected, ${stale.length} stale -> ${outDir}/${doc.date}.{json,md}`);
}
