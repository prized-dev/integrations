/**
 * @prized/pi: with `--prized`, Pi's tools run in a Prized sandbox that belongs to the Pi session.
 * Pi itself, the model calls and your files stay on your machine.
 *
 * - Start: Pi opens at once. The sandbox starts with your first prompt, and your git working tree is
 *   uploaded (ignored files stay home).
 * - Work: bash, read, write and edit run in the sandbox. A turn's end pauses it, memory and running
 *   processes included, and the next tool call resumes it.
 * - Pull: `/prized pull` applies the sandbox's changes to your checkout.
 * - Resume and fork: `pi -c` finds the same sandbox again; `/fork` copies it, processes and all.
 * - Exit: a sandbox with nothing left to pull is deleted; one with changes is paused and kept.
 */

import { realpath } from 'node:fs/promises';
import path from 'node:path';

import type { ExtensionAPI, ExtensionContext } from '@earendil-works/pi-coding-agent';
import { PrizedError, Sandbox } from '@prized/sdk';

import { AUTO_DELETE_MIN, AUTO_DELETE_UNSAVED_MIN, type Config, FLAGS, readConfig } from './config.ts';
import { type Target, registerGuards, registerSandboxTools, useSandboxTools } from './tools.ts';
import { type Packed, capStat, changes, gitRoot, pack, pull, risks, send } from './workspace.ts';

const ENTRY = 'prized-sandbox';
const KEY_HELP = 'Make a key at https://prized.dev/dashboard/workspace/api-keys?via=pi and export it as PRIZED_API_KEY.';
const GONE = new Set(['sandbox_deleted', 'not_found']);
const SETTLING = new Set(['starting', 'pausing', 'resizing']);
/** Sessions that are not saved leave no entry behind; a reload keeps its sandbox through this, in the same process. */
const UNSAVED = Symbol.for('@prized/pi.unsaved');

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

