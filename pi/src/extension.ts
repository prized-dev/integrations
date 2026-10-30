/**
 * @prized/pi: with `--prized`, Pi's tools run in a Prized sandbox that belongs to the Pi session.
 * Pi itself, the model calls and your files stay on your machine.
 *
 * - Start: Pi opens at once. The sandbox starts with your first prompt, and your git working tree is
 *   uploaded (ignored files stay home).
 * - Work: bash, read, write and edit run in the sandbox. A turn's end pauses it, memory and running
 *   processes included, and the next tool call resumes it in about a second.
 * - Pull: `/prized pull` applies the sandbox's changes to your checkout.
 * - Resume and fork: `pi -c` finds the same sandbox again; `/fork` copies it, processes and all.
 * - Exit: a sandbox with nothing left to pull is deleted; one with changes is paused and kept.
 */

import { realpath } from 'node:fs/promises';
import path from 'node:path';

import type { ExtensionAPI, ExtensionContext } from '@earendil-works/pi-coding-agent';
import { PrizedError, Sandbox } from '@prized/sdk';

import { AUTO_DELETE_MIN, type Config, FLAGS, readConfig } from './config.ts';
import { type Target, registerTools, useSandboxTools } from './tools.ts';
import { changes, gitRoot, init, pull, upload } from './workspace.ts';

const ENTRY = 'prized-sandbox';
const KEY_HELP = 'Make a key at https://prized.dev/dashboard/workspace/api-keys?via=pi and export it as PRIZED_API_KEY.';
const GONE = new Set(['sandbox_deleted', 'not_found']);
const SETTLING = new Set(['starting', 'pausing', 'resizing']);

export interface Sdk {
  Sandbox: typeof Sandbox;
}

/** Where the project lives in the sandbox. */
interface Layout {
  /** Working directory in the sandbox. */
  cwd: string;
  /** Repository root in the sandbox. */
  dir: string;
  /** Your checkout, which `/prized pull` writes to; unset outside a git checkout. */
  hostRoot?: string;
}

/** Recorded in the session so a resumed session finds its sandbox again. */
interface Entry extends Layout {
  sandboxId: string;
  /** Session that owns the sandbox. A fork inherits the parent's entry, so a different owner means "copy it". */
  owner: string;
}

/** What session_start learns; the sandbox itself starts later, on first use. */
interface Pending {
  apiKey: string;
  config: Config;
  layout: Layout;
  /** The sandbox this session (or the session it was forked from) used last. */
  prev?: Entry;
}

/** Pause and resume, one at a time, so a resume always lands after the pause that came before it. */
class Power {
  paused: boolean;
  private chain: Promise<unknown> = Promise.resolve();
  constructor(
    readonly sandbox: Sandbox,
    paused: boolean,
  ) {
    this.paused = paused;
  }

  /** Make sure the sandbox is running. */
  wake(): Promise<void> {
    const next = this.chain.then(async () => {
      if (!this.paused) return;
      await this.sandbox.resume();
      this.paused = false;
    });
    this.chain = next.catch(() => undefined);
    return next;
  }

  /** Pause it, unless `busy()` says something is using it by the time this turn comes. */
  pause(busy: () => boolean): Promise<void> {
    const next = this.chain.then(async () => {
      if (this.paused || busy()) return;
      await this.sandbox.pause();
      this.paused = true;
    });
    this.chain = next.catch(() => undefined);
    return next;
  }
}

interface Active extends Entry {
  sandbox: Sandbox;
  power: Power;
}

