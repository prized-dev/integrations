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
//    [client-ip] wherever they appear: 203.0.113.7, 203.0.113.7:443, host.203.0.113.7,
//    203.000.113.007, ec2-203-0-113-7, 7.113.0.203.in-addr.arpa, 3405803783, 0xcb007107,
//    ::ffff:203.0.113.7 and ::ffff:cb00:7107, an IPv6 address in full or shortened, in upper or
//    lower case and with characters %-encoded (203%2E0%2E113%2E7);
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

const DIGIT = /^\d$/;
const HEX = /^[0-9a-f]$/;
const ALNUM = /^[0-9a-z]$/;
const HEX_PAIR = /^[0-9a-fA-F]{2}$/;
const UPPER = /[A-Z]+/g;

/**
 * The text with each %XX that encodes a printable ASCII character decoded once (203%2E0%2e113%2E9
 * reads 203.0.113.9). `at[k]` is where view character k starts in `text`, and `at[view.length]` is
 * `text.length`.
 */
function decodeOnce(text) {
  let view = '';
  const at = [];
  for (let i = 0; i < text.length; ) {
    if (text[i] === '%' && HEX_PAIR.test(text.slice(i + 1, i + 3))) {
      const code = parseInt(text.slice(i + 1, i + 3), 16);
      if (code >= 0x20 && code <= 0x7e) {
        view += String.fromCharCode(code);
        at.push(i);
        i += 3;
        continue;
      }
    }
    view += text[i];
    at.push(i);
    i += 1;
  }
  at.push(text.length);
  return { view, at };
}

/**
 * The ways the text is read when looking for an address, each with ASCII letters lower-cased: as
 * it is, with %XX decoded once, and decoded twice (%252E). `at` maps a view position back to the
 * text (null: one to one). The text as it is is always read too, so a literal % before or after an
 * address (100%203.0.113.9) cannot hide it.
 */
function addressViews(text) {
  const lower = (s) => s.replace(UPPER, (u) => u.toLowerCase());
  const views = [{ view: lower(text), at: null }];
  if (!text.includes('%')) return views;
  const once = decodeOnce(text);
  views.push({ view: lower(once.view), at: once.at });
  if (once.view.includes('%')) {
    const twice = decodeOnce(once.view);
    if (twice.view !== once.view) views.push({ view: lower(twice.view), at: twice.at.map((k) => once.at[k]) });
  }
  return views;
}

// When a found needle is a whole address, from the characters around it. No digit right before
// or right after it (11.2.3.4 and 1.2.3.45 are other addresses). A dot on either side of a dotted
// address is fine: host.1.2.3.4, and 1.2.3.4.443 (an address and a port). A dashed address may
// have a dot or a dash before it (ec2-1-2-3-4, x.1-2-3-4) but not a dash and a digit after it
// (the minus signs of SVG path data read that way); one that a replacement leaves whole is found
// on the next pass. A number is not inside a longer word or number (a hash, an id).
const WHOLE = {
  dotted: (before, after) => !DIGIT.test(before) && !DIGIT.test(after),
  dashed: (before, after, next) => !DIGIT.test(before) && !DIGIT.test(after) && !(after === '-' && DIGIT.test(next)),
  decimal: (before, after) => !ALNUM.test(before) && !ALNUM.test(after),
  hex: (before, after) => !ALNUM.test(before) && !HEX.test(after),
};

/** Where `needle` occurs whole in `view`, by plain string search: [start, end) pairs, in order. */
function findWhole(view, needle, kind) {
  const found = [];
  let from = 0;
  for (let i = view.indexOf(needle); i >= 0; i = view.indexOf(needle, i + 1)) {
    if (i < from) continue;
    const end = i + needle.length;
    if (!WHOLE[kind](i > 0 ? view[i - 1] : '', view[end] ?? '', view[end + 1] ?? '')) continue;
    found.push([i, end]);
    from = end;
  }
  return found;
}

/** Replace the given [start, end) spans of `text` with [client-ip]; spans that overlap become one. */
function replaceSpans(text, spans) {
  if (!spans.length) return text;
  const merged = [];
  for (const [s, e] of [...spans].sort((x, y) => x[0] - y[0] || y[1] - x[1])) {
    const last = merged.at(-1);
    if (last && s < last[1]) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  }
  let out = '';
  let from = 0;
  for (const [s, e] of merged) {
    out += `${text.slice(from, s)}[client-ip]`;
    from = e;
  }
  return out + text.slice(from);
}

