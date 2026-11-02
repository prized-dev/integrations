// Prized sandbox hosts for Paseo. Create makes a sandbox, installs the pinned Paseo CLI in it, starts
// its daemon and keeps a relay pairing link. Stop pauses the sandbox: memory, the daemon and any agent
// mid-turn are kept, and only storage is billed. Resume picks up where it stopped; the daemon
// reconnects to the relay by itself, so the host needs no new pairing. Delete removes the sandbox.

import { randomBytes } from "node:crypto";

import { installScript, pairScript, parsePairingUrl, startDaemonScript, tail } from "./bootstrap.ts";
import { PrizedApi, PrizedApiError, type Sandbox } from "./prized-api.ts";
import { DEFAULT_SHAPE, Store, type HostRecord, type Phase, type Shape } from "./store.ts";

export const TEMPLATE = "prized-agent-v1";
const BUSY: Phase[] = ["creating", "installing", "starting", "pausing", "resuming", "deleting"];

export interface HostsApi {
  add(input: { connection: string; label?: string; metadata?: Record<string, string>; sourceKey?: string }): Promise<{ registrationId: string; status: string }>;
  remove(registrationId: string): Promise<void>;
}

export interface ServiceOptions {
  stateDir: string;
  env?: NodeJS.ProcessEnv;
  api?: (key: string) => PrizedApi;
  log?: (line: string) => void;
}

export class UserError extends Error {}

export interface PublicHostView {
  id: string;
  label: string;
  sandboxId?: string;
  phase: Phase;
  sandboxState?: string;
  busy: boolean;
  error?: string;
  paired: boolean;
  addedAsHost: boolean;
  createdAt: string;
  expiresAt?: string;
}

export class HostService {
  readonly store: Store;
  private env: NodeJS.ProcessEnv;
  private makeApi: (key: string) => PrizedApi;
  private log: (line: string) => void;
  private running = new Map<string, Promise<void>>();
  private ready: Promise<void>;

  constructor(options: ServiceOptions) {
    this.store = new Store(options.stateDir);
    this.env = options.env ?? process.env;
    this.makeApi = options.api ?? ((key) => new PrizedApi(key));
    this.log = options.log ?? ((line) => console.log(line));
    // Work that was in flight when the plugin last stopped did not finish: say so, and offer Retry.
    this.ready = this.store
      .updateHosts((file) => {
        for (const host of Object.values(file.hosts)) {
          if (BUSY.includes(host.phase)) {
            host.failedAt = host.phase;
            host.phase = "failed";
            host.error = "Interrupted when the plugin stopped. Retry picks it up.";
          }
        }
      })
      .then(() => undefined);
  }

  // ------------------------------------------------------------------ settings

  private async apiKey(): Promise<string> {
    const secrets = await this.store.readSecrets();
    const key = secrets.prizedApiKey || this.env.PRIZED_API_KEY;
    if (!key) throw new UserError("Add a Prized API key in this plugin's settings first.");
    return key;
  }

  private async api(): Promise<PrizedApi> {
    return this.makeApi(await this.apiKey());
  }

  async saveSettings(input: {
    prizedApiKey?: string;
    anthropicApiKey?: string;
    openaiApiKey?: string;
    vcpu?: number;
    memoryGb?: number;
    maxRuntimeHours?: number;
  }): Promise<{ workspace?: string }> {
    await this.ready;
    let workspace: string | undefined;
    if (input.prizedApiKey) {
      const key = input.prizedApiKey.trim();
      if (!/^dcp_[0-9a-f]{48}$/.test(key)) throw new UserError("That is not a Prized API key (dcp_ and 48 hex characters).");
      let me: Awaited<ReturnType<PrizedApi["me"]>>;
      try {
        me = await this.makeApi(key).me();
      } catch (error) {
        throw new UserError(error instanceof PrizedApiError && error.status === 401 ? "Prized did not accept that key." : "Could not reach Prized to check the key.");
      }
      if (me.access?.sandboxes !== "write") throw new UserError("That key cannot create sandboxes: give it Sandboxes: Write, or use a Full access key.");
      workspace = me.workspace?.name ?? me.workspace?.id;
    }
    await this.store.updateSecrets((s) => {
      if (input.prizedApiKey !== undefined) {
        s.prizedApiKey = input.prizedApiKey.trim() || undefined;
        s.workspace = s.prizedApiKey ? workspace : undefined;
      }
      if (input.anthropicApiKey !== undefined) s.anthropicApiKey = input.anthropicApiKey.trim() || undefined;
      if (input.openaiApiKey !== undefined) s.openaiApiKey = input.openaiApiKey.trim() || undefined;
    });
    await this.store.updateHosts((file) => {
      const shape: Shape = { ...DEFAULT_SHAPE, ...file.shape };
      if (input.vcpu !== undefined) shape.vcpu = input.vcpu;
      if (input.memoryGb !== undefined) shape.memoryGb = input.memoryGb;
      if (input.maxRuntimeHours !== undefined) shape.maxRuntimeHours = input.maxRuntimeHours;
      file.shape = shape;
    });
    return { workspace };
  }

