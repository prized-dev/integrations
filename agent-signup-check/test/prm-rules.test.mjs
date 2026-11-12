// Offline tests: no network, no keys. They pin the protected resource metadata rules that
// METHOD.md states (request order, which file a verdict rests on, and what happens to metadata
// for an MCP server), using made-up domains shaped like rows of the 2026-11-12 run.
// Run with `node --test agent-signup-check/test/prm-rules.test.mjs` from the repo root.
import assert from 'node:assert/strict';
import test from 'node:test';
import { checkSite } from '../agent-signup-check.mjs';

const md = (body) => ({ status: 200, headers: { 'content-type': 'text/markdown; charset=utf-8' }, body });
const json = (o, extra = {}) => ({ status: 200, headers: { 'content-type': 'application/json' }, body: JSON.stringify(o), ...extra });
const notFound = { status: 404, headers: { 'content-type': 'text/html' }, body: '<!doctype html><title>404</title>' };
const stub = (pages) => async (url) => ({ url, finalUrl: url, hops: [], ...structuredClone(pages[url] ?? notFound) });
const cell = (r, id) => r.checks.find((c) => c.id === id);
const asked = (r) => r.requests.map((q) => q.url);
const challenge401 = (metadataUrl) => ({
  status: 401,
  headers: { 'content-type': 'application/json', 'www-authenticate': `Bearer resource_metadata="${metadataUrl}"` },
  body: '{"error":"unauthorized"}',
});

test('metadata the 401 names must cover the probed API URL; the probed URL decides (a platform whose 401 names metadata for another API path)', async () => {
  // One site serves its API under /api/v1/ but names /api/box/v1/ as the resource.
  const meta = { resource: 'https://one.example/api/box/v1/', authorization_servers: ['https://one.example/'] };
  const pages = {
    'https://one.example/api/v1/me': challenge401('https://one.example/api/box/.well-known/oauth-protected-resource'),
    'https://one.example/api/box/v1/me': challenge401('https://one.example/api/box/.well-known/oauth-protected-resource'),
    'https://one.example/api/box/.well-known/oauth-protected-resource': json(meta),
    'https://one.example/.well-known/oauth-protected-resource': json(meta),
  };
  const outside = await checkSite('one.example', { api: 'https://one.example/api/v1/me', get: stub(pages) });
  assert.equal(cell(outside, 'prm').status, 'partial');
  assert.equal(cell(outside, 'prm').via, 'www-authenticate');
  assert.match(cell(outside, 'prm').reason, /does not match the URL the metadata came from/);
  // The site's own copy has the same resource, and is not at the path RFC 9728 section 3.1 builds
  // for it, so it does not rescue the row.
  assert.ok(asked(outside).includes('https://one.example/.well-known/oauth-protected-resource'));
  const inside = await checkSite('one.example', { api: 'https://one.example/api/box/v1/me', get: stub(pages) });
  assert.equal(cell(inside, 'prm').status, 'pass');
});