/** Every way of writing an IPv4 address that is searched for as plain text: [needle, kind]. */
function ipv4Needles(ip) {
  const o = ip.split('.').map(Number);
  const n = o[0] * 2 ** 24 + o[1] * 2 ** 16 + o[2] * 2 ** 8 + o[3];
  // Each octet as written, and zero-padded to 2 and 3 digits: 203.0.113.9, 203.000.113.009, ...
  let padded = [[]];
  for (const x of o) {
    const forms = [...new Set([String(x), String(x).padStart(2, '0'), String(x).padStart(3, '0')])];
    padded = padded.flatMap((p) => forms.map((f) => [...p, f]));
  }
  const all = [
    ...padded.map((p) => [p.join('.'), 'dotted']),
    // ec2-203-0-113-9, ip-203-0-113-9 (a dash before is fine), and zero-padded the same way.
    ...padded.map((p) => [p.join('-'), 'dashed']),
    // Reverse DNS: 9.113.0.203, and 9.113.0.203.in-addr.arpa.
    [[...o].reverse().join('.'), 'dotted'],
    // One number: 3405803785, 0xcb007109, 0x0a000001. Only for an address whose first octet is not
    // 0 (every public one): below that the number is short enough to be anything.
    ...(o[0] > 0
      ? [
          [String(n), 'decimal'],
          [`0x${n.toString(16)}`, 'hex'],
          [`0x${n.toString(16).padStart(8, '0')}`, 'hex'],
        ]
      : []),
  ];
  // The same needle twice (0x with eight digits already, an address that reads the same reversed).
  return [...new Map(all.map((x) => [x.join(' '), x])).values()];
}

/** IPv6 addresses that carry an IPv4 address, in canonical form: ::ffff:a.b.c.d and friends. Not
 *  for 0.x.x.x, whose ::a.b.c.d reads :: or ::1. */
const embedding6 = (ip) => (ip.startsWith('0.') ? [] : [`::ffff:${ip}`, `::${ip}`, `64:ff9b::${ip}`].map(canonical6));

/**
 * Where an IPv6 candidate is one of the addresses in `v6`, as [start, end) within the candidate,
 * or null. A candidate can carry a colon that is not part of it (addr:2001:db8::5, or an address
 * at the end of a sentence with a colon), so one or two leading colons and one trailing colon are
 * tried off too.
 */
function matchV6(m, v6) {
  for (let lead = 0; lead <= 2; lead++) {
    if (lead && m[lead - 1] !== ':') break;
    for (let trail = 0; trail <= 1; trail++) {
      if (trail && !m.endsWith(':')) break;
      if (v6.has(canonical6(m.slice(lead, m.length - trail)))) return [lead, m.length - trail];
    }
  }
  return null;
}

/** One pass of redactClientIps: every needle and IPv6 candidate in every view of the text. */
function redactOnce(text, needles, v6) {
  let out = text;
  if (needles.length) {
    const spans = [];
    for (const { view, at } of addressViews(out)) {
      for (const [needle, kind] of needles) for (const [s, e] of findWhole(view, needle, kind)) spans.push(at ? [at[s], at[e]] : [s, e]);
    }
    out = replaceSpans(out, spans);
  }
  if (v6.size) {
    const spans = [];
    for (const { view, at } of addressViews(out)) {
      for (const m of view.matchAll(IPV6_CANDIDATE)) {
        const hit = matchV6(m[0], v6);
        if (hit) spans.push(at ? [at[m.index + hit[0]], at[m.index + hit[1]]] : [m.index + hit[0], m.index + hit[1]]);
      }
    }
    out = replaceSpans(out, spans);
  }
  return out;
}

/**
 * Replace every spelling of the given client addresses with [client-ip]: for an IPv4 address the
 * dotted, zero-padded, dashed, reverse-DNS, one-number and IPv4-in-IPv6 (mapped, compatible,
 * NAT64) forms; an IPv6 address in full or shortened, alone or in brackets. Upper or lower case and characters
 * %-encoded once or twice read the same. Addresses are found by plain string search and by
 * comparing canonical forms; no pattern is built from them. A replacement can make a neighbour
 * whole, so passes repeat until nothing changes (at most 4); the audit flags anything left.
 */
export function redactClientIps(text, clientIps) {
  const needles = [];
  const v6 = new Set();
  for (const ip of clientIps) {
    if (net.isIP(ip) === 4) {
      needles.push(...ipv4Needles(ip));
      for (const c of embedding6(ip)) v6.add(c);
    } else if (net.isIP(ip) === 6) {
      v6.add(canonical6(ip));
    }
  }
  v6.delete(null);
  let out = text;
  for (let pass = 0; pass < 4; pass++) {
    const next = redactOnce(out, needles, v6);
    if (next === out) break;
    out = next;
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
