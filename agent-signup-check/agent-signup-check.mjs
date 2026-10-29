#!/usr/bin/env node
// Agent sign-up check, v0.2.1. Prized (https://prized.dev/agent-signup-check).
// Copyright 2026 Prized. Licensed under the Apache License, Version 2.0:
// https://www.apache.org/licenses/LICENSE-2.0
//
// Can an AI agent sign up for, fund and use this developer platform on its own?
// Seven checks, each read from public discovery documents with plain GET requests:
//
//   authmd    /auth.md in the shape of WorkOS's auth.md spec (AUTH.md v0.6.0)
//   prm       OAuth Protected Resource Metadata (RFC 9728)
//   register  an agent registration endpoint: auth.md's agent_auth block in RFC 8414
//             metadata, or a documented sign-up API in auth.md or llms.txt
//   challenge a signed-out API call answers 401 with WWW-Authenticate (RFC 9110 15.5.2)
//   limits    spend limits an agent can read (auth-md-spend-limits 0.1, AAuth budgets)
//   payment   a payment handoff an agent can act on (402: x402, the Payment auth scheme,
//             auth-md-spend-limits billing)
//   markdown  docs an agent can read: /llms.txt (llmstxt.org)
//
// Read-only by construction: GET only, never a POST, never a sign-up, at most 16 GETs a site,
// redirect hops included (usually 7 to 9), https on port 443 only, public IP addresses only (checked on every connection,
// redirects included), 1 MB and 8 s per response.
//
// Usage:
//   node agent-signup-check.mjs example.com [--api https://api.example.com/v1/me] [--docs URL] [--json] [--save DIR]
//   node agent-signup-check.mjs --targets targets.json --out results.json [--save DIR] [--concurrency 4]
//
// Needs Node 18 or later. No dependencies.

/* global URL, Buffer, setTimeout, clearTimeout, console, process */

import dns from 'node:dns';
import fs from 'node:fs';
import https from 'node:https';
import net from 'node:net';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import zlib from 'node:zlib';

export const VERSION = '0.2.1';
export const USER_AGENT = `PrizedAgentSignupCheck/${VERSION} (+https://prized.dev/agent-signup-check; read-only GET)`;
export const MAX_BYTES = 1024 * 1024;
export const TIMEOUT_MS = 8000;
export const MAX_REDIRECTS = 4;
export const MAX_REQUESTS = 16;

export const CHECKS = [
  {
    id: 'authmd',
    name: 'auth.md',
    question: 'Does /auth.md tell an agent how to register, in the shape of the auth.md spec?',
    spec: 'WorkOS auth.md, AUTH.md v0.6.0',
    specUrl: 'https://github.com/workos/auth.md/blob/main/AUTH.md',
  },
  {
    id: 'prm',
    name: 'Protected resource metadata',
    question: 'Is there RFC 9728 metadata that names the authorization server?',
    spec: 'RFC 9728, sections 2, 3 and 3.3',
    specUrl: 'https://www.rfc-editor.org/rfc/rfc9728',
  },
  {
    id: 'register',
    name: 'Sign-up by API',
    question: 'Can an agent find an API endpoint that creates an account, with no web form or CAPTCHA?',
    spec: 'auth.md Step 1b (agent_auth in RFC 8414 metadata)',
    specUrl: 'https://github.com/workos/auth.md/blob/main/AUTH.md#1b-fetch-the-authorization-server-metadata',
  },
  {
    id: 'challenge',
    name: 'Key works without a browser',
    question: 'Does a signed-out API call get a 401 with a WWW-Authenticate challenge, not a login page or a bot wall?',
    spec: 'RFC 9110 section 15.5.2; RFC 6750 section 3; RFC 9728 section 5.1',
    specUrl: 'https://www.rfc-editor.org/rfc/rfc9110#section-15.5.2',
  },
  {
    id: 'limits',
    name: 'Spend limits',
    question: 'Can an agent read the spend cap its key will have?',
    spec: 'auth-md-spend-limits 0.1 (draft, Prized); draft-hardt-aauth-budgets-00',
    specUrl: 'https://github.com/prized-dev/integrations/blob/auth-md-spend-limits/auth-md-spend-limits/README.md',
  },
  {
    id: 'payment',
    name: 'Payment handoff',
    question: 'When money is needed, is there a machine-readable way for the agent to say so and hand over?',
    spec: 'RFC 9110 section 15.5.3 (402); x402 v2 HTTP transport; draft-httpauth-payment; auth-md-spend-limits section 4',
    specUrl: 'https://github.com/x402-foundation/x402/blob/main/specs/transports-v2/http.md',
  },
  {
    id: 'markdown',
    name: 'Docs in Markdown',
    question: 'Is there an llms.txt an agent can read?',
    spec: 'llms.txt (llmstxt.org)',
    specUrl: 'https://llmstxt.org/',
  },
];

// ---------------------------------------------------------------------------------------
// Network: GET only, https only, public addresses only.