  // ------------------------------------------------------------------ status

  async status(refresh: boolean) {
    await this.ready;
    if (refresh) await this.refresh();
    const [file, secrets] = await Promise.all([this.store.readHosts(), this.store.readSecrets()]);
    const shape = { ...DEFAULT_SHAPE, ...file.shape };
    return {
      settings: {
        apiKeySource: secrets.prizedApiKey ? ("saved" as const) : this.env.PRIZED_API_KEY ? ("environment" as const) : ("none" as const),
        workspace: secrets.prizedApiKey ? secrets.workspace : undefined,
        anthropicKeySaved: Boolean(secrets.anthropicApiKey),
        openaiKeySaved: Boolean(secrets.openaiApiKey),
        ...shape,
      },
      hosts: Object.values(file.hosts)
        .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
        .map((h): PublicHostView => ({
          id: h.id,
          label: h.label,
          sandboxId: h.sandboxId,
          phase: h.phase,
          sandboxState: h.sandboxState,
          busy: this.running.has(h.id),
          error: h.error,
          paired: Boolean(secrets.pairing[h.id]),
          addedAsHost: Boolean(h.registrationId),
          createdAt: h.createdAt,
          expiresAt: h.expiresAt,
        })),
    };
  }

  // Read each idle host's sandbox (a read is not activity: it never wakes or keeps a sandbox up).
  private async refresh(): Promise<void> {
    const file = await this.store.readHosts();
    const idle = Object.values(file.hosts).filter((h) => h.sandboxId && !this.running.has(h.id) && h.phase !== "gone");
    if (!idle.length) return;
    let api: PrizedApi;
    try {
      api = await this.api();
    } catch {
      return;
    }
    for (const host of idle) {
      try {
        const sandbox = await api.getSandbox(host.sandboxId!);
        if (!sandbox) {
          await this.store.patchHost(host.id, { phase: "gone", sandboxState: "deleted", expiresAt: undefined, error: "The sandbox is gone: deleted, or its run time ended." });
          continue;
        }
        const patch: Partial<HostRecord> = { sandboxState: sandbox.state, expiresAt: sandbox.expiresAt ?? undefined };
        if (host.phase === "ready" && sandbox.state === "paused") patch.phase = "paused";
        if (host.phase === "paused" && sandbox.state === "running") patch.phase = "ready";
        await this.store.patchHost(host.id, patch);
      } catch {
        // Status is best effort: the next refresh tries again.
      }
    }
  }

  // ------------------------------------------------------------------ operations

  private async host(id: string): Promise<HostRecord> {
    await this.ready;
    const host = (await this.store.readHosts()).hosts[id];
    if (!host) throw new UserError("No such host.");
    return host;
  }

  // Run one operation on a host in the background; the client polls status. One at a time per host.
  private start(id: string, work: () => Promise<void>): void {
    if (this.running.has(id)) throw new UserError("Something is already running on this host.");
    const op = (async () => {
      try {
        await work();
      } catch (error) {
        const message = error instanceof UserError || error instanceof PrizedApiError ? error.message : `Unexpected error: ${(error as Error)?.message ?? error}`;
        const current = (await this.store.readHosts()).hosts[id];
        if (current) await this.store.patchHost(id, { failedAt: current.phase === "failed" ? current.failedAt : current.phase, phase: "failed", error: message });
        this.log(`prized-sandbox: host ${id} failed: ${message}`);
      } finally {
        this.running.delete(id);
      }
    })();
    this.running.set(id, op);
  }

