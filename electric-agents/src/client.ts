/**
 * Electric Agents' `RemoteSandboxClient`, backed by a Prized sandbox.
 *
 * Electric's `remoteSandbox()` owns path containment, `fetch()` inside the VM and the
 * kill-or-suspend choice on dispose; a provider client only moves bytes and runs commands.
 * This one is shaped like Electric's own e2b adapter (`connectOrCreate…` plus `adapt…`, with
 * the SDK class injected so the reattach logic is testable offline):
 *
 * - Identity: a sandbox is labelled with a hash of the `sandboxKey` (and an optional
 *   `namespace`), so a later wake on any host finds it with `Sandbox.list({ labels })` and
 *   reattaches; the oldest live one wins.
 * - Lifecycle: created with `autoPauseMin` (the keep-alive window) and `autoDeleteMin`
 *   (1 minute for an ephemeral sandbox, `pausedRetentionMin` for a persistent one). While a
 *   wake holds it, a heartbeat (`keepAlive()`) keeps it running; `suspend()` stops the
 *   heartbeat, so it pauses with its memory once idle, and the next command resumes it.
 *   `kill()` deletes it, and any other sandbox left under its key.
 * - Files: reads and writes go through the files API inside /home/prized (whole bytes, up to
 *   100 MiB); a read elsewhere in the VM, and every listing, stat, mkdir and remove, run as
 *   one shell command each.
 */
import { createHash, randomUUID } from 'node:crypto';
import { posix } from 'node:path';

import { SandboxError } from '@electric-ax/agents-runtime/sandbox';
import type { DirEntry, FileStat, NetworkPolicy, RemoteSandboxClient } from '@electric-ax/agents-runtime/sandbox';
import { Sandbox as PrizedSdkSandbox } from '@prized/sdk';
import type { ClientOptions, CreateOptions, ExecResult, ListOptions, ResourcesInput, SandboxState, WaitableOptions } from '@prized/sdk';

/** The sandbox user's home: the files API serves paths inside it only. */
export const PRIZED_HOME = '/home/prized';
/** Where commands run, and Electric's working directory, unless you choose another. */
export const DEFAULT_WORKING_DIRECTORY = '/home/prized/work';
/** The label that carries a hash of the Electric `sandboxKey`. */
export const SANDBOX_KEY_LABEL = 'electric-sandbox-key';
/** Added to every sandbox this package creates, so `prized sandbox ls` shows where it came from. */
export const CREATED_BY_LABEL: Readonly<Record<string, string>> = Object.freeze({ 'created-by': 'electric-agents' });
/** The keep-alive window: how long a sandbox idles, after the last heartbeat, before it pauses. */
export const DEFAULT_KEEP_ALIVE_MS: number = 2 * 60 * 1000;
/** How long a persistent sandbox stays paused before Prized deletes it: 7 days. */
export const DEFAULT_PAUSED_RETENTION_MIN: number = 7 * 24 * 60;
/** The longest `autoPauseMin` and `autoDeleteMin` the API takes (28 days). */
const MAX_LIFECYCLE_MIN = 40_320;
/** The longest delay a Node timer takes; anything longer fires at once. */
const MAX_TIMER_MS = 2 ** 31 - 1;
/** What one exec answer carries of each stream without `fullOutput` (the edge's cap). */
const PLAIN_OUTPUT_BYTES = 4 * 1024 * 1024;
/** Where a command's stdin is staged before the command redirects it in. */
const STDIN_DIR = '/home/prized/.cache/electric-agents/stdin';
/** The bound on each listing, stat, mkdir and exists command, and on a read with a command. */
const FS_COMMAND_TIMEOUT_MS = 60_000;
/** The bound on a remove, which may be a big tree (node_modules). */
const REMOVE_TIMEOUT_MS = 10 * 60_000;
/** Directories every Prized sandbox has and its user cannot remove: mkdir of them needs no round trip. */
const ALWAYS_THERE = new Set(['/', '/home', PRIZED_HOME]);
/** States a sandbox can be reattached in. Deleting, deleted and error ones are gone. */
const LIVE_STATES: SandboxState[] = ['starting', 'running', 'pausing', 'paused', 'resizing'];

