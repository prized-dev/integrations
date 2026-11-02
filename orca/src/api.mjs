// The Prized Sandbox API routes the plugin uses (https://prized.dev/docs/sandboxes-api?via=orca).
// The key goes in the Authorization header, to api.prized.dev and to the sandbox's own edge address,
// and nowhere else; no error or log line carries it.

import { UserError, VERSION, redact, rememberSecret, sleep as realSleep } from "./util.mjs";

export const API_BASE = "https://api.prized.dev/api/v1";

export class PrizedApiError extends Error {
  constructor(status, code, message, body = null) {
    super(redact(message));
    this.name = "PrizedApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

function isLoopback(url) {
  return url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
}

// The API base: Prized's, or (for tests) a server on this machine. Nothing else gets the key.
export function apiBase(env = process.env) {
  const raw = env.PRIZED_ORCA_API_URL || API_BASE;
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new UserError(`PRIZED_ORCA_API_URL is not a URL: ${raw}`);
  }
  if (!(url.protocol === "https:" && url.hostname === "api.prized.dev") && !isLoopback(url)) {
    throw new UserError(`refusing to send PRIZED_API_KEY to ${url.origin}: only api.prized.dev (or this machine, for tests)`);
  }
  return raw.replace(/\/+$/, "");
}

export class PrizedApi {
  constructor(key, { base = API_BASE, fetchImpl = fetch, sleep = realSleep } = {}) {
    rememberSecret(key);
    this.key = key;
    this.base = base;
    this.loopback = isLoopback(new URL(base));
    this.fetch = fetchImpl;
    this.sleep = sleep;
  }

  // The edge address the API gave for a sandbox: https, or loopback when the API itself is (tests).
  edge(sandbox) {
    const raw = sandbox?.toolboxBase;
    if (!raw) throw new PrizedApiError(409, "no_toolbox", `sandbox ${sandbox?.id} has no command address yet`);
    const url = new URL(raw);
    if (url.protocol !== "https:" && !(this.loopback && isLoopback(url))) {
      throw new PrizedApiError(409, "bad_toolbox", `sandbox ${sandbox.id} has a command address that is not https`);
    }
    return raw.replace(/\/+$/, "");
  }

  async request(method, url, { body, raw, headers = {}, timeoutMs = 120_000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response;
    try {
      response = await this.fetch(url, {
        method,
        headers: {
          Authorization: `Bearer ${this.key}`,
          Accept: "application/json",
          "User-Agent": `orca-prized-plugin/${VERSION}`,
          ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
          ...(raw !== undefined ? { "Content-Type": "application/octet-stream" } : {}),
          ...headers,
        },
        body: body !== undefined ? JSON.stringify(body) : raw,
        redirect: "error",
        signal: controller.signal,
      });
    } catch (error) {
      const why = error.name === "AbortError" ? `no answer in ${Math.round(timeoutMs / 1000)}s` : error.message;
      throw new PrizedApiError(0, "network", `${method} ${new URL(url).pathname}: ${why}`);
    } finally {
      clearTimeout(timer);
    }
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    if (!response.ok) {
      const code = typeof data?.error === "string" ? data.error : `http_${response.status}`;
      const message = typeof data?.message === "string" ? data.message : `Prized answered ${response.status} (${code})`;
      throw new PrizedApiError(response.status, code, message, data);
    }
    return { status: response.status, data };
  }

  async me() {
    return (await this.request("GET", `${this.base}/me`)).data;
  }

  // Idempotency-Key: a create whose answer was lost is found again, never made twice.
  async create(body, idempotencyKey) {
    const { data } = await this.request("POST", `${this.base}/sandboxes?wait=running`, {
      body,
      headers: { "Idempotency-Key": idempotencyKey },
    });
    return data.sandbox;
  }

  // null once it is gone.
  async get(id) {
    try {
      return (await this.request("GET", `${this.base}/sandboxes/${encodeURIComponent(id)}`)).data.sandbox;
    } catch (error) {
      if (error instanceof PrizedApiError && error.status === 404) return null;
      throw error;
    }
  }

  // Read until the sandbox is in one of `states`; fail on error or once it is gone.
  async waitFor(id, states, timeoutMs = 300_000) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const sandbox = await this.get(id);
      if (!sandbox) throw new PrizedApiError(404, "not_found", `sandbox ${id} is gone`);
      if (states.includes(sandbox.state)) return sandbox;
      if (sandbox.state === "error") {
        throw new PrizedApiError(409, "sandbox_error", `sandbox ${id} failed: ${sandbox.errorReason ?? "no reason given"}`);
      }
      if (["deleting", "deleted"].includes(sandbox.state)) throw new PrizedApiError(410, "sandbox_deleted", `sandbox ${id} is being deleted`);
      if (Date.now() > deadline) throw new PrizedApiError(504, "timeout", `sandbox ${id} is still ${sandbox.state}`);
      await this.sleep(2_000);
    }
  }

  async pause(id) {
    const { data } = await this.request("POST", `${this.base}/sandboxes/${encodeURIComponent(id)}/pause?wait=paused`);
    const sandbox = data?.sandbox;
    return sandbox?.state === "paused" ? sandbox : this.waitFor(id, ["paused"]);
  }

  async resume(id) {
    const { data } = await this.request("POST", `${this.base}/sandboxes/${encodeURIComponent(id)}/resume?wait=running`);
    const sandbox = data?.sandbox;
    return sandbox?.state === "running" ? sandbox : this.waitFor(id, ["running"]);
  }

  // Deleting is safe to repeat: a sandbox that is already gone counts as deleted.
  async remove(id) {
    try {
      await this.request("DELETE", `${this.base}/sandboxes/${encodeURIComponent(id)}`);
      return true;
    } catch (error) {
      if (error instanceof PrizedApiError && (error.status === 404 || error.status === 410)) return false;
      throw error;
    }
  }

  async upload(sandbox, remotePath, bytes) {
    const url = `${this.edge(sandbox)}/files/upload?${new URLSearchParams({ path: remotePath })}`;
    await this.request("POST", url, { raw: bytes, timeoutMs: 600_000 });
  }

  // One command, answered when it ends. Kept well under Node fetch's 300 s answer limit.
  async exec(sandbox, command, timeoutSec = 120) {
    const { data } = await this.request("POST", `${this.edge(sandbox)}/process/execute`, {
      body: { command, timeout: timeoutSec },
      timeoutMs: (timeoutSec + 30) * 1000,
    });
    return { exitCode: data?.exitCode, stdout: String(data?.stdout ?? ""), stderr: String(data?.stderr ?? ""), timedOut: Boolean(data?.timedOut) };
  }
}

// Can this key make sandboxes? A key with limited access says so in /me's `access`; one limited to
// some boxes, or a contractor's, reaches no sandboxes at all.
export function canWriteSandboxes(me) {
  if (me?.user?.role === "contractor") return false;
  if (Array.isArray(me?.key?.boxIds) && me.key.boxIds.length) return false;
  return me?.access ? me.access.sandboxes === "write" : true;
}
