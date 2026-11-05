/**
 * A key for OpenCode running inside a sandbox, minted on the machine that holds your own key.
 *
 * Normally the plugin runs on your machine and no key goes into any sandbox. When OpenCode itself runs in
 * a sandbox (a CI runner, a cloud agent), the plugin in there needs a key to make its tool sandbox. Your
 * long-lived key should never go in: on the machine that holds it, mint a short-lived scoped key and pass
 * only that in. It reaches only the sandboxes it makes (and their forks) plus any you select by `labels`,
 * for what this plugin does with them, and it expires.
 *
 *   const key = await mintSandboxKey({ spendCapUsd: 5 });    // PRIZED_API_KEY here is your own key
 *   if (!key.available) throw new Error(key.reason);          // nothing was minted: do what you did before
 *   const runner = await Sandbox.create({ envVars: key.env }); // run OpenCode with this plugin in there
 *   ...
 *   await key.revoke();
 *
 * It fails closed. An installed @prized/sdk without `Sandbox.scopedKey`, or an API that answers the mint
 * with 404 (scoped keys switched off, or a release from before them), is `{ available: false, reason }`
 * and nothing is minted. Any other refusal throws. It never hands back your own key.
 *
 * Host side only: nothing in the plugin calls it. Erasable TypeScript only, so Node 22.18+ runs it from a
 * checkout as it is.
 */

import { PrizedError, Sandbox } from '@prized/sdk';

import { readEnv } from './config.ts';

/** What the plugin does with sandboxes (start, reconnect, run tools, pause, resume, fork, delete): all a key for it needs. */
export const PLUGIN_OPS = ['read', 'create', 'exec', 'pause', 'resume', 'fork', 'delete'] as const;
/** Long enough for a working session. Up to 86 400 (a day), and never past your own key's expiry. */
export const DEFAULT_TTL_SECONDS = 4 * 3600;

export interface MintOptions {
  /** How long the key lives, in seconds (60 to 86 400). Default 4 hours. */
  ttlSeconds?: number;
  /** It also reaches every sandbox carrying all of these labels (1 to 8 pairs), such as one a run before kept. */
  labels?: Record<string, string>;
  /** Caps what everything the key creates may spend, in dollars. */
  spendCapUsd?: number;
  /** Its name in your workspace's audit log. Default "opencode-prized". */
  name?: string;
  /** Where your key and the API's address come from: PRIZED_API_KEY, PRIZED_BASE_URL, PRIZED_EDGE_URL. Default process.env. */
  env?: NodeJS.ProcessEnv;
  /** The SDK (a test seam). */
  sdk?: { Sandbox: unknown };
}

export interface Unavailable {
  available: false;
  /** Why, for a log line: nothing was minted. */
  reason: string;
}

/** The SDK's ScopedKey, as far as this needs it (an SDK from before scoped keys has no such type). */
interface ScopedKeyLike {
  readonly id: string;
  readonly token: string;
  readonly expiresAt: Date;
}

interface ScopedSandbox {
  scopedKey?: (options: Record<string, unknown>) => Promise<ScopedKeyLike>;
  revokeScopedKey?: (keyId: string, options: Record<string, unknown>) => Promise<boolean>;
}

const INSPECT = Symbol.for('nodejs.util.inspect.custom');

/**
 * A minted key. `env` holds the secret: pass it to the sandbox (`envVars`), never log it. Printing,
 * `JSON.stringify` or `String()` of the object itself shows the id and expiry only.
 */
export class SandboxKey {
  readonly available = true as const;
  readonly id: string;
  readonly expiresAt: Date;
  readonly #env: Readonly<Record<string, string>>;
  readonly #revoke: () => Promise<boolean>;

  constructor(id: string, expiresAt: Date, env: Record<string, string>, revoke: () => Promise<boolean>) {
    this.id = id;
    this.expiresAt = expiresAt;
    this.#env = Object.freeze({ ...env });
    this.#revoke = revoke;
  }

  /** `PRIZED_API_KEY` (the scoped key) and, if you set them here, `PRIZED_BASE_URL` and `PRIZED_EDGE_URL`. */
  get env(): Record<string, string> {
    return { ...this.#env };
  }

  /** End it now, with your own key. False when it was no longer live (expired, or revoked already). */
  revoke(): Promise<boolean> {
    return this.#revoke();
  }

  toJSON(): { available: true; id: string; expiresAt: string } {
    return { available: true, id: this.id, expiresAt: this.expiresAt.toISOString() };
  }

  toString(): string {
    return `SandboxKey ${this.id} (expires ${this.expiresAt.toISOString()})`;
  }

  [INSPECT](): string {
    return `SandboxKey ${JSON.stringify(this.toJSON())}`;
  }
}

const isPrizedError = (err: unknown): err is PrizedError =>
  err instanceof PrizedError || (err instanceof Error && err.name === 'PrizedError');

/** Mint a scoped key for OpenCode in a sandbox, from the key in this process's environment. See the top of this file. */
export async function mintSandboxKey(options: MintOptions = {}): Promise<SandboxKey | Unavailable> {
  const { apiKey, baseUrl, edgeUrl } = readEnv(options.env ?? process.env);
  if (!apiKey) throw new Error('No PRIZED_API_KEY here to mint a scoped key from: run this where your own key is.');
  const api = (options.sdk?.Sandbox ?? Sandbox) as ScopedSandbox;
  if (typeof api.scopedKey !== 'function' || typeof api.revokeScopedKey !== 'function') {
    return { available: false, reason: 'the installed @prized/sdk has no Sandbox.scopedKey: update @prized/sdk to mint scoped keys' };
  }
  const client = { apiKey, baseUrl, edgeUrl };
  let key: ScopedKeyLike;
  try {
    key = await api.scopedKey({
      ...client,
      name: options.name ?? 'opencode-prized',
      ttlSeconds: options.ttlSeconds ?? DEFAULT_TTL_SECONDS,
      ops: [...PLUGIN_OPS],
      ...(options.labels === undefined ? {} : { labels: options.labels }),
      ...(options.spendCapUsd === undefined ? {} : { spendCapUsd: options.spendCapUsd }),
    });
  } catch (err) {
    // The route is not there: scoped keys are off on this API, or it predates them. (The mint sends no
    // sandboxIds, the only other way it answers 404.)
    if (isPrizedError(err) && err.status === 404) {
      return { available: false, reason: `this Prized API does not offer scoped keys (it answered the mint with 404 ${err.code})` };
    }
    throw err;
  }
  // Whatever answered, your own key never goes on to a sandbox.
  if (key.token === apiKey) throw new Error('The Prized API answered the mint with your own key, so it was not passed on.');
  const env: Record<string, string> = { PRIZED_API_KEY: key.token };
  if (baseUrl) env.PRIZED_BASE_URL = baseUrl;
  if (edgeUrl) env.PRIZED_EDGE_URL = edgeUrl;
  const revokeScopedKey = api.revokeScopedKey.bind(api);
  return new SandboxKey(key.id, key.expiresAt, env, () => revokeScopedKey(key.id, client));
}