/** The parts of a `@prized/sdk` Sandbox this adapter uses. */
export interface PrizedSandboxHandle {
  readonly id: string;
  readonly state: SandboxState;
  readonly createdAt: Date | null;
  readonly files: {
    readBytes(path: string): Promise<Uint8Array>;
    write(path: string, data: string | Uint8Array): Promise<unknown>;
    remove(path: string, options?: { recursive?: boolean }): Promise<void>;
  };
  exec(
    command: string,
    options?: { cwd?: string; env?: Record<string, string>; timeoutMs?: number; fullOutput?: boolean },
  ): Promise<ExecResult>;
  keepAlive(): Promise<void>;
  resume(options?: WaitableOptions): Promise<void>;
  delete(options?: WaitableOptions): Promise<void>;
}

/** The parts of the `@prized/sdk` Sandbox class this adapter uses (tests pass a fake). */
export interface PrizedSandboxClass {
  create(options?: CreateOptions): Promise<PrizedSandboxHandle>;
  list(options?: ListOptions): Promise<PrizedSandboxHandle[]>;
  deleteByLabels(labels: Record<string, string>, options?: ClientOptions): Promise<string[]>;
}

/** What `createPrizedClient` and `prizedSandbox` take. */
export interface PrizedSandboxOptions {
  /** Your Prized API key (`dcp_…`). Default: the `PRIZED_API_KEY` environment variable. */
  apiKey?: string;
  /** The API origin. Default `https://api.prized.dev`. */
  baseUrl?: string;
  /** More SDK client settings (retries, timeouts, a fetch of your own). */
  clientOptions?: ClientOptions;
  /** The sandbox template. Default `prized-base-v1` (Ubuntu 24.04 with git, curl, Python 3 and Node.js 22). */
  template?: string;
  /** vCPU, memory and disk; the template's defaults fill the gaps. */
  resources?: ResourcesInput;
  /** Variables for every command in the sandbox. */
  envVars?: Record<string, string>;
  /** Labels of your own, beside `created-by: electric-agents` and the key label. */
  labels?: Record<string, string>;
  /** Minutes of running before Prized deletes the sandbox. Default: your plan's (24 hours; 60 minutes on the free tier). */
  maxRuntimeMin?: number;
  /** Must be inside /home/prized. Default `/home/prized/work`. */
  workingDirectory?: string;
  /** Electric's resolved identity: a later wake with the same key, on any host, reattaches. */
  sandboxKey?: string;
  /**
   * Folded into the key's hash, so deployments that share a Prized workspace (a laptop and
   * production, say) never reattach to, or reclaim, each other's sandboxes. Electric's keys are
   * entity paths like `/worker/fixer`, the same in every deployment: set this to something
   * unique to the deployment, such as its agents server URL.
   */
  namespace?: string;
  /** `true`: dispose keeps the sandbox (it pauses with its memory once idle); needs a `sandboxKey`. `false` (default): dispose deletes it. */
  persistent?: boolean;
  /** `false`: only attach to a live sandbox for `sandboxKey`, never create one. Default `true`. */
  owner?: boolean;
  /** Idle time after the last heartbeat before the sandbox pauses, in ms (rounded up to whole minutes, at most 28 days). Default 2 minutes. */
  keepAliveMs?: number;
  /** The heartbeat's period in ms. Default half of `keepAliveMs`, at least 15 s. */
  heartbeatIntervalMs?: number;
  /** Minutes a persistent sandbox stays paused before Prized deletes it; `0` keeps it until you delete it. Default 7 days. */
  pausedRetentionMin?: number;
  /**
   * Prized sandboxes always have outbound internet, so only `allow-all` (the default here)
   * is accepted; `deny-all` and `allowlist` throw rather than be ignored.
   */
  initialNetworkPolicy?: NetworkPolicy;
  /**
   * `true`: keep each command's whole output, up to 100 MiB a stream. `false`: the first 4 MiB of
   * each, as one exec answer carries. Default: whole output only for Electric's in-sandbox
   * `fetch()` (whose base64 body can pass 4 MiB), so a `cat` of a huge log never pulls 100 MiB
   * into the runner for a tool that keeps 50 KB of it.
   */
  fullOutput?: boolean;
  /** Where diagnostics go (a keep-alive that failed, a duplicate sandbox removed). */
  log?: (message: string) => void;
  /** The SDK's Sandbox class. Tests pass a fake; leave it out otherwise. */
  sdk?: PrizedSandboxClass;
}

