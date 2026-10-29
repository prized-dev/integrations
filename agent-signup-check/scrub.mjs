// Copyright 2026 Prized. Licensed under the Apache License, Version 2.0.
//
// Make saved evidence safe to publish. The checker's --save writes one .http file per request:
// the request line, the response status and headers, then the body. Everything in it came from a
// public GET with no credentials, but a response can still carry things about the client that
// fetched it, so before evidence is committed:
//
//  - cookie headers, and headers that echo the client's address (True-Client-IP and friends),
//    are dropped;
//  - the client's own public IP addresses (given, or learned once per run) are replaced with
//    [client-ip] wherever they appear, and so is any IPv4 address a page labels as the
//    visitor's ("visitorIP": "...", "ip_address": "...", "your IP address is ...");
//  - anti-forgery tokens a page hands an anonymous visitor (csrf-token meta tags and the like)
//    are replaced with [redacted];
//  - an HTML body (a sign-in page, a 404 page, a bot wall) is cut to its first 16 KB, with its
//    full length and the SHA-256 of the full body noted. The checker reads an HTML answer only to
//    see that it is HTML and, for a bot wall, its first 8 KB; agent documents (Markdown, text,
//    JSON) are kept whole.
//
// usage: node scrub.mjs SRC_DIR DST_DIR [--client-ip 203.0.113.7 ...]
// Writes DST_DIR/<site>/... from SRC_DIR/<site>/...; result.json files are copied as they are.

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { isPublicAddress } from './agent-signup-check.mjs';

