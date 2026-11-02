// Prized sandbox hosts for Paseo. Create makes a sandbox, installs the pinned Paseo CLI in it, starts
// its daemon and keeps a relay pairing link. Stop pauses the sandbox: its memory and processes (the
// daemon, terminals, agent CLIs) are kept, and only storage is billed. Resume carries on; the daemon
// reconnects to the relay by itself, so the host needs no new pairing. Delete removes the sandbox.

import { createHash, randomBytes } from "node:crypto";

import { installScript, pairScript, parsePairingUrl, startDaemonScript, tail } from "./bootstrap.ts";
import { canWriteSandboxes, PrizedApi, PrizedApiError, type Me, type Sandbox } from "./prized-api.ts";
import { DEFAULT_SHAPE, Store, type HostRecord, type Phase, type Shape } from "./store.ts";

export const TEMPLATE = "prized-agent-v1";
const LABEL = "paseo-host";
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
  otherWorkspace?: boolean;
  createdAt: string;
  expiresAt?: string;
}

const OTHER_KEY = "This host was made with a key from another Prized workspace. Save that workspace's key to manage it.";

export class HostService {
  readonly store: Store;
  private env: NodeJS.ProcessEnv;
  private makeApi: (key: string) => PrizedApi;
  private log: (line: string) => void;
  private running = new Map<string, Promise<void>>();
  private ready: Promise<void>;
  private owners = new Map<string, string>();
  private others = new Set<string>();

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

  // ------------------------------------------------------------------ keys and ownership

  private async apiKey(): Promise<string> {
    const secrets = await this.store.readSecrets();
    const key = secrets.prizedApiKey || this.env.PRIZED_API_KEY;
    if (!key) throw new UserError("Add a Prized API key in this plugin's settings first.");
    return key;
  }

  private async api(): Promise<PrizedApi> {
    return this.makeApi(await this.apiKey());
  }

  // Who the current key acts for: its workspace when /me says, else (a key that cannot read its
  // workspace) the key itself. undefined when /me could not be read: then nothing is compared and
  // nothing recorded. A host records its owner at create; a 404 means "gone" only for the same owner.
  private async owner(): Promise<string | undefined> {
    const key = await this.apiKey();
    const cached = this.owners.get(key);
    if (cached) return cached;
    let owner: string;
    try {
      const id = (await this.makeApi(key).me()).workspace?.id;
      owner = id ? `ws:${id}` : `key:${createHash("sha256").update(key).digest("hex").slice(0, 16)}`;
    } catch {
      return undefined;
    }
    this.owners.set(key, owner);
    return owner;
  }

  private differs(host: HostRecord, owner: string | undefined): boolean {
    return Boolean(host.owner && owner && host.owner !== owner);
  }