/** A `RemoteSandboxClient` that also says which sandbox it drives. */
export interface PrizedRemoteSandboxClient extends RemoteSandboxClient {
  readonly sandboxId: string;
  suspend(): Promise<void>;
}

/** The SDK client settings (key, origin and the rest) that every call takes. */
export function clientOptionsOf(opts: Pick<PrizedSandboxOptions, 'apiKey' | 'baseUrl' | 'clientOptions'>): ClientOptions {
  const out: ClientOptions = { ...opts.clientOptions };
  if (opts.apiKey !== undefined) out.apiKey = opts.apiKey;
  if (opts.baseUrl !== undefined) out.baseUrl = opts.baseUrl;
  return out;
}

/**
 * The label value for a `sandboxKey`: a 32-hex-digit SHA-256 prefix, as Electric's e2b adapter
 * tags its VMs, with the `namespace` (when there is one) hashed in ahead of the key.
 */
export function sandboxKeyTag(sandboxKey: string, namespace?: string): string {
  const input = namespace === undefined || namespace === '' ? sandboxKey : `${namespace}\0${sandboxKey}`;
  return createHash('sha256').update(input).digest('hex').slice(0, 32);
}

/**
 * Throws on options that could only fail later (at the first tool call of a lazy profile), or
 * do harm: a working directory outside /home/prized, a network policy Prized can't enforce,
 * lifecycle numbers that aren't finite and positive.
 */
export function assertPrizedSandboxOptions(
  opts: Pick<PrizedSandboxOptions, 'workingDirectory' | 'initialNetworkPolicy' | 'keepAliveMs' | 'heartbeatIntervalMs' | 'pausedRetentionMin'>,
): void {
  assertPrizedWorkingDirectory(opts.workingDirectory ?? DEFAULT_WORKING_DIRECTORY);
  assertPrizedNetworkPolicy(opts.initialNetworkPolicy);
  const positive = (name: string, value: number | undefined, zeroOk = false): void => {
    if (value === undefined) return;
    if (!Number.isFinite(value) || value < 0 || (value === 0 && !zeroOk)) {
      throw new SandboxError('runtime', `prizedSandbox: ${name} must be a finite number above 0${zeroOk ? ' (or 0)' : ''} (got ${value}).`);
    }
  };
  positive('keepAliveMs', opts.keepAliveMs);
  positive('heartbeatIntervalMs', opts.heartbeatIntervalMs);
  positive('pausedRetentionMin', opts.pausedRetentionMin, true);
}

/** Throws unless `dir` is an absolute, normalized path inside /home/prized. */
export function assertPrizedWorkingDirectory(dir: string): void {
  const inside = dir === PRIZED_HOME || dir.startsWith(`${PRIZED_HOME}/`);
  if (!dir.startsWith('/') || posix.normalize(dir) !== dir || dir.endsWith('/') || !inside) {
    throw new SandboxError(
      'policy',
      `prizedSandbox: workingDirectory must be an absolute path inside ${PRIZED_HOME} (got "${dir}"); the default is ${DEFAULT_WORKING_DIRECTORY}.`,
    );
  }
}

/** Throws unless the policy is one Prized can honour: outbound internet is always on. */
export function assertPrizedNetworkPolicy(policy: NetworkPolicy | undefined): void {
  if (policy === undefined || policy.mode === 'allow-all') return;
  throw new SandboxError(
    'unavailable',
    `prizedSandbox: Prized sandboxes always have outbound internet, so the "${policy.mode}" network policy can't be enforced. Pass initialNetworkPolicy: { mode: 'allow-all' }, or use a provider with egress rules.`,
  );
}

/** autoPauseMin for a keep-alive window: whole minutes, 1 to 40,320. */
export function autoPauseMinFor(keepAliveMs: number): number {
  return Math.min(MAX_LIFECYCLE_MIN, Math.max(1, Math.ceil(keepAliveMs / 60_000)));
}

/**
 * The heartbeat's period: half the keep-alive window (as sent, in whole minutes), at least
 * 15 s, and never past what a Node timer takes. An explicit period is only kept in range.
 */