  async create(label?: string): Promise<{ hostId: string }> {
    await this.ready;
    await this.apiKey();
    const id = randomBytes(6).toString("hex");
    const now = new Date().toISOString();
    const record: HostRecord = { id, label: (label ?? "").trim() || `prized-${id.slice(0, 4)}`, phase: "creating", createdAt: now, updatedAt: now };
    await this.store.updateHosts((file) => {
      file.hosts[id] = record;
    });
    this.start(id, () => this.provision(id));
    return { hostId: id };
  }

  // Create (once), install, start, pair. Each step is safe to run again, so Retry runs this whole.
  private async provision(id: string): Promise<void> {
    const api = await this.api();
    let host = await this.host(id);
    let sandbox: Sandbox | null;
    if (!host.sandboxId) {
      await this.store.patchHost(id, { phase: "creating", error: undefined });
      const [file, secrets] = await Promise.all([this.store.readHosts(), this.store.readSecrets()]);
      const shape = { ...DEFAULT_SHAPE, ...file.shape };
      const envVars: Record<string, string> = {};
      if (secrets.anthropicApiKey) envVars.ANTHROPIC_API_KEY = secrets.anthropicApiKey;
      if (secrets.openaiApiKey) envVars.OPENAI_API_KEY = secrets.openaiApiKey;
      // The same idempotency key on a retry finds the sandbox a lost answer created, instead of a second one.
      sandbox = await api.createSandbox(
        { template: TEMPLATE, vcpu: shape.vcpu, memoryGb: shape.memoryGb, envVars, labels: { via: "paseo", "paseo-host": id }, maxRuntimeMin: shape.maxRuntimeHours * 60 },
        `paseo-host-${id}`,
      );
      host = (await this.store.patchHost(id, { sandboxId: sandbox.id, sandboxState: sandbox.state }))!;
    }
    sandbox = await api.getSandbox(host.sandboxId!);
    if (!sandbox) throw new UserError("The sandbox is gone. Delete this host and create a new one.");
    if (sandbox.state === "paused" || sandbox.state === "pausing") sandbox = await api.resume(sandbox.id);
    if (sandbox.state !== "running") sandbox = await api.waitFor(sandbox.id, ["running"]);
    if (!sandbox) throw new UserError("The sandbox is gone.");

    await this.store.patchHost(id, { phase: "installing", sandboxState: sandbox.state, error: undefined });
    const install = await api.run(sandbox, installScript(), 600);
    if (install.exitCode !== 0) throw new UserError(`Installing Paseo in the sandbox failed (exit ${install.exitCode}): ${tail(install.stderr || install.stdout)}`);

    await this.store.patchHost(id, { phase: "starting" });
    await this.startDaemon(api, sandbox);

    const secrets = await this.store.readSecrets();
    if (!secrets.pairing[id]) {
      const pair = await api.run(sandbox, pairScript(), 120);
      if (pair.exitCode !== 0) throw new UserError(`Pairing failed (exit ${pair.exitCode}): ${tail(pair.stderr)}`);
      const url = parsePairingUrl(pair.stdout);
      await this.store.updateSecrets((s) => {
        s.pairing[id] = url;
      });
    }
    const after = await api.getSandbox(sandbox.id);
    await this.store.patchHost(id, { phase: "ready", failedAt: undefined, error: undefined, sandboxState: after?.state, expiresAt: after?.expiresAt ?? undefined });
  }

  private async startDaemon(api: PrizedApi, sandbox: Sandbox): Promise<void> {
    const started = await api.run(sandbox, startDaemonScript(), 240);
    if (started.exitCode !== 0) throw new UserError(`The Paseo daemon did not start (exit ${started.exitCode}): ${tail(started.stderr || started.stdout)}`);
  }