  async saveSettings(input: {
    prizedApiKey?: string;
    anthropicApiKey?: string;
    openaiApiKey?: string;
    vcpu?: number;
    memoryGb?: number;
    maxRuntimeHours?: number | null;
  }): Promise<{ workspace?: string }> {
    await this.ready;
    let me: Me | undefined;
    if (input.prizedApiKey) {
      const key = input.prizedApiKey.trim();
      if (!/^dcp_[0-9a-f]{48}$/.test(key)) throw new UserError("That is not a Prized API key (dcp_ and 48 hex characters).");
      try {
        me = await this.makeApi(key).me();
      } catch (error) {
        throw new UserError(error instanceof PrizedApiError && error.status === 401 ? "Prized did not accept that key." : "Could not reach Prized to check the key.");
      }
      if (!canWriteSandboxes(me)) throw new UserError("That key cannot create sandboxes: use a Full access key, or a Custom one with Sandboxes: Write.");
    }
    await this.store.updateSecrets((s) => {
      if (input.prizedApiKey !== undefined) {
        s.prizedApiKey = input.prizedApiKey.trim() || undefined;
        s.workspace = s.prizedApiKey ? (me?.workspace?.name ?? me?.workspace?.id) : undefined;
        s.workspaceId = s.prizedApiKey ? me?.workspace?.id : undefined;
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
    return { workspace: me?.workspace?.name ?? me?.workspace?.id };
  }

  // ------------------------------------------------------------------ status

  async status(refresh: boolean) {
    await this.ready;
    if (refresh) await this.refresh();
    // The last remote refresh's verdict holds until the next one, so local polls don't flicker.
    const others = this.others;
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
          error: others.has(h.id) ? OTHER_KEY : h.error,
          paired: Boolean(secrets.pairing[h.id]),
          addedAsHost: Boolean(h.registrationId),
          otherWorkspace: others.has(h.id) || undefined,
          createdAt: h.createdAt,
          expiresAt: h.expiresAt,
        })),
    };
  }

  // Read each idle host's sandbox (a read is not activity: it never wakes or keeps a sandbox up).
  // Returns the hosts the current key cannot see because another workspace made them.
  private async refresh(): Promise<void> {
    const others = new Set<string>();
    const file = await this.store.readHosts();
    const idle = Object.values(file.hosts).filter((h) => h.sandboxId && !this.running.has(h.id));
    let api: PrizedApi;
    let owner: string | undefined;
    try {
      api = await this.api();
      owner = await this.owner();
    } catch {
      this.others = others;
      return;
    }
    for (const host of idle) {
      try {
        const sandbox = await api.getSandbox(host.sandboxId!);
        if (!sandbox) {
          if (this.differs(host, owner)) {
            others.add(host.id);
            continue;
          }
          // Without a known owner on either side a 404 cannot be told apart from another workspace.
          if (!owner && host.owner) continue;
          if (host.phase !== "gone") {
            await this.store.patchHost(host.id, { phase: "gone", sandboxState: "deleted", expiresAt: undefined, error: "The sandbox is gone: deleted, or its running time ended." });
          }
          continue;
        }
        const patch: Partial<HostRecord> = { sandboxState: sandbox.state, expiresAt: sandbox.expiresAt ?? undefined };
        if (owner) patch.owner = owner; // seen by this owner: it is this owner's
        if (["ready", "gone"].includes(host.phase) && sandbox.state === "paused") patch.phase = "paused";
        if (["paused", "gone"].includes(host.phase) && sandbox.state === "running") patch.phase = "ready";
        if (host.phase === "gone") patch.error = undefined;
        await this.store.patchHost(host.id, patch);
      } catch {
        // Status is best effort: the next refresh tries again.
      }
    }
    this.others = others;
  }

  // ------------------------------------------------------------------ operations

  private async host(id: string): Promise<HostRecord> {
    await this.ready;
    const host = (await this.store.readHosts()).hosts[id];
    if (!host) throw new UserError("No such host.");
    return host;
  }

  // The current key must be able to see this host's sandbox, or be the owner that made it.
  private async assertOwned(host: HostRecord, api: PrizedApi): Promise<Sandbox | null> {
    const sandbox = host.sandboxId ? await api.getSandbox(host.sandboxId) : null;
    if (sandbox) return sandbox;
    if (this.differs(host, await this.owner())) throw new UserError(OTHER_KEY);
    return null;
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
    const owner = await this.owner();
    const id = randomBytes(6).toString("hex");
    const now = new Date().toISOString();
    const record: HostRecord = { id, label: (label ?? "").trim() || `prized-${id.slice(0, 4)}`, phase: "creating", owner, createdAt: now, updatedAt: now };
    await this.store.updateHosts((file) => {
      file.hosts[id] = record;
    });
    this.start(id, () => this.provision(id));
    return { hostId: id };
  }

  // The sandbox a host's create made, if any: found by its label, so a create whose answer was lost
  // (or a retry after the settings changed, which its idempotency key would refuse) adopts it.
  private async adopt(api: PrizedApi, id: string): Promise<Sandbox | undefined> {
    return (await api.listByLabel(LABEL, id))[0];
  }

  // Create (once), install, start, pair. Each step is safe to run again, so Retry runs this whole.
  private async provision(id: string): Promise<void> {
    const api = await this.api();
    let host = await this.host(id);
    let sandbox: Sandbox | null | undefined;
    if (!host.sandboxId) {
      await this.store.patchHost(id, { phase: "creating", error: undefined });
      sandbox = await this.adopt(api, id);
      if (!sandbox) {
        const [file, secrets] = await Promise.all([this.store.readHosts(), this.store.readSecrets()]);
        const shape = { ...DEFAULT_SHAPE, ...file.shape };
        const envVars: Record<string, string> = {};
        if (secrets.anthropicApiKey) envVars.ANTHROPIC_API_KEY = secrets.anthropicApiKey;
        if (secrets.openaiApiKey) envVars.OPENAI_API_KEY = secrets.openaiApiKey;
        const input = {
          template: TEMPLATE,
          vcpu: shape.vcpu,
          memoryGb: shape.memoryGb,
          envVars,
          labels: { via: "paseo", [LABEL]: id },
          maxRuntimeMin: shape.maxRuntimeHours ? shape.maxRuntimeHours * 60 : undefined,
        };
        try {
          sandbox = await api.createSandbox(input, `paseo-host-${id}`);
        } catch (error) {
          if (!(error instanceof PrizedApiError && error.code === "idempotency_conflict")) throw error;
          sandbox = await this.adopt(api, id);
          if (!sandbox) throw error;
        }
      }
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

  private async pauseHost(id: string, sandboxId: string): Promise<void> {
    const api = await this.api();
    await this.store.patchHost(id, { phase: "pausing", error: undefined });
    await api.pause(sandboxId);
    const sandbox = await api.waitFor(sandboxId, ["paused"]);
    await this.store.patchHost(id, { phase: "paused", sandboxState: sandbox?.state, expiresAt: undefined });
  }

  async act(input: { hostId: string; action: "stop" | "resume" | "retry" | "delete" | "forget"; confirm?: string }, hosts?: HostsApi): Promise<{ accepted: boolean }> {
    const host = await this.host(input.hostId);
    const id = host.id;
    // Before anything starts, so a refusal leaves the host as it was: a key must be there, and it must
    // belong to the workspace that made the host.
    await this.apiKey();
    if (this.running.has(id)) throw new UserError("Something is already running on this host.");
    if (input.action !== "forget") await this.assertOwned(host, await this.api());
    switch (input.action) {
      case "stop":
        if (!host.sandboxId || !["ready", "failed"].includes(host.phase)) throw new UserError("Only a ready host can be stopped.");
        this.start(id, async () => {
          await this.assertOwned(host, await this.api());
          await this.pauseHost(id, host.sandboxId!);
        });
        break;
      case "resume":
        if (!host.sandboxId || host.phase !== "paused") throw new UserError("Only a stopped host can be resumed.");
        this.start(id, async () => {
          const api = await this.api();
          if (!(await this.assertOwned(host, api))) throw new UserError("The sandbox is gone.");
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
        if (host.failedAt === "pausing" && host.sandboxId) this.start(id, () => this.pauseHost(id, host.sandboxId!));
        else if (host.failedAt === "deleting") this.start(id, () => this.removeSandbox(id, hosts));
        else this.start(id, () => this.provision(id));
        break;
      case "delete": {
        const expected = host.sandboxId ?? host.label;
        if ((input.confirm ?? "").trim() !== expected) throw new UserError(`Type ${expected} to delete this host.`);
        this.start(id, () => this.removeSandbox(id, hosts));
        break;
      }
      case "forget": {
        if (host.phase !== "gone") throw new UserError("Only a host whose sandbox is gone can be removed from the list. Delete the others.");
        const api = await this.api();
        if (await this.assertOwned(host, api)) throw new UserError("The sandbox is there after all: refresh, then stop or delete it.");
        await this.forget(id, hosts);
        break;
      }
    }
    return { accepted: true };
  }

  // Deletes the host's sandbox: by id, or (a create cut short before its id was saved) by its label,
  // then reads until it is gone.
  private async removeSandbox(id: string, hosts?: HostsApi): Promise<void> {
    const host = await this.host(id);
    const api = await this.api();
    await this.assertOwned(host, api);
    await this.store.patchHost(id, { phase: "deleting", error: undefined });
    if (host.sandboxId) {
      await api.remove(host.sandboxId);
      await api.waitFor(host.sandboxId, null);
    }
    await api.removeByLabel(LABEL, id);
    const deadline = Date.now() + 180_000;
    while ((await api.listByLabel(LABEL, id)).some((s) => s.state !== "deleted")) {
      if (Date.now() > deadline) throw new UserError("The sandbox is still being deleted. Retry checks again.");
      await new Promise((r) => setTimeout(r, 2_000));
    }
    await this.forget(id, hosts);
  }

  private async forget(id: string, hosts?: HostsApi): Promise<void> {
    const host = (await this.store.readHosts()).hosts[id];
    if (host?.registrationId && hosts) {
      try {
        await hosts.remove(host.registrationId);
      } catch (error) {
        this.log(`prized-sandbox: removing Paseo host ${host.registrationId} failed (${(error as Error)?.message ?? error}); remove it under Hosts`);
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
    if (this.running.has(host.id)) throw new UserError("Wait for this host's current operation to finish.");
    if (!["ready", "paused"].includes(host.phase)) throw new UserError("Only a ready or stopped host can be added.");
    const url = (await this.store.readSecrets()).pairing[host.id];
    if (!url) throw new UserError("This host has no pairing link yet.");
    const added = await hosts.add({
      connection: url,
      label: host.label,
      metadata: { prizedSandbox: host.sandboxId ?? "" },
      sourceKey: `prized-sandbox-${host.id}`,
    });
    // Deleted meanwhile: take the registration back out rather than leave it pointing at nothing.
    const still = await this.store.patchHost(host.id, { registrationId: added.registrationId });
    if (!still) {
      await hosts.remove(added.registrationId).catch(() => undefined);
      throw new UserError("The host was deleted meanwhile.");
    }
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
