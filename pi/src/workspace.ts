/**
 * Moves the project into the sandbox and the agent's changes back.
 *
 * Up: the files git sees (tracked, plus untracked that are not ignored) go up as one tarball, so
 * ignored files such as `.env` stay on your machine. In the sandbox they become a fresh git
 * repository whose first commit is the baseline.
 *
 * Down: the sandbox tree is diffed against that baseline, whatever the agent did with git meanwhile,
 * and the patch is applied to your checkout with `git apply`. The baseline then moves forward, so the
 * next pull brings only newer changes. No git credentials are involved: nothing is cloned or pushed.
 */

import { spawn } from 'node:child_process';
import { lstatSync } from 'node:fs';
import path from 'node:path';

import type { Sandbox } from '@prized/sdk';

/** Where the sandbox keeps what this extension stages. Inside /home/prized, so the files API reaches it. */
export const STAGE = '/home/prized/.cache/pi-prized';
/** The sandbox's file transfers stop at 100 MiB. */
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

const BASE_REF = 'refs/pi-prized/base';
const IDENTITY = '-c user.name="Pi agent" -c user.email=pi-agent@localhost';

/** Single-quote a string for a POSIX shell. */
export function shq(s: string): string {
  return `'${s.replace(/'/g, `'\\''`)}'`;
}

/** Run a command on this machine; resolves with stdout, rejects with stderr. */
export function run(cmd: string, args: string[], opts: { cwd: string; input?: Buffer | string }): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    // COPYFILE_DISABLE stops macOS tar from adding `._*` metadata files.
    const child = spawn(cmd, args, { cwd: opts.cwd, env: { ...process.env, COPYFILE_DISABLE: '1' } });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    child.stdout.on('data', (d: Buffer) => out.push(d));
    child.stderr.on('data', (d: Buffer) => err.push(d));
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) resolve(Buffer.concat(out));
      else reject(new Error(Buffer.concat(err).toString().trim() || `${cmd} exited ${code}`));
    });
    child.stdin.on('error', () => undefined); // the process may exit before it reads its input
    child.stdin.end(opts.input);
  });
}

/** Root of the git checkout that contains `cwd`, or undefined. */
export async function gitRoot(cwd: string): Promise<string | undefined> {
  return run('git', ['rev-parse', '--show-toplevel'], { cwd }).then(
    (out) => out.toString().trim(),
    () => undefined,
  );
}

/** Run a script in the sandbox; resolves with stdout, throws with stderr when it fails. */
export async function sh(sandbox: Sandbox, script: string, cwd?: string): Promise<string> {
  // Under 4 minutes, so the SDK sends it as one request instead of starting it in the background and polling.
  const r = await sandbox.exec(script, { cwd, timeoutMs: 200_000, fullOutput: true });
  if (r.exitCode !== 0) throw new Error((r.stderr || r.stdout).trim() || `command exited ${r.exitCode}`);
  return r.stdout;
}

const exists = (p: string): boolean => {
  try {
    lstatSync(p); // a symlink counts even when its target is gone
    return true;
  } catch {
    return false;
  }
};

/** Upload the working tree at `root` to `dir` in the sandbox. Returns the number of files. */
export async function upload(sandbox: Sandbox, root: string, dir: string, maxBytes: number = MAX_UPLOAD_BYTES): Promise<number> {
  const listed = (await run('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], { cwd: root }))
    .toString()
    .split('\0');
  // Tracked files deleted in the working tree are still listed; tar would fail on them.
  const files = listed.filter((f) => f && exists(path.join(root, f)));
  if (files.length === 0) {
    await init(sandbox, dir);
    return 0;
  }
  const tarball = await run('tar', ['-czf', '-', '--null', '-T', '-'], { cwd: root, input: files.join('\0') });
  if (tarball.length > maxBytes) {
    const mib = Math.round(tarball.length / 1024 / 1024);
    throw new Error(
      `the project is ${mib} MiB packed and uploads stop at 100 MiB. Add the big files to .gitignore, or start Pi in a smaller folder.`,
    );
  }
  const stage = `${STAGE}/upload.tgz`;
  await sandbox.files.write(stage, new Uint8Array(tarball));
  await sh(sandbox, `mkdir -p ${shq(dir)} && tar -xzf ${shq(stage)} -C ${shq(dir)} && rm -f ${shq(stage)}`);
  await baseline(sandbox, dir, 'Your working tree when the session started');
  return files.length;
}

/** An empty repository, for sessions started outside a git checkout. */
export async function init(sandbox: Sandbox, dir: string): Promise<void> {
  await sh(sandbox, `mkdir -p ${shq(dir)}`);
  await baseline(sandbox, dir, 'Empty workspace');
}

async function baseline(sandbox: Sandbox, dir: string, message: string): Promise<void> {
  await sh(
    sandbox,
    `git init -q -b main && git config user.name "Pi agent" && git config user.email pi-agent@localhost` +
      ` && git add -A && git ${IDENTITY} commit -q --allow-empty -m ${shq(message)} && git update-ref ${BASE_REF} HEAD`,
    dir,
  );
}

export interface Changes {
  /** Tree of the sandbox working directory now. */
  tree: string;
  /** `git diff --stat` against the baseline; empty when nothing changed. */
  stat: string;
  patch: Buffer;
}

/** Changes in the sandbox since the baseline. Uses a scratch index, so the agent's own index is untouched. */
export async function changes(sandbox: Sandbox, dir: string): Promise<Changes> {
  const out = await sh(
    sandbox,
    [
      'set -e',
      'idx=$(mktemp -u)',
      'export GIT_INDEX_FILE=$idx',
      `trap 'rm -f "$idx"' EXIT`,
      'git add -A',
      'tree=$(git write-tree)',
      'echo "$tree"',
      `git diff --stat=120 ${BASE_REF} "$tree"`,
      `mkdir -p ${shq(STAGE)}`,
      `git diff --binary ${BASE_REF} "$tree" > ${shq(`${STAGE}/changes.patch`)}`,
    ].join('\n'),
    dir,
  );
  const [tree = '', ...stat] = out.split('\n');
  const text = stat.join('\n').trim();
  // Bytes, not text: a patch can carry bytes that are not UTF-8.
  const patch = text ? Buffer.from(await sandbox.files.readBytes(`${STAGE}/changes.patch`)) : Buffer.alloc(0);
  return { tree: tree.trim(), stat: text, patch };
}

/**
 * Apply `changes` to the checkout at `root`, then move the sandbox baseline to them.
 *
 * With `branch`, the changes are committed on that local branch instead (created if needed), so the
 * commit holds the agent's changes only. That needs a clean working tree. Pushing and opening a pull
 * request stay with your own git.
 */
export async function pull(sandbox: Sandbox, dir: string, root: string, c: Changes, branch?: string): Promise<void> {
  const git = (...args: string[]) => run('git', args, { cwd: root });
  const apply = (...args: string[]) => run('git', ['apply', '--whitespace=nowarn', ...args, '-'], { cwd: root, input: c.patch });
  if (branch) {
    if ((await git('status', '--porcelain')).length > 0) throw new Error('commit or stash your local changes first');
    const before = (await git('rev-parse', '--abbrev-ref', 'HEAD')).toString().trim();
    let created = false;
    await git('switch', branch).catch(async () => {
      await git('switch', '-c', branch);
      created = true;
    });
    try {
      await apply('--index'); // all or nothing
    } catch (err) {
      await git('switch', before).catch(() => undefined);
      if (created) await git('branch', '-D', branch).catch(() => undefined);
      throw new Error(`the changes do not apply to ${branch}: ${(err as Error).message}`);
    }
    await git('commit', '-q', '-m', `Changes from a Pi session in a Prized sandbox\n\n${c.stat}`).catch((err: Error) => {
      throw new Error(`the changes are staged on ${branch} but git could not commit them: ${err.message}`);
    });
  } else {
    await apply('--check').catch((err: Error) => {
      throw new Error(`your files changed in a way that conflicts, so nothing was applied: ${err.message}`);
    });
    await apply();
  }
  await sh(sandbox, `git update-ref ${BASE_REF} $(git ${IDENTITY} commit-tree ${c.tree} -p ${BASE_REF} -m 'Pulled to your machine')`, dir);
}