// Two lists: Node matches IPv4 addresses against IPv4-mapped IPv6 rules in a shared list.
const BLOCKED4 = new net.BlockList();
const BLOCKED6 = new net.BlockList();
for (const [a, p] of [
  ['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10], ['127.0.0.0', 8], ['169.254.0.0', 16],
  ['172.16.0.0', 12], ['192.0.0.0', 24], ['192.0.2.0', 24], ['192.88.99.0', 24], ['192.168.0.0', 16],
  ['198.18.0.0', 15], ['198.51.100.0', 24], ['203.0.113.0', 24], ['224.0.0.0', 4], ['240.0.0.0', 4],
]) BLOCKED4.addSubnet(a, p, 'ipv4');
for (const [a, p] of [
  ['::', 96], ['::ffff:0:0', 96], ['::ffff:0:0:0', 96], ['64:ff9b:1::', 48], ['100::', 64],
  ['2001::', 23], ['2001:db8::', 32], ['2002::', 16], ['3fff::', 20], ['5f00::', 16],
  ['fc00::', 7], ['fe80::', 10], ['fec0::', 10], ['ff00::', 8],
]) BLOCKED6.addSubnet(a, p, 'ipv6');
const NAT64 = new net.BlockList();
NAT64.addSubnet('64:ff9b::', 96, 'ipv6');

/** The IPv4 address in the last 32 bits of an IPv6 address. */
function embeddedV4(address) {
  const canon = new URL(`http://[${address}]/`).hostname.slice(1, -1);
  const tail = canon.split(':').slice(-2);
  const hex = tail.map((g) => g.padStart(4, '0')).join('');
  return [0, 2, 4, 6].map((i) => parseInt(hex.slice(i, i + 2), 16)).join('.');
}

/** True when an IP address is publicly routable (not private, loopback, link-local, reserved...). */
export function isPublicAddress(address) {
  const family = net.isIP(address);
  if (family === 4) return !BLOCKED4.check(address, 'ipv4');
  if (family === 6) {
    // NAT64 (64:ff9b::/96) is judged by the IPv4 address inside it; IPv4-mapped and
    // IPv4-compatible forms are blocked outright.
    if (NAT64.check(address, 'ipv6')) {
      try { return isPublicAddress(embeddedV4(address)); } catch { return false; }
    }
    return !BLOCKED6.check(address, 'ipv6');
  }
  return false;
}

// DNS through c-ares with a timeout, not getaddrinfo: a name server that never answers cannot
// tie up the libuv thread pool, and /etc/hosts is never consulted.
const resolver = new dns.promises.Resolver({ timeout: 3000, tries: 1 });

function guardedLookup(hostname, options, callback) {
  const name = String(hostname).replace(/\.+$/, '');
  Promise.allSettled([resolver.resolve4(name), resolver.resolve6(name)]).then(([v4, v6]) => {
    const list = [
      ...(v4.status === 'fulfilled' ? v4.value.map((address) => ({ address, family: 4 })) : []),
      ...(v6.status === 'fulfilled' ? v6.value.map((address) => ({ address, family: 6 })) : []),
    ];
    if (list.length === 0) return callback(Object.assign(new Error(`no address for ${name}`), { code: 'ENOTFOUND' }));
    // Every address the name has must be public, not just the one we would connect to.
    if (list.some((a) => !isPublicAddress(a.address))) {
      return callback(Object.assign(new Error(`${name} resolves to a non-public address`), { code: 'EBLOCKED' }));
    }
    if (options && options.all) return callback(null, list);
    return callback(null, list[0].address, list[0].family);
  });
}

/** Why a URL may not be fetched, or null when it may. */
export function urlProblem(raw) {
  let u;
  try { u = new URL(raw); } catch { return 'not a URL'; }
  if (u.protocol !== 'https:') return 'only https URLs are fetched';
  if (u.port && u.port !== '443') return 'only port 443 is fetched';
  if (u.username || u.password) return 'URLs with credentials are not fetched';
  const host = u.hostname.replace(/^\[|\]$/g, '').replace(/\.+$/, '');
  if (net.isIP(host)) return isPublicAddress(host) ? null : 'non-public address';
  const labels = host.split('.');
  if (labels.length < 2 || labels.some((l) => !l) || host === 'localhost' ||
      /\.(local|localhost|internal|lan|home|corp|intranet|arpa)$/i.test(host)) {
    return 'not a public host name';
  }
  return null;
}

/**
 * One GET, no redirects followed. The socket is never reused (agent: false), so every
 * connection goes through guardedLookup. Size is capped after decompression and before it,
 * and every stream is destroyed on the way out so a bomb stops inflating.
 */
function requestOnce(url, accept, timeoutMs, signal) {
  return new Promise((resolve) => {
    const started = Date.now();
    const u = new URL(url);
    let settled = false;
    let timer = null;
    let req = null;
    let res = null;
    let stream = null;
    const teardown = () => {
      if (stream && stream !== res) stream.destroy();
      if (res) res.destroy();
      if (req) req.destroy();
    };
    const done = (r) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
      teardown();
      resolve({ ms: Date.now() - started, ...r });
    };
    const onAbort = () => done({ error: 'aborted' });
    if (signal) {
      if (signal.aborted) return done({ error: 'aborted' });
      signal.addEventListener('abort', onAbort, { once: true });
    }
    const host = u.hostname.replace(/^\[|\]$/g, '');
    req = https.request({
      method: 'GET',
      host,
      servername: net.isIP(host) ? undefined : host.replace(/\.+$/, ''),
      port: 443,
      path: u.pathname + u.search,
      agent: false,
      lookup: guardedLookup,
      maxHeaderSize: 32 * 1024,
      headers: {
        'user-agent': USER_AGENT,
        accept,
        'accept-encoding': 'gzip, br, deflate',
        'cache-control': 'no-cache',
        connection: 'close',
      },
    }, (response) => {
      res = response;
      const headers = {};
      for (const [k, v] of Object.entries(res.headers)) headers[k.toLowerCase()] = Array.isArray(v) ? v.join(', ') : String(v);
      const enc = (headers['content-encoding'] || '').trim().toLowerCase();
      stream = res;
      if (enc === 'gzip' || enc === 'x-gzip') stream = res.pipe(zlib.createGunzip());
      else if (enc === 'br') stream = res.pipe(zlib.createBrotliDecompress());
      else if (enc === 'deflate') stream = res.pipe(zlib.createInflate());
      const chunks = [];
      let size = 0;
      let raw = 0;
      let truncated = false;
      const finish = (extra = {}) => done({ status: res.statusCode, headers, body: Buffer.concat(chunks).toString('utf8'), truncated, ...extra });
      if (stream !== res) {
        res.on('data', (c) => {
          raw += c.length;
          if (raw > MAX_BYTES) { truncated = true; finish(); }
        });
      }
      stream.on('data', (c) => {
        if (settled) return;
        size += c.length;
        if (size > MAX_BYTES) {
          chunks.push(c.subarray(0, c.length - (size - MAX_BYTES)));
          truncated = true;
          finish();
        } else chunks.push(c);
      });
      stream.on('end', () => finish());
      stream.on('error', (e) => finish({ error: `incomplete body (${e.code || 'decode error'})` }));
      res.on('aborted', () => finish({ error: 'incomplete body' }));
      res.on('error', (e) => finish({ error: `incomplete body (${e.code || 'reset'})` }));
    });
    timer = setTimeout(() => done({ error: 'timeout' }), timeoutMs);
    req.on('error', (e) => done({ error: e.code || 'request failed' }));
    req.end();
  });
}

/**
 * GET a URL with every guard: https, port 443, public addresses, manual redirects (each hop
 * checked again and counted against the caller's request budget), one time cap across all
 * hops, size caps. Never throws.
 *   budget: { left: number } shared by every fetch of one check
 *   signal: an AbortSignal that ends the fetch at once
 */
export async function guardedGet(url, { accept = '*/*', timeoutMs = TIMEOUT_MS, budget = null, signal = null } = {}) {
  const hops = [];
  const deadline = Date.now() + timeoutMs;
  let current = url;
  for (let i = 0; i <= MAX_REDIRECTS; i++) {
    const problem = urlProblem(current);
    if (problem) return { url, finalUrl: current, hops, error: problem };
    if (signal && signal.aborted) return { url, finalUrl: current, hops, error: 'aborted' };
    if (budget) {
      if (budget.left <= 0) return { url, finalUrl: current, hops, error: 'request budget spent' };
      budget.left -= 1;
    }
    const left = deadline - Date.now();
    if (left <= 0) return { url, finalUrl: current, hops, error: 'timeout' };
    const r = await requestOnce(current, accept, left, signal);
    if (r.error && r.status === undefined) return { url, finalUrl: current, hops, ...r };
    if (r.status >= 300 && r.status < 400 && r.headers.location) {
      hops.push({ url: current, status: r.status });
      try { current = new URL(r.headers.location, current).toString(); } catch { return { url, finalUrl: current, hops, ...r, error: 'bad redirect' }; }
      continue;
    }
    return { url, finalUrl: current, hops, ...r };
  }
  return { url, finalUrl: current, hops, error: 'too many redirects' };
}

// ---------------------------------------------------------------------------------------
// Parsing helpers.

const ok = (r) => r && !r.error && r.status === 200;
const ctype = (r) => ((r && r.headers && r.headers['content-type']) || '').toLowerCase();
const isHtml = (r) => ctype(r).includes('html') || /^\s*(<!doctype|<html|<head|<body|<\?xml)/i.test((r && r.body) || '');
const stripBom = (s) => (s || '').replace(/^\uFEFF/, '');

function parseJson(r) {
  if (!ok(r)) return null;
  try {
    const v = JSON.parse(stripBom(r.body));
    return v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  } catch { return null; }
}

function httpsUrl(v) {
  if (typeof v !== 'string') return null;
  try { const u = new URL(v); return u.protocol === 'https:' ? u.toString() : null; } catch { return null; }
}

// Untrusted text is only ever scanned in linear time: helpers below use indexOf loops, and
// every regex runs on one line at a time (cut to MAX_LINE) with bounded quantifiers.
const MAX_LINE = 2000;
const MAX_SCAN = 512 * 1024;
const str = (v) => (typeof v === 'string' ? v : '');
function trimSlash(v) {
  const t = str(v);
  let e = t.length;
  while (e > 0 && t.charCodeAt(e - 1) === 47) e--;
  return t.slice(0, e);
}
/** Lines of a document with their offsets, each cut to MAX_LINE, the document to MAX_SCAN. */
function linesOf(text) {
  const t = str(text).slice(0, MAX_SCAN);
  const out = [];
  let i = 0;
  while (i <= t.length) {
    let j = t.indexOf('\n', i);
    if (j < 0) j = t.length;
    out.push([t.slice(i, Math.min(j, i + MAX_LINE)), i]);
    if (j >= t.length) break;
    i = j + 1;
  }
  return out;
}
/** Remove spans between open and close markers; an unclosed one hides the rest, as in HTML. */
function stripSpans(text, open, close) {
  let out = '';
  let i = 0;
  for (;;) {
    const a = text.indexOf(open, i);
    if (a < 0) return out + text.slice(i);
    out += text.slice(i, a);
    const b = text.indexOf(close, a + open.length);
    if (b < 0) return out;
    i = b + close.length;
  }
}
const URL_IN_LINE = /https:\/\/[^\s)"'`<>\]\\]{1,500}/g;
function urlsInLine(line) {
  URL_IN_LINE.lastIndex = 0;
  const out = [];
  let m;
  while ((m = URL_IN_LINE.exec(line))) out.push({ url: m[0].replace(/[.,:;!?]+$/, ''), index: m.index });
  return out;
}
/** Markdown links [label](https://...), found from each "](https://" with a short look back. */
function markdownLinks(text) {
  const t = str(text).slice(0, MAX_SCAN);
  const out = [];
  let i = 0;
  while ((i = t.indexOf('](https://', i)) >= 0) {
    const from = Math.max(0, i - 200);
    const open = t.slice(from, i).lastIndexOf('[');
    if (open >= 0) {
      const label = t.slice(from + open + 1, i);
      const close = t.indexOf(')', i + 2);
      const url = t.slice(i + 2, close >= 0 && close - i < 600 ? close : i + 502).split(/\s/)[0];
      if (!label.includes('\n')) out.push({ label, url, index: from + open });
    }
    i += 2;
  }
  return out;
}

