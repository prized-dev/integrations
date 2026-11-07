#!/usr/bin/env node
// The agent sign-up check as a GitHub Actions step (action.yml). It asks prized.dev's API to run
// the seven checks against one domain, writes the verdicts to the job summary with a link to the
// domain's row on prized.dev, sets the step's outputs, and fails the step only when fail-on says
// so. One request per run and no retries: a rate limit, an outage or a network error is a
// warning, never a failure, because no check ran.
//
// Inputs come from the environment, as action.yml passes them: INPUT_DOMAIN, INPUT_API,
// INPUT_FAIL_ON, INPUT_FRESH. AGENT_SIGNUP_CHECK_URL points it at another API (the tests use one
// on 127.0.0.1). Node 18 or later, no dependencies.
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const VERSION = '0.1.0';
export const API_URL = 'https://prized.dev/api/agent-signup-check';
export const PAGE_URL = 'https://prized.dev/agent-signup-check';
const METHOD_URL = 'https://github.com/prized-dev/integrations/blob/main/agent-signup-check/METHOD.md';
const USER_AGENT = `agent-signup-check-action/${VERSION} (+https://github.com/prized-dev/integrations/tree/main/agent-signup-check/action)`;
// The API gives a check 40 seconds; this leaves room for the trip.
const TIMEOUT_MS = 90_000;
const MAX_BODY = 1024 * 1024;

const MARK = { pass: 'Pass', partial: 'Partly', fail: 'Fail', skipped: 'Skipped' };
const LOG_MARK = { pass: 'PASS', partial: 'PART', fail: 'FAIL', skipped: 'SKIP' };
const CHANGE = { fixed: 'Fixed', better: 'Better', worse: 'Worse', broke: 'Broke', changed: 'Changed' };
const WORSE = new Set(['worse', 'broke']);

// ---------------------------------------------------------------------------------------
// Inputs.

/** fail-on: "any-regression", "below-score N" (0 to 7) or "never". */
export function parseFailOn(raw) {
  const s = String(raw ?? '').trim().toLowerCase().replace(/\s+/g, ' ') || 'any-regression';
  if (s === 'any-regression' || s === 'never') return { mode: s };
  const m = /^below-score[ :=]*(\d+)$/.exec(s);
  if (m && Number(m[1]) <= 7) return { mode: 'below-score', min: Number(m[1]) };
  throw new Error(`fail-on must be any-regression, below-score N (N from 0 to 7) or never, not "${oneLine(raw).slice(0, 60)}"`);
}

export function parseBool(name, raw, fallback) {
  const s = String(raw ?? '').trim().toLowerCase();
  if (!s) return fallback;
  if (['true', 'yes', '1', 'on'].includes(s)) return true;
  if (['false', 'no', '0', 'off'].includes(s)) return false;
  throw new Error(`${name} must be true or false, not "${oneLine(raw).slice(0, 60)}"`);
}

export function readInputs(env) {
  const domain = String(env.INPUT_DOMAIN ?? '').trim();
  if (!domain) throw new Error('domain is required, like example.com');
  return {
    domain,
    api: String(env.INPUT_API ?? '').trim(),
    failOn: parseFailOn(env.INPUT_FAIL_ON),
    fresh: parseBool('fresh', env.INPUT_FRESH, true),
  };
}

export function requestUrl(base, { domain, api, fresh }) {
  const u = new URL(base);
  u.searchParams.set('domain', domain);
  if (api) u.searchParams.set('api', api);
  if (fresh) u.searchParams.set('fresh', '1');
  return u.toString();
}

// ---------------------------------------------------------------------------------------
// The one request.

