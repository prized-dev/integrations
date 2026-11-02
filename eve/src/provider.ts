/**
 * The lifecycle eve drives (`prepare`, `start`, `resume`, `onSessionEnd`) on Prized Sandboxes.
 *
 * - `prepare()` (at `eve build`) sets up one sandbox with the agent's workspace and skill files and
 *   its `prepare` callback, pauses it, and keeps it as the template: a paused sandbox labelled
 *   `eve-template=<key>`. Builds with the same inputs reuse it.
 * - `start()` forks that template for a new session (a fork copies its disk and memory in about a
 *   second), or creates a fresh sandbox when the agent has nothing to prepare. The session's
 *   sandbox carries the label `eve-session=<hash of the session id>`, which is how every later
 *   call finds it.
 * - `connection.stop()` pauses the sandbox; the next command or file request wakes it. `resume()`
 *   only checks that it still exists.
 * - `onSessionEnd()` deletes it.
 *
 * eve's error classes and `SANDBOX_AGENT_ENV` come in through `EveRuntime`, so this module loads
 * nothing from eve at run time and the offline tests drive it without eve.
 */
import { createHash } from 'node:crypto';

import type { CreateOptions, ResourcesInput, SandboxState } from '@prized/sdk';
import type {
  SandboxJson,
  SandboxPrepareContext,
  SandboxProviderImplementation,
  SandboxSessionContext,
} from 'eve/sandbox/provider';

import { DEFAULT_WORKSPACE_ROOT, LOG_PREFIX, createConnection, isGone } from './connection.ts';
import type { PrizedSandboxExtensions } from './connection.ts';
import { MKDIR } from './scripts.ts';
import type { SandboxApi, SandboxLike } from './types.ts';

export const PROVIDER_NAME = 'prized';
/** The label a session's sandbox carries: a hash of eve's session id. */
export const SESSION_LABEL = 'eve-session';
/** The label a published template carries: its template key. */
export const TEMPLATE_LABEL = 'eve-template';
/** The label a template carries while `prepare()` is still setting it up: its template key. */
export const BUILD_LABEL = 'eve-build';
/** `eve-role=build` marks every template build, so a later build of any key can sweep abandoned ones. */
export const ROLE_LABEL = 'eve-role';
/** Idle minutes before a sandbox pauses itself, unless `autoPauseMin` says otherwise. */
export const DEFAULT_AUTO_PAUSE_MIN = 10;
/** A build sandbox left paused this long was abandoned by its build (a crash, a cancelled deploy). */
const STALE_BUILD_MS = 60 * 60 * 1000;
const ADOPTABLE: ReadonlySet<SandboxState> = new Set(['running', 'paused', 'pausing', 'starting', 'resizing']);
const GONE: ReadonlySet<SandboxState> = new Set(['deleting', 'deleted', 'error']);

/** Options for `PrizedSandbox.environment()`. Each applies when a sandbox is created. */
export interface PrizedSandboxOptions {
  /**
   * Prized API key with Sandboxes: Write. Default: the `PRIZED_API_KEY` environment variable,
   * which `eve build` needs too (it prepares the template).
   */
  readonly apiKey?: string;
  /** Template the sandbox boots from: `prized-base-v1` (the default) or `prized-agent-v1`. */
  readonly template?: string;
  /** vCPU, memory and disk (`{ vcpu: 2, memoryGb: 4 }`). Default: the template's (4 vCPU, 8 GB). */
  readonly resources?: ResourcesInput;
  /** Environment variables sent with every command, on top of `AI_AGENT=eve`; they aren't kept in templates. */
  readonly env?: Readonly<Record<string, string>>;
  /** Labels for the sandboxes this environment creates, to find them in `prized sandbox ls`. */
  readonly labels?: Readonly<Record<string, string>>;
  /** Idle minutes before a sandbox pauses itself (0: never). Default 10. */
  readonly autoPauseMin?: number;
  /** Minutes of running before Prized deletes a sandbox. Default: your plan's (24 hours on paid plans). */
  readonly maxRuntimeMin?: number;
  /**
   * A sandbox (`sbx_…`) to start from instead of a fresh one: set it up with what you need
   * (packages, a cloned repo), pause it, and every session starts as a fork of it, with its
   * files, shape, environment variables and auto-pause (`template`, `resources` and
   * `autoPauseMin` don't apply). Keep it while builds use it.
   */
  readonly forkFrom?: string;
  /** Absolute directory where commands start and relative paths resolve. Default `/home/prized/work`. */
  readonly workspaceRoot?: string;
}