/** Bot walls and challenges look like a 403/429/503 with a vendor marker. */
function blockedReason(r) {
  if (!r || r.error) return null;
  const h = r.headers || {};
  if (h['cf-mitigated'] === 'challenge') return 'a Cloudflare bot challenge';
  // Vendor markers of bot-management pages only: a plain 403 (an S3 AccessDenied, a WAF rule)
  // is reported as its status code, not guessed to be a bot wall.
  if ([403, 429, 503].includes(r.status) && /captcha|cf-chl|challenge-platform|just a moment\.\.\.|attention required!? \| cloudflare|px-captcha|perimeterx|datadome|incapsula|are you a robot|verify you are (?:a )?human/i.test((r.body || '').slice(0, 8000))) {
    return `a bot wall (HTTP ${r.status})`;
  }
  return null;
}

function describeMiss(r, what) {
  if (!r) return `${what} was not fetched`;
  if (r.error) return `${what}: ${r.error}`;
  const wall = blockedReason(r);
  const via = r.hops && r.hops.length && r.finalUrl && r.finalUrl !== r.url ? ` (after a redirect to ${r.finalUrl})` : '';
  if (wall) return `${what} answers with ${wall}${via}`;
  if (r.status === 404 || r.status === 410) return `${what} not found (HTTP ${r.status})${via}`;
  if (r.status !== 200) return `${what} answers HTTP ${r.status}${via}`;
  return `${what} is not usable`;
}

/** The first line that is content: skips blank lines, HTML comments and YAML front matter. */
function firstHeading(text) {
  let t = stripSpans(stripBom(str(text)).slice(0, MAX_SCAN), '<!--', '-->');
  const lead = t.length - t.trimStart().length;
  if (t.startsWith('---', lead)) {
    const close = t.indexOf('\n---', lead + 3);
    if (close >= 0) { const nl = t.indexOf('\n', close + 4); t = nl >= 0 ? t.slice(nl + 1) : ''; }
  }
  for (const [raw] of linesOf(t)) {
    const line = raw.trim();
    if (line) return line;
  }
  return '';
}

function snippet(text, index, len = 160) {
  const start = Math.max(0, index - 50);
  return text.slice(start, start + len).replace(/\s+/g, ' ').trim();
}

// Every phrase pattern has only bounded repeats, so it is linear on the whole (capped) text.
function findPhrase(text, patterns) {
  if (!text) return null;
  const t = str(text).slice(0, MAX_SCAN);
  for (const re of patterns) {
    const m = re.exec(t);
    if (m) return { match: m[0].slice(0, 80), snippet: snippet(t, m.index) };
  }
  return null;
}