/** { status, body, retryAfter } for an HTTP answer, { error } when there was none. */
export async function callApi(url, { timeoutMs = TIMEOUT_MS } = {}) {
  let res;
  try {
    res = await fetch(url, {
      headers: { accept: 'application/json', 'user-agent': USER_AGENT },
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (e) {
    const name = e && (e.name === 'TimeoutError' || e.name === 'AbortError') ? `no answer in ${Math.round(timeoutMs / 1000)} s` : networkError(e);
    return { error: name };
  }
  let text = '';
  try {
    text = await res.text();
  } catch (e) {
    return { error: networkError(e) };
  }
  let body = null;
  if (text.length <= MAX_BODY) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  return { status: res.status, body, retryAfter: res.headers.get('retry-after') };
}

function networkError(e) {
  const cause = e && e.cause;
  const code = cause && (cause.code || cause.name);
  return oneLine(code ? `${code}${cause.message ? `: ${cause.message}` : ''}` : (e && e.message) || String(e)).slice(0, 160);
}

// ---------------------------------------------------------------------------------------
// What an answer means for the step.

const isStr = (s) => typeof s === 'string';
const isInt = (n) => Number.isInteger(n) && n >= 0;
const isCheck = (c) => c && isStr(c.id) && isStr(c.name) && isStr(c.status);
const isResult = (b) =>
  b && typeof b === 'object' && isStr(b.site) && isStr(b.checker) && isInt(b.passed) && isInt(b.partial) && isInt(b.total) &&
  Array.isArray(b.checks) && b.checks.length > 0 && b.checks.every(isCheck);
const isListed = (l) =>
  l && typeof l === 'object' && isStr(l.site) && isStr(l.date) && isStr(l.checker) && isInt(l.passed) && isInt(l.total) &&
  Array.isArray(l.changes) && l.changes.every((c) => c && isStr(c.id) && isStr(c.name) && isStr(c.was) && isStr(c.now) && isStr(c.change));
/** A word from one of the tables above, never from the object prototype. */
const word = (table, key) => (Object.prototype.hasOwnProperty.call(table, key) ? table[key] : null);

/** The domain's place on prized.dev: its row when the board lists it, else the page with the
 *  domain (and api, so it is the same check) filled in. */
export function rowLink(site, listed, api = '') {
  const u = new URL(PAGE_URL);
  if (!listed) {
    u.searchParams.set('domain', site);
    if (api) u.searchParams.set('api', api);
  }
  u.searchParams.set('via', 'github-action');
  // A site from the API is a bare host name; anything else gets the page, not a fragment.
  if (listed && /^[a-z0-9.-]+$/i.test(site)) u.hash = site;
  return u.toString();
}

export const shortDate = (iso) => {
  const d = new Date(`${iso}T12:00:00Z`);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
};

/**
 * The step's verdict for one answer: what happened (`status`), whether the step fails, the one
 * annotation it shows (`level` and `message`) and its outputs. Pure, so the tests can feed it
 * recorded answers.
 */
export function evaluate(answer, { failOn, domain, api = '' }) {
  const base = { status: 'unavailable', fail: false, level: 'warning', result: null, listed: null, regressions: [], row: rowLink(domain, null, api) };
  if (answer.error) {
    return { ...base, message: `Could not reach prized.dev (${answer.error}), so nothing was checked. This does not fail the step.` };
  }
  const { status, body } = answer;
  const said = body && isStr(body.message) ? ` ${oneLine(body.message).slice(0, 300)}` : '';
  if (status === 429) {
    // The API allows 4 fresh checks of a site per address per 10 minutes, and has a few more
    // limits besides; its message says which one this was.
    const secs = Number(answer.retryAfter);
    const wait = secs > 0 ? ` Try again in ${secs < 120 ? `${Math.ceil(secs)} seconds` : `${Math.ceil(secs / 60)} minutes`}.` : '';
    return {
      ...base,
      status: 'rate-limited',
      message: `prized.dev rate-limited this check (HTTP 429), so nothing was checked. This does not fail the step.${said}${wait}`,
    };
  }
  // The API's own refusal of the inputs (bad_domain, api_not_on_domain...), not a proxy's 400.
  if (status === 400 && body && isStr(body.error)) {
    return { ...base, status: 'invalid', fail: true, level: 'error', message: `prized.dev refused the inputs (HTTP 400).${said}` };
  }
  if (status !== 200 || !isResult(body)) {
    const what = status === 200 ? 'an answer this step does not understand' : `HTTP ${status}${body && isStr(body.error) ? ` (${oneLine(body.error).slice(0, 40)})` : ''}`;
    return { ...base, message: `prized.dev answered with ${what}, so nothing was checked. This does not fail the step.${status === 200 ? '' : said}` };
  }

  const result = body;
  const listed = isListed(result.listed) ? result.listed : null;
  const row = rowLink(listed ? listed.site : result.site, listed, api);
  const regressions = listed ? listed.changes.filter((c) => WORSE.has(c.change)) : [];
  const site = oneLine(result.site);
  const score = `${result.passed} of ${result.total}`;
  const out = { ...base, status: 'checked', result, listed, regressions, row, level: 'notice' };
  const worse = regressions.map((c) => `${oneLine(c.name)} (was ${word(MARK, c.was) || oneLine(c.was)}, now ${word(MARK, c.now) || oneLine(c.now)})`).join('; ');
  const rowName = listed ? `its row on prized.dev (${shortDate(listed.date)} run)` : '';
  // A regression fails the step only when this check and the row were made the same way: by the
  // same checker version, and probing the API URL the row probed (no api= of the step's own).
  const unlike = !listed
    ? ''
    : listed.checker !== result.checker
      ? ` The row was scored by ${oneLine(listed.checker)} and this check by ${oneLine(result.checker)}, so the change may be the checker's rules, not your site; this does not fail the step.`
      : result.probedAsListed === false
        ? ' This check probed the api you gave, not the API URL the row probed, so the change may be the URL; this does not fail the step (leave api empty to compare like for like).'
        : '';

  if (failOn.mode === 'below-score' && result.passed < failOn.min) {
    const most = failOn.min > result.total ? ` (only ${result.total} checks run for this domain)` : '';
    return { ...out, fail: true, level: 'error', message: `${site}: ${score} checks pass, below the ${failOn.min} this step asks for${most}.${worse ? ` Worse than ${rowName}: ${worse}.` : ''}` };
  }
  if (regressions.length) {
    const msg = `${site}: ${regressions.length === 1 ? '1 check is' : `${regressions.length} checks are`} worse than ${rowName}: ${worse}.`;
    if (failOn.mode === 'any-regression' && !unlike) return { ...out, fail: true, level: 'error', message: msg };
    return { ...out, level: 'warning', message: msg + unlike };
  }
  if (failOn.mode === 'any-regression' && !listed) {
    return {
      ...out,
      message: `${site}: ${score} checks pass. It isn't on the prized.dev board, so there is no row to compare with; to fail when the score drops, set fail-on: below-score ${result.passed}.`,
    };
  }
  return { ...out, message: `${site}: ${score} checks pass${listed ? `, and none is worse than ${rowName}` : ''}.` };
}

export function outputsOf(v, resultPath) {
  const r = v.result;
  return {
    status: v.status,
    score: r ? `${r.passed}/${r.total}` : '',
    passed: r ? String(r.passed) : '',
    total: r ? String(r.total) : '',
    failed: r ? r.checks.filter((c) => c.status === 'fail').map((c) => c.id).join(',') : '',
    regressions: v.regressions.map((c) => c.id).join(','),
    row: v.row,
    result: resultPath || '',
  };
}

// ---------------------------------------------------------------------------------------
// What it writes. Every string from the answer is untrusted (reasons quote the checked site's
// own files): one line each, Markdown and HTML escaped in the summary, and the log is printed
// with workflow commands stopped.

/** One line: no control characters or line breaks of any kind. */
export function oneLine(s) {
  return String(s ?? '').replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, ' ').trim();
}

/** Text that is safe in a Markdown table cell: no HTML, no links, autolinks or images, no
 *  formatting or math, no pipes. `max` cuts the text, not the escapes. */
export function md(s, max = Infinity) {
  return oneLine(s)
    .slice(0, max)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/[\\`*_{}[\]()#+!|~:.$]/g, (c) => `&#${c.charCodeAt(0)};`)
    // GitHub links an email even through an escaped @; an empty comment splits it.
    .replace(/@/g, '&#64;<!-- -->');
}

/** One line for the log: no legacy ##[command] either (the log also runs with commands stopped). */
const logLine = (s) => oneLine(s).replace(/##\[/g, '## [');

/** A workflow command's message (https://docs.github.com/actions/reference/workflow-commands). */
export const commandData = (s) => String(s).replace(/%/g, '%25').replace(/\r/g, '%0D').replace(/\n/g, '%0A');

export function summaryOf(v, inputs) {
  const lines = [];
  const r = v.result;
  const listed = v.listed;
  if (!r) {
    lines.push(`### Agent sign-up check: ${md(inputs.domain)}, not checked`, '', md(v.message), '');
    lines.push(`[${md(inputs.domain)} on prized.dev](${v.row})`, '');
    return lines.join('\n');
  }
  lines.push(`### Agent sign-up check: ${md(r.site)}, ${r.passed} of ${r.total}${r.partial ? ` (${r.partial} partly)` : ''}`, '');
  const lead = v.fail ? '**Failed.** ' : v.level === 'warning' ? '**Warning.** ' : '';
  lines.push(lead + md(v.message), '');
  const byId = new Map((listed ? listed.changes : []).map((c) => [c.id, c]));
  if (listed) {
    lines.push(`| Check | Now | Since the ${md(shortDate(listed.date))} row | Why |`, '| --- | --- | --- | --- |');
  } else {
    lines.push('| Check | Now | Why |', '| --- | --- | --- |');
  }
  for (const c of r.checks) {
    const now = word(MARK, c.status) || md(c.status, 20);
    const why = md(c.reason, 600);
    if (listed) {
      const ch = byId.get(c.id);
      const since = ch ? `${word(CHANGE, ch.change) || md(ch.change, 20)} (was ${word(MARK, ch.was) || md(ch.was, 20)})` : 'Same';
      lines.push(`| ${md(c.name)} | ${now} | ${ch && WORSE.has(ch.change) ? `**${since}**` : since} | ${why} |`);
    } else {
      lines.push(`| ${md(c.name)} | ${now} | ${why} |`);
    }
  }
  lines.push('');
  const where = listed
    ? `[${md(r.site)} on prized.dev](${v.row}): ${listed.passed} of ${listed.total} in the ${md(shortDate(listed.date))} run${isInt(listed.rank) ? `, rank ${listed.rank}` : ''}.`
    : `${md(r.site)} isn't on the prized.dev board. [Open it on prized.dev](${v.row}), or ask for a row: open a pull request that adds it to [targets.json](https://github.com/prized-dev/integrations/blob/main/agent-signup-check/targets.json).`;
  lines.push(where, '');
  const when = typeof r.checkedAt === 'string' ? md(r.checkedAt.replace(/\.\d+Z$/, 'Z')) : '';
  lines.push(
    `Checked ${when}${r.cached ? ' (a cached answer)' : ''} by ${md(r.checker)}. Prized wrote this check, and the spend-limits draft two of the checks accept; Prized is scored by the same code. [How it checks](${METHOD_URL}).`,
    '',
  );
  return lines.join('\n');
}

export function logOf(v, inputs) {
  const r = v.result;
  if (!r) return [`Agent sign-up check: ${logLine(inputs.domain)}, not checked`, `  ${logLine(v.message)}`, `  ${v.row}`];
  const lines = [`Agent sign-up check: ${logLine(r.site)}, ${r.passed} of ${r.total} pass, ${r.partial} partly (${logLine(r.checker)}, ${logLine(r.checkedAt)})`];
  const byId = new Map((v.listed ? v.listed.changes : []).map((c) => [c.id, c]));
  for (const c of r.checks) {
    const ch = byId.get(c.id);
    const since = ch ? `  [${word(CHANGE, ch.change) || logLine(ch.change).slice(0, 20)}, was ${word(LOG_MARK, ch.was) || logLine(ch.was).slice(0, 20)}]` : '';
    lines.push(`  ${word(LOG_MARK, c.status) || logLine(c.status).slice(0, 4)}  ${logLine(c.name).padEnd(28)} ${logLine(c.reason).slice(0, 400)}${since}`);
  }
  lines.push(`  ${v.row}`);
  return lines;
}

function setOutputs(file, outputs) {
  const chunks = Object.entries(outputs).map(([k, val]) => {
    const delim = `ghadelimiter_${crypto.randomUUID()}`;
    return `${k}<<${delim}\n${oneLine(val)}\n${delim}\n`;
  });
  fs.appendFileSync(file, chunks.join(''));
}

// ---------------------------------------------------------------------------------------

export async function main(env = process.env, out = process.stdout) {
  const print = (s) => out.write(`${s}\n`);
  const refuse = (message) => {
    // Nothing was asked of prized.dev: say why in the annotation, the summary and the outputs.
    if (env.GITHUB_STEP_SUMMARY) fs.appendFileSync(env.GITHUB_STEP_SUMMARY, `### Agent sign-up check: not run\n\n${md(message)}\n\n`);
    if (env.GITHUB_OUTPUT) setOutputs(env.GITHUB_OUTPUT, { status: 'invalid', score: '', passed: '', total: '', failed: '', regressions: '', row: '', result: '' });
    print(`::error title=Agent sign-up check::${commandData(oneLine(message))}`);
    return 1;
  };
  // Node before 18 has no fetch: that would read as a network error, a warning, and pass.
  if (typeof fetch !== 'function' || typeof AbortSignal === 'undefined' || typeof AbortSignal.timeout !== 'function') {
    return refuse(`This step needs Node 18 or later on the runner; this one has ${process.version}. Add actions/setup-node before it.`);
  }
  let inputs;
  try {
    inputs = readInputs(env);
  } catch (e) {
    return refuse(e.message);
  }
  const url = requestUrl(env.AGENT_SIGNUP_CHECK_URL || API_URL, inputs);
  const timeoutMs = Number(env.AGENT_SIGNUP_CHECK_TIMEOUT_MS) > 0 ? Number(env.AGENT_SIGNUP_CHECK_TIMEOUT_MS) : TIMEOUT_MS;
  const answer = await callApi(url, { timeoutMs });
  const v = evaluate(answer, { failOn: inputs.failOn, domain: inputs.domain, api: inputs.api });

  let resultPath = '';
  if (v.result) {
    // A folder of its own per run: two steps in one job may check the same site.
    const dir = fs.mkdtempSync(path.join(env.RUNNER_TEMP || os.tmpdir(), 'agent-signup-check-'));
    resultPath = path.join(dir, `${v.result.site.replace(/[^a-z0-9.-]/gi, '_').slice(0, 253)}.json`);
    fs.writeFileSync(resultPath, JSON.stringify(v.result, null, 1));
  }

  // The log quotes the checked site; nothing in it may run as a workflow command. The lines are
  // built first, so an error can't leave commands stopped.
  const lines = logOf(v, inputs);
  const token = crypto.randomUUID();
  print(`::stop-commands::${token}`);
  for (const line of lines) print(line);
  print(`::${token}::`);

  if (env.GITHUB_STEP_SUMMARY) fs.appendFileSync(env.GITHUB_STEP_SUMMARY, `${summaryOf(v, inputs)}\n`);
  if (env.GITHUB_OUTPUT) setOutputs(env.GITHUB_OUTPUT, outputsOf(v, resultPath));
  print(`::${v.level} title=Agent sign-up check::${commandData(`${v.message} ${v.row}`)}`);
  return v.fail ? 1 : 0;
}

// Run when started as a script, by any path to it: a runner's work folder may sit behind a
// symlink, and Node resolves the module's own URL to the real path.
const invoked = (() => {
  try {
    return Boolean(process.argv[1]) && fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
})();
if (invoked) {
  main().then(
    (code) => process.exit(code),
    (e) => {
      process.stdout.write(`::error title=Agent sign-up check::${commandData(oneLine((e && e.message) || e))}\n`);
      process.exit(1);
    },
  );
}
