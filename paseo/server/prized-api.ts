// The few Prized Sandbox API routes this plugin uses (https://prized.dev/docs/sandboxes-api). The key
// goes in the Authorization header only; no error, log line or thrown message ever carries it.

export const API_BASE = "https://api.prized.dev/api/v1";

export interface Sandbox {
  id: string;
  state: string;
  toolboxBase?: string;
  expiresAt?: string | null;
  labels?: Record<string, string>;
  errorReason?: string | null;
}

export interface ExecResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  timedOut?: boolean;
}

export interface CreateSandboxInput {
  template: string;
  vcpu: number;
  memoryGb: number;
  envVars: Record<string, string>;
  labels: Record<string, string>;
  maxRuntimeMin: number;
}

export class PrizedApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

type Fetch = typeof fetch;

export interface PrizedApiOptions {
  base?: string;
  fetch?: Fetch;
  sleep?: (ms: number) => Promise<void>;
}

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export class PrizedApi {
  private key: string;
  private base: string;
  private fetchImpl: Fetch;
  private sleep: (ms: number) => Promise<void>;

  constructor(key: string, options: PrizedApiOptions = {}) {
    this.key = key;
    this.base = options.base ?? API_BASE;
    this.fetchImpl = options.fetch ?? fetch;
    this.sleep = options.sleep ?? wait;
  }

  private async request<T>(method: string, url: string, body?: unknown, headers: Record<string, string> = {}, timeoutMs = 120_000): Promise<{ status: number; data: T }> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await this.send<T>(method, url, body, headers, controller.signal);
    } finally {
      clearTimeout(timer);
    }
  }

  private async send<T>(method: string, url: string, body: unknown, headers: Record<string, string>, signal: AbortSignal): Promise<{ status: number; data: T }> {
    const response = await this.fetchImpl(url, {
      method,
      headers: {
        Authorization: `Bearer ${this.key}`,
        Accept: "application/json",
        "User-Agent": "paseo-prized-sandbox/0.1.0",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      redirect: "error",
      signal,
    });
    const text = await response.text();
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    if (!response.ok) {
      const error = (data as { error?: unknown; message?: unknown; code?: unknown } | null) ?? {};
      const code = typeof error.error === "string" ? error.error : typeof error.code === "string" ? error.code : `http_${response.status}`;
      const message = typeof error.message === "string" ? error.message : `Prized answered ${response.status} (${code})`;
      throw new PrizedApiError(response.status, code, message.split(this.key).join("[key]"));
    }
    return { status: response.status, data: data as T };
  }

  async me(): Promise<{ workspace?: { id?: string; name?: string }; access?: { sandboxes?: string } }> {
    return (await this.request<{ workspace?: { id?: string; name?: string }; access?: { sandboxes?: string } }>("GET", `${this.base}/me`)).data;
  }

  // Idempotency-Key: a create whose answer was lost is found again by the same key, never doubled.
  async createSandbox(input: CreateSandboxInput, idempotencyKey: string): Promise<Sandbox> {
    const body = {
      template: input.template,
      resources: { vcpu: input.vcpu, memoryMb: input.memoryGb * 1024 },
      envVars: input.envVars,
      labels: input.labels,
      maxRuntimeMin: input.maxRuntimeMin,
    };
    const { data } = await this.request<{ sandbox: Sandbox }>("POST", `${this.base}/sandboxes?wait=running`, body, { "Idempotency-Key": idempotencyKey });
    return data.sandbox;
  }

  // null when the sandbox is gone (404).
  async getSandbox(id: string): Promise<Sandbox | null> {
    try {
      return (await this.request<{ sandbox: Sandbox }>("GET", `${this.base}/sandboxes/${encodeURIComponent(id)}`)).data.sandbox;
    } catch (error) {
      if (error instanceof PrizedApiError && error.status === 404) return null;
      throw error;
    }
  }

  async pause(id: string): Promise<Sandbox> {
    return (await this.request<{ sandbox: Sandbox }>("POST", `${this.base}/sandboxes/${encodeURIComponent(id)}/pause?wait=paused`)).data.sandbox;
  }

  async resume(id: string): Promise<Sandbox> {
    return (await this.request<{ sandbox: Sandbox }>("POST", `${this.base}/sandboxes/${encodeURIComponent(id)}/resume?wait=running`)).data.sandbox;
  }

  async remove(id: string): Promise<void> {
    try {
      await this.request("DELETE", `${this.base}/sandboxes/${encodeURIComponent(id)}`);
    } catch (error) {
      if (!(error instanceof PrizedApiError && error.status === 404)) throw error;
    }
  }

  // Read until the sandbox reaches one of `states` (null: until it is gone).
  async waitFor(id: string, states: string[] | null, timeoutMs = 180_000): Promise<Sandbox | null> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const sandbox = await this.getSandbox(id);
      if (states === null ? sandbox === null : sandbox !== null && states.includes(sandbox.state)) return sandbox;
      if (sandbox?.state === "error") throw new PrizedApiError(409, "sandbox_error", sandbox.errorReason ?? "the sandbox went to error");
      if (states !== null && sandbox === null) throw new PrizedApiError(404, "not_found", "the sandbox is gone");
      if (Date.now() > deadline) throw new PrizedApiError(504, "timeout", `the sandbox did not reach ${states?.join(" or ") ?? "deleted"} in time`);
      await this.sleep(2_000);
    }
  }

  // A command on the sandbox's edge. Long commands run as a background process read until done, so
  // no single request outlives Node's fetch limits.
  async run(sandbox: Sandbox, command: string, timeoutSec: number, env: Record<string, string> = {}): Promise<ExecResult> {
    const base = sandbox.toolboxBase;
    if (!base) throw new PrizedApiError(409, "no_toolbox", "the sandbox has no command endpoint yet");
    const started = await this.request<{ process: { processId: string } }>("POST", `${base}/process/start`, { command, timeout: timeoutSec, env });
    const id = started.data.process.processId;
    const deadline = Date.now() + (timeoutSec + 60) * 1000;
    for (;;) {
      const { data } = await this.request<{ process: { running: boolean; exitCode: number | null; timedOut?: boolean; lost?: boolean; stdout?: string; stderr?: string } }>(
        "GET",
        `${base}/process/${encodeURIComponent(id)}`,
      );
      const p = data.process;
      if (!p.running) {
        return { exitCode: p.exitCode ?? (p.lost ? -2 : -1), stdout: p.stdout ?? "", stderr: p.stderr ?? "", timedOut: p.timedOut };
      }
      if (Date.now() > deadline) throw new PrizedApiError(504, "timeout", "a command in the sandbox did not finish in time");
      await this.sleep(2_000);
    }
  }
}