/** Parse a WWW-Authenticate value into [{scheme, params}]. Tolerant, not a full RFC 9110 parser. */
export function parseChallenges(value) {
  const out = [];
  if (!value) return out;
  // Split on commas that start a new scheme: "Bearer a=1, b=2, Basic realm=x".
  const parts = [];
  let quoted = false;
  let escaped = false;
  let cur = '';
  for (const ch of str(value)) {
    if (quoted && escaped) { escaped = false; cur += ch; continue; }
    if (quoted && ch === '\\') { escaped = true; cur += ch; continue; }
    if (ch === '"') quoted = !quoted;
    if (ch === ',' && !quoted) { parts.push(cur); cur = ''; continue; }
    cur += ch;
  }
  parts.push(cur);
  let challenge = null;
  for (const p of parts.map((s) => s.trim()).filter(Boolean)) {
    const scheme = p.match(/^([A-Za-z0-9!#$%&'*+.^_`|~-]+)(\s+|$)/);
    const isParam = /^[A-Za-z0-9!#$%&'*+.^_`|~-]+\s*=/.test(p);
    if (scheme && !isParam) {
      challenge = { scheme: scheme[1], params: {} };
      out.push(challenge);
      const rest = p.slice(scheme[0].length);
      const pm = rest.match(/^([A-Za-z0-9!#$%&'*+.^_`|~-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^,\s]*)/);
      if (pm) challenge.params[pm[1].toLowerCase()] = pm[2].replace(/^"|"$/g, '').replace(/\\(.)/g, '$1');
      else if (rest.trim()) challenge.params.token68 = rest.trim();
    } else if (challenge && isParam) {
      const pm = p.match(/^([A-Za-z0-9!#$%&'*+.^_`|~-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^,\s]*)/);
      if (pm) challenge.params[pm[1].toLowerCase()] = pm[2].replace(/^"|"$/g, '').replace(/\\(.)/g, '$1');
    }
  }
  return out;
}

/** RFC 8414 section 3.1: the well-known string goes between the host and any path. */
export function asMetadataUrl(issuer) {
  const u = new URL(issuer);
  const p = trimSlash(u.pathname);
  return `${u.origin}/.well-known/oauth-authorization-server${p}`;
}

/** RFC 9728 metadata an agent may use: an https resource that matches where it came from
 *  (section 3.3), and at least one authorization server. */
function prmUsable(prm, via, fetchedUrl, apiUrl) {
  const resource = httpsUrl(prm.resource);
  if (!resource) return false;
  const servers = Array.isArray(prm.authorization_servers) ? prm.authorization_servers.filter((x) => httpsUrl(x)) : [];
  if (!servers.length) return false;
  if (via === 'www-authenticate') return apiUrl ? underResource(apiUrl, resource) : true;
  return [].concat(fetchedUrl).some((u) => u && trimSlash(prmUrlFor(resource)) === trimSlash(u));
}

/** The request URL is the resource or sits under it (at a path boundary). */
function underResource(url, resource) {
  const r = trimSlash(resource);
  const u = str(url);
  return u === r || u.startsWith(`${r}/`) || u.startsWith(`${r}?`);
}

const SECOND_LEVEL = new Set(['co', 'com', 'net', 'org', 'gov', 'edu', 'ac', 'or', 'ne', 'go']);
/** A close guess at the registrable domain (no public suffix list): api.example.co.uk -> example.co.uk. */
export function registrable(host) {
  const labels = str(host).toLowerCase().replace(/\.+$/, '').split('.');
  const n = labels.length >= 3 && labels[labels.length - 1].length === 2 && SECOND_LEVEL.has(labels[labels.length - 2]) ? 3 : 2;
  return labels.slice(-n).join('.');
}
export function sameSite(hostA, hostB) {
  return registrable(hostA) === registrable(hostB);
}

/** The issuer identifier an RFC 8414 metadata URL was built from, or null. */
function issuerFromMetadataUrl(url) {
  try {
    const u = new URL(url);
    const m = u.pathname.match(/^\/\.well-known\/oauth-authorization-server(\/.*)?$/);
    return m ? u.origin + (m[1] || '') : null;
  } catch { return null; }
}

/** RFC 9728 section 3.1, the same rule for protected resources. */
export function prmUrlFor(resource) {
  const u = new URL(resource);
  const p = trimSlash(u.pathname);
  return `${u.origin}/.well-known/oauth-protected-resource${p}`;
}

// Prose an agent reads. Each list is tight on purpose: a match is shown with its snippet.
const LIMIT_PHRASES = [
  /\bspend(?:ing)?[ _-](?:limit|cap|ceiling)s?\b/i,
  /\bcredit[ _-]limits?\b/i,
  /\bbudget[ _-](?:limit|cap)s?\b/i,
  /\b(?:per[ -]key|key)[ -](?:spend|credit|budget|usage)[ -](?:limit|cap)s?\b/i,
  /\blimit_remaining\b/,
  /\bmax(?:imum)?[ _-]spend\b/i,
  /\bdefault_spend_cap\b/,
  /\bspend[ -]capped\b/i,
  /\[[^\]\n]{0,80}\bbudgets?\]\(/i,
  /\busage_limit(?:_type)?\b/,
  /\blimit\b[^.\n]{0,40}\bUSD\b/i,
  /\$\s?\d[\d,.]{0,12}\s{0,3}(?:a |per )?(?:day|week|month)?\s{0,3}(?:spend(?:ing)?\s{1,3})?(?:limit|cap)\b/i,
];
const PAYMENT_PHRASES = [
  /\b402\s+Payment\s+Required\b/i,
  /\b(?:HTTP|status|code)\s*`?402`?/i,
  /`402`/,
  /\bpayment_required\b/i,
  /\bx402\b/i,
  /\bcheckout_link(?:_endpoint)?\b/i,
  /\bmachine payments protocol\b/i,
  /\bbilling_?url\b|\bbillingUrl\b|\bpayUrl\b/,
  /\b(?:payment|checkout)_(?:link|url)\b/,
  /\bbilling_required\b|\bcredit_exhausted\b|\binsufficient_(?:balance|credits?|funds)\b/,
  /\btop(?:s)?[ -]up\b[^.\n]{0,40}\bbalance\b|\bbalance\/add\b/i,
];
const AUTH_WORDS = /api[_ -]?key|access[_ -]?key|accesskeyid|\btoken\b|subscription-token|unauthori[sz]ed|authenticat|credential/i;
const REG_TYPES = [
  ['anonymous', /\banonymous(?:ly)?\b|\baccountless\b|\bclaimable\b/i],
  ['identity_assertion', /\bidentity_assertion\b|\bid-jag\b/i],
  ['service_auth', /\bservice_auth\b|\bverified_email\b|\blogin_hint\b/i],
];
// Says plainly that agents cannot register here.
const DECLINES = new RegExp([
  String.raw`\b(?:does not|doesn['’]t|do not|don['’]t)\s+(?:yet\s+|currently\s+)?(?:support|implement|offer|advertise)\b[^.\n]{0,80}?\b(?:agentic|agent|auth\.md)\b[^.\n]{0,40}?\bregistration`,
  String.raw`\bno agentic registration\b`,
  String.raw`\bagent(?:ic)? registration is not (?:yet )?(?:supported|available|enabled)\b`,
  String.raw`\bis working toward\b[^.\n]{0,60}\bregistration\b`,
].join('|'), 'i');
// POST endpoints in a document. Paths that look like making an account, an identity or a
// provisioned resource count as registration; token, OAuth and product calls do not.
const POST_LINE = /\bPOST\s{1,10}(https:\/\/[^\s"'`)<>\]]{1,500}|\/[^\s"'`)<>\]]{0,500})/;
const REG_PATH = /sign-?up|signup|regist\w*|\/identity\b|onboard\w*|provision\w*|one-shot|accountless\w*|claimable\w*|\/agent[s]?(?:[/._-]|$)/i;
// A bare /register is OAuth dynamic client registration (RFC 7591, the MCP default), not an account.
const NOT_REG_PATH = /oauth|\/token|revoke|authori[sz]e|webhook|\/claim(?:\/|$)|start_claim|\/keys?\b|^\/register\/?$/i;
function registrationPosts(text) {
  const out = [];
  if (!text) return out;
  for (const [line, off] of linesOf(text)) {
    let target = null;
    let at = 0;
    const p = POST_LINE.exec(line);
    if (p) { target = p[1]; at = p.index; }
    else if (/\bcurl\b/.test(line) && /-X\s{0,3}POST\b/.test(line)) {
      const u = urlsInLine(line)[0];
      if (u) { target = u.url; at = u.index; }
    }
    if (!target) continue;
    const m = { index: off + at };
    let pathPart = target;
    try { if (/^https:/.test(target)) pathPart = new URL(target).pathname; } catch { /* keep as is */ }
    if (REG_PATH.test(pathPart) && !NOT_REG_PATH.test(pathPart)) out.push({ endpoint: target, index: m.index });
  }
  return out;
}
// llms.txt documents many product APIs, so only an explicit agent sign-up counts there: a POST
// to an agent sign-up path, or a link to an agent sign-up guide.
const AGENT_WORD = /\bagents?\b/i;
const SIGNUP_WORD = /\bsign[ -]?up\b|\bsignup\b|\bself-register\w*|\bregistration\b/i;
const SIGNUP_URL = /agent[-_]?sign-?up|agents?\/sign-?up/i;
function llmsSignupPosts(text) {
  const posts = registrationPosts(text).filter((p) => /sign-?up|signup|\/agent[s]?\/(?:identity|regist|onboard|provision)|agent[._-]?(?:signup|sign-up|provision|regist)/i.test(p.endpoint));
  if (posts.length) return posts;
  const links = markdownLinks(text).filter((l) => AGENT_WORD.test(l.label) && SIGNUP_WORD.test(l.label));
  if (links.length) return links.map((l) => ({ endpoint: l.url, index: l.index, link: true }));
  // A bare URL to an agent sign-up guide ("follow https://example.com/agent-signup.md").
  for (const [line, off] of linesOf(text)) {
    const u = urlsInLine(line).find((x) => SIGNUP_URL.test(x.url));
    if (u) return [{ endpoint: u.url, index: off + u.index, link: true }];
  }
  return [];
}
const WHO_ANON_RE = /\b(?:anonymous(?:ly)?|accountless|claimable)\b|\bwithout (?:a |an )?account\b/gi;
const NEGATED = /\b(?:not|no|never|doesn['’]t|don['’]t|cannot|unsupported|disabled)\b/i;
/** True when the text offers sign-up with no account: a mention in a sentence that does not negate it. */
function offersAnonymous(text) {
  const t = str(text).slice(0, MAX_SCAN);
  WHO_ANON_RE.lastIndex = 0;
  let m;
  let seen = 0;
  while ((m = WHO_ANON_RE.exec(t)) && seen++ < 50) {
    // The sentence around the match, looked for within 300 characters each way.
    const from = Math.max(0, m.index - 300);
    const before = t.slice(from, m.index);
    const start = from + Math.max(before.lastIndexOf('.'), before.lastIndexOf('\n')) + 1;
    const after = t.slice(m.index, m.index + 300);
    const cut = [after.indexOf('.'), after.indexOf('\n')].filter((i) => i >= 0);
    const sentence = t.slice(start, m.index + (cut.length ? Math.min(...cut) : after.length));
    if (!NEGATED.test(sentence)) return true;
  }
  return false;
}
const WHO_EMAIL = /\blogin_hint\b|\bservice_auth\b|verified[_ ]email|\bOTP\b|\b6-digit\b|\bone-time (?:code|password)\b|\bemailed code\b/i;

/** The first H1 anywhere in a Markdown document (isitagentready reads auth.md the same way). */
function firstH1(text) {
  const t = stripSpans(stripSpans(stripBom(str(text)).slice(0, MAX_SCAN), '<!--', '-->'), '```', '```');
  for (const [line] of linesOf(t)) if (/^#[ \t]+\S/.test(line)) return line.trim();
  return '';
}

/** Evidence values come from the site being checked: keep them short and plain, so a result
 *  can quote a site but never relay it. */
function clipText(v, n = 300) {
  let t = '';
  if (typeof v === 'string') t = v.slice(0, n + 1);
  else if (v != null) { try { t = (JSON.stringify(v) ?? '').slice(0, n + 1); } catch { t = ''; } }
  // Control and bidi characters from a hostile site never reach a terminal or a page.
  // eslint-disable-next-line no-control-regex
  t = t.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, ' ');
  return t.length > n ? `${t.slice(0, n)}…` : t;
}
function clipEvidence(ev) {
  const out = {};
  for (const [k, v] of Object.entries(ev || {})) {
    if (v == null || typeof v === 'number' || typeof v === 'boolean') out[k] = v;
    else if (Array.isArray(v)) out[k] = v.slice(0, 6).map((x) => clipText(x, 200));
    else out[k] = clipText(v, 300);
  }
  return out;
}

/** In the spec's shape: an H1 naming auth.md and a documented registration endpoint. */
function specShaped(text) {
  return /auth\.md/i.test(firstH1(text)) && (registrationPosts(text).length > 0 || /\bidentity_endpoint\b|\bregister_uri\b/.test(text));
}

/** Written for agents about getting a credential (not, say, the Markdown of a product page). */
function aboutAgentAuth(text) {
  const body = text.replace(/^#.*$/m, '');
  if (/\bauth\.md\b|\bagent_auth\b|\bagentic registration\b|\/agent\/identity\b/i.test(body)) return true;
  return /\bagents?\b/i.test(body) && /\bapi[ _-]?keys?\b|\baccess[ _-]token\b|\bbearer\b|\bcredentials?\b|\boauth\b|\bregist(?:er|ration)\b|\bsign[ -]?up\b/i.test(body);
}

function moneyKeys(obj, prefix = '', depth = 0, out = []) {
  if (!obj || typeof obj !== 'object' || depth > 3) return out;
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (/spend|budget|billing|pricing|credit|payment/i.test(k) || (/limit/i.test(k) && !/rate/i.test(k))) out.push(key);
    if (v && typeof v === 'object' && !Array.isArray(v)) moneyKeys(v, key, depth + 1, out);
  }
  return out;
}

// ---------------------------------------------------------------------------------------
// The check.

/** "https://www.Example.com/x" -> "www.example.com". */
export function normalizeSite(input) {
  let s = str(input).trim().slice(0, 300);
  if (!s) throw new Error('no domain given');
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) s = `https://${s}`;
  const u = new URL(s);
  if (u.protocol !== 'https:' && u.protocol !== 'http:') throw new Error('give a domain name');
  const host = u.hostname.toLowerCase().replace(/\.+$/, '');
  if (!host || (!/^[a-z0-9.-]+$/.test(host) && !/^\[[0-9a-f:.]+\]$/.test(host))) throw new Error('That is not a domain name.');
  const problem = urlProblem(`https://${host}/`);
  if (problem) throw new Error(`${host}: ${problem}`);
  return host;
}

/**
 * Run the seven checks on one site. Options:
 *   api:      a documented API URL that needs a key (default https://api.<registrable domain>/)
 *   docs:     a second llms.txt URL to try (default https://<domain>/docs/llms.txt)
 *   get:      the fetcher, (url, {accept}) => response (default guardedGet)
 *   pauseMs:  wait between requests (default 0)
 *   parallel: fetch the independent documents at once (default false)
 *   apiAccept: the Accept header the API needs (default application/json)
 *   skipApi:  a reason the API probe is not run
 *   signal:   an AbortSignal that stops every request at once
 */
export async function checkSite(input, opts = {}) {
  const site = normalizeSite(input);
  const get = opts.get || guardedGet;
  const requests = [];
  const pause = () => (opts.pauseMs ? new Promise((r) => setTimeout(r, opts.pauseMs)) : null);
  // One budget for the whole check: every GET counts, redirect hops included.
  const budget = { left: MAX_REQUESTS };
  const fetchOne = async (label, url, accept) => {
    if (budget.left <= 0) return { url, error: 'request budget spent' };
    if (opts.signal && opts.signal.aborted) return { url, error: 'aborted' };
    const slot = { label, url };
    requests.push(slot);
    const r = await get(url, { accept, budget, signal: opts.signal });
    Object.assign(slot, r, { label });
    await pause();
    return r;
  };
  const MD = 'text/markdown, text/plain;q=0.9, */*;q=0.1';
  const JSONA = 'application/json';

  let apiUrl = null;
  if (!opts.skipApi) {
    apiUrl = opts.api || `https://api.${registrable(site)}/`;
    if (urlProblem(apiUrl)) apiUrl = null;
  }

  // Round 1: auth.md, llms.txt, the API (independent of each other).
  const round1 = [
    () => fetchOne('auth.md', `https://${site}/auth.md`, MD),
    () => fetchOne('llms.txt', `https://${site}/llms.txt`, MD),
    () => (apiUrl ? fetchOne('api', apiUrl, opts.apiAccept || JSONA) : Promise.resolve(null)),
  ];
  let authmdR, llmsR, apiR;
  if (opts.parallel) [authmdR, llmsR, apiR] = await Promise.all(round1.map((f) => f()));
  else { authmdR = await round1[0](); llmsR = await round1[1](); apiR = await round1[2](); }

  // Round 2: a second llms.txt candidate when the root one is missing.
  let llmsSource = llmsR;
  const llmsOk = (r) => ok(r) && !isHtml(r) && (r.body || '').trim().length > 0;
  // Also when the root one does not start with an H1: a docs llms.txt that does is a pass.
  // A not-found page with an H1 ("# Page Not Found") is not well formed either.
  const llmsWellFormed = (r) => llmsOk(r) && /^#\s+\S/.test(firstHeading(r.body)) && !/not found|\b404\b/i.test(firstHeading(r.body));
  if (!llmsWellFormed(llmsR)) {
    const second = opts.docs || `https://${site}/docs/llms.txt`;
    if (!urlProblem(second)) {
      const r = await fetchOne('llms.txt (docs)', second, MD);
      if (llmsWellFormed(r) || (llmsOk(r) && !llmsOk(llmsR))) llmsSource = r;
    }
  }

  // The API's challenge: RFC 9728 section 5.1 resource_metadata, or RFC 9110 402 hints.
  const challenges = apiR && apiR.headers ? parseChallenges(apiR.headers['www-authenticate']) : [];
  let rmUrl = null;
  for (const c of challenges) {
    if (c.params.resource_metadata && httpsUrl(c.params.resource_metadata)) { rmUrl = httpsUrl(c.params.resource_metadata); break; }
  }

  // Round 3: protected resource metadata. The URL the API's 401 named first, then this site's
  // well-known path, then the API host's (auth.md Step 1: "the conventional path on the
  // resource server").
  const sitePrm = `https://${site}/.well-known/oauth-protected-resource`;
  let apiPrm = null;
  if (apiUrl) {
    const o = new URL(apiUrl).origin;
    if (o !== `https://${site}`) apiPrm = `${o}/.well-known/oauth-protected-resource`;
  }
  let prmR = null;
  let prm = null;
  let prmVia = null;
  let prmOk = false;
  const PRM_LABEL = { 'www-authenticate': 'prm (from 401)', 'well-known': 'prm', 'api-well-known': 'prm (API host)' };
  for (const [via, url] of [['www-authenticate', rmUrl], ['well-known', sitePrm], ['api-well-known', apiPrm]]) {
    if (!url || (prmR && prmR.url === url)) continue;
    const r = await fetchOne(PRM_LABEL[via], url, JSONA);
    const j = parseJson(r);
    if (!prmR) prmR = r;
    if (j && !prm) { prmR = r; prm = j; prmVia = via; }
    // A usable one ends the search; a mismatched or incomplete one is kept only as a fallback.
    if (j && prmUsable(j, via, [url, r.finalUrl], apiUrl)) { prmR = r; prm = j; prmVia = via; prmOk = true; break; }
  }

  // Round 4: authorization server metadata, from the PRM's authorization_servers, else this site's.
  const asCandidates = [];
  // Only a PRM a client may use (RFC 9728 section 3.3) leads to its authorization server.
  if (prm && prmOk && Array.isArray(prm.authorization_servers)) {
    const first = prm.authorization_servers.map(httpsUrl).find(Boolean);
    if (first) asCandidates.push(['prm', first, asMetadataUrl(first)]);
  }
  if (!asCandidates.length) asCandidates.push(['well-known', `https://${site}`, `https://${site}/.well-known/oauth-authorization-server`]);
  const [, asIssuerExpected, asUrl] = asCandidates[0];
  const asR = await fetchOne('as-metadata', asUrl, JSONA);
  const asMeta = parseJson(asR);
  const agentAuth = asMeta && asMeta.agent_auth && typeof asMeta.agent_auth === 'object' ? asMeta.agent_auth : null;

  // Round 5: auth.md named by agent_auth.skill, when /auth.md itself is missing.
  let authmdSource = authmdR;
  const authmdFound = ok(authmdR) && !isHtml(authmdR);
  const skill = agentAuth && httpsUrl(agentAuth.skill);
  if (!authmdFound && skill && trimSlash(skill) !== trimSlash(`https://${site}/auth.md`)) {
    const r = await fetchOne('auth.md (agent_auth.skill)', skill, MD);
    if (ok(r) && !isHtml(r)) authmdSource = r;
  }

  const authmdText = ok(authmdSource) && !isHtml(authmdSource) ? stripBom(authmdSource.body) : '';

  // A site that answers every path with the same Markdown (a catch-all, or a soft 404) has no
  // auth.md. One extra GET, only when the file is not already in the spec's shape.
  let authmdCatchAll = false;
  if (authmdText && !specShaped(authmdText)) {
    const origin = new URL(authmdSource.finalUrl || authmdSource.url).origin;
    const probe = await fetchOne('catch-all probe', `${origin}/agent-signup-check-no-such-page.md`, MD);
    if (ok(probe) && !isHtml(probe) && firstHeading(probe.body) === firstHeading(authmdText)) authmdCatchAll = true;
  }
  const llmsText = llmsOk(llmsSource) ? stripBom(llmsSource.body) : '';
  const prose = [
    ['auth.md', authmdText, authmdSource && authmdSource.finalUrl],
    ['llms.txt', llmsText, llmsSource && llmsSource.finalUrl],
  ];

  // Round 6: x402 discovery, at the URL the docs name, else this site's well-known
  // (draft-hawkins-x402-dns-discovery; x402scan's DISCOVERY.md).
  let x402Ref = null;
  for (const [, text] of prose) {
    if (!text || !text.includes('/.well-known/x402')) continue;
    for (const [line] of linesOf(text)) {
      if (!line.includes('/.well-known/x402')) continue;
      const u = urlsInLine(line).find((x) => /\/\.well-known\/x402$/.test(x.url));
      if (u) { try { if (sameSite(new URL(u.url).hostname, site)) x402Ref = u.url; } catch { /* not a URL */ } }
      if (x402Ref) break;
    }
    if (x402Ref) break;
  }
  const x402Url = x402Ref && !urlProblem(x402Ref) ? x402Ref : `https://${site}/.well-known/x402`;
  const x402R = await fetchOne('x402 discovery', x402Url, JSONA);
  const x402Doc = parseJson(x402R);
  const x402Ok = !!(x402Doc && ('x402Version' in x402Doc || 'x402_version' in x402Doc || Array.isArray(x402Doc.accepts) || Array.isArray(x402Doc.resources)));

  const results = {};
  const set = (id, status, reason, evidence = {}) => { results[id] = { ...clipEvidence(evidence), status: String(status), reason: clipText(reason, 400) }; };

  // --- authmd -------------------------------------------------------------------------
  {
    if (!authmdText) {
      const why = ok(authmdR) && isHtml(authmdR) ? '/auth.md answers with an HTML page, not Markdown' : describeMiss(authmdR, '/auth.md');
      set('authmd', 'fail', why, { url: authmdR && authmdR.url });
    } else {
      const h1 = firstH1(authmdText) || firstHeading(authmdText);
      const posts = registrationPosts(authmdText);
      const endpoint = posts.length > 0 || /\bidentity_endpoint\b|\bregister_uri\b/.test(authmdText);
      const types = REG_TYPES.filter(([, re]) => re.test(authmdText)).map(([t]) => t);
      const evidence = { url: authmdSource.finalUrl, heading: h1.slice(0, 80), registrationEndpoints: posts.slice(0, 3).map((p) => p.endpoint), mentions: types };
      const declines = !endpoint && DECLINES.exec(authmdText);
      if (authmdCatchAll) {
        set('authmd', 'fail', 'The site answers any path with the same Markdown page, so this is not an auth.md', evidence);
      } else if (!aboutAgentAuth(authmdText)) {
        set('authmd', 'fail', 'A Markdown page is served at /auth.md, but it is not about agent sign-in (it never mentions auth.md, agent_auth, or how an agent gets a credential)', evidence);
      } else if (declines) {
        set('authmd', 'partial', 'auth.md is published, and says agents cannot register here yet', { ...evidence, quote: snippet(authmdText, declines.index) });
      } else if (!/^#\s.*auth\.md/i.test(h1)) {
        set('authmd', 'partial', `An agent sign-in guide is published at /auth.md, but its heading (${h1.slice(0, 50)}) does not name auth.md, so tools looking for the spec may skip it`, evidence);
      } else if (!endpoint) {
        set('authmd', 'partial', 'auth.md is published, but documents no registration endpoint the checker recognizes (it describes OAuth, device login or keys the person makes)', evidence);
      } else {
        set('authmd', 'pass', `auth.md documents agent registration (${posts.length ? posts[0].endpoint : 'agent_auth endpoints'})`, evidence);
      }
    }
  }

  // --- prm ----------------------------------------------------------------------------
  {
    if (!prm) {
      const why = prmR && ok(prmR) ? 'The metadata URL answers, but not with a JSON object' : describeMiss(prmR, 'Protected resource metadata');
      set('prm', 'fail', why, { url: prmR && prmR.url });
    } else {
      const resource = httpsUrl(prm.resource);
      const servers = Array.isArray(prm.authorization_servers) ? prm.authorization_servers.filter((s) => httpsUrl(s)) : [];
      const fetched = prmR.url;
      let matches = true;
      if (resource) {
        if (prmVia !== 'www-authenticate') matches = [fetched, prmR.finalUrl].some((u) => u && trimSlash(prmUrlFor(resource)) === trimSlash(u));
        // Discovered from a 401: the request URL should sit under the resource.
        else matches = apiUrl ? underResource(apiUrl, resource) : true;
      }
      const evidence = { url: fetched, via: prmVia, resource: prm.resource, authorization_servers: prm.authorization_servers };
      if (!resource) set('prm', 'fail', 'Metadata has no https resource value (RFC 9728 section 2 makes it required)', evidence);
      else if (!servers.length) set('prm', 'partial', 'Metadata names no authorization_servers, so an agent cannot find where to register', evidence);
      else if (!matches) set('prm', 'partial', `resource (${clipText(str(prm.resource), 120)}) does not match the URL the metadata came from; RFC 9728 section 3.3 says a client MUST NOT use it`, evidence);
      else set('prm', 'pass', `Names ${servers.length} authorization server${servers.length > 1 ? 's' : ''}`, evidence);
    }
  }

  // --- register -----------------------------------------------------------------------
  let whoActs = null;
  {
    const endpoint = agentAuth && (httpsUrl(agentAuth.identity_endpoint) || httpsUrl(agentAuth.register_uri));
    const types = agentAuth && Array.isArray(agentAuth.identity_types_supported) ? agentAuth.identity_types_supported.filter((t) => typeof t === 'string').slice(0, 10) : [];
    // RFC 8414 section 3.3: the issuer must be the identifier the metadata URL was built from.
    // When the metadata URL redirected, the URL it landed on may name the issuer instead.
    const landed = asR && asR.finalUrl && asR.finalUrl !== asR.url ? issuerFromMetadataUrl(asR.finalUrl) : null;
    const issuerOk = asMeta
      ? trimSlash(asMeta.issuer) === trimSlash(asIssuerExpected) || (!!landed && trimSlash(asMeta.issuer) === trimSlash(landed))
      : false;
    if (endpoint && types.length) {
      const dialect = agentAuth.identity_endpoint && agentAuth.register_uri ? 'identity_endpoint and register_uri' : agentAuth.identity_endpoint ? 'identity_endpoint' : 'register_uri';
      const atypes = (agentAuth.identity_assertion && Array.isArray(agentAuth.identity_assertion.assertion_types_supported)) ? agentAuth.identity_assertion.assertion_types_supported.filter((t) => typeof t === 'string').slice(0, 10) : [];
      const who = new Set();
      for (const t of types) {
        if (t === 'anonymous') who.add('nobody (anonymous)');
        else if (t === 'service_auth') who.add('the person confirms an email code');
        else if (t === 'identity_assertion') {
          if (atypes.some((a) => /id-jag/i.test(a))) who.add("the agent's provider vouches (ID-JAG)");
          if (atypes.includes('verified_email')) who.add('the person confirms an email code');
          if (!atypes.length) who.add('an identity assertion');
        } else who.add(clipText(t, 40));
      }
      whoActs = clipText([...who].slice(0, 4).join('; '), 200);
      const ev = { url: asR.url, endpoint, identity_types_supported: types, field: dialect, issuer: str(asMeta.issuer) };
      const tokenEp = asMeta && httpsUrl(asMeta.token_endpoint);
      if (tokenEp && trimSlash(tokenEp) === trimSlash(endpoint)) set('register', 'partial', `agent_auth names the OAuth token endpoint (${endpoint}) as its identity endpoint: it exchanges an assertion for a token, it does not register a new account`, ev);
      else if (!issuerOk) set('register', 'partial', `agent_auth names ${clipText(endpoint, 120)}, but the metadata issuer (${clipText(str(asMeta.issuer), 120)}) is not the authorization server it was fetched for; RFC 8414 section 3.3 says a client MUST NOT use it`, ev);
      else set('register', 'pass', `agent_auth names a registration endpoint (${types.join(', ')})`, ev);
    } else {
      let found = null;
      for (const [where, text, url] of prose) {
        const hits = !text ? [] : where === 'auth.md' ? (aboutAgentAuth(text) ? registrationPosts(text) : []) : llmsSignupPosts(text);
        if (hits.length) { found = { where, url, endpoint: hits[0].endpoint, link: !!hits[0].link, quote: snippet(text, hits[0].index), text }; break; }
      }
      if (found) {
        const { text, ...ev } = found;
        set('register', 'partial', found.link
          ? `${found.where} links an agent sign-up guide (${found.endpoint}), but there is no agent_auth metadata for tools to find it`
          : `${found.where} documents a sign-up API (${found.endpoint}), but there is no agent_auth metadata for tools to find it`, ev);
        const who = [];
        if (offersAnonymous(text)) who.push('nobody (provisioned without an account, claimed later)');
        if (WHO_EMAIL.test(text)) who.push('the person confirms an email code');
        whoActs = who.length ? `${who.join('; ')} (from ${found.where} prose)` : 'not stated';
      } else if (agentAuth) {
        set('register', 'fail', 'agent_auth is present but names no registration endpoint and identity types', { url: asR.url });
      } else {
        set('register', 'fail', asMeta ? 'Authorization server metadata has no agent_auth block, and no sign-up API is documented in auth.md or llms.txt' : `${describeMiss(asR, 'Authorization server metadata')}; no sign-up API is documented in auth.md or llms.txt`, { url: asR && asR.url });
      }
    }
  }

  // --- challenge ----------------------------------------------------------------------
  {
    if (opts.skipApi) set('challenge', 'skipped', opts.skipApi);
    else if (!apiR) set('challenge', 'fail', 'No API URL to probe');
    else {
      const wall = blockedReason(apiR);
      const ev = { url: apiR.url, httpStatus: apiR.status ?? null, www_authenticate: (apiR.headers && apiR.headers['www-authenticate']) || null };
      if (apiR.error) set('challenge', 'fail', `API: ${apiR.error}`, ev);
      else if (wall) set('challenge', 'fail', `The API answers with ${wall}`, ev);
      else if (apiR.status === 401 && challenges.length) {
        const rm = challenges.some((c) => c.params.resource_metadata);
        set('challenge', 'pass', `401 with a ${challenges.map((c) => c.scheme).join(', ')} challenge${rm ? ' that points to resource metadata (RFC 9728 section 5.1)' : ''}`, ev);
      } else if (apiR.status === 401) set('challenge', 'partial', '401, but no WWW-Authenticate header (RFC 9110 section 15.5.2 says the server MUST send one)', ev);
      else if (apiR.status === 402) set('challenge', 'pass', '402 Payment Required: the API takes payment from a client with no account', ev);
      else if (apiR.status === 403 && challenges.length) set('challenge', 'partial', `403 with a ${challenges.map((c) => c.scheme).join(', ')} challenge, instead of 401`, ev);
      else if (apiR.status === 403) set('challenge', 'partial', AUTH_WORDS.test((apiR.body || '').slice(0, 2000)) ? '403 instead of 401: the body asks for a key, but there is no WWW-Authenticate challenge' : '403 instead of 401, with no WWW-Authenticate challenge', ev);
      else if ([400, 422].includes(apiR.status) && AUTH_WORDS.test((apiR.body || '').slice(0, 2000))) set('challenge', 'partial', `HTTP ${apiR.status} asking for a key, instead of a 401 challenge`, ev);
      else if (apiR.status === 200 && isHtml(apiR)) set('challenge', 'fail', 'The API URL answers with an HTML page, not an API error', ev);
      else set('challenge', 'fail', `The API URL answers HTTP ${apiR.status}${apiR.hops && apiR.hops.length ? ` after ${apiR.hops.length} redirect(s)` : ''}`, ev);
    }
  }

  // --- limits -------------------------------------------------------------------------
  {
    const billing = agentAuth && agentAuth.billing && typeof agentAuth.billing === 'object' ? agentAuth.billing : null;
    const budgets = prm && (prm.budget_units || prm.usage_endpoint);
    const vendor = [...moneyKeys(agentAuth, 'agent_auth'), ...moneyKeys(prm && { ...prm, authorization_servers: undefined }, 'prm')]
      .filter((k) => !/^agent_auth\.billing/.test(k));
    if (billing && billing.default_spend_cap) {
      set('limits', 'pass', 'agent_auth.billing declares a default spend cap', { url: asR.url, default_spend_cap: billing.default_spend_cap, periods: billing.spend_cap_periods_supported || null });
    } else if (budgets) {
      set('limits', 'pass', 'Resource metadata declares budgets (AAuth budget_units / usage_endpoint)', { url: prmR.url });
    } else if (vendor.length) {
      set('limits', 'partial', `Metadata carries vendor fields about money or limits (${vendor.slice(0, 4).join(', ')}), not a standard spend cap`, { fields: vendor });
    } else {
      let hit = null;
      for (const [where, text, url] of prose) { const f = findPhrase(text, LIMIT_PHRASES); if (f) { hit = { where, url, ...f }; break; } }
      if (hit) set('limits', 'partial', `${hit.where} mentions ${hit.match.replace(/^\[|\]\($/g, '').toLowerCase()} in prose; no machine-readable cap`, hit);
      else set('limits', 'fail', 'No spend cap in metadata, auth.md or llms.txt');
    }
  }

  // --- payment ------------------------------------------------------------------------
  {
    const billing = agentAuth && agentAuth.billing && typeof agentAuth.billing === 'object' ? agentAuth.billing : null;
    const h = (apiR && apiR.headers) || {};
    const paymentScheme = challenges.find((c) => /^payment$/i.test(c.scheme));
    const body402 = apiR && apiR.status === 402 ? parseJson({ ...apiR, status: 200 }) : null;
    if (apiR && apiR.status === 402 && (h['payment-required'] || (body402 && (body402.x402Version || body402.accepts)))) {
      set('payment', 'pass', 'The API answers 402 with an x402 payment requirement', { url: apiR.url });
    } else if (x402Ok) {
      set('payment', 'pass', 'Publishes an x402 discovery document (/.well-known/x402) that an agent can pay from without an account', { url: x402R.finalUrl || x402R.url });
    } else if (paymentScheme) {
      set('payment', 'pass', 'The API challenges with the Payment HTTP authentication scheme', { url: apiR.url });
    } else if (apiR && apiR.status === 402 && body402 && body402.billing) {
      set('payment', 'pass', 'The API answers 402 with a billing object', { url: apiR.url });
    } else if (billing && httpsUrl(billing.pricing_uri)) {
      set('payment', 'pass', 'agent_auth.billing declares a pricing_uri for the 402 billing handoff', { url: asR.url, pricing_uri: billing.pricing_uri });
    } else {
      let hit = null;
      for (const [where, text, url] of prose) { const f = findPhrase(text, PAYMENT_PHRASES); if (f) { hit = { where, url, ...f }; break; } }
      if (hit) set('payment', 'partial', `${hit.where} mentions a payment path (${hit.match}) in prose; nothing machine-readable`, hit);
      else set('payment', 'fail', `No 402, payment challenge or billing metadata, and no payment path the checker recognizes in auth.md or llms.txt${x402R && x402R.error === 'request budget spent' ? ' (the request budget ran out before /.well-known/x402)' : ''}`);
    }
  }

  // --- markdown -----------------------------------------------------------------------
  {
    if (!llmsText) {
      const last = requests.filter((r) => r.label.startsWith('llms.txt')).pop();
      const tried = requests.filter((r) => r.label.startsWith('llms.txt'));
      const why = tried.map((r) => (ok(r) && isHtml(r) ? `${r.url} answers with an HTML page` : describeMiss(r, r.url))).join('; ');
      set('markdown', 'fail', why || 'llms.txt was not fetched', { url: last && last.url, tried: tried.map((r) => r.url) });
    } else {
      const first = firstHeading(llmsText);
      const ev = { url: llmsSource.finalUrl, heading: first.slice(0, 80), bytes: llmsText.length };
      if (/^#\s+\S/.test(first) && /not found|\b404\b/i.test(first)) set('markdown', 'fail', `llms.txt answers 200 with a not-found page (${first.slice(0, 60)})`, ev);
      else if (/^#\s+\S/.test(first)) set('markdown', 'pass', `llms.txt found${llmsSource !== llmsR ? ' at a docs path' : ''}`, ev);
      else set('markdown', 'partial', 'A text file is served at llms.txt, but it does not start with an H1 (the one section llmstxt.org requires)', ev);
    }
  }

  const ordered = CHECKS.map((c) => ({ id: c.id, name: c.name, ...results[c.id] }));
  const passed = ordered.filter((c) => c.status === 'pass').length;
  const partial = ordered.filter((c) => c.status === 'partial').length;
  const applicable = ordered.filter((c) => c.status !== 'skipped').length;
  return {
    site,
    checkedAt: new Date().toISOString(),
    checker: `agent-signup-check/${VERSION}`,
    passed,
    partial,
    total: applicable,
    whoActs,
    checks: ordered,
    requests: requests.map((r) => ({
      label: r.label,
      url: clipText(r.url, 300),
      finalUrl: clipText(r.finalUrl, 300),
      status: r.status ?? null,
      error: r.error ? clipText(r.error, 100) : null,
      contentType: r.headers && r.headers['content-type'] ? clipText(r.headers['content-type'], 100) : null,
      ms: r.ms ?? null,
      redirects: r.hops ? r.hops.length : 0,
    })),
    _raw: requests,
  };
}

// ---------------------------------------------------------------------------------------
// CLI.

export function saveEvidence(dir, result) {
  const d = path.join(dir, result.site.replace(/[^a-z0-9.-]/g, '_'));
  fs.mkdirSync(d, { recursive: true });
  result._raw.forEach((r, i) => {
    const name = `${String(i + 1).padStart(2, '0')}-${r.label.replace(/[^a-z0-9.-]+/gi, '_')}.http`;
    const lines = [`GET ${r.url}`, `user-agent: ${USER_AGENT}`, `fetched: ${result.checkedAt}`];
    for (const h of r.hops || []) lines.push(`redirect: ${h.status} from ${h.url}`);
    lines.push(`final-url: ${r.finalUrl || r.url}`, '');
    if (r.error) lines.push(`error: ${r.error}`);
    if (r.status !== undefined) lines.push(`HTTP ${r.status}`);
    for (const [k, v] of Object.entries(r.headers || {})) {
      // Never keep cookies, or a header that echoes the client's own address back.
      if (/^(set-cookie|cookie|true-client-ip|cf-connecting-ip|x-forwarded-for|x-real-ip|x-client-ip|forwarded)$/i.test(k)) continue;
      lines.push(`${k}: ${v}`);
    }
    lines.push('', r.body || '');
    if (r.truncated) lines.push('', `[truncated at ${MAX_BYTES} bytes]`);
    fs.writeFileSync(path.join(d, name), lines.join('\n'));
  });
  const { _raw, ...clean } = result;
  fs.writeFileSync(path.join(d, 'result.json'), JSON.stringify(clean, null, 1));
}

const MARK = { pass: 'PASS', partial: 'PART', fail: 'FAIL', skipped: 'SKIP' };

function printResult(r) {
  console.log(`${r.site}: ${r.passed} of ${r.total} pass, ${r.partial} partial  (${r.checker}, ${r.checkedAt})`);
  for (const c of r.checks) console.log(`  ${MARK[c.status] || c.status}  ${c.name.padEnd(28)} ${c.reason}`);
  if (r.whoActs) console.log(`  who has to act at sign-up: ${r.whoActs}`);
}

async function main(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const k = a.slice(2);
      if (['json'].includes(k)) args[k] = true;
      else args[k] = argv[++i];
    } else args._.push(a);
  }
  if (args.targets) {
    // The comment keeps bundlers (Next's Turbopack) from tracing the whole project into a server
    // function that imports this file: the path is only known when the CLI runs.
    const targets = JSON.parse(fs.readFileSync(/* turbopackIgnore: true */ args.targets, 'utf8'));
    const list = Array.isArray(targets) ? targets : targets.targets;
    const conc = Math.max(1, Math.min(8, Number(args.concurrency) || 4));
    const out = new Array(list.length);
    let next = 0;
    const worker = async () => {
      while (next < list.length) {
        const i = next++;
        const t = list[i];
        try {
          const r = await checkSite(t.site, { api: t.api || undefined, apiAccept: t.apiAccept || undefined, docs: t.docs || undefined, skipApi: t.skipApi || undefined, pauseMs: 300 });
          if (args.save) saveEvidence(args.save, r);
          const { _raw, ...clean } = r;
          out[i] = { name: t.name || t.site, category: t.category || null, ...clean };
          process.stderr.write(`${String(i + 1).padStart(3)} ${String(t.name || t.site).padEnd(24)} ${r.passed}/${r.total} (+${r.partial})\n`);
        } catch (e) {
          out[i] = { name: t.name || t.site, category: t.category || null, site: t.site, error: clipText(String(e && e.message), 200) };
          process.stderr.write(`${String(i + 1).padStart(3)} ${String(t.name || t.site).padEnd(24)} ERROR ${clipText(String(e && e.message), 200)}\n`);
        }
      }
    };
    await Promise.all(Array.from({ length: conc }, worker));
    const doc = { checker: `agent-signup-check/${VERSION}`, userAgent: USER_AGENT, generatedAt: new Date().toISOString(), checks: CHECKS, results: out };
    if (args.out) fs.writeFileSync(args.out, JSON.stringify(doc, null, 1));
    else console.log(JSON.stringify(doc, null, 1));
    return;
  }
  if (!args._.length) {
    console.error('usage: node agent-signup-check.mjs <domain> [--api URL] [--docs URL] [--json] [--save DIR]\n       node agent-signup-check.mjs --targets targets.json --out results.json [--save DIR]');
    process.exit(2);
  }
  for (const d of args._) {
    const r = await checkSite(d, { api: args.api, docs: args.docs });
    if (args.save) saveEvidence(args.save, r);
    const { _raw, ...clean } = r;
    if (args.json) console.log(JSON.stringify(clean, null, 1));
    else printResult(clean);
  }
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invoked) main(process.argv.slice(2)).catch((e) => { console.error(e.message || e); process.exit(1); });
