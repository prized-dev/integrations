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
export function run(
  cmd: string,
  args: string[],
  opts: { cwd: string; input?: Buffer | string; okCodes?: number[] },
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    // COPYFILE_DISABLE stops macOS tar from adding `._*` metadata files.
    const child = spawn(cmd, args, { cwd: opts.cwd, env: { ...process.env, COPYFILE_DISABLE: '1' } });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    child.stdout.on('data', (d: Buffer) => out.push(d));
    child.stderr.on('data', (d: Buffer) => err.push(d));
    child.on('error', reject);
    child.on('close', (code) => {
      if (code !== null && (code === 0 || opts.okCodes?.includes(code))) resolve(Buffer.concat(out));
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

/** What a project packed for upload looks like. */
export interface Packed {
  tarball: Buffer;
  /** Files in the tarball. */
  files: number;
  /** Nested repositories and submodules, left at home: their own ignore rules and secrets are not ours to read. */
  skipped: string[];
}

/** Split git's NUL-separated output without decoding it, so file names that are not UTF-8 survive. */
function splitNul(buf: Buffer): Buffer[] {
  const out: Buffer[] = [];
  let from = 0;
  for (let i = buf.indexOf(0, from); i >= 0; i = buf.indexOf(0, from)) {
    if (i > from) out.push(buf.subarray(from, i));
    from = i + 1;
  }
  if (from < buf.length) out.push(buf.subarray(from));
  return out;
}

/**
 * Pack the working tree at `root` into one tarball: the files git lists as tracked, plus untracked
 * ones that are not ignored. Nothing is sent yet, so a project that is too big fails before any
 * sandbox exists.
 */
export async function pack(root: string, maxBytes: number = MAX_UPLOAD_BYTES): Promise<Packed> {
  const listed = splitNul(await run('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], { cwd: root }));
  const base = Buffer.from(`${root}/`);
  const names: Buffer[] = [];
  const skipped: string[] = [];
  for (const name of listed) {
    let st;
    try {
      st = lstatSync(Buffer.concat([base, name])); // a symlink counts even when its target is gone
    } catch {
      continue; // tracked, but deleted from the working tree
    }
    if (st.isDirectory()) skipped.push(name.toString('utf8')); // a submodule or a nested repository
    else names.push(name);
  }
  if (names.length === 0) return { tarball: Buffer.alloc(0), files: 0, skipped };
  const input = Buffer.concat(names.flatMap((n) => [n, Buffer.from([0])]));
  // tar exits 1 when a file changed while it was read; the archive is still good.
  const tarball = await run('tar', ['-czf', '-', '--null', '--no-recursion', '-T', '-'], { cwd: root, input, okCodes: [1] });
  if (tarball.length > maxBytes) {
    const mib = Math.round(tarball.length / 1024 / 1024);
    throw new Error(
      `the project is ${mib} MiB packed and uploads stop at 100 MiB. Add the big files to .gitignore, or start Pi in a smaller folder.`,
    );
  }
  return { tarball, files: names.length, skipped };
}

/** Put a packed project in `dir` in the sandbox and commit it as the baseline. */
export async function send(sandbox: Sandbox, packed: Packed, dir: string): Promise<void> {
  if (packed.files === 0) return init(sandbox, dir);
  const stage = `${STAGE}/upload.tgz`;
  await sandbox.files.write(stage, new Uint8Array(packed.tarball));
  await sh(sandbox, `mkdir -p ${shq(dir)} && tar -xzf ${shq(stage)} -C ${shq(dir)} && rm -f ${shq(stage)}`);
  await baseline(sandbox, dir, 'Your working tree when the session started');
}

/** Pack and send. Returns what was packed. */
export async function upload(sandbox: Sandbox, root: string, dir: string, maxBytes: number = MAX_UPLOAD_BYTES): Promise<Packed> {
  const packed = await pack(root, maxBytes);
  await send(sandbox, packed, dir);
  return packed;
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
      ` && git add -f -A && git ${IDENTITY} commit -q --allow-empty -m ${shq(message)} && git update-ref ${BASE_REF} HEAD`,
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
      // Start from the baseline: a file it holds stays tracked even if the agent ignores it later.
      `git read-tree ${BASE_REF}`,
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
    // Where to go back to if the changes do not apply: the branch, or the commit on a detached HEAD.
    const head = (await git('rev-parse', '--abbrev-ref', 'HEAD')).toString().trim();
    const back = head === 'HEAD' ? ['--detach', (await git('rev-parse', 'HEAD')).toString().trim()] : [head];
    let created = false;
    await git('switch', branch).catch(async () => {
      await git('switch', '-c', branch);
      created = true;
    });
    try {
      await apply('--index'); // all or nothing
    } catch (err) {
      await git('switch', ...back).catch(() => undefined);
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

/** Files that run code on your machine when you next build, commit or open the project. */
const RUNS_CODE = /^(\.husky\/|\.githooks\/|\.pi\/|\.envrc$|\.vscode\/|\.devcontainer\/|\.gitmodules$|\.github\/workflows\/)/;

/**
 * What in a patch deserves a second look before it lands in your checkout: a symlink, an executable
 * file, or a file your tools run by themselves. The patch is what the agent wrote, so it is also what a
 * prompt injection in the code it read could have written.
 */
export function risks(patch: Buffer): string[] {
  const found: string[] = [];
  for (const block of patch.toString('latin1').split(/^diff --git /m).slice(1)) {
    const head = block.split('\n', 1)[0] ?? '';
    const file = (/ "?b\/(.*?)"?$/.exec(head)?.[1] ?? head).trim();
    const mode = /^(?:new file mode|new mode) (\d+)$/m.exec(block)?.[1];
    if (mode === '120000') found.push(`a symlink: ${file}`);
    else if (mode === '100755') found.push(`an executable: ${file}`);
    if (RUNS_CODE.test(file)) found.push(`a file your tools run on their own: ${file}`);
  }
  return found;
}

/** The stat as the confirm dialog shows it: a few dozen files, then a count. */
export function capStat(stat: string, lines = 30): string {
  const all = stat.split('\n');
  if (all.length <= lines + 1) return stat;
  return [...all.slice(0, lines), ` ... and ${all.length - lines - 1} more files`, all.at(-1)].join('\n');
}
