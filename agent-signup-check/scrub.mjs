// Copyright 2026 Prized. Licensed under the Apache License, Version 2.0.
//
// Make saved evidence safe to publish. The checker's --save writes one .http file per request:
// the request line, the response status and headers, then the body. Everything in it came from a
// public GET with no credentials, but a response can still carry things about the client that
// fetched it, so before evidence is committed:
//
//  - cookie headers, anti-forgery token headers, and headers that echo the client's address
//    (True-Client-IP and friends) are dropped;
//  - the client's own public addresses (given, or learned once per run) are replaced with
//    [client-ip] wherever they appear, in any spelling: 203.0.113.7, 203.0.113.7:443,
//    ::ffff:203.0.113.7, ec2-203-0-113-7, and every way of writing an IPv6 address;
//  - in HTML pages and headers, any public IPv4 address the page labels as the visitor's
//    ("visitorIP": "...", "ip_address": "...", "your IP address is ...") is replaced too, and so
//    are per-visitor tokens (csrf and session ids, Cloudflare challenge tokens): [redacted];
//  - an HTML page (a sign-in page, a 404 page, a bot wall) is cut to its first 16 KB, with its
//    length and the SHA-256 of the body as published (after the replacements above). The
//    checker reads an HTML answer only to see that it is HTML and, for a bot wall, its first 8 KB.
//
// Agent documents (Markdown, text, JSON, and JSON served with an HTML content type) are kept
// byte for byte, except for the client's own address if it appears in one.
//
// usage: node scrub.mjs SRC_DIR DST_DIR [--client-ip 203.0.113.7 ...]
// Writes DST_DIR/<site>/... from SRC_DIR/<site>/...; result.json files are copied as they are.

import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { isPublicAddress } from './agent-signup-check.mjs';

export const HTML_KEEP = 16 * 1024;
const DROP_HEADER =
  /^(set-cookie|cookie|true-client-ip|cf-connecting-ip|x-forwarded-for|x-real-ip|x-client-ip|x-originating-ip|forwarded|x-cluster-client-ip|fastly-client-ip|x-csrf-token|x-xsrf-token|csrf-token)$/i;