export function createExtension(sdk: Sdk = { Sandbox }, limits: { maxUploadBytes?: number } = {}): (pi: ExtensionAPI) => void {
  return function prizedExtension(pi: ExtensionAPI): void {
    for (const f of FLAGS) pi.registerFlag(f.name, { description: f.description, type: f.type });

    let pending: Pending | undefined;
    let active: Active | undefined;
    let starting: Promise<Active> | undefined;
    let apiKey: string | undefined;
    let lastCtx: ExtensionContext | undefined;
    let toolsRegistered = false;
    /** Tool calls and commands in flight. */
    let busy = 0;
    /** An agent run is going: Pi is thinking or waiting for a tool. */
    let agentRunning = false;
    /** Which run last failed to start the sandbox, so one run's tool calls do not each try again. */
    let run = 0;
    let startFailure: { run: number; error: unknown } | undefined;
    let timer: NodeJS.Timeout | undefined;

    const enabled = () => pi.getFlag('prized') === true;
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
        // RPC mode has no theme.
        ui.setStatus('prized', text === undefined ? undefined : (ui.theme?.fg('accent', text) ?? text));
      } catch {
        // The context can be stale after a session switch; the status line is not worth an error.
      }
    }

    /** Tell the user. Print mode has no screen for notify, so there it goes to stderr as well. */
    function say(ctx: ExtensionContext, text: string, level: 'info' | 'warning' | 'error' = 'info'): void {
      const full = text.startsWith('Prized') ? text : `Prized: ${text}`;
      try {
        ctx.ui.notify(full, level);
      } catch {
        // see status()
      }
      if (!ctx.hasUI) process.stderr.write(`${full}\n`);
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
      // A run that already failed to start it does not try again on every tool call; the next prompt does.
      if (startFailure && startFailure.run === run && agentRunning) return Promise.reject(startFailure.error);
      starting ??= start(ctx, pending)
        .catch((err: unknown) => {
          startFailure = { run, error: err };
          throw err;
        })
        .finally(() => {
          starting = undefined;
        });
      return starting;
    }

    registerGuards(pi, enabled, async (ctx) => targetOf(await sandbox(ctx)));

    /** Reconnect to the recorded sandbox; undefined when it is gone. Any other error must not create a second one. */
    async function reconnect(p: Pending, prev: Entry, ctx: ExtensionContext): Promise<Sandbox | undefined> {
      try {
        const sb = await sdk.Sandbox.connect(prev.sandboxId, { apiKey: p.apiKey });
        // A sandbox on its way to paused or running is waited for; fork and resume need it settled.
        for (let i = 0; i < 120 && SETTLING.has(sb.state); i += 1) {
          await new Promise((r) => setTimeout(r, 500));
          await sb.refresh();
        }
        if (SETTLING.has(sb.state)) throw new Error(`sandbox ${prev.sandboxId} is still ${sb.state} after a minute; try again`);
        if (sb.state === 'running' || sb.state === 'paused') return sb;
        say(ctx, `sandbox ${prev.sandboxId} is ${sb.state}, creating a new one.`, 'warning');
        return undefined;
      } catch (err) {
        if (!(err instanceof PrizedError) || !GONE.has(err.code)) throw err;
        say(ctx, `sandbox ${prev.sandboxId} is gone, creating a new one.`, 'warning');
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
          // Pack first: a project that is too big fails before there is a sandbox to pay for.
          let packed: Packed | undefined;
          if (p.layout.hostRoot) {
            status({ sandboxId: 'sandbox', cwd: p.layout.cwd }, 'packing...');
            packed = await pack(p.layout.hostRoot, limits.maxUploadBytes);
            if (packed.skipped.length) {
              const n = packed.skipped.length;
              say(ctx, `left out ${n} nested repositor${n === 1 ? 'y' : 'ies'} or submodule${n === 1 ? '' : 's'}: ${packed.skipped.slice(0, 5).join(', ')}`, 'warning');
            }
          }
          sb = created = await sdk.Sandbox.create({
            apiKey: p.apiKey,
            template: p.config.template,
            resources: p.config.resources,
            maxRuntimeMin: p.config.maxRuntimeMin,
            autoPauseMin: p.config.idleMin,
            // A paused sandbox that nobody comes back to goes away by itself.
            autoDeleteMin: saved ? AUTO_DELETE_MIN : AUTO_DELETE_UNSAVED_MIN,
            labels: { 'created-by': 'pi-prized', 'pi-session': sessionId },
          });
          status({ sandboxId: sb.id, cwd: p.layout.cwd }, 'uploading...');
          await send(sb, packed ?? { tarball: Buffer.alloc(0), files: 0, skipped: [] }, p.layout.dir);
        }
        const entry: Entry = { ...p.layout, sandboxId: sb.id, owner: sessionId };
        if (created) remember(ctx, entry, saved);
        const a: Active = { ...entry, sandbox: sb, power: new Power(sb, sb.state === 'paused') };
        active = a;
        const r = sb.resources;
        const secs = ((Date.now() - started) / 1000).toFixed(1);
        say(ctx, `Prized sandbox ${sb.id} ${how} in ${secs}s (${r.vcpu} vCPU, ${r.memoryGb} GB).`);
        status(a, a.power.paused ? 'paused' : undefined);
        armPause(); // the run may have settled while the sandbox was starting
        return a;
      } catch (err) {
        await created?.delete().catch(() => undefined);
        status(undefined);
        say(ctx, `could not start the sandbox: ${message(err)}`, 'error');
        throw err;
      }
    }

    /** Record the sandbox so the session finds it again: in the session file, or for an unsaved session in this process. */
    function remember(ctx: ExtensionContext, entry: Entry, saved: boolean): void {
      if (saved) pi.appendEntry(ENTRY, entry);
      else unsaved().set(ctx.sessionManager.getSessionId(), entry);
    }

    function lastEntry(ctx: ExtensionContext): Entry | undefined {
      const e = ctx.sessionManager.getEntries().findLast((e) => e.type === 'custom' && e.customType === ENTRY);
      // Entries of this custom type are written only by this extension, always as Entry.
      if (e?.type === 'custom') return e.data as Entry;
      return ctx.sessionManager.getSessionFile() === undefined ? unsaved().get(ctx.sessionManager.getSessionId()) : undefined;
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
          say(ctx, 'Not running. Start Pi with --prized.', 'warning');
          return;
        }
        // A resumed session's sandbox has not been touched yet: connect to it. A new session has none to show.
        if (!active && !pending.prev) {
          say(ctx, 'No sandbox yet. It starts with your first prompt.');
          return;
        }
        const a = await sandbox(ctx).catch(() => undefined);
        if (!a) return;
        const t = targetOf(a);
        if (sub === 'pull' || sub === 'pull --branch') {
          return pullChanges(ctx, a, t, sub === 'pull' ? undefined : `pi/${ctx.sessionManager.getSessionId().slice(0, 8)}`);
        }
        if (sub === 'pause') {
          await a.power.pause(inUse);
          if (a.power.paused) {
            status(a, 'paused');
            say(ctx, `Paused ${a.sandboxId}. The next tool call resumes it.`);
          } else {
            say(ctx, `${a.sandboxId} is in use right now. It pauses by itself when the turn is over.`, 'warning');
          }
          return;
        }
        if (sub === 'delete') {
          if (!(await ctx.ui.confirm('Delete sandbox', `Delete ${a.sandboxId}? Changes you did not pull are lost.`))) return;
          try {
            await a.sandbox.delete();
          } catch (err) {
            say(ctx, `Could not delete ${a.sandboxId}: ${message(err)}`, 'error');
            return;
          }
          active = undefined;
          pending = { ...pending, prev: undefined };
          cancelTimer();
          status(undefined);
          say(ctx, `Deleted ${a.sandboxId}. The next tool call starts a new one from your checkout.`);
          return;
        }
        if (sub !== 'status') {
          say(ctx, `Unknown subcommand "${sub}". Use status, pull, pull --branch, pause or delete.`, 'warning');
          return;
        }
        try {
          const { stat } = await t.use((sb) => changes(sb, a.dir));
          await a.sandbox.refresh().catch(() => undefined);
          const r = a.sandbox.resources;
          say(ctx, `Prized sandbox ${a.sandboxId} (${a.sandbox.state}, ${r.vcpu} vCPU, ${r.memoryGb} GB)\ncwd: ${a.cwd}\n${stat ? capStat(stat) : 'No changes to pull.'}`);
        } catch (err) {
          say(ctx, message(err), 'error');
        }
      },
    });

    /** Apply the sandbox's changes to your checkout; with `branch`, commit them on that local branch. */
    async function pullChanges(ctx: ExtensionContext, a: Active, t: Target, branch?: string): Promise<void> {
      if (!a.hostRoot) {
        say(ctx, 'Pi was not started in a git checkout, so there is nowhere to pull to.', 'warning');
        return;
      }
      try {
        const c = await t.use((sb) => changes(sb, a.dir));
        if (!c.stat) {
          say(ctx, 'No changes to pull.');
          return;
        }
        const where = branch ? `a commit on branch ${branch} in ${a.hostRoot}` : a.hostRoot;
        const care = risks(c.patch);
        const look = care.length ? `\n\nLook twice at:\n${care.slice(0, 8).map((r) => `- ${r}`).join('\n')}` : '';
        if (!(await ctx.ui.confirm('Pull changes', `Apply to ${where}?\n\n${capStat(c.stat)}${look}`))) return;
        await t.use((sb) => pull(sb, a.dir, a.hostRoot as string, c, branch));
        say(ctx, branch ? `Committed on ${branch}. Push it with git push.` : 'Pulled. Review with git diff.');
      } catch (err) {
        say(ctx, `Pull failed: ${message(err)}`, 'error');
      }
    }

    pi.on('session_start', async (_event, ctx) => {
      lastCtx = ctx;
      if (!enabled() || active || starting) return;
      // Fail closed: from here on Pi's tools go to the sandbox or fail, whatever else goes wrong below.
      if (!toolsRegistered) {
        toolsRegistered = true;
        registerSandboxTools(pi, async (c) => targetOf(await sandbox(c)));
      }
      useSandboxTools(pi);
      let config: Config;
      try {
        config = readConfig((name) => pi.getFlag(name));
      } catch (err) {
        say(ctx, `${message(err)}. Tools stay off until you fix it.`, 'error');
        return;
      }
      apiKey ??= process.env.PRIZED_API_KEY?.trim() || (ctx.hasUI ? (await ctx.ui.input('Prized API key', 'dcp_...'))?.trim() : undefined);
      if (!apiKey) {
        say(ctx, `no API key. Tools stay off until you set one. ${KEY_HELP}`, 'error');
        return;
      }
      const prev = lastEntry(ctx);
      pending = { apiKey, config, prev, layout: prev ?? (await layoutFor(ctx.sessionManager.getCwd())) };
      status({ sandboxId: prev?.sandboxId ?? 'sandbox', cwd: pending.layout.cwd }, prev ? undefined : 'starts with your first prompt');
    });

    pi.on('before_agent_start', (event, ctx) => {
      lastCtx = ctx;
      if (!pending) return;
      agentRunning = true;
      run += 1;
      cancelTimer();
      // Start (or resume) the sandbox while the model works on its first reply.
      void sandbox(ctx)
        .then((a) => a.power.wake())
        .then(() => (active ? status(active) : undefined))
        .catch(() => undefined);
      const options = event.systemPromptOptions;
      if (!options) return;
      options.cwd = `${pending.layout.cwd} (in a Prized sandbox, not on the user's machine)`;
      // Skills are files on your machine, and read runs in the sandbox: the model could not open them.
      options.skills = [];
      options.promptGuidelines = [
        ...(options.promptGuidelines ?? []),
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
      // The new runtime finds the sandbox through the session's entry (or, for an unsaved session, through this process).
      if (event.reason === 'reload') return;
      // /new, /resume and /fork leave this session behind, to be resumed or forked from later.
      if (event.reason !== 'quit') {
        await a.power.pause(() => false).catch(() => undefined);
        return;
      }
      const saved = ctx.sessionManager.getSessionFile() !== undefined;
      const days = (saved ? AUTO_DELETE_MIN : AUTO_DELETE_UNSAVED_MIN) / 1440;
      const expiry = `Prized deletes it ${days === 1 ? 'a day' : `${days} days`} after it paused.`;
      if (config.keep && saved) {
        await a.power.pause(() => false).catch(() => undefined);
        return finish(ctx, `Prized sandbox ${a.sandboxId} is paused. pi -c picks it up again. ${expiry}`);
      }
      const how = (id: string): string => {
        if (saved && a.hostRoot) return 'Run pi -c and /prized pull to bring them back';
        const read = `read the files with the prized CLI (prized sandbox exec ${id})`;
        if (saved) return `There is no git checkout to pull to, so ${read}`;
        return `This session is not saved, so pi -c cannot reconnect: use --prized-pull next time, or ${read}`;
      };
      let c: Awaited<ReturnType<typeof changes>> | undefined;
      let held: string[] = [];
      try {
        await a.power.wake();
        c = await changes(a.sandbox, a.dir);
        if (c.stat && config.pullOnExit && a.hostRoot) {
          held = risks(c.patch);
          // Nobody is there to look at what the agent wrote, so what needs a second look waits for one.
          if (held.length === 0) {
            await pull(a.sandbox, a.dir, a.hostRoot, c);
            c = undefined;
          }
        }
      } catch (err) {
        // Not knowing is not a reason to delete someone's work.
        await a.power.pause(() => false).catch(() => undefined);
        return finish(ctx, `Prized sandbox ${a.sandboxId} is paused, not deleted: ${message(err)}. Delete it with: prized sandbox rm ${a.sandboxId}`);
      }
      if (c?.stat) {
        await a.power.pause(() => false).catch(() => undefined);
        const why = held.length ? `\nNot pulled automatically, because it includes:\n${held.slice(0, 8).map((r) => `- ${r}`).join('\n')}` : '';
        return finish(
          ctx,
          `Prized sandbox ${a.sandboxId} is paused with changes you have not pulled:\n${capStat(c.stat)}${why}\n` +
            `${how(a.sandboxId)}, or delete it with: prized sandbox rm ${a.sandboxId}. ${expiry}`,
        );
      }
      await a.sandbox.delete().catch(() => undefined);
      unsaved().delete(ctx.sessionManager.getSessionId());
    });

    /** Say it once more after Pi has restored the terminal; the screen may already be gone. */
    function finish(ctx: ExtensionContext, text: string): void {
      try {
        if (ctx.hasUI) ctx.ui.notify(text, 'info');
      } catch {
        // see status()
      }
      process.once('exit', () => process.stderr.write(`${text}\n`));
    }
  };
}

/** Sandboxes of sessions that are not saved, by session id, for a reload in the same process. */
function unsaved(): Map<string, Entry> {
  const g = globalThis as unknown as Record<symbol, Map<string, Entry> | undefined>;
  return (g[UNSAVED] ??= new Map());
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

const message = (err: unknown): string => (err instanceof Error ? err.message : String(err));