export function heartbeatIntervalFor(keepAliveMs: number, explicit?: number): number {
  const ms = explicit ?? Math.max(15_000, Math.floor((autoPauseMinFor(keepAliveMs) * 60_000) / 2));
  return Math.min(MAX_TIMER_MS, Math.max(1, Math.floor(ms)));
}

/**
 * Build the client: find or create the sandbox, make sure the working directory exists, and
 * start the heartbeat. Use it with Electric's `remoteSandbox({ provider: 'e2b', client })`
 * (the provider name only labels a sandbox that brings its own client), or use
 * `prizedSandbox()`, which does that for you and names the sandbox `remote:prized`.
 */
export async function createPrizedClient(opts: PrizedSandboxOptions = {}): Promise<PrizedRemoteSandboxClient> {
  const workingDirectory = opts.workingDirectory ?? DEFAULT_WORKING_DIRECTORY;
  assertPrizedSandboxOptions(opts);
  if (opts.persistent === true && opts.sandboxKey === undefined) {
    // Kept paused for days with nothing that could ever find it again.
    throw new SandboxError('runtime', 'prizedSandbox: a persistent sandbox needs a sandboxKey to be reattached by.');
  }
  const keepAliveMs = opts.keepAliveMs ?? DEFAULT_KEEP_ALIVE_MS;
  const Sdk = opts.sdk ?? (PrizedSdkSandbox satisfies PrizedSandboxClass);
  const client = clientOptionsOf(opts);
  const { sandbox: sbx, created } = await findOrCreate(Sdk, {
    client,
    template: opts.template,
    resources: opts.resources,
    envVars: opts.envVars,
    labels: opts.labels,
    maxRuntimeMin: opts.maxRuntimeMin,
    persistent: opts.persistent === true,
    owner: opts.owner !== false,
    sandboxKey: opts.sandboxKey,
    namespace: opts.namespace,
    keepAliveMs,
    pausedRetentionMin: opts.pausedRetentionMin,
    log: opts.log,
  });
  try {
    // On every attach, as e2b's adapter does: an agent may have removed the directory in an
    // earlier wake (`rm -rf ~/work`), and every command runs in it.
    const r = await sbx.exec(`mkdir -p -- ${shellQuote(workingDirectory)}`, { cwd: '/', timeoutMs: FS_COMMAND_TIMEOUT_MS });
    if (r.exitCode !== 0) throw new SandboxError('runtime', `prizedSandbox: cannot make ${workingDirectory}: ${r.stderr.trim()}`);
  } catch (err) {
    // Nothing holds a sandbox this call made until it returns: delete it rather than leave it.
    if (created) await sbx.delete().catch((e: unknown) => opts.log?.(`prized: deleting ${sbx.id} after a failed setup failed: ${message(e)}`));
    throw err;
  }
  const keyLabel = opts.sandboxKey === undefined ? undefined : { [SANDBOX_KEY_LABEL]: sandboxKeyTag(opts.sandboxKey, opts.namespace) };
  return adaptPrized(sbx, {
    workingDirectory,
    keepAliveMs,
    heartbeatIntervalMs: opts.heartbeatIntervalMs,
    fullOutput: opts.fullOutput,
    log: opts.log,
    sweep: keyLabel && (() => Sdk.deleteByLabels(keyLabel, client)),
  });
}

/** What `connectOrCreatePrizedSandbox` takes. */
export interface ConnectOrCreateOptions {
  client?: ClientOptions;
  template?: string;
  resources?: ResourcesInput;
  envVars?: Record<string, string>;
  labels?: Record<string, string>;
  maxRuntimeMin?: number;
  persistent: boolean;
  owner?: boolean;
  sandboxKey?: string;
  namespace?: string;
  keepAliveMs: number;
  pausedRetentionMin?: number;
  log?: (message: string) => void;
}

/**
 * Find the sandbox to drive. With a `sandboxKey`, a live sandbox carrying its label is
 * reattached (resumed first if paused), whichever host made it; an owner that finds none
 * creates one, and an attacher (`owner: false`) throws `SandboxError('unavailable')` rather
 * than make an empty one. Without a key it always creates.
 *
 * If two hosts create for the same key at once, each lists again after its create and both
 * keep the oldest; the other is deleted at once rather than left to idle out. A sandbox found
 * paused that is deleted before it can resume (its autoDeleteMin ran out) counts as not found.
 */
