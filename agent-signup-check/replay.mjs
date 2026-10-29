#!/usr/bin/env node
// Copyright 2026 Prized. Licensed under the Apache License, Version 2.0.
//
// Re-score saved evidence with the current checker, offline (no network).
//
// usage: node replay.mjs targets.json evidence/ out.json
//
// Every request is answered from the evidence files instead of the network. A URL the saved run
// never fetched answers as a network error, so a replay can lose checks that need new requests,
// never invent a pass. Use it to see what a checker change does to a past run, or to check a
// published row against the responses it came from.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { checkSite, CHECKS, VERSION } from './agent-signup-check.mjs';
import { bodyOf, parseEvidence } from './scrub.mjs';

/** The saved responses for one site, keyed by the URL first requested. */
export function loadSite(dir) {
  const map = new Map();
  if (!fs.existsSync(dir)) return map;
  for (const f of fs.readdirSync(dir).filter((x) => x.endsWith('.http')).sort()) {
    const raw = fs.readFileSync(path.join(dir, f), 'utf8');
    const { request, response } = parseEvidence(raw);
    const url = request.split('\n')[0].replace(/^GET /, '');
    const finalUrl = (request.match(/^final-url: (.*)$/m) || [])[1] || url;
    const hops = [...request.matchAll(/^redirect: (\d+) from (.*)$/gm)].map((m) => ({ status: Number(m[1]), url: m[2] }));
    const err = (response.match(/^error: (.*)$/m) || [])[1];
    const statusM = response.match(/^HTTP (\d+)$/m);
    const headers = {};
    for (const line of response.split('\n')) {
      const m = line.match(/^([a-z0-9-]+): (.*)$/);
      if (m && m[1] !== 'error') headers[m[1]] = m[2];
    }
    map.set(
      url,
      statusM
        ? { url, finalUrl, hops, status: Number(statusM[1]), headers, body: bodyOf(raw) }
        : { url, finalUrl, hops, error: err || 'not saved' },
    );
  }
  return map;
}

export const siteDir = (site) => site.replace(/[^a-z0-9.-]/g, '_');

/** Re-score every target from evidence/<site>/. Results carry no raw bodies. */
export async function replay(targets, evDir) {
  const results = [];
  for (const t of targets) {
    const saved = loadSite(path.join(evDir, siteDir(t.site)));
    const get = async (url) => saved.get(url) || { url, finalUrl: url, hops: [], error: 'not in saved evidence' };
    const r = await checkSite(t.site, { api: t.api, apiAccept: t.apiAccept, docs: t.docs, skipApi: t.skipApi, get });
    const { _raw, ...clean } = r;
    results.push({ name: t.name, category: t.category, ...clean });
  }
  return results;
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invoked) {
  const [targetsPath, evDir, outPath] = process.argv.slice(2);
  if (!targetsPath || !evDir || !outPath) {
    console.error('usage: node replay.mjs targets.json evidence/ out.json');
    process.exit(2);
  }
  const targets = JSON.parse(fs.readFileSync(targetsPath, 'utf8'));
  const results = await replay(targets, evDir);
  const doc = { checker: `agent-signup-check/${VERSION}`, replayOf: evDir, generatedAt: new Date().toISOString(), checks: CHECKS, results };
  fs.writeFileSync(outPath, JSON.stringify(doc, null, 1));
  console.log(`replayed ${results.length} sites -> ${outPath}`);
}
