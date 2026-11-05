/**
 * mintSandboxKey (src/scoped.ts) against a stand-in for the Prized API's scoped key routes
 * (prized-dev/doppel controlplane core handlers/scopedKeys.ts): on, off (404 before any auth), a release from
 * before them (404 for an unknown route), and answers that must not be passed on. Each case runs through a
 * minimal SDK stand-in, and through the installed @prized/sdk when it has Sandbox.scopedKey (once released,
 * or with PRIZED_SDK_DIST in scripts/test.sh).
 */
import { randomBytes, randomUUID } from 'node:crypto';
import { createServer, type IncomingMessage, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { inspect } from 'node:util';

import { PrizedError, Sandbox } from '@prized/sdk';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { DEFAULT_TTL_SECONDS, mintSandboxKey, PLUGIN_OPS, SandboxKey } from '../src/scoped.ts';

const PARENT = `dcp_${'ab'.repeat(24)}`;

type Mode = 'on' | 'off' | 'old' | 'proxy404' | 'echo' | 'key_required' | 'too_many';

/** The API's side, as far as these routes go. */
class FakeApi {
  mode: Mode = 'on';
  readonly requests: { method: string; path: string; auth: string; body: unknown }[] = [];
  readonly keys: { id: string; token: string; revoked: boolean }[] = [];
  private server: Server | undefined;
  url = '';

  async start(): Promise<void> {
    this.server = createServer(async (req, res) => {
      const body = await read(req);
      const path = new URL(req.url ?? '/', 'http://x').pathname;
      this.requests.push({ method: req.method ?? '', path, auth: String(req.headers.authorization ?? ''), body });
      const send = (status: number, json?: unknown) => {
        res.writeHead(status, json === undefined ? {} : { 'content-type': 'application/json' });
        res.end(json === undefined ? undefined : JSON.stringify(json));
      };
      const route = /^\/api\/v1\/api-keys\/scoped(?:\/([^/]+))?$/.exec(path);
      // The flag off answers 404 before any auth; a release from before it has no such route at all.
      if (!route || this.mode === 'off' || this.mode === 'old') return send(404, { error: 'not_found' });
      // A proxy or a wrong PRIZED_BASE_URL: a page, not the API.
      if (this.mode === 'proxy404') {
        res.writeHead(404, { 'content-type': 'text/html' });
        return res.end('<html><body>Not Found</body></html>');
      }
      if (req.headers.authorization !== `Bearer ${PARENT}`) return send(401, { error: 'unauthorized' });
      if (this.mode === 'key_required') return send(403, { error: 'key_required', message: 'A scoped key is minted from an API key.' });
      if (this.mode === 'too_many') return send(429, { error: 'too_many_scoped_keys', message: 'This key already has 100 live scoped keys.' });
      if (route[1] !== undefined && req.method === 'DELETE') {
        const key = this.keys.find((k) => k.id === route[1] && !k.revoked);
        if (!key) return send(404, { error: 'not_found' });
        key.revoked = true;
        return send(204);
      }
      if (req.method !== 'POST') return send(404, { error: 'not_found' });
      const token = this.mode === 'echo' ? PARENT : `dcp_${randomBytes(24).toString('hex')}`;
      const key = { id: randomUUID(), token, revoked: false };
      this.keys.push(key);
      const b = body as { ttlSeconds?: number; ops?: string[]; labels?: Record<string, string> };
      return send(201, {
        token,
        id: key.id,
        parentId: randomUUID(),
        expiresAt: new Date(Date.now() + (b.ttlSeconds ?? 3600) * 1000).toISOString(),
        scope: { ops: b.ops ?? [...PLUGIN_OPS], sandboxIds: null, labels: b.labels ?? null },
        spendCapUsd: null,
      });
    });
    await new Promise<void>((resolve) => this.server!.listen(0, '127.0.0.1', resolve));
    this.url = `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`;
  }

  close(): Promise<void> {
    return new Promise((resolve) => {
      this.server?.closeAllConnections();
      this.server?.close(() => resolve());
    });
  }
}

async function read(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  const text = Buffer.concat(chunks).toString('utf8');
  return text ? JSON.parse(text) : null;
}

/** The SDK's two calls over plain fetch: the API's contract, without the SDK's retries. */
const standIn = {
  Sandbox: {
    async scopedKey(o: Record<string, unknown>) {
      const { apiKey, baseUrl, edgeUrl: _edge, ...body } = o;
      let res: Response;
      try {
        res = await fetch(`${baseUrl}/api/v1/api-keys/scoped`, { method: 'POST', headers: { authorization: `Bearer ${apiKey}`, 'content-type': 'application/json' }, body: JSON.stringify(body) });
      } catch (cause) {
        throw new PrizedError('network_error', null, { cause });
      }
      const text = await res.text();
      let doc: Record<string, string> = {};
      try {
        doc = JSON.parse(text) as Record<string, string>;
      } catch {
        // An answer that is not the API's (a proxy's page), as the SDK reads it: http_error.
      }
      if (res.status !== 201) throw new PrizedError(doc.error ?? 'http_error', doc.message, { status: res.status });
      return { id: doc.id!, token: doc.token!, expiresAt: new Date(doc.expiresAt!) };
    },
    async revokeScopedKey(id: string, o: Record<string, unknown>) {
      const res = await fetch(`${o.baseUrl}/api/v1/api-keys/scoped/${id}`, { method: 'DELETE', headers: { authorization: `Bearer ${o.apiKey}` } });
      if (res.status === 204) return true;
      if (res.status === 404) return false;
      throw new PrizedError('http_error', null, { status: res.status });
    },
  },
};

const installedHasIt = typeof (Sandbox as unknown as { scopedKey?: unknown }).scopedKey === 'function';
const sdks: [string, { Sandbox: unknown }][] = [['an SDK stand-in', standIn]];
if (installedHasIt) sdks.push(['the installed @prized/sdk', { Sandbox }]);

let api: FakeApi;
beforeEach(async () => {
  api = new FakeApi();
  await api.start();
});
afterEach(() => api.close());

const hostEnv = () => ({ PRIZED_API_KEY: PARENT, PRIZED_BASE_URL: api.url });

describe.each(sdks)('mintSandboxKey through %s', (_name, sdk) => {
  it('mints a key for what the plugin does, and hands back only it', async () => {
    const key = await mintSandboxKey({ env: hostEnv(), sdk, labels: { run: 'r1' }, spendCapUsd: 5 });
    expect(key.available).toBe(true);
    if (!key.available) return;
    const minted = api.keys[0]!;
    expect(key.id).toBe(minted.id);
    expect(key.env).toEqual({ PRIZED_API_KEY: minted.token, PRIZED_BASE_URL: api.url });
    expect(key.env.PRIZED_API_KEY).not.toBe(PARENT);
    expect(key.expiresAt.getTime() - Date.now()).toBeGreaterThan((DEFAULT_TTL_SECONDS - 60) * 1000);
    const [mint] = api.requests;
    expect(mint).toMatchObject({ method: 'POST', path: '/api/v1/api-keys/scoped', auth: `Bearer ${PARENT}` });
    expect(mint!.body).toEqual({ name: 'opencode-prized', ttlSeconds: DEFAULT_TTL_SECONDS, ops: [...PLUGIN_OPS], labels: { run: 'r1' }, spendCapUsd: 5 });
  });

  it('the key object never shows the secret when printed or serialized', async () => {
    const key = (await mintSandboxKey({ env: hostEnv(), sdk })) as SandboxKey;
    const secret = api.keys[0]!.token;
    for (const text of [inspect(key), inspect({ key }, { depth: 5, showHidden: true }), String(key), JSON.stringify(key), JSON.stringify({ ...key })]) {
      expect(text).not.toContain(secret.slice(4, 16));
      expect(text).not.toContain(PARENT.slice(4, 16));
    }
    expect(JSON.parse(JSON.stringify(key))).toEqual({ available: true, id: key.id, expiresAt: key.expiresAt.toISOString() });
  });

  it('revoke ends it, with your own key', async () => {
    const key = (await mintSandboxKey({ env: hostEnv(), sdk })) as SandboxKey;
    expect(await key.revoke()).toBe(true);
    expect(api.keys[0]!.revoked).toBe(true);
    expect(api.requests.at(-1)).toMatchObject({ method: 'DELETE', path: `/api/v1/api-keys/scoped/${key.id}`, auth: `Bearer ${PARENT}` });
    expect(await key.revoke()).toBe(false);
  });

  it.each(['off', 'old', 'proxy404'] as const)('an API answering the mint with 404 (%s) is "not available", and nothing is minted', async (mode) => {
    api.mode = mode;
    const key = await mintSandboxKey({ env: hostEnv(), sdk });
    expect(key).toEqual({ available: false, reason: expect.stringMatching(/does not offer scoped keys.*404/) });
    expect(api.keys).toHaveLength(0);
    expect(api.requests).toHaveLength(1);
  });

  it('an answer carrying your own key is refused, never passed on', async () => {
    api.mode = 'echo';
    const error = await mintSandboxKey({ env: hostEnv(), sdk }).then(() => undefined, (e: unknown) => e as Error);
    // The SDK refuses it itself (bad_response); the helper does whatever SDK it is given.
    expect(error?.message).toMatch(/your own key, so it was not passed on|the key it was minted from, so it was not used/);
    expect(`${String(error)} ${inspect(error, { depth: 5 })}`).not.toContain(PARENT.slice(4, 16));
  });

  it.each([
    ['key_required', 403],
    ['too_many_scoped_keys', 429],
  ] as const)('any other refusal throws (%s): a key that cannot mint is a setup to fix, not a reason to carry on', async (code, status) => {
    api.mode = code === 'key_required' ? 'key_required' : 'too_many';
    await expect(mintSandboxKey({ env: hostEnv(), sdk })).rejects.toMatchObject({ code, status });
    expect(api.keys).toHaveLength(0);
  });

  it('an API it cannot reach throws, rather than "not available"', async () => {
    const url = api.url;
    await api.close();
    await expect(mintSandboxKey({ env: { PRIZED_API_KEY: PARENT, PRIZED_BASE_URL: url }, sdk })).rejects.toMatchObject({ code: 'network_error' });
  });
});

describe('mintSandboxKey before anything is sent', () => {
  it('needs your own key in the environment', async () => {
    await expect(mintSandboxKey({ env: { PRIZED_BASE_URL: api.url }, sdk: standIn })).rejects.toThrow(/No PRIZED_API_KEY/);
    expect(api.requests).toHaveLength(0);
  });

  it('an SDK from before scoped keys is "not available", and nothing is sent', async () => {
    const key = await mintSandboxKey({ env: hostEnv(), sdk: { Sandbox: {} } });
    expect(key).toEqual({ available: false, reason: expect.stringMatching(/no Sandbox\.scopedKey/) });
    expect(api.requests).toHaveLength(0);
  });

  it.runIf(!installedHasIt)('the installed @prized/sdk (from before scoped keys) is "not available"', async () => {
    expect(await mintSandboxKey({ env: hostEnv() })).toMatchObject({ available: false });
    expect(api.requests).toHaveLength(0);
  });
});