export function createExtension(sdk: Sdk = { Sandbox }): (pi: ExtensionAPI) => void {
  return function prizedExtension(pi: ExtensionAPI): void {
    for (const f of FLAGS) pi.registerFlag(f.name, { description: f.description, type: f.type });

    let pending: Pending | undefined;
    let active: Active | undefined;
    let starting: Promise<Active> | undefined;
    let apiKey: string | undefined;
    let lastCtx: ExtensionContext | undefined;
    /** Tool calls and commands in flight. */
    let busy = 0;
    /** An agent run is going: Pi is thinking or waiting for a tool. */
    let agentRunning = false;
    let timer: NodeJS.Timeout | undefined;

    const inUse = () => busy > 0 || agentRunning;

    function cancelTimer(): void {
      if (timer) clearTimeout(timer);
      timer = undefined;
    }

    /** After a turn, pause once nothing has used the sandbox for `--prized-pause` seconds. */
    function armPause(): void {
      cancelTimer();
      const a = active;
      const secs = pending?.config.pauseSecs ?? 0;
      if (!a || secs <= 0 || inUse()) return;
      timer = setTimeout(() => {
        a.power
          .pause(inUse)
          .then(() => (a.power.paused ? status(a, 'paused') : undefined))
          .catch(() => undefined);
      }, secs * 1000);
      timer.unref?.();
    }

    function status(a?: { sandboxId: string; cwd: string }, state?: string): void {
      try {
        const ui = lastCtx?.ui;
        if (!ui) return;
        const text = a ? `prized: ${a.sandboxId.slice(0, 12)}${state ? ` ${state}` : ''} ${a.cwd}` : undefined;
        ui.setStatus('prized', text === undefined ? undefined : ui.theme.fg('accent', text));
      } catch {
        // The context can be stale after a session switch; the status line is not worth an error.
      }
    }

    /** What a failed call on the sandbox means for the model and for the next call. */
    async function explain(err: unknown, a: Active): Promise<unknown> {
      if (!(err instanceof PrizedError) || !GONE.has(err.code)) return err;
      // not_found also answers for a missing file; only a sandbox that is gone makes it the sandbox's doing.
      if (err.code === 'not_found' && !(await isGone(a.sandbox))) return err;
      if (active === a) {
        active = undefined;
        if (pending) pending = { ...pending, prev: undefined };
        status(undefined);
      }
      return new Error(
        `The Prized sandbox ${a.sandboxId} is gone (deleted, or its run time ended), and so is everything the agent changed in it. ` +
          'The next tool call starts a new one from your checkout. Check what is still there before you carry on.',
      );
    }

    function targetOf(a: Active): Target {
      return {
        cwd: a.cwd,
        use: async (fn) => {
          busy += 1;
          cancelTimer();
          try {
            await a.power.wake();
            return await fn(a.sandbox);
          } catch (err) {
            throw await explain(err, a);
          } finally {
            busy -= 1;
            armPause();
          }
        },
      };
    }

    /** The session's sandbox. The first call starts it; calls meanwhile wait for the same start. */
    function sandbox(ctx: ExtensionContext): Promise<Active> {
      if (active) return Promise.resolve(active);
      if (!pending) return Promise.reject(new Error('Prized is not set up. Restart Pi with --prized and PRIZED_API_KEY.'));
      starting ??= start(ctx, pending).finally(() => {
        starting = undefined;
      });
      return starting;
    }

    registerTools(pi, async (ctx) => targetOf(await sandbox(ctx)));

    /** Reconnect to the recorded sandbox; undefined when it is gone. Any other error must not create a second one. */
    async function reconnect(p: Pending, prev: Entry, ctx: ExtensionContext): Promise<Sandbox | undefined> {
      try {
        const sb = await sdk.Sandbox.connect(prev.sandboxId, { apiKey: p.apiKey });
        // A sandbox on its way to paused or running is waited for; fork and resume need it settled.
        for (let i = 0; i < 120 && SETTLING.has(sb.state); i += 1) {
          await new Promise((r) => setTimeout(r, 500));
          await sb.refresh();
        }
        if (sb.state === 'running' || sb.state === 'paused') return sb;
        ctx.ui.notify(`Prized: sandbox ${prev.sandboxId} is ${sb.state}, creating a new one.`, 'warning');
        return undefined;
      } catch (err) {
        if (!(err instanceof PrizedError) || !GONE.has(err.code)) throw err;
        ctx.ui.notify(`Prized: sandbox ${prev.sandboxId} is gone, creating a new one.`, 'warning');
        return undefined;
      }
    }

    /** Reconnect to the session's sandbox, copy the parent session's one, or create one and upload the project. */
    async function start(ctx: ExtensionContext, p: Pending): Promise<Active> {
      const sessionId = ctx.sessionManager.getSessionId();
      const saved = ctx.sessionManager.getSessionFile() !== undefined;
      const started = Date.now();
      status({ sandboxId: 'sandbox', cwd: p.layout.cwd }, 'starting...');
      let created: Sandbox | undefined;
      try {
        let sb: Sandbox | undefined;
        let how = 'ready';
        if (p.prev) {
          sb = await reconnect(p, p.prev, ctx);
          if (sb && p.prev.owner !== sessionId) {
            const [copy] = await sb.fork({ count: 1, labels: { 'created-by': 'pi-prized', 'pi-session': sessionId } });
            if (!copy) throw new Error('Prized returned no fork');
            sb = created = copy;
            how = `forked from ${p.prev.sandboxId}`;
          } else if (sb) {
            how = sb.state === 'paused' ? 'reconnected (paused)' : 'reconnected';
          }
        }
        if (!sb) {
          sb = created = await sdk.Sandbox.create({
            apiKey: p.apiKey,
            template: p.config.template,
            resources: p.config.resources,
            maxRuntimeMin: p.config.maxRuntimeMin,
            autoPauseMin: p.config.idleMin,
            // A paused sandbox that nobody comes back to goes away by itself.
            ...(saved ? { autoDeleteMin: AUTO_DELETE_MIN } : {}),
            labels: { 'created-by': 'pi-prized', 'pi-session': sessionId },
          });
          if (p.layout.hostRoot) {
            status({ sandboxId: sb.id, cwd: p.layout.cwd }, 'uploading...');
            await upload(sb, p.layout.hostRoot, p.layout.dir);
          } else {
            await init(sb, p.layout.dir);
          }
        }
        const entry: Entry = { ...p.layout, sandboxId: sb.id, owner: sessionId };
        if (created && saved) pi.appendEntry(ENTRY, entry);
        const a: Active = { ...entry, sandbox: sb, power: new Power(sb, sb.state === 'paused') };
        active = a;
        const r = sb.resources;
        const secs = ((Date.now() - started) / 1000).toFixed(1);
        ctx.ui.notify(`Prized sandbox ${sb.id} ${how} in ${secs}s (${r.vcpu} vCPU, ${r.memoryGb} GB).`, 'info');
        status(a, a.power.paused ? 'paused' : undefined);
        return a;
      } catch (err) {
        await created?.delete().catch(() => undefined);
        status(undefined);
        ctx.ui.notify(`Prized: could not start the sandbox: ${message(err)}`, 'error');
        throw err;
      }
    }

    pi.registerCommand('prized', {
      description: 'Prized sandbox: status | pull [--branch] | pause | delete',
      getArgumentCompletions: (prefix) =>
        ['status', 'pull', 'pull --branch', 'pause', 'delete'].flatMap((s) =>
          s.startsWith(prefix.trim()) ? [{ value: s, label: s }] : [],
        ),
      handler: async (args, ctx) => {
        lastCtx = ctx;
        const sub = args.trim() || 'status';
        if (!pending) {
          ctx.ui.notify('No Prized sandbox. Start Pi with --prized.', 'warning');
          return;
        }
        // A resumed session's sandbox has not been touched yet: connect to it. A new session has none to show.
        if (!active && !pending.prev) {
          ctx.ui.notify('No Prized sandbox yet. It starts with your first prompt.', 'info');
          return;
        }
        const a = await sandbox(ctx).catch(() => undefined);
        if (!a) return;
        const t = targetOf(a);
        if (sub === 'pull' || sub === 'pull --branch') {
          return pullChanges(ctx, a, t, sub === 'pull' ? undefined : `pi/${ctx.sessionManager.getSessionId().slice(0, 8)}`);
        }
        if (sub === 'pause') {
          await a.power.pause(() => false);
          status(a, 'paused');
          ctx.ui.notify(`Paused ${a.sandboxId}. The next tool call resumes it.`, 'info');
          return;
        }
        if (sub === 'delete') {
          if (!(await ctx.ui.confirm('Delete sandbox', `Delete ${a.sandboxId}? Changes you did not pull are lost.`))) return;
          active = undefined;
          pending = { ...pending, prev: undefined };
          cancelTimer();
          await a.sandbox.delete();
          status(undefined);
          ctx.ui.notify(`Deleted ${a.sandboxId}. The next tool call starts a new one from your checkout.`, 'info');
          return;
        }
        if (sub !== 'status') {
          ctx.ui.notify(`Unknown subcommand "${sub}". Use status, pull, pull --branch, pause or delete.`, 'warning');
          return;
        }
        try {
          const { stat } = await t.use((sb) => changes(sb, a.dir));
          await a.sandbox.refresh().catch(() => undefined);
          const r = a.sandbox.resources;
          ctx.ui.notify(
            `Prized sandbox ${a.sandboxId} (${a.sandbox.state}, ${r.vcpu} vCPU, ${r.memoryGb} GB)\ncwd: ${a.cwd}\n${stat || 'No changes to pull.'}`,
            'info',
          );
        } catch (err) {
          ctx.ui.notify(message(err), 'error');
        }
      },
    });

    /** Apply the sandbox's changes to your checkout; with `branch`, commit them on that local branch. */
    async function pullChanges(ctx: ExtensionContext, a: Active, t: Target, branch?: string): Promise<void> {
      if (!a.hostRoot) {
        ctx.ui.notify('Pi was not started in a git checkout, so there is nowhere to pull to.', 'warning');
        return;
      }
      try {
        const c = await t.use((sb) => changes(sb, a.dir));
        if (!c.stat) {
          ctx.ui.notify('No changes to pull.', 'info');
          return;
        }
        const where = branch ? `a commit on branch ${branch} in ${a.hostRoot}` : a.hostRoot;
        if (!(await ctx.ui.confirm('Pull changes', `Apply to ${where}?\n\n${c.stat}`))) return;
        await t.use((sb) => pull(sb, a.dir, a.hostRoot as string, c, branch));
        ctx.ui.notify(branch ? `Committed on ${branch}. Push it with git push.` : 'Pulled. Review with git diff.', 'info');
      } catch (err) {
        ctx.ui.notify(`Pull failed: ${message(err)}`, 'error');
      }
    }

    pi.on('session_start', async (_event, ctx) => {
      lastCtx = ctx;
      if (pi.getFlag('prized') !== true || active || starting) return;
      let config: Config;
      try {
        config = readConfig((name) => pi.getFlag(name));
      } catch (err) {
        ctx.ui.notify(`Prized: ${message(err)}. Tools stay off until you fix it.`, 'error');
        return;
      }
      apiKey ??= process.env.PRIZED_API_KEY?.trim() || (ctx.hasUI ? (await ctx.ui.input('Prized API key', 'dcp_...'))?.trim() : undefined);
      if (!apiKey) {
        ctx.ui.notify(`Prized: no API key. Tools stay off until you set one. ${KEY_HELP}`, 'error');
        return;
      }
      const prev = lastEntry(ctx);
      pending = { apiKey, config, prev, layout: prev ?? (await layoutFor(ctx.sessionManager.getCwd())) };
      useSandboxTools(pi);
      status({ sandboxId: prev?.sandboxId ?? 'sandbox', cwd: pending.layout.cwd }, prev ? undefined : 'starts with your first prompt');
    });

    pi.on('before_agent_start', (event, ctx) => {
      lastCtx = ctx;
      if (!pending) return;
      agentRunning = true;
      cancelTimer();
      // Start (or resume) the sandbox while the model works on its first reply.
      void sandbox(ctx)
        .then((a) => a.power.wake())
        .then(() => (active ? status(active) : undefined))
        .catch(() => undefined);
      if (!event.systemPromptOptions) return;
      event.systemPromptOptions.cwd = `${pending.layout.cwd} (in a Prized sandbox, not on the user's machine)`;
      event.systemPromptOptions.promptGuidelines = [
        ...(event.systemPromptOptions.promptGuidelines ?? []),
        'You work in a copy of the project in a Prized sandbox: a Linux microVM, user prized, no sudo. ' +
          'The user brings your file changes back with /prized pull. Use bash (grep, rg, find, ls) to search. ' +
          'The sandbox pauses between turns and processes you started carry on at the next call; ' +
          'a server you start has no URL the user can open.',
      ];
    });

    // The run is over once Pi has nothing left to continue with; then the pause clock starts.
    pi.on('agent_settled', (_event, ctx) => {
      lastCtx = ctx;
      agentRunning = false;
      armPause();
    });

    pi.on('session_shutdown', async (event, ctx) => {
      cancelTimer();
      const a = active ?? (await starting?.catch(() => undefined));
      const config = pending?.config;
      active = undefined;
      pending = undefined;
      agentRunning = false;
      try {
        ctx.ui.setStatus('prized', undefined);
      } catch {
        // A stale context is fine at this point.
      }
      if (!a || !config) return;
      if (event.reason === 'reload') {
        // The new runtime finds the sandbox through the session's entry (or starts one for --no-session).
        if (ctx.sessionManager.getSessionFile() !== undefined) return;
      } else if (event.reason !== 'quit') {
        // /new, /resume and /fork leave this session behind, to be resumed or forked from later.
        await a.power.pause(() => false).catch(() => undefined);
        return;
      }
      const saved = ctx.sessionManager.getSessionFile() !== undefined;
      if (config.keep && saved) {
        await a.power.pause(() => false).catch(() => undefined);
        return finish(`Prized sandbox ${a.sandboxId} is paused. pi -c picks it up again.`);
      }
      let c: Awaited<ReturnType<typeof changes>> | undefined;
      try {
        await a.power.wake();
        c = await changes(a.sandbox, a.dir);
        if (c.stat && config.pullOnExit && a.hostRoot) {
          await pull(a.sandbox, a.dir, a.hostRoot, c);
          c = undefined;
        }
      } catch (err) {
        // Not knowing is not a reason to delete someone's work.
        await a.power.pause(() => false).catch(() => undefined);
        return finish(`Prized sandbox ${a.sandboxId} is paused, not deleted: ${message(err)}. Delete it with: prized sandbox rm ${a.sandboxId}`);
      }
      if (c?.stat) {
        await a.power.pause(() => false).catch(() => undefined);
        return finish(
          `Prized sandbox ${a.sandboxId} is paused with changes you have not pulled:\n${c.stat}\n` +
            `Run pi -c and /prized pull to bring them back, or delete it with: prized sandbox rm ${a.sandboxId}`,
        );
      }
      await a.sandbox.delete().catch(() => undefined);
    });

    /** Say it again once Pi has restored the terminal; the TUI may already be gone. */
    function finish(text: string): void {
      try {
        lastCtx?.ui.notify(text, 'info');
      } catch {
        // see above
      }
      process.once('exit', () => process.stderr.write(`${text}\n`));
    }
  };
}

