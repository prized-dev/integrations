/**
 * One Prized sandbox per OpenCode session tree (a session and its subagents), started on the first tool
 * call. OpenCode, the model calls and your files stay on your machine.
 *
 * - Start: the first tool call reconnects to the session's sandbox (found by its labels, so a restart of
 *   OpenCode picks it up again), forks the sandbox of the session this one was forked from, or creates
 *   one and uploads your git working tree (ignored files stay home).
 * - Pause: when the session goes idle, the sandbox pauses after `pauseSeconds`, memory and processes
 *   included; the next tool call resumes it.
 * - Pull: `/prized-pull` applies the changes to your checkout (see pullText).
 * - Exit: a sandbox with nothing left to pull is deleted; one with changes is paused and kept.
 *
 * Host-agnostic: the OpenCode plugin adapters (v1.ts) give it a Host.
 */

import { lstat, mkdir, readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import { PrizedError, Sandbox } from '@prized/sdk';

import { AUTO_DELETE_MIN, type Config, type Env } from './config.ts';
import type { Layout } from './paths.ts';
import { type ChangedFile, type Changes, type Packed, capStat, changes, diffOf, leftBehind, localStat, pack, pull, risks, send, sh, shq, verifyPatch } from './workspace.ts';

export const LABEL_CREATOR = 'opencode-prized';
const GONE = new Set(['sandbox_deleted', 'not_found']);
const SETTLING = new Set(['starting', 'pausing', 'resizing']);
const KEY_HELP = 'Make a key at https://prized.dev/dashboard/workspace/api-keys?via=opencode and export PRIZED_API_KEY before you start OpenCode.';

export interface Sdk {
  Sandbox: typeof Sandbox;
}

/** Where a tool result records its sandbox, so a forked session (which copies the results) knows what to fork. */
export interface SandboxRef {
  sandboxId: string;
  /** The root session that owns the sandbox. A fork carries the parent's record, so a different owner means "copy it". */
  owner: string;
}

/** What the plugin adapter tells the manager about OpenCode. */
export interface Host {
  /** The session at the top of `sessionID`'s tree (a subagent's parent's parent ...). */
  rootOf(sessionID: string): Promise<string>;
  /** The newest sandbox record in a session's history (copied there by a fork), if any. */
  inherited(sessionID: string): Promise<SandboxRef | undefined>;
  /** Tell the user (a toast, a log line). */
  notify(text: string, level: 'info' | 'warning' | 'error'): void;
}

/** The sandbox a tool runs in. */
export interface Target extends SandboxRef {
  layout: Layout;
  /** Run `fn` with the awake sandbox; the sandbox does not pause while `fn` runs. */
  use<T>(fn: (sandbox: Sandbox) => Promise<T>): Promise<T>;
}

/**
 * Pause and resume, one at a time, so a resume always lands after the pause that came before it. What it
 * remembers can be out of date (a command from somewhere else wakes a paused sandbox, Prized's own idle
 * pause stops a running one), so both ask Prized before they act.
 */
export class Power {
  paused: boolean;
  private chain: Promise<unknown> = Promise.resolve();
  constructor(
    readonly sandbox: Sandbox,
    paused: boolean,
  ) {
    this.paused = paused;
  }

  wake(): Promise<void> {
    const next = this.chain.then(async () => {
      if (!this.paused) return;
      await this.sandbox.refresh().catch(() => undefined);
      if (this.sandbox.state !== 'running') await this.sandbox.resume();
      this.paused = false;
    });
    this.chain = next.catch(() => undefined);
    return next;
  }

  /** Pause, unless `busy()` says something is using it by the time this turn comes. */
  pause(busy: () => boolean): Promise<void> {
    const next = this.chain.then(async () => {
      if (busy()) return;
      await this.sandbox.refresh().catch(() => undefined);
      if (this.sandbox.state !== 'paused' && this.sandbox.state !== 'pausing') await this.sandbox.pause();
      this.paused = true;
    });
    this.chain = next.catch(() => undefined);
    return next;
  }
}

interface Active extends SandboxRef {
  sandbox: Sandbox;
  power: Power;
  /** Tool calls in flight. */
  inflight: number;
  /** Sessions of this tree that are working (OpenCode said "busy" and not yet "idle"). */
  working: Set<string>;
  timer?: NodeJS.Timeout;
  warnedExpiry?: boolean;
}

const message = (err: unknown): string => (err instanceof Error ? err.message : String(err));

export class Sandboxes {
  private readonly active = new Map<string, Active>();
  private readonly starting = new Map<string, Promise<Active>>();
  /** Roots we already know, so a subagent's every call does not ask OpenCode again. */
  private readonly roots = new Map<string, string>();
  /** Sessions OpenCode said are busy before their sandbox existed. */
  private readonly early = new Map<string, Set<string>>();
  /** Where the tree each session's held-back review showed is kept, on your machine (never in the sandbox,
   *  which the agent controls), so `--all` applies that tree and no other, across restarts too. */
  private readonly reviewFile: string;
  private disposed = false;

  constructor(
    private readonly sdk: Sdk,
    private readonly config: Config,
    private readonly env: Env,
    readonly layout: Layout,
    private readonly host: Host,
    private readonly limits: { maxUploadBytes?: number; stateDir?: string } = {},
    /** Why the plugin is not set up (a bad option): every tool call fails with it, nothing runs anywhere. */
    private readonly setupError?: string,
  ) {
    const state = limits.stateDir ?? path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state'), 'opencode-prized');
    this.reviewFile = path.join(state, 'reviewed.json');
  }

  private async reviewed(): Promise<{ [root: string]: { tree: string; at: number } }> {
    try {
      return JSON.parse(await readFile(this.reviewFile, 'utf8'));
    } catch {
      return {};
    }
  }

  private async setReviewed(root: string, tree: string | undefined): Promise<void> {
    const all = await this.reviewed();
    if (tree === undefined) delete all[root];
    else all[root] = { tree, at: Date.now() };
    // Keep the newest hundred.
    const kept = Object.fromEntries(Object.entries(all).sort((a, b) => b[1].at - a[1].at).slice(0, 100));
    await mkdir(path.dirname(this.reviewFile), { recursive: true });
    await writeFile(this.reviewFile, JSON.stringify(kept), { mode: 0o600 });
  }

  private client() {
    if (this.setupError) throw new Error(`${this.setupError} Nothing ran (and nothing ran on your machine) until it is fixed.`);
    if (!this.env.apiKey) throw new Error(`No Prized API key, so nothing ran (and nothing ran on your machine). ${KEY_HELP}`);
    return { apiKey: this.env.apiKey, baseUrl: this.env.baseUrl, edgeUrl: this.env.edgeUrl };
  }

  async root(sessionID: string): Promise<string> {
    const known = this.roots.get(sessionID);
    if (known) return known;
    try {
      const root = await this.host.rootOf(sessionID);
      this.roots.set(sessionID, root);
      return root;
    } catch {
      // Not known this time (OpenCode did not answer): use the session itself, and ask again next call.
      return sessionID;
    }
  }

  /** The sandbox for a tool call in `sessionID`, started (or reconnected, or forked) on first use. */
  async target(sessionID: string): Promise<Target> {
    if (this.disposed) throw new Error('OpenCode is shutting down; the Prized sandbox takes no more commands.');
    const root = await this.root(sessionID);
    const a = this.active.get(root) ?? (await this.start(root));
    return this.targetOf(a);
  }

  private targetOf(a: Active): Target {
    return {
      sandboxId: a.sandboxId,
      owner: a.owner,
      layout: this.layout,
      use: async (fn) => {
        a.inflight += 1;
        this.cancel(a);
        try {
          await a.power.wake();
          return await fn(a.sandbox);
        } catch (err) {
          throw await this.explain(err, a);
        } finally {
          a.inflight -= 1;
          this.arm(a);
        }
      },
    };
  }

  private start(root: string): Promise<Active> {
    let p = this.starting.get(root);
    if (!p) {
      p = this.open(root).finally(() => this.starting.delete(root));
      this.starting.set(root, p);
    }
    return p;
  }

  private labels(root: string): { [k: string]: string } {
    return { 'created-by': LABEL_CREATOR, 'opencode-session': root };
  }

  /** Wait for a sandbox to settle; undefined when it is gone or broken. */
  private async settle(sb: Sandbox): Promise<Sandbox | undefined> {
    for (let i = 0; i < 120 && SETTLING.has(sb.state); i += 1) {
      await new Promise((r) => setTimeout(r, 500));
      await sb.refresh();
    }
    if (SETTLING.has(sb.state)) throw new Error(`sandbox ${sb.id} is still ${sb.state} after a minute; try again`);
    return sb.state === 'running' || sb.state === 'paused' ? sb : undefined;
  }

  /** Reconnect to the session's sandbox, fork the one it was forked from, or create one and upload the project. */
  private async open(root: string): Promise<Active> {
    const client = this.client();
    const started = Date.now();
    let created: Sandbox | undefined;
    try {
      let sb: Sandbox | undefined;
      let how = 'ready';
      // 1. This session's own sandbox, from before a restart.
      const mine = await this.sdk.Sandbox.list({ ...client, labels: this.labels(root) });
      for (const cand of mine) {
        sb = await this.settle(cand).catch(() => undefined);
        if (sb) {
          how = sb.state === 'paused' ? 'reconnected (paused)' : 'reconnected';
          break;
        }
      }
      // 2. A forked session: its history names the parent's sandbox. Copy it, processes and all.
      if (!sb) {
        const prev = await this.host.inherited(root).catch(() => undefined);
        if (prev && prev.owner !== root) {
          let parent: Sandbox | undefined;
          try {
            parent = await this.settle(await this.sdk.Sandbox.connect(prev.sandboxId, client));
          } catch (err) {
            if (!(err instanceof PrizedError) || !GONE.has(err.code)) throw err;
          }
          if (parent) {
            const [copy] = await parent.fork({ count: 1, labels: this.labels(root) });
            if (!copy) throw new Error('Prized returned no fork');
            sb = created = copy;
            how = `forked from ${prev.sandboxId}`;
          } else {
            this.host.notify(`the sandbox of the session this one was forked from (${prev.sandboxId}) is gone; starting a new one from your checkout.`, 'warning');
          }
        }
      }
      // 3. A new one with your working tree. Pack first: a project that is too big fails before there is a sandbox to pay for.
      if (!sb) {
        let packed: Packed | undefined;
        if (this.layout.hostRoot) {
          packed = await pack(this.layout.hostRoot, this.limits.maxUploadBytes, this.env.apiKey ? [this.env.apiKey] : []);
          if (packed.skipped.length) {
            const n = packed.skipped.length;
            this.host.notify(`left out ${n} nested repositor${n === 1 ? 'y' : 'ies'} or submodule${n === 1 ? '' : 's'}: ${packed.skipped.slice(0, 5).join(', ')}`, 'warning');
          }
        } else {
          this.host.notify('this is not a git checkout, so the agent starts with an empty workspace and /prized-pull has nowhere to bring files to.', 'warning');
        }
        sb = created = await this.sdk.Sandbox.create({
          ...client,
          template: this.config.template,
          resources: this.config.resources,
          maxRuntimeMin: this.config.maxRuntimeMin,
          autoPauseMin: this.config.idleMin,
          autoDeleteMin: AUTO_DELETE_MIN,
          labels: this.labels(root),
        });
        await send(sb, packed ?? { tarball: Buffer.alloc(0), files: 0, skipped: [] }, this.layout.dir);
        if (this.layout.cwd !== this.layout.dir) await sh(sb, `mkdir -p ${shq(this.layout.cwd)}`);
      }
      if (this.disposed) {
        // OpenCode shut down while this sandbox was starting: nobody will close it, so do not keep it.
        await created?.delete().catch(() => undefined);
        created = undefined;
        throw new Error('OpenCode is shutting down');
      }
      const a: Active = {
        sandboxId: sb.id,
        owner: root,
        sandbox: sb,
        power: new Power(sb, sb.state === 'paused'),
        inflight: 0,
        working: this.early.get(root) ?? new Set(),
      };
      this.early.delete(root);
      this.active.set(root, a);
      const r = sb.resources;
      this.host.notify(`Prized sandbox ${sb.id} ${how} in ${((Date.now() - started) / 1000).toFixed(1)}s (${r.vcpu} vCPU, ${r.memoryGb} GB).`, 'info');
      return a;
    } catch (err) {
      await created?.delete().catch(() => undefined);
      throw new Error(`The Prized sandbox could not start, so nothing ran (and nothing ran on your machine): ${message(err)}`);
    }
  }

  /** What a failed call means for the model and for the next call. */
  private async explain(err: unknown, a: Active): Promise<unknown> {
    if (!(err instanceof PrizedError) || !GONE.has(err.code)) return err;
    // not_found also answers for a missing file; only a sandbox that is gone makes it the sandbox's doing.
    if (err.code === 'not_found' && !(await isGone(a.sandbox))) return err;
    if (this.active.get(a.owner) === a) {
      this.cancel(a);
      this.active.delete(a.owner);
    }
    return new Error(
      `The Prized sandbox ${a.sandboxId} is gone (deleted, or its run time ended), and so is everything the agent changed in it. ` +
        'The next tool call starts a new one from your checkout. Check what is still there before you carry on.',
    );
  }

  private inUse(a: Active): boolean {
    return a.inflight > 0 || a.working.size > 0;
  }

  private cancel(a: Active): void {
    if (a.timer) clearTimeout(a.timer);
    a.timer = undefined;
  }

  /** Once nothing has used the sandbox for `pauseSeconds`, pause it. */
  private arm(a: Active): void {
    this.cancel(a);
    if (this.disposed || this.config.pauseSecs <= 0 || this.inUse(a) || this.active.get(a.owner) !== a) return;
    a.timer = setTimeout(() => {
      a.timer = undefined;
      a.power.pause(() => this.inUse(a)).catch(() => undefined);
    }, this.config.pauseSecs * 1000);
    a.timer.unref?.();
  }

  /** OpenCode says a session started or stopped working. */
  async status(sessionID: string, busy: boolean): Promise<void> {
    const root = await this.root(sessionID);
    const a = this.active.get(root);
    let working = a?.working;
    if (!a) {
      working = this.early.get(root) ?? new Set();
      this.early.set(root, working);
    }
    if (busy) working?.add(sessionID);
    else working?.delete(sessionID);
    if (!a) return;
    if (busy) {
      this.cancel(a);
    } else {
      this.arm(a);
      if (a.working.size === 0) this.warnIfEnding(a);
    }
  }

  /** A sandbox is deleted when its run time ends, with whatever was not pulled. Say so while there is time. */
  private warnIfEnding(a: Active): void {
    if (a.warnedExpiry) return;
    void a.sandbox
      .refresh()
      .then(() => {
        const left = a.sandbox.expiresAt ? a.sandbox.expiresAt.getTime() - Date.now() : undefined;
        if (left === undefined || left > 10 * 60_000 || a.warnedExpiry) return;
        a.warnedExpiry = true;
        this.host.notify(`sandbox ${a.sandboxId} is deleted in ${Math.max(0, Math.round(left / 60_000))} minutes, when its run time ends. /prized-pull before then keeps its changes.`, 'warning');
      })
      .catch(() => undefined);
  }

  /** The sandbox of a session if it already exists or existed before a restart; never creates one. */
  private async existing(sessionID: string): Promise<Active | undefined> {
    if (this.disposed) return undefined;
    const root = await this.root(sessionID);
    const a = this.active.get(root) ?? (await this.starting.get(root)?.catch(() => undefined));
    if (a) return a;
    if (!this.env.apiKey) return undefined;
    const found = await this.sdk.Sandbox.list({ ...this.client(), labels: this.labels(root) }).catch(() => []);
    if (found.length === 0) return undefined;
    return this.start(root);
  }

  /** `/prized`: what the session's sandbox is doing. */
  async statusText(sessionID: string): Promise<string> {
    if (this.setupError) return `Prized is not set up: ${this.setupError}`;
    if (!this.env.apiKey) return `Prized is not set up: no API key. ${KEY_HELP}`;
    const a = await this.existing(sessionID);
    if (!a) return 'No Prized sandbox for this session yet. It starts with the first tool call.';
    await a.sandbox.refresh().catch(() => undefined);
    const r = a.sandbox.resources;
    const head = `Prized sandbox ${a.sandboxId} (${a.sandbox.state}, ${r.vcpu} vCPU, ${r.memoryGb} GB), folder ${this.layout.cwd}`;
    if (a.sandbox.state === 'paused') return `${head}\nPaused with its memory: /prized-pull, or the next tool call, wakes it.`;
    const c = await this.targetOf(a).use((sb) => changes(sb, this.layout.dir));
    return [head, c.stat ? `Not pulled yet:\n${capStat(c.stat)}` : 'No changes to pull.', leftBehind(c)].filter(Boolean).join('\n');
  }

  /**
   * `/prized-pull [--branch] [--all]`: apply the sandbox's changes to your checkout, or commit them on a
   * local branch. A change that needs a second look (a symlink, an executable, a file your tools run on
   * their own) holds the pull back until you have read it and run `/prized-pull --all`.
   */
  async pullText(sessionID: string, args: string): Promise<string> {
    const flags = new Set(args.split(/\s+/).filter(Boolean));
    const unknown = [...flags].filter((f) => f !== '--branch' && f !== '--all');
    if (unknown.length) return `Unknown option ${unknown.join(' ')}. Use /prized-pull, /prized-pull --branch, and --all to include changes that need a second look.`;
    if (this.setupError) return `Prized is not set up: ${this.setupError}`;
    if (!this.layout.hostRoot) return 'OpenCode was not started in a git checkout, so there is nowhere to pull to.';
    if (!this.env.apiKey) return `Prized is not set up: no API key. ${KEY_HELP}`;
    const hostRoot = this.layout.hostRoot;
    const a = await this.existing(sessionID);
    if (!a) return 'No Prized sandbox for this session yet, so there is nothing to pull.';
    const t = this.targetOf(a);
    const c = await t.use((sb) => changes(sb, this.layout.dir));
    const left = leftBehind(c);
    if (!c.stat) return ['No changes to pull.', left].filter(Boolean).join(' ');
    // Everything below is read on this machine from the patch that will be applied.
    await verifyPatch(c.patch, c.files, hostRoot);
    const stat = await localStat(c.patch, hostRoot);
    const care = [...risks(c.files), ...(await executableHere(hostRoot, c.files))];
    const root = await this.root(sessionID);
    // --all applies only the changes a review showed; anything newer gets a review of its own first.
    if (care.length && (!flags.has('--all') || (await this.reviewed())[root]?.tree !== c.tree)) {
      await this.setReviewed(root, c.tree);
      const changed = flags.has('--all') ? ['The changes are not the ones the last review showed, so nothing was applied. Here they are again.', ''] : [];
      return [
        ...changed,
        'Not pulled yet. These changes need a second look before they reach your machine:',
        ...care.slice(0, 12).map((r) => `- ${r}`),
        '',
        'Their diff:',
        diffOf(c.patch, [...new Set(c.files.filter((f) => risks([f]).length > 0 || care.some((r) => r.endsWith(`: ${f.path}`))).map((f) => f.path))]),
        '',
        `All changes:\n${capStat(stat)}`,
        '',
        'Run /prized-pull --all to apply exactly these (add --branch to commit them on a branch instead).',
      ].join('\n');
    }
    const branch = flags.has('--branch') ? `opencode/${root.replace(/[^\w-]/g, '').slice(-12)}` : undefined;
    await t.use((sb) => pull(sb, this.layout.dir, hostRoot, { ...c, stat }, branch));
    await this.setReviewed(root, undefined).catch(() => undefined);
    const where = branch ? `Committed on the local branch ${branch} in ${hostRoot}; push it with git push.` : `Pulled into ${hostRoot}; review with git diff.`;
    return [where, capStat(stat), care.length ? `Included, as you asked:\n${care.map((r) => `- ${r}`).join('\n')}` : '', left].filter(Boolean).join('\n');
  }

  /** The user deleted a session: delete its sandbox, unless it holds changes nobody pulled. */
  async sessionDeleted(sessionID: string): Promise<void> {
    const root = await this.root(sessionID);
    if (root !== sessionID) return; // a subagent's session; its root owns the sandbox
    const a = await this.existing(sessionID).catch(() => undefined);
    if (!a) return;
    this.cancel(a);
    this.active.delete(root);
    const note = await this.close(a, false, true);
    if (note) this.host.notify(note, 'warning');
  }

  /** Delete it if there is nothing to pull (unless `keep`), else pause it. Returns what the user should know, if anything. */
  private async close(a: Active, keep: boolean, deletedSession = false): Promise<string | undefined> {
    let c: Changes | undefined;
    try {
      await a.power.wake();
      c = await changes(a.sandbox, this.layout.dir);
    } catch (err) {
      // Not knowing is not a reason to delete someone's work.
      await a.power.pause(() => false).catch(() => undefined);
      return `Prized sandbox ${a.sandboxId} is paused, not deleted: ${message(err)}. Delete it with: prized sandbox rm ${a.sandboxId}`;
    }
    if (c.stat || c.nested.length > 0 || keep) {
      await a.power.pause(() => false).catch(() => undefined);
      if (!c.stat && c.nested.length === 0) return undefined;
      const left = leftBehind(c);
      return (
        `Prized sandbox ${a.sandboxId} is paused with changes you have not pulled:\n${c.stat ? capStat(c.stat) : ''}${left ? `\n${left}` : ''}\n` +
        (deletedSession
          ? `The session was deleted, so read the files with the prized CLI (prized sandbox exec ${a.sandboxId}), or delete it with: prized sandbox rm ${a.sandboxId}. `
          : `Continue the session (opencode --session ${a.owner}) and run /prized-pull to bring them back, or delete it with: prized sandbox rm ${a.sandboxId}. `) +
        'Prized deletes it a week after it paused.'
      );
    }
    try {
      await a.sandbox.delete();
      return undefined;
    } catch (err) {
      return `Prized could not delete sandbox ${a.sandboxId}: ${message(err)}. Delete it with: prized sandbox rm ${a.sandboxId}`;
    }
  }

  /** OpenCode is shutting down. Returns the notes for the user, one per sandbox kept. */
  async dispose(): Promise<string[]> {
    this.disposed = true;
    const all = new Map(this.active);
    for (const [root, p] of this.starting) {
      const a = await p.catch(() => undefined);
      if (a) all.set(root, a);
    }
    this.active.clear();
    const notes = await Promise.all(
      [...all.values()].map(async (a) => {
        this.cancel(a);
        return this.close(a, this.config.keep);
      }),
    );
    return notes.filter((n): n is string => !!n);
  }
}

/**
 * Files the patch changes that are executable or symlinks on your machine now, whatever the patch says their
 * mode was: an edit to one is code you may run later.
 */
async function executableHere(root: string, files: ChangedFile[]): Promise<string[]> {
  const out: string[] = [];
  for (const f of files) {
    if (f.oldMode === '000000' || f.oldMode === '100755' || f.oldMode === '120000') continue; // listed by risks() already, or new
    try {
      const st = await lstat(path.join(root, f.path));
      if (st.isSymbolicLink()) out.push(`a change to a symlink on your machine: ${f.path}`);
      else if (st.isFile() && (st.mode & 0o111) !== 0) out.push(`a change to an executable file: ${f.path} (read the diff)`);
    } catch {
      // not on your machine: the patch would create it
    }
  }
  return out;
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