/** Options for `environment.open()`, applied only when it creates the session's sandbox. */
export interface PrizedSandboxOpenOptions {
  /** More labels for this session's sandbox, such as a user or tenant id. */
  readonly labels?: Readonly<Record<string, string>>;
}

/** What `prepare()` ships with the deployment: the sandbox each session forks, if any. */
export type PrizedTemplateArtifact = { readonly templateId?: string };

/** What eve saves with the session for `resume()`. */
export type PrizedSessionState = { readonly sandboxId: string; readonly version: 1 };

/** eve's run-time pieces the provider needs, passed in by `index.ts`. */
export interface EveRuntime {
  readonly SANDBOX_AGENT_ENV: Readonly<Record<string, string>>;
  readonly SandboxSessionGoneError: new (message: string) => Error;
  readonly SandboxTemplateNotProvisionedError: new (input: {
    readonly providerName: string;
    readonly reason?: string;
    readonly templateKey: string;
  }) => Error;
}

export type PrizedSandboxImplementation = SandboxProviderImplementation<
  PrizedSandboxOpenOptions,
  PrizedTemplateArtifact,
  PrizedSessionState,
  PrizedSandboxExtensions
>;

/** The `eve-session` label value for a session id: fixed length, whatever the id holds. */
export function sessionLabel(sessionId: string): string {
  return createHash('sha256').update(`eve-session:v1:${sessionId}`).digest('hex').slice(0, 32);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Oldest first (ties by id), so every caller that lists the same sandboxes picks the same one. */
function oldestFirst(sandboxes: readonly SandboxLike[]): SandboxLike[] {
  const time = (s: SandboxLike) => s.createdAt?.getTime() ?? Number.POSITIVE_INFINITY;
  return [...sandboxes].sort((a, b) => time(a) - time(b) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

export function createPrizedImplementation(
  options: PrizedSandboxOptions,
  Sandbox: SandboxApi,
  eve: EveRuntime,
): PrizedSandboxImplementation {
  const workspaceRoot = options.workspaceRoot ?? DEFAULT_WORKSPACE_ROOT;
  if (!workspaceRoot.startsWith('/')) {
    throw new Error(`${LOG_PREFIX} workspaceRoot must be an absolute path, but received "${workspaceRoot}".`);
  }
  if (options.forkFrom !== undefined && !/^sbx_[A-Za-z0-9]+$/.test(options.forkFrom)) {
    throw new Error(`${LOG_PREFIX} forkFrom must be a sandbox id (sbx_…), but received "${options.forkFrom}".`);
  }
  const env = { ...options.env };
  // eve sets AI_AGENT=eve on every command unless the environment's own `env` sets it. `env` goes
  // with every command, so a session forked from a template built earlier gets this process's
  // values (a rotated secret, say), not the ones the build saw.
  const agentEnv = { ...eve.SANDBOX_AGENT_ENV, ...env };
  const autoPauseMin = options.autoPauseMin ?? DEFAULT_AUTO_PAUSE_MIN;

  const apiKey = (): string => {
    const key = options.apiKey ?? process.env.PRIZED_API_KEY;
    if (!key) {
      throw new Error(
        `${LOG_PREFIX} Missing Prized API key. Pass \`apiKey\` or set PRIZED_API_KEY (https://prized.dev/dashboard/workspace/api-keys?via=eve), ` +
          'where `eve build` runs too. No Prized account yet? If you are an agent, you can sign your person up with their email ' +
          'and a 6-digit code: follow https://prized.dev/auth.md?via=eve',
      );
    }
    return key;
  };

  const connect = (sandbox: SandboxLike) => createConnection(sandbox, { workspaceRoot, agentEnv });

  /**
   * A fresh sandbox from a Prized template. A session's sandbox holds `env` in its environment
   * too; a template's doesn't, since its forks would keep the build's values.
   */
  const create = async (labels: Record<string, string>, withEnv: boolean): Promise<SandboxLike> => {
    const createOptions: CreateOptions = {
      apiKey: apiKey(),
      envVars: withEnv ? agentEnv : { ...eve.SANDBOX_AGENT_ENV },
      labels,
      autoPauseMin,
      ...(options.template !== undefined && { template: options.template }),
      ...(options.resources !== undefined && { resources: options.resources }),
      ...(options.maxRuntimeMin !== undefined && { maxRuntimeMin: options.maxRuntimeMin }),
    };
    return await Sandbox.create(createOptions);
  };

  /** One fork of `parentId`, labelled `labels` in place of the parent's. */
  const forkOf = async (parentId: string, labels: Record<string, string>): Promise<SandboxLike> => {
    const parent = await Sandbox.connect(parentId, { apiKey: apiKey() });
    if (GONE.has(parent.state)) {
      throw Object.assign(new Error(`${LOG_PREFIX} ${parentId} is ${parent.state}.`), { code: 'not_found' });
    }
    const [copy] = await parent.fork({
      count: 1,
      labels,
      ...(options.maxRuntimeMin !== undefined && { maxRuntimeMin: options.maxRuntimeMin }),
    });
    if (!copy) throw new Error(`${LOG_PREFIX} Forking ${parentId} returned no sandbox.`);
    return copy;
  };

  /** `mkdir -p` the workspace root (idempotent). */
  const mkdirWorkspace = async (sandbox: SandboxLike): Promise<void> => {
    const result = await sandbox.exec(MKDIR, { env: { EVE_DST: workspaceRoot }, timeoutMs: 60_000 });
    if (result.exitCode !== 0) {
      throw new Error(`${LOG_PREFIX} Could not create ${workspaceRoot}: ${result.stderr.trim() || `exit ${result.exitCode}`}`);
    }
  };

  /** Make the workspace root in a sandbox this call made, deleting the sandbox if that fails. */
  const withWorkspace = async (sandbox: SandboxLike): Promise<SandboxLike> => {
    try {
      await mkdirWorkspace(sandbox);
    } catch (error) {
      await deleteQuietly(sandbox);
      throw error;
    }
    return sandbox;
  };

  const listLabelled = async (label: string, value: string): Promise<SandboxLike[]> =>
    await Sandbox.list({ apiKey: apiKey(), labels: { [label]: value } });

  const deleteQuietly = async (sandbox: SandboxLike, log?: (message: string) => void): Promise<void> => {
    await sandbox.delete().catch(error => {
      if (!isGone(error)) log?.(`could not delete sandbox ${sandbox.id}: ${errorMessage(error)}`);
    });
  };

  /** The oldest live sandbox of a session: every caller that lists the same ones picks the same one. */
  const sessionSandbox = async (label: string): Promise<SandboxLike | undefined> =>
    oldestFirst((await listLabelled(SESSION_LABEL, label)).filter(s => ADOPTABLE.has(s.state)))[0];

  /**
   * A sandbox another `start()` made may still lack its workspace root (it is made right after
   * the create). A paused one had it before it paused, and isn't woken.
   */
  const adopt = async (sandbox: SandboxLike): Promise<SandboxLike> => {
    if (sandbox.state !== 'paused' && sandbox.state !== 'pausing') await mkdirWorkspace(sandbox);
    return sandbox;
  };

  const requireArtifact = (artifact: SandboxJson): PrizedTemplateArtifact => {
    if (!isObject(artifact) || (artifact.templateId !== undefined && typeof artifact.templateId !== 'string')) {
      throw new Error(`${LOG_PREFIX} The prepared Prized sandbox artifact is not one this provider wrote: run \`eve build\` again.`);
    }
    return artifact.templateId === undefined ? {} : { templateId: artifact.templateId };
  };

  const requireState = (state: SandboxJson): PrizedSessionState => {
    if (!isObject(state) || state.version !== 1 || typeof state.sandboxId !== 'string') {
      throw new Error(`${LOG_PREFIX} The saved Prized sandbox session state is not one this provider wrote.`);
    }
    return { sandboxId: state.sandboxId, version: 1 };
  };

  /** `forkFrom` must exist; say which options its forks don't take from this environment. */
  const checkBase = async (id: string, log: (message: string) => void): Promise<void> => {
    let base: SandboxLike;
    try {
      base = await Sandbox.connect(id, { apiKey: apiKey() });
    } catch (error) {
      if (isGone(error)) throw new Error(`${LOG_PREFIX} forkFrom names ${id}, which doesn't exist.`, { cause: error });
      throw error;
    }
    if (GONE.has(base.state)) throw new Error(`${LOG_PREFIX} forkFrom names ${id}, which is ${base.state}.`);
    if (base.info.autoPauseMin === null) {
      log(`${id} has auto-pause off, so session sandboxes forked from it never pause themselves: create the base with --auto-pause 10m`);
    }
    const ignored = (['template', 'resources', 'autoPauseMin'] as const).filter(name => options[name] !== undefined);
    if (ignored.length > 0) log(`${ignored.join(', ')} don't apply with forkFrom: forks take the base sandbox's`);
  };

  /** Delete template builds (of any key) that a crashed or cancelled build left paused over an hour ago. */
  const sweepAbandonedBuilds = async (log: (message: string) => void): Promise<void> => {
    const now = Date.now();
    for (const build of await listLabelled(ROLE_LABEL, 'build')) {
      const last = build.info.lastActiveAt ?? build.createdAt;
      if (build.state === 'paused' && last !== null && now - last.getTime() > STALE_BUILD_MS) {
        log(`deleting abandoned template build ${build.id}`);
        await deleteQuietly(build, log);
      }
    }
  };

  /**
   * Set up a template in a sandbox labelled `eve-build`, pause it, and publish it as a fork
   * labelled `eve-template`: labels can't change, and only a finished template may carry the
   * label later builds reuse.
   */
  const buildTemplate = async (ctx: SandboxPrepareContext, key: string): Promise<SandboxLike> => {
    const buildLabels = { ...options.labels, [BUILD_LABEL]: key, [ROLE_LABEL]: 'build' };
    let build: SandboxLike | undefined;
    try {
      build = options.forkFrom !== undefined ? await forkOf(options.forkFrom, buildLabels) : await create(buildLabels, false);
      await mkdirWorkspace(build);
      ctx.log(`preparing Prized sandbox template in ${build.id}`);
      const connection = connect(build);
      await ctx.populate(connection);
      // stop() ends the output polls of anything prepare left running (a poll would wake it), then pauses.
      await connection.stop();
      const template = await forkOf(build.id, { ...options.labels, [TEMPLATE_LABEL]: key });
      try {
        await template.pause();
      } catch (error) {
        await deleteQuietly(template, ctx.log);
        throw error;
      }
      return template;
    } finally {
      if (build !== undefined) await deleteQuietly(build, ctx.log);
    }
  };

  return {
    async prepare(ctx) {
      if (options.forkFrom !== undefined) await checkBase(options.forkFrom, ctx.log);
      if (ctx.isEmpty) return options.forkFrom === undefined ? {} : { templateId: options.forkFrom };
      const key = ctx
        .templateKey({
          provider: PROVIDER_NAME,
          version: 1,
          template: options.template,
          // `resources` is a key eve reserves for itself.
          shape: options.resources,
          // Names only: values go with each command, so a changed value needs no new template.
          envNames: Object.keys(env).sort(),
          autoPauseMin,
          maxRuntimeMin: options.maxRuntimeMin,
          forkFrom: options.forkFrom,
          workspaceRoot,
          labels: options.labels,
        })
        .slice(0, 32);
      try {
        // Only a paused template is finished. Concurrent builds of one key can each publish one;
        // none deletes another's, since a deployment may already ship it. Later builds reuse the oldest.
        const published = oldestFirst((await listLabelled(TEMPLATE_LABEL, key)).filter(s => s.state === 'paused'));
        if (published[0] !== undefined) {
          ctx.log(`reusing Prized sandbox template ${published[0].id}`);
          return { templateId: published[0].id };
        }
        await sweepAbandonedBuilds(ctx.log);
        const template = await buildTemplate(ctx, key);
        ctx.log(`prepared Prized sandbox template ${template.id}`);
        return { templateId: template.id };
      } catch (error) {
        throw new Error(`${LOG_PREFIX} Failed to prepare the Prized sandbox template: ${errorMessage(error)}`, { cause: error });
      }
    },

    async start(ctx: SandboxSessionContext, open, artifactValue) {
      const artifact = requireArtifact(artifactValue);
      const label = sessionLabel(ctx.session.id);
      const existing = await sessionSandbox(label);
      if (existing !== undefined) {
        const sandbox = await adopt(existing);
        return { connection: connect(sandbox), state: { sandboxId: sandbox.id, version: 1 } };
      }
      const labels = { ...options.labels, ...open?.labels, [SESSION_LABEL]: label };
      let mine: SandboxLike;
      if (artifact.templateId !== undefined) {
        let copy: SandboxLike;
        try {
          copy = await forkOf(artifact.templateId, labels);
        } catch (error) {
          if (!isGone(error) && (error as { code?: unknown }).code !== 'not_found') throw error;
          throw new eve.SandboxTemplateNotProvisionedError({
            providerName: PROVIDER_NAME,
            reason: `The template sandbox ${artifact.templateId} was deleted.`,
            templateKey: artifact.templateId,
          });
        }
        mine = await withWorkspace(copy);
      } else {
        mine = await withWorkspace(await create(labels, true));
      }
      // Concurrent starts of one session (a prewarm and the first turn, two processes) each made
      // one: all keep the oldest, and each deletes its own if it isn't that one.
      const winner = (await sessionSandbox(label)) ?? mine;
      if (winner.id === mine.id) return { connection: connect(mine), state: { sandboxId: mine.id, version: 1 } };
      await deleteQuietly(mine);
      const sandbox = await adopt(winner);
      return { connection: connect(sandbox), state: { sandboxId: sandbox.id, version: 1 } };
    },

    async resume(_ctx, _artifact, stateValue) {
      const state = requireState(stateValue);
      let sandbox: SandboxLike;
      try {
        sandbox = await Sandbox.connect(state.sandboxId, { apiKey: apiKey() });
      } catch (error) {
        if (isGone(error)) throw new eve.SandboxSessionGoneError(`Prized sandbox ${state.sandboxId} no longer exists.`);
        throw error;
      }
      if (GONE.has(sandbox.state)) {
        // A failed sandbox can come back on its own; delete it so the session's next one is the only one.
        if (sandbox.state === 'error') await deleteQuietly(sandbox);
        throw new eve.SandboxSessionGoneError(`Prized sandbox ${state.sandboxId} is ${sandbox.state}.`);
      }
      return connect(sandbox);
    },

    async onSessionEnd(ctx, stateValue, { signal }) {
      signal?.throwIfAborted();
      const key = apiKey();
      const sandboxId = isObject(stateValue) && typeof stateValue.sandboxId === 'string' ? stateValue.sandboxId : undefined;
      if (sandboxId !== undefined) {
        try {
          await (await Sandbox.connect(sandboxId, { apiKey: key })).delete();
        } catch (error) {
          if (!isGone(error)) throw error;
        }
      }
      signal?.throwIfAborted();
      await Sandbox.deleteByLabels({ [SESSION_LABEL]: sessionLabel(ctx.session.id) }, { apiKey: key });
    },
  };
}