const IPV4 = String.raw`(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}`;
// A public IPv4 address the page itself labels as the visitor's.
const LABELLED_IP = new RegExp(
  String.raw`((?:visitor|client|remote|user|your|my|request(?:er)?|source)?[ _-]?(?:ip|ip[ _-]?address|address)["']?\s*(?:[:=]|is)\s*["']?)` + `(${IPV4})(?![\\d])`,
  'gi',
);
// A JSON or JS key that holds the visitor's address ("ip_address": "...", 'remote_addr': '...').
const KEYED_IP = new RegExp(String.raw`(["'](?:ip|ip_?addr(?:ess)?|remote_?addr(?:ess)?|client_?ip|visitor_?ip|user_?ip)["']\s*:\s*["'])` + `(${IPV4})`, 'gi');
// Per-visitor tokens a page hands an anonymous client: anti-forgery tokens (tied to the cookie that
// was dropped, so worthless, but per visitor), session and visitor ids, Cloudflare challenge tokens.
const TOKEN_NAMES = String.raw`csrf[-_]?token|csrf[-_]?param|csrfmiddlewaretoken|authenticity_token|_csrf|_token|xsrf[-_]?token|__RequestVerificationToken`;
const TOKENS = [
  // <meta name="csrf-token" content="..."> and <input name="_token" value="...">, either order.
  new RegExp(String.raw`((?:name|id)=["'](?:${TOKEN_NAMES})["'][^>]{0,120}?(?:content|value)=["'])([^"']{8,})(["'])`, 'gi'),
  new RegExp(String.raw`((?:content|value)=["'])([^"']{8,})(["'][^>]{0,120}?(?:name|id)=["'](?:${TOKEN_NAMES})["'])`, 'gi'),
  // csrfToken: "...", window.csrfToken = '...', 'anonymous_session_id': '...', "visitorId":"..."
  /(\b(?:csrf\w*|xsrf\w*|_token|anonymous_?session_?id|session_?id|sessionid|visitor_?id|anonymous_?id|anon_?id|device_?id|distinct_?id)["']?\s*[:=]\s*["'])([^"'\s]{8,})(["'])/gi,
];
// Cloudflare's challenge page: __cf_chl_tk=..., and the per-visit values in _cf_chl_opt. Only on
// a page that is one (md: and fa: mean other things elsewhere).
const CHALLENGE_TOKENS = [/(__cf_chl_\w*?tk=)([^&"'\s<>]{8,})()/g, /(\b(?:cH|cN|cRq|cUPMDTk|md|mdrd|fa)\s*:\s*["'])([^"']{12,})(["'])/g];
const TRIM_NOTE = /\n\n\[body trimmed for publication: kept \d+ of \d+ characters; sha256 of the published body [0-9a-f]{64}\]$/;
// Candidate IPv6 literals: hex groups and colons, maybe ending in a dotted IPv4.
const IPV6_CANDIDATE = /(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}(?:[0-9a-f]{0,4}|\d{1,3}(?:\.\d{1,3}){3})(?![0-9a-f:])/gi;

/** The canonical (compressed, lower-case) form of an IPv6 literal, or null. */
function canonical6(s) {
  if (net.isIP(s) !== 6) return null;
  try {
    return new URL(`http://[${s}]/`).hostname.slice(1, -1);
  } catch {
    return null;
  }
}

/** Replace every spelling of the given client addresses with [client-ip]. */
export function redactClientIps(text, clientIps) {
  let out = text;
  const v6 = new Set();
  for (const ip of clientIps) {
    if (net.isIP(ip) === 4) {
      const dotted = ip.replace(/\./g, '\\.');
      const dashed = ip.replace(/\./g, '-');
      // Not inside a longer address or number: 1.2.3.4 is not 1.2.3.45 or 11.2.3.4.
      out = out.replace(new RegExp(`(?<![\\d.])${dotted}(?!\\d|\\.\\d)`, 'g'), '[client-ip]');
      // ec2-54-201-10-20, ip-54-201-10-20: a dash before is fine, a digit or a dot is not.
      out = out.replace(new RegExp(`(?<![\\d.])${dashed}(?!\\d|-\\d)`, 'g'), '[client-ip]');
    } else if (net.isIP(ip) === 6) {
      v6.add(canonical6(ip));
    }
  }
  if (v6.size) {
    out = out.replace(IPV6_CANDIDATE, (m) => (v6.has(canonical6(m)) ? '[client-ip]' : m));
  }
  return out;
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
const parsesAsJson = (body) => {
  const t = body.replace(/\n\n\[truncated at \d+ bytes\]$/, '').trim();
  if (!/^[[{]/.test(t)) return false;
  try {
    JSON.parse(t);
    return true;
  } catch {
    return false;
  }
};
/** An HTML page, as opposed to an agent document. JSON is a document whatever its content type. */
export const looksHtml = (response, body) =>
  (contentType(response).includes('html') || /^\s*(<!doctype html|<html|<head|<body)/i.test(body)) && !parsesAsJson(body);

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
  // The patterns run on headers and HTML pages only, and only on public addresses: an agent
  // document is kept as served, so its example addresses (192.0.2.1 in API docs) stay.
  const html = looksHtml(response, body);
  const patterns = (s) => {
    const ipIfPublic = (m, label, ip) => (isPublicAddress(ip) ? `${label}[client-ip]` : m);
    let out = s.replace(LABELLED_IP, ipIfPublic).replace(KEYED_IP, ipIfPublic);
    const tokens = /_cf_chl_|__cf_chl/.test(s) ? [...TOKENS, ...CHALLENGE_TOKENS] : TOKENS;
    for (const re of tokens) out = out.replace(re, (m, a, value, b) => (/^\[|^(?:true|false|null|undefined)$/i.test(value) ? m : `${a}[redacted]${b}`));
    return out;
  };
  request = redactClientIps(request, clientIps);
  response = redactClientIps(patterns(response), clientIps);
  body = redactClientIps(html ? patterns(body) : body, clientIps);
  if (!TRIM_NOTE.test(body) && html && body.length > HTML_KEEP) {
    const kept = body.replace(/\n\n\[truncated at \d+ bytes\]$/, '');
    const sha = crypto.createHash('sha256').update(kept).digest('hex');
    body = `${kept.slice(0, HTML_KEEP)}\n\n[body trimmed for publication: kept ${HTML_KEEP} of ${kept.length} characters; sha256 of the published body ${sha}]`;
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
      if (clean !== raw && (clean.includes('[client-ip]') || clean.includes('[redacted]'))) counts.redacted += 1;
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
      const { response, body } = parseEvidence(text);
      for (const line of response.split('\n')) {
        const m = line.match(/^([A-Za-z0-9-]+): /);
        if (m && DROP_HEADER.test(m[1])) problems.push(`${file}: header ${m[1]}`);
      }
      if (clientIps.length && redactClientIps(text, clientIps) !== text) problems.push(`${file}: client address`);
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
    if (args[i] === '--client-ip') {
      const ip = args[++i];
      if (!net.isIP(ip ?? '')) {
        console.error(`--client-ip needs an IP address, got ${ip ?? 'nothing'}`);
        process.exit(2);
      }
      ips.push(ip);
    } else pos.push(args[i]);
  }
  const [src, dst] = pos;
  if (!src || !dst) {
    console.error('usage: node scrub.mjs SRC_DIR DST_DIR [--client-ip ADDRESS ...]');
    process.exit(2);
  }
  const counts = scrubDir(src, dst, { clientIps: ips });
  const problems = auditDir(dst, { clientIps: ips });
  console.log(`scrubbed ${counts.files} files: ${counts.trimmed} HTML bodies trimmed, ${counts.redacted} with a value redacted, ${counts.headersDropped} headers dropped`);
  if (problems.length) {
    console.error(problems.join('\n'));
    process.exit(1);
  }
}