export const HTML_KEEP = 16 * 1024;
const DROP_HEADER = /^(set-cookie|cookie|true-client-ip|cf-connecting-ip|x-forwarded-for|x-real-ip|x-client-ip|x-originating-ip|forwarded|x-cluster-client-ip|fastly-client-ip)$/i;
const IPV4 = String.raw`(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}`;
// An IPv4 address the page itself labels as the visitor's.
const LABELLED_IP = new RegExp(
  String.raw`((?:visitor|client|remote|user|your|my|request(?:er)?|source)[ _-]?(?:ip|ip[ _-]?address|address)["']?\s*(?:[:=]|is)\s*["']?)` + `(${IPV4})`,
  'gi',
);
// A JSON or JS key that holds the visitor's address ("ip_address": "...", 'remote_addr': '...').
const KEYED_IP = new RegExp(String.raw`(["'](?:ip|ip_?addr(?:ess)?|remote_?addr(?:ess)?|client_?ip|visitor_?ip|user_?ip)["']\s*:\s*["'])` + `(${IPV4})`, 'gi');
// Anti-forgery tokens a page hands an anonymous visitor (tied to the cookie that was dropped, so
// worthless, but they are per-visitor values, so they go too).
const CSRF = [
  /((?:name|id)=["'](?:csrf[-_]?token|authenticity_token|_csrf|xsrf[-_]?token)["'][^>]{0,80}?(?:content|value)=["'])([^"']{8,})(["'])/gi,
  /((?:content|value)=["'])([^"']{8,})(["'][^>]{0,80}?(?:name|id)=["'](?:csrf[-_]?token|authenticity_token|_csrf|xsrf[-_]?token)["'])/gi,
  /(["'](?:csrf|xsrf)[A-Za-z_]*["']\s*:\s*["'])([^"']{12,})(["'])/gi,
];
const TRIM_NOTE = /\n\n\[body trimmed for publication: kept \d+ of \d+ characters; sha256 of the full body [0-9a-f]{64}\]$/;

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Every spelling of an address that could appear in a page: as given, and IPv6 expanded. */
function spellings(ip) {
  const out = new Set([ip]);
  if (ip.includes(':')) {
    try {
      out.add(new URL(`http://[${ip}]/`).hostname.slice(1, -1));
    } catch {
      /* not a valid IPv6 literal: keep it as given */
    }
  }
  return [...out].filter((s) => s.length >= 7);
}

/** Split an .http evidence file into its request head, response head and body. */
export function parseEvidence(text) {
  const first = text.indexOf('\n\n');
  if (first < 0) return { request: text, response: '', body: '' };
  const second = text.indexOf('\n\n', first + 2);
  if (second < 0) return { request: text.slice(0, first), response: text.slice(first + 2), body: '' };
  return { request: text.slice(0, first), response: text.slice(first + 2, second), body: text.slice(second + 2) };
}

const contentType = (response) => ((response.match(/^content-type: (.*)$/m) || [])[1] || '').toLowerCase();
const looksHtml = (response, body) => contentType(response).includes('html') || /^\s*(<!doctype html|<html|<head|<body)/i.test(body);

/** Scrub one evidence file's text. */
export function scrubEvidence(text, { clientIps = [] } = {}) {
  let { request, response, body } = parseEvidence(text);
  response = response
    .split('\n')
    .filter((line) => {
      const m = line.match(/^([A-Za-z0-9-]+): /);
      return !(m && m[1] !== 'error' && DROP_HEADER.test(m[1]));
    })
    .join('\n');
  // The patterns (an address a page labels as the visitor's, anti-forgery tokens) run on headers
  // and HTML pages only, and only on public addresses: an agent document (Markdown, text, JSON) is
  // kept exactly as served, so its example addresses (192.0.2.1 in API docs) stay. The client's
  // own address is replaced everywhere.
  const html = looksHtml(response, body);
  const patterns = (s) => {
    const ipIfPublic = (m, label, ip) => (isPublicAddress(ip) ? `${label}[client-ip]` : m);
    let out = s.replace(LABELLED_IP, ipIfPublic).replace(KEYED_IP, ipIfPublic);
    for (const re of CSRF) out = out.replace(re, (m, a, value, b) => (/^\[|^(?:true|false|null)$/i.test(value) ? m : `${a}[redacted]${b}`));
    return out;
  };
  const redact = (s, withPatterns = true) => {
    let out = withPatterns ? patterns(s) : s;
    for (const ip of clientIps) {
      for (const spelling of spellings(ip)) {
        // Not inside a longer address or number (1.2.3.45 is not 1.2.3.4).
        out = out.replace(new RegExp(`(?<![0-9A-Za-z.:])${escapeRe(spelling)}(?![0-9A-Za-z:]|\\.\\d)`, 'gi'), '[client-ip]');
      }
    }
    return out;
  };
  request = redact(request);
  response = redact(response);
  body = redact(body, html);
  if (!TRIM_NOTE.test(body) && html && body.length > HTML_KEEP) {
    const kept = body.replace(/\n\n\[truncated at \d+ bytes\]$/, '');
    const sha = crypto.createHash('sha256').update(kept).digest('hex');
    body = `${kept.slice(0, HTML_KEEP)}\n\n[body trimmed for publication: kept ${HTML_KEEP} of ${kept.length} characters; sha256 of the full body ${sha}]`;
  }
  return `${request}\n\n${response}\n\n${body}`;
}

/** The body as the checker saw it, minus the trailers the saver and the scrubber add. */
export function bodyOf(text) {
  return parseEvidence(text).body.replace(TRIM_NOTE, '').replace(/\n\n\[truncated at \d+ bytes\]$/, '');
}

/** Scrub every site folder under src into dst. Returns counts. */
export function scrubDir(src, dst, opts = {}) {
  const counts = { files: 0, trimmed: 0, redacted: 0, headersDropped: 0 };
  for (const site of fs.readdirSync(src, { withFileTypes: true })) {
    if (!site.isDirectory()) continue;
    const from = path.join(src, site.name);
    const to = path.join(dst, site.name);
    fs.mkdirSync(to, { recursive: true });
    for (const f of fs.readdirSync(from)) {
      const raw = fs.readFileSync(path.join(from, f), 'utf8');
      if (!f.endsWith('.http')) {
        fs.writeFileSync(path.join(to, f), raw);
        continue;
      }
      const clean = scrubEvidence(raw, opts);
      counts.files += 1;
      if (TRIM_NOTE.test(clean) && !TRIM_NOTE.test(raw)) counts.trimmed += 1;
      if (clean.includes('[client-ip]') && !raw.includes('[client-ip]')) counts.redacted += 1;
      const lines = (s) => parseEvidence(s).response.split('\n').length;
      counts.headersDropped += Math.max(0, lines(raw) - lines(clean));
      fs.writeFileSync(path.join(to, f), clean);
    }
  }
  return counts;
}

/** Problems a published evidence tree must not have. Empty when it is clean. */
export function auditDir(dir, { clientIps = [] } = {}) {
  const problems = [];
  for (const site of fs.readdirSync(dir, { withFileTypes: true })) {
    if (!site.isDirectory()) continue;
    for (const f of fs.readdirSync(path.join(dir, site.name))) {
      const file = path.join(site.name, f);
      const text = fs.readFileSync(path.join(dir, file), 'utf8');
      const { response } = parseEvidence(text);
      for (const line of response.split('\n')) {
        const m = line.match(/^([A-Za-z0-9-]+): /);
        if (m && DROP_HEADER.test(m[1])) problems.push(`${file}: header ${m[1]}`);
      }
      for (const ip of clientIps) for (const s of spellings(ip)) if (text.includes(s)) problems.push(`${file}: client address`);
      const { body } = parseEvidence(text);
      const scope = looksHtml(response, body) ? text : response;
      for (const re of [LABELLED_IP, KEYED_IP]) {
        for (const m of scope.matchAll(re)) if (isPublicAddress(m[2])) problems.push(`${file}: an address labelled as the visitor's`);
      }
    }
  }
  return [...new Set(problems)];
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invoked) {
  const args = process.argv.slice(2);
  const ips = [];
  const pos = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--client-ip') ips.push(args[++i]);
    else pos.push(args[i]);
  }
  const [src, dst] = pos;
  if (!src || !dst) {
    console.error('usage: node scrub.mjs SRC_DIR DST_DIR [--client-ip ADDRESS ...]');
    process.exit(2);
  }
  const counts = scrubDir(src, dst, { clientIps: ips });
  const problems = auditDir(dst, { clientIps: ips });
  console.log(`scrubbed ${counts.files} files: ${counts.trimmed} HTML bodies trimmed, ${counts.redacted} with an address redacted, ${counts.headersDropped} headers dropped`);
  if (problems.length) {
    console.error(problems.join('\n'));
    process.exit(1);
  }
}