export async function connectOrCreatePrizedSandbox(
  Sdk: PrizedSandboxClass,
  opts: ConnectOrCreateOptions,
): Promise<PrizedSandboxHandle> {
  return (await findOrCreate(Sdk, opts)).sandbox;
}

async function findOrCreate(
  Sdk: PrizedSandboxClass,
  opts: ConnectOrCreateOptions,
): Promise<{ sandbox: PrizedSandboxHandle; created: boolean }> {
  const client = opts.client ?? {};
  const owner = opts.owner !== false;
  const keyLabel = opts.sandboxKey === undefined ? undefined : { [SANDBOX_KEY_LABEL]: sandboxKeyTag(opts.sandboxKey, opts.namespace) };
  const createOptions: CreateOptions = {
    ...client,
    labels: { ...opts.labels, ...CREATED_BY_LABEL, ...keyLabel },
    autoPauseMin: autoPauseMinFor(opts.keepAliveMs),
    // Ephemeral: a sandbox whose host died is deleted a minute after it pauses (e2b kills it on
    // timeout). Persistent: kept paused, memory and all, for the next wake to resume.
    autoDeleteMin: opts.persistent
      ? Math.min(MAX_LIFECYCLE_MIN, Math.max(0, Math.ceil(opts.pausedRetentionMin ?? DEFAULT_PAUSED_RETENTION_MIN)))
      : 1,
  };
  if (opts.template !== undefined) createOptions.template = opts.template;
  if (opts.resources !== undefined) createOptions.resources = opts.resources;
  if (opts.envVars !== undefined) createOptions.envVars = opts.envVars;
  if (opts.maxRuntimeMin !== undefined) createOptions.maxRuntimeMin = opts.maxRuntimeMin;

  if (keyLabel === undefined) return { sandbox: await Sdk.create(createOptions), created: true };

  const existing = await oldestLive(Sdk, keyLabel, client);
  const woken = existing && (await wake(existing, opts.log));
  if (woken) return { sandbox: woken, created: false };
  if (!owner) {
    throw new SandboxError(
      'unavailable',
      'prizedSandbox: cannot attach: no sandbox is live for this key. The owning entity must create it first.',
    );
  }
  const created = await Sdk.create(createOptions);
  const winner = await oldestLive(Sdk, keyLabel, client).catch(() => undefined);
  if (!winner || winner.id === created.id) return { sandbox: created, created: true };
  // Attach to the older one first: if it is gone after all, keep ours.
  const joined = await wake(winner, opts.log);
  if (!joined) return { sandbox: created, created: true };
  opts.log?.(`prized: another host created ${winner.id} for this key first; deleting ${created.id} and attaching to it`);
  await created.delete().catch((err: unknown) => opts.log?.(`prized: deleting duplicate ${created.id} failed: ${message(err)}`));
  return { sandbox: joined, created: false };
}

async function oldestLive(
  Sdk: PrizedSandboxClass,
  labels: Record<string, string>,
  client: ClientOptions,
): Promise<PrizedSandboxHandle | undefined> {
  const found = await Sdk.list({ ...client, labels, state: LIVE_STATES });
  // The API filters by label and state; check again so a looser server can't hand back a stranger.
  const live = found.filter(s => LIVE_STATES.includes(s.state));
  return live.sort(byAge)[0];
}