  async act(input: { hostId: string; action: "stop" | "resume" | "retry" | "delete" | "forget"; confirm?: string }, hosts?: HostsApi): Promise<{ accepted: boolean }> {
    const host = await this.host(input.hostId);
    const id = host.id;
    switch (input.action) {
      case "stop":
        if (!host.sandboxId || !["ready", "failed"].includes(host.phase)) throw new UserError("Only a ready host can be stopped.");
        this.start(id, async () => {
          const api = await this.api();
          await this.store.patchHost(id, { phase: "pausing", error: undefined });
          await api.pause(host.sandboxId!);
          const sandbox = await api.waitFor(host.sandboxId!, ["paused"]);
          await this.store.patchHost(id, { phase: "paused", sandboxState: sandbox?.state, expiresAt: undefined });
        });
        break;
      case "resume":
        if (!host.sandboxId || host.phase !== "paused") throw new UserError("Only a stopped host can be resumed.");
        this.start(id, async () => {
          const api = await this.api();
          await this.store.patchHost(id, { phase: "resuming", error: undefined });
          let sandbox: Sandbox | null = await api.resume(host.sandboxId!);
          if (sandbox.state !== "running") sandbox = await api.waitFor(host.sandboxId!, ["running"]);
          // A paused sandbox keeps its processes, so the daemon is normally still there; start it if not.
          await this.startDaemon(api, sandbox!);
          await this.store.patchHost(id, { phase: "ready", sandboxState: "running", expiresAt: sandbox?.expiresAt ?? undefined });
        });
        break;
      case "retry":
        if (host.phase !== "failed") throw new UserError("Only a failed host can be retried.");
        if (host.failedAt === "pausing") return this.act({ hostId: id, action: "stop" }, hosts);
        if (host.failedAt === "deleting") {
          this.start(id, () => this.removeSandbox(id, host, hosts));
          break;
        }
        this.start(id, () => this.provision(id));
        break;
      case "delete": {
        const expected = host.sandboxId ?? host.label;
        if ((input.confirm ?? "").trim() !== expected) throw new UserError(`Type ${expected} to delete this host.`);
        this.start(id, () => this.removeSandbox(id, host, hosts));
        break;
      }
      case "forget":
        if (host.phase !== "gone") throw new UserError("Only a host whose sandbox is gone can be removed from the list. Delete the others.");
        await this.forget(id, hosts);
        break;
    }
    return { accepted: true };
  }

  private async removeSandbox(id: string, host: HostRecord, hosts?: HostsApi): Promise<void> {
    await this.store.patchHost(id, { phase: "deleting", error: undefined });
    if (host.sandboxId) {
      const api = await this.api();
      await api.remove(host.sandboxId);
      await api.waitFor(host.sandboxId, null);
    }
    await this.forget(id, hosts);
  }

  private async forget(id: string, hosts?: HostsApi): Promise<void> {
    const host = (await this.store.readHosts()).hosts[id];
    if (host?.registrationId && hosts) {
      try {
        await hosts.remove(host.registrationId);
      } catch {
        // Already removed in Paseo: nothing to do.
      }
    }
    await this.store.updateSecrets((s) => {
      delete s.pairing[id];
    });
    await this.store.updateHosts((file) => {
      delete file.hosts[id];
    });
  }

  async revealPairing(hostId: string): Promise<{ url: string }> {
    const host = await this.host(hostId);
    const url = (await this.store.readSecrets()).pairing[host.id];
    if (!url) throw new UserError("This host has no pairing link yet.");
    return { url };
  }

  // Save the host in this daemon's own host list, so it shows in Paseo without pasting the link.
  async addHost(hostId: string, hosts: HostsApi): Promise<{ registrationId: string; status: string }> {
    const host = await this.host(hostId);
    const url = (await this.store.readSecrets()).pairing[host.id];
    if (!url) throw new UserError("This host has no pairing link yet.");
    const added = await hosts.add({
      connection: url,
      label: host.label,
      metadata: { prizedSandbox: host.sandboxId ?? "" },
      sourceKey: `prized-sandbox-${host.id}`,
    });
    await this.store.patchHost(host.id, { registrationId: added.registrationId });
    return { registrationId: added.registrationId, status: added.status };
  }

  // Resolves when no host has work running (tests, and anyone who needs a quiet state).
  async settled(): Promise<void> {
    await this.ready;
    while (this.running.size) await Promise.allSettled([...this.running.values()]);
  }

  // Nothing to wait for: work cut short by a stop is marked failed at the next start, with Retry.
  dispose(): void {}
}