test('when no metadata may be used, the verdict rests on the first file read: the one the 401 named (a platform whose site metadata names its MCP server)', async () => {
  const pages = {
    'https://two.example/auth.md': md('# auth.md\n\nMCP server metadata: https://mcp.two.example/.well-known/oauth-protected-resource\n'),
    'https://api.two.example/v0/inboxes': challenge401('https://api.two.example/.well-known/oauth-protected-resource'),
    // The API's own file: right resource, no authorization server.
    'https://api.two.example/.well-known/oauth-protected-resource': json({ resource: 'https://api.two.example', bearer_methods_supported: ['header'] }),
    // The site's file: an authorization server, but the resource is the MCP server.
    'https://two.example/.well-known/oauth-protected-resource': json(
      { resource: 'https://mcp.two.example', authorization_servers: ['https://auth.two.example'] },
      { finalUrl: 'https://www.two.example/.well-known/oauth-protected-resource', hops: [{ url: 'https://two.example/.well-known/oauth-protected-resource', status: 307 }] },
    ),
  };
  const r = await checkSite('two.example', { api: 'https://api.two.example/v0/inboxes', get: stub(pages) });
  assert.equal(cell(r, 'prm').status, 'partial');
  assert.equal(cell(r, 'prm').via, 'www-authenticate');
  assert.equal(cell(r, 'prm').url, 'https://api.two.example/.well-known/oauth-protected-resource');
  assert.match(cell(r, 'prm').reason, /names no authorization_servers/);
  // Both were read, in METHOD.md's order; neither is usable, so the site's is not the one reported.
  const order = asked(r).filter((u) => u.includes('oauth-protected-resource'));
  assert.deepEqual(order, ['https://api.two.example/.well-known/oauth-protected-resource', 'https://two.example/.well-known/oauth-protected-resource']);
  // Had the site's file been the one reported, the verdict would be the same: partly.
  const siteOnly = await checkSite('two.example', { api: 'https://api.two.example/v0/inboxes', get: stub({ ...pages, 'https://api.two.example/.well-known/oauth-protected-resource': notFound }) });
  assert.equal(cell(siteOnly, 'prm').status, 'partial');
  assert.match(cell(siteOnly, 'prm').reason, /resource \(https:\/\/mcp\.two\.example\) does not match/);
  assert.equal(r.passed, siteOnly.passed);
});

test('an MCP host that a platform names in its own auth.md is not requested (a platform whose MCP metadata sits on a separate host)', async () => {
  const pages = {
    'https://three.example/auth.md': md('# Authenticating AI agents with Three\n\nOAuth metadata: https://mcp.three.example/.well-known/oauth-protected-resource\n'),
    'https://api.three.example/v1/sites': { status: 401, headers: { 'content-type': 'application/json', 'www-authenticate': 'Bearer realm="three"' }, body: '{}' },
    'https://mcp.three.example/.well-known/oauth-protected-resource': json({ resource: 'https://mcp.three.example', authorization_servers: ['https://mcp.three.example'] }),
  };
  const r = await checkSite('three.example', { api: 'https://api.three.example/v1/sites', get: stub(pages) });
  assert.equal(cell(r, 'prm').status, 'fail');
  assert.ok(!asked(r).some((u) => new URL(u).hostname === 'mcp.three.example'), asked(r).join(' '));
});

test("a site path that redirects to an MCP server's metadata is followed, and passes when the resource matches where it landed (a platform that redirects its site path to its MCP server)", async () => {
  const landed = 'https://mcp.four.example/.well-known/oauth-protected-resource/mcp';
  const pages = {
    'https://api.four.example/search': { status: 401, headers: { 'content-type': 'application/json', 'www-authenticate': 'Bearer realm="four"' }, body: '{}' },
    'https://four.example/.well-known/oauth-protected-resource': json(
      { resource: 'https://mcp.four.example/mcp', authorization_servers: ['https://auth.four.example'] },
      { finalUrl: landed, hops: [{ url: 'https://four.example/.well-known/oauth-protected-resource', status: 308 }] },
    ),
  };
  const r = await checkSite('four.example', { api: 'https://api.four.example/search', get: stub(pages) });
  assert.equal(cell(r, 'prm').status, 'pass');
  assert.equal(cell(r, 'prm').via, 'well-known');
});

test("a copy of an MCP server's metadata served on the site is partly: its resource is another server (a platform whose site metadata names another resource)", async () => {
  const pages = {
    'https://api.five.example/v2/users/me': { status: 401, headers: { 'content-type': 'application/json' }, body: '{}' },
    'https://five.example/.well-known/oauth-protected-resource': json({ resource: 'https://mcp.five.example', authorization_servers: ['https://auth.five.example'] }),
  };
  const r = await checkSite('five.example', { api: 'https://api.five.example/v2/users/me', get: stub(pages) });
  assert.equal(cell(r, 'prm').status, 'partial');
  assert.match(cell(r, 'prm').reason, /resource \(https:\/\/mcp\.five\.example\) does not match/);
});
