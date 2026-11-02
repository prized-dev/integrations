// Two private files in the daemon user's Paseo home: hosts.json (what each host is doing) and
// secrets.json (the Prized key, the agents' keys and each host's pairing link). Both are written
// whole, mode 600, through a temp file and a rename, so a crash never leaves half a file.

import { mkdir, readFile, rename, writeFile, chmod } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export type Phase = "creating" | "installing" | "starting" | "ready" | "pausing" | "paused" | "resuming" | "deleting" | "gone" | "failed";

export interface HostRecord {
  id: string;
  label: string;
  sandboxId?: string;
  phase: Phase;
  // Where a failed host stopped, so Retry knows what to redo.
  failedAt?: Phase;
  error?: string;
  sandboxState?: string;
  expiresAt?: string;
  registrationId?: string;
  // Who made it: "ws:<workspace id>" when the key could read its workspace, else "key:<key hash>".
  // Another workspace's key sees none of its sandboxes, so it must not read them as gone.
  owner?: string;
  createdAt: string;
  updatedAt: string;
}

export interface Shape {
  vcpu: number;
  memoryGb: number;
  // null: Prized's default (24 hours, or 60 minutes of running in all on the free tier).
  maxRuntimeHours: number | null;
}

export interface HostsFile {
  version: 1;
  shape: Shape;
  hosts: Record<string, HostRecord>;
}

export interface SecretsFile {
  prizedApiKey?: string;
  workspace?: string;
  workspaceId?: string;
  anthropicApiKey?: string;
  openaiApiKey?: string;
  pairing: Record<string, string>;
}

export const DEFAULT_SHAPE: Shape = { vcpu: 2, memoryGb: 4, maxRuntimeHours: null };

export function defaultStateDir(env: NodeJS.ProcessEnv = process.env): string {
  return env.PASEO_PRIZED_SANDBOX_STATE_DIR ?? join(env.PASEO_HOME ?? join(homedir(), ".paseo"), "prized-sandbox");
}

async function readJson<T>(path: string, fallback: T): Promise<T> {
  try {
    return JSON.parse(await readFile(path, "utf8")) as T;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return fallback;
    throw new Error(`${path} could not be read: ${(error as Error).message}`);
  }
}

async function writePrivate(path: string, value: unknown): Promise<void> {
  const tmp = `${path}.${process.pid}.tmp`;
  await writeFile(tmp, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  await chmod(tmp, 0o600);
  await rename(tmp, path);
}

export class Store {
  readonly dir: string;
  private queue: Promise<unknown> = Promise.resolve();

  constructor(dir: string) {
    this.dir = dir;
  }

  private async ensureDir(): Promise<void> {
    await mkdir(this.dir, { recursive: true, mode: 0o700 });
    await chmod(this.dir, 0o700);
  }

  // One read-modify-write at a time within the plugin's process.
  private serial<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.queue.then(fn, fn);
    this.queue = next.catch(() => undefined);
    return next;
  }

  readHosts(): Promise<HostsFile> {
    return readJson<HostsFile>(join(this.dir, "hosts.json"), { version: 1, shape: { ...DEFAULT_SHAPE }, hosts: {} });
  }

  readSecrets(): Promise<SecretsFile> {
    return readJson<SecretsFile>(join(this.dir, "secrets.json"), { pairing: {} });
  }

  updateHosts<T>(fn: (file: HostsFile) => T): Promise<T> {
    return this.serial(async () => {
      await this.ensureDir();
      const file = await this.readHosts();
      const result = fn(file);
      await writePrivate(join(this.dir, "hosts.json"), file);
      return result;
    });
  }

  updateSecrets<T>(fn: (file: SecretsFile) => T): Promise<T> {
    return this.serial(async () => {
      await this.ensureDir();
      const file = await this.readSecrets();
      file.pairing ??= {};
      const result = fn(file);
      await writePrivate(join(this.dir, "secrets.json"), file);
      return result;
    });
  }

  patchHost(id: string, patch: Partial<HostRecord>): Promise<HostRecord | undefined> {
    return this.updateHosts((file) => {
      const current = file.hosts[id];
      if (!current) return undefined;
      file.hosts[id] = { ...current, ...patch, updatedAt: new Date().toISOString() };
      return file.hosts[id];
    });
  }
}