function byAge(a: PrizedSandboxHandle, b: PrizedSandboxHandle): number {
  const at = a.createdAt ? a.createdAt.getTime() : 0;
  const bt = b.createdAt ? b.createdAt.getTime() : 0;
  if (at !== bt) return at - bt;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

/**
 * Resume a paused sandbox now (a command would wake it too, but waits only 10 s for that).
 * Undefined when it was deleted in the meantime.
 */
async function wake(sbx: PrizedSandboxHandle, log?: (message: string) => void): Promise<PrizedSandboxHandle | undefined> {
  if (sbx.state === 'paused' || sbx.state === 'pausing') {
    try {
      await sbx.resume();
    } catch (err) {
      if (hasCode(err, 'sandbox_deleted', 'not_found')) {
        log?.(`prized: ${sbx.id} was deleted before it could resume`);
        return undefined;
      }
      // Another host may have resumed it in between; the next command wakes it if not, and
      // says why if it can't (a refused resume: balance, quota).
      log?.(`prized: resuming ${sbx.id} failed: ${message(err)}`);
    }
  }
  return sbx;
}

/** Delete every sandbox for a key (an owner's terminal entity), without attaching to it. */
export async function reclaimPrizedSandboxByKey(
  sandboxKey: string,
  opts: Pick<PrizedSandboxOptions, 'apiKey' | 'baseUrl' | 'clientOptions' | 'sdk' | 'namespace'> = {},
): Promise<string[]> {
  const Sdk = opts.sdk ?? (PrizedSdkSandbox satisfies PrizedSandboxClass);
  return Sdk.deleteByLabels({ [SANDBOX_KEY_LABEL]: sandboxKeyTag(sandboxKey, opts.namespace) }, clientOptionsOf(opts));
}

/** What `adaptPrized` takes. */
export interface AdaptOptions {
  workingDirectory?: string;
  keepAliveMs?: number;
  heartbeatIntervalMs?: number;
  fullOutput?: boolean;
  log?: (message: string) => void;
  /** Called by `kill()` after the delete: removes any other sandbox under the same key (a lost race's, say). */
  sweep?: () => Promise<unknown>;
}

/** Wrap a Prized sandbox as Electric's provider-neutral `RemoteSandboxClient`. */
export function adaptPrized(sbx: PrizedSandboxHandle, opts: AdaptOptions = {}): PrizedRemoteSandboxClient {
  const defaultCwd = opts.workingDirectory ?? DEFAULT_WORKING_DIRECTORY;
  const keepAliveMs = opts.keepAliveMs ?? DEFAULT_KEEP_ALIVE_MS;
  const log = opts.log;
  // Commands and file transfers count as activity, but a wake can sit idle while its model
  // thinks; the heartbeat keeps the sandbox running (and its background processes going) for
  // as long as this lease holds it. A collaborator heartbeating elsewhere keeps it up too:
  // nothing here pauses it, so no cross-host refcount is needed.
  let heartbeat: ReturnType<typeof setInterval> | undefined = setInterval(() => {
    sbx.keepAlive().catch((err: unknown) => log?.(`prized: keep-alive for ${sbx.id} failed: ${message(err)}`));
  }, heartbeatIntervalFor(keepAliveMs, opts.heartbeatIntervalMs));
  heartbeat.unref?.();
  const stopHeartbeat = (): void => {
    if (heartbeat) clearInterval(heartbeat);
    heartbeat = undefined;
  };

  /** One filesystem command: C locale, from `/`, bounded; a timeout is ETIMEDOUT, a cut output EFBIG. */
  const fsCommand = async (
    script: string,
    op: string,
    path: string,
    { full = false, timeoutMs = FS_COMMAND_TIMEOUT_MS }: { full?: boolean; timeoutMs?: number } = {},
  ): Promise<ExecResult> => {
    let r: ExecResult;
    try {
      r = await sbx.exec(script, { cwd: '/', env: { LC_ALL: 'C' }, timeoutMs, fullOutput: full });
    } catch (err) {
      if (commandTimeoutResult(err)) throw errno('ETIMEDOUT', `timed out after ${timeoutMs} ms`, op, path, err);
      throw err;
    }
    if (r.truncated) throw errno('EFBIG', 'too large to read with a command (over 100 MiB of output)', op, path);
    return r;
  };

  return {
    get sandboxId() {
      return sbx.id;
    },

    async exec(o) {
      let command = o.command;
      let staged: string | undefined;
      if (o.stdin !== undefined) {
        // The exec API has no stdin: stage it as a file, point the shell's stdin at it and
        // unlink it before the command runs (the open descriptor keeps it readable).
        staged = `${STDIN_DIR}/${randomUUID()}`;
        await sbx.files.write(staged, typeof o.stdin === 'string' ? o.stdin : new Uint8Array(o.stdin));
        const q = shellQuote(staged);
        command = `{ exec 0<${q}; } 2>/dev/null || { echo 'prized: stdin was not staged' >&2; exit 125; }\nrm -f -- ${q}\n${o.command}`;
      }
      // Electric's fetch() is the one caller known to need more than 4 MiB (a base64 body).
      const fullOutput = opts.fullOutput ?? o.env?.FETCH_URL !== undefined;
      try {
        const r = await sbx.exec(command, { cwd: o.cwd ?? defaultCwd, env: o.env, timeoutMs: o.timeoutMs, fullOutput });
        // The contract has no truncation flag of its own: say so where a reader will see it.
        const cut = r.truncated ? `\n[prized: output cut at ${fullOutput ? '100' : PLAIN_OUTPUT_BYTES / 1024 / 1024} MiB a stream]\n` : '';
        return { stdout: Buffer.from(r.stdout), stderr: Buffer.from(r.stderr + cut), exitCode: r.exitCode < 0 ? null : r.exitCode, timedOut: false };
      } catch (err) {
        const timedOut = commandTimeoutResult(err);
        if (timedOut) {
          return { stdout: Buffer.from(timedOut.stdout), stderr: Buffer.from(timedOut.stderr), exitCode: null, timedOut: true };
        }
        if (staged) await sbx.files.remove(staged).catch(() => undefined);
        throw err;
      }
    },

    async readFile(path) {
      if (insideHome(path)) {
        try {
          return Buffer.from(await sbx.files.readBytes(path));
        } catch (err) {
          // A link out of /home/prized is the files API's to refuse but not Electric's (reads
          // anywhere in the VM are allowed): read those with a command instead.
          if (!hasCode(err, 'symlink_not_allowed', 'path_outside_root')) throw fsErrorFromApi(err, 'readFile', path);
        }
      }
      const r = await fsCommand(`base64 -w 0 -- ${shellQuote(path)}`, 'readFile', path, { full: true });
      if (r.exitCode !== 0) throw shellError(r.stderr, 'readFile', path);
      return Buffer.from(r.stdout.trim(), 'base64');
    },

    async writeFile(path, content) {
      try {
        await sbx.files.write(path, content);
      } catch (err) {
        throw fsErrorFromApi(err, 'writeFile', path);
      }
    },

    async mkdir(path, o) {
      // Electric's recursive mkdir walks every parent from /home down; those two never need asking.
      if (ALWAYS_THERE.has(posix.normalize(path).replace(/(.)\/$/, '$1'))) {
        if (o?.recursive) return;
        throw errno('EEXIST', 'File exists', 'mkdir', path);
      }
      const r = await fsCommand(`mkdir ${o?.recursive ? '-p ' : ''}-- ${shellQuote(path)}`, 'mkdir', path);
      if (r.exitCode !== 0) throw shellError(r.stderr, 'mkdir', path);
    },

    async readdir(path): Promise<ReadonlyArray<DirEntry>> {
      const p = shellQuote(path);
      // NUL-separated `<type>\t<name>`: names with tabs or newlines survive. -H follows `path`
      // itself when it is a link to a directory, as readdir does.
      const r = await fsCommand(
        `if [ ! -e ${p} ]; then echo 'readdir: No such file or directory' >&2; exit 2; fi\n` +
          `if [ ! -d ${p} ]; then echo 'readdir: Not a directory' >&2; exit 20; fi\n` +
          `find -H ${p} -mindepth 1 -maxdepth 1 -printf '%y\\t%f\\0'`,
        'readdir',
        path,
        { full: true },
      );
      if (r.exitCode !== 0) throw shellError(r.stderr, 'readdir', path);
      return r.stdout
        .split('\0')
        .filter(record => record.length > 0)
        .map(record => {
          const tab = record.indexOf('\t');
          const kind = record.slice(0, tab);
          const name = record.slice(tab + 1);
          const type = kind === 'd' ? 'directory' : kind === 'f' ? 'file' : kind === 'l' ? 'symlink' : 'other';
          return { name, type } satisfies DirEntry;
        });
    },

    async exists(path) {
      const r = await fsCommand(`test -e ${shellQuote(path)}`, 'exists', path);
      return r.exitCode === 0;
    },

    async remove(path, o) {
      // Plain `rm`, never `-f`: removing a missing path throws, as Electric's conformance suite expects.
      const r = await fsCommand(`rm ${o?.recursive ? '-r ' : ''}-- ${shellQuote(path)}`, 'remove', path, { timeoutMs: REMOVE_TIMEOUT_MS });
      if (r.exitCode !== 0) throw shellError(r.stderr, 'remove', path);
    },

    async stat(path): Promise<FileStat> {
      // -L follows links, as fs.stat does; %f is the raw mode in hex, so no locale wording is parsed.
      const r = await fsCommand(`stat -L -c '%f|%s|%Y' -- ${shellQuote(path)}`, 'stat', path);
      const fields = r.stdout.trim().split('|');
      if (r.exitCode !== 0 || fields.length !== 3) throw shellError(r.stderr, 'stat', path);
      const mode = Number.parseInt(fields[0] ?? '', 16);
      const format = mode & 0o170000;
      const type = format === 0o100000 ? 'file' : format === 0o040000 ? 'directory' : format === 0o120000 ? 'symlink' : 'other';
      const mtime = Number(fields[2]);
      return { type, size: Number(fields[1]) || 0, mtimeMs: Number.isFinite(mtime) ? mtime * 1000 : 0 };
    },

    async kill() {
      stopHeartbeat();
      try {
        await sbx.delete();
      } catch (err) {
        // Already gone (deleted elsewhere, or its run time ended): nothing left to kill.
        if (!hasCode(err, 'not_found', 'sandbox_deleted')) throw err;
      }
      // Wiping a keyed workspace wipes every sandbox under its key: a race's loser whose delete
      // failed, one in error. Best effort; this one is already gone.
      await opts.sweep?.().catch((err: unknown) => log?.(`prized: removing other sandboxes for this key failed: ${message(err)}`));
    },

    async suspend() {
      // Persistent teardown: stop the heartbeat and let autoPauseMin pause it, memory and
      // processes kept, once nobody else keeps it busy. Pausing now could freeze a
      // collaborator's command on another host.
      stopHeartbeat();
    },
  };
}

function insideHome(path: string): boolean {
  const p = posix.normalize(path);
  return p.startsWith(`${PRIZED_HOME}/`);
}

function shellQuote(arg: string): string {
  return `'${arg.replace(/'/g, `'\\''`)}'`;
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function hasCode(err: unknown, ...codes: string[]): boolean {
  const code = (err as { code?: unknown } | null)?.code;
  return typeof code === 'string' && codes.includes(code);
}

/** A CommandTimeoutError's result, matched by shape so a second copy of the SDK still counts. */
function commandTimeoutResult(err: unknown): ExecResult | undefined {
  if (!hasCode(err, 'command_timeout')) return undefined;
  const result = (err as { result?: unknown }).result;
  return result && typeof result === 'object' ? (result as ExecResult) : undefined;
}

type ErrnoError = Error & { code: string; syscall?: string; path?: string };

function errno(code: string, text: string, op: string, path: string, cause?: unknown): ErrnoError {
  const err = new Error(`${op} ${path}: ${text}`, cause === undefined ? undefined : { cause }) as ErrnoError;
  err.code = code;
  err.syscall = op;
  err.path = path;
  return err;
}

/** A files API refusal as the errno-coded error a filesystem gives (Electric's tools read `code`). */
function fsErrorFromApi(err: unknown, op: string, path: string): unknown {
  const codes: Record<string, string> = {
    not_found: 'ENOENT',
    not_regular_file: 'EISDIR',
    permission_denied: 'EACCES',
    path_outside_root: 'EACCES',
    symlink_not_allowed: 'EACCES',
    unsupported_file_type: 'EISDIR',
    file_too_large: 'EFBIG',
  };
  const code = (err as { code?: unknown } | null)?.code;
  const mapped = typeof code === 'string' ? codes[code] : undefined;
  return mapped ? errno(mapped, message(err), op, path, err) : err;
}

/** A failed shell command's stderr as an errno-coded error. */
function shellError(stderr: string, op: string, path: string): ErrnoError {
  const text = stderr.trim() || `${op} failed`;
  const code = /Is a directory/i.test(text)
    ? 'EISDIR'
    : /Not a directory/i.test(text)
      ? 'ENOTDIR'
      : /File exists/i.test(text)
        ? 'EEXIST'
        : /Directory not empty/i.test(text)
          ? 'ENOTEMPTY'
          : /Permission denied/i.test(text)
            ? 'EACCES'
            : /Operation not permitted/i.test(text)
              ? 'EPERM'
              : /No such file|cannot stat|cannot access/i.test(text)
                ? 'ENOENT'
                : 'EIO';
  return errno(code, text, op, path);
}