/** Whether Prized no longer has this sandbox. An error that says nothing about it counts as "still there". */
async function isGone(sandbox: Sandbox): Promise<boolean> {
  try {
    await sandbox.refresh();
    return sandbox.state === 'deleted' || sandbox.state === 'deleting';
  } catch (err) {
    return err instanceof PrizedError && GONE.has(err.code);
  }
}

/** The sandbox mirrors your checkout, so the agent sees the same subdirectory you started Pi in. */
async function layoutFor(startDir: string): Promise<Layout> {
  // git prints the resolved path (/private/var/... on macOS), so resolve symlinks here too.
  const hostCwd = await realpath(startDir);
  const hostRoot = await gitRoot(hostCwd);
  const name = path.basename(hostRoot ?? 'workspace').replace(/[^\w.-]/g, '_') || 'workspace';
  const dir = `/home/prized/work/${name}`;
  const cwd = hostRoot ? path.posix.join(dir, path.relative(hostRoot, hostCwd).split(path.sep).join('/')) : dir;
  return { cwd, dir, hostRoot };
}

function lastEntry(ctx: ExtensionContext): Entry | undefined {
  const e = ctx.sessionManager.getEntries().findLast((e) => e.type === 'custom' && e.customType === ENTRY);
  // Entries of this custom type are written only by this extension, always as Entry.
  return e?.type === 'custom' ? (e.data as Entry) : undefined;
}

const message = (err: unknown): string => (err instanceof Error ? err.message : String(err));
