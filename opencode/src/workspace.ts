/**
 * Moves the project into the sandbox and the agent's changes back. Ported from @prized/pi (integrations/pi).
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
import { lstatSync, readFileSync, readlinkSync } from 'node:fs';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import type { Sandbox } from '@prized/sdk';

/** Where the sandbox keeps what this extension stages. Inside /home/prized, so the files API reaches it. */
export const STAGE = '/home/prized/.cache/opencode-prized';
/** The sandbox's file transfers stop at 100 MiB. */
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

const BASE_REF = 'refs/opencode-prized/base';
const IDENTITY = '-c user.name="OpenCode agent" -c user.email=opencode-agent@localhost';

/** Single-quote a string for a POSIX shell. */
export function shq(s: string): string {
  return `'${s.replace(/'/g, `'\\''`)}'`;
}

/** Thrown by run() when a command printed more than its `maxOut`. */
export class OutputTooBig extends Error {}

/**
 * Run a command on this machine; resolves with stdout, rejects with stderr. `ok` says whether a
 * non-zero exit still counts as success; `maxOut` kills the command once it has printed that many bytes.
 */
export function run(
  cmd: string,
  args: string[],
  opts: { cwd: string; input?: Buffer | string; ok?: (code: number, stderr: string) => boolean; maxOut?: number; env?: NodeJS.ProcessEnv },
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    // COPYFILE_DISABLE stops macOS tar from adding `._*` metadata files.
    const child = spawn(cmd, args, { cwd: opts.cwd, env: { ...process.env, ...opts.env, COPYFILE_DISABLE: '1' } });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let size = 0;
    let over = false;
    child.stdout.on('data', (d: Buffer) => {
      size += d.length;
      if (opts.maxOut !== undefined && size > opts.maxOut) {
        over = true;
        out.length = 0; // do not keep what is already too much
        child.kill('SIGKILL');
      } else if (!over) {
        out.push(d);
      }
    });
    child.stderr.on('data', (d: Buffer) => err.push(d));
    child.on('error', reject);
    child.on('close', (code) => {
      const stderr = Buffer.concat(err).toString();
      if (over) reject(new OutputTooBig(`${cmd} printed more than ${opts.maxOut} bytes`));
      else if (code !== null && (code === 0 || opts.ok?.(code, stderr))) resolve(Buffer.concat(out));
      else reject(new Error(stderr.trim() || `${cmd} exited ${code}`));
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
export async function pack(root: string, maxBytes: number = MAX_UPLOAD_BYTES, secrets: string[] = []): Promise<Packed> {
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
  // Your Prized key must never reach a sandbox: a file that holds it (an .envrc git does not ignore, say) stops the upload.
  const needles = secrets.filter((x) => x.length >= 8).map((x) => Buffer.from(x));
  if (needles.length) {
    for (const name of names) {
      const file = Buffer.concat([base, name]);
      let bytes: Buffer;
      try {
        if (!lstatSync(file).isFile()) continue;
        bytes = readFileSync(file);
      } catch {
        continue;
      }
      if (needles.some((n) => bytes.includes(n))) {
        throw new Error(`${name.toString('utf8')} contains your PRIZED_API_KEY, so the project was not uploaded. Add the file to .gitignore (or take the key out of it) and try again.`);
      }
    }
  }
  const input = Buffer.concat(names.flatMap((n) => [n, Buffer.from([0])]));
  // GNU tar exits 1 when a file changed while it was read; the archive is still good. Anything else that fails is an error.
  const ok = (code: number, stderr: string) => code === 1 && /changed as we read it/.test(stderr);
  const tooBig = new Error(
    `the project is over ${Math.round(maxBytes / 1024 / 1024)} MiB packed and uploads stop there. Add the big files to .gitignore, or start OpenCode in a smaller folder.`,
  );
  const tarball = await run('tar', ['-czf', '-', '--null', '--no-recursion', '-T', '-'], { cwd: root, input, ok, maxOut: maxBytes }).catch(
    (err: unknown) => {
      throw err instanceof OutputTooBig ? tooBig : err;
    },
  );
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
export async function upload(sandbox: Sandbox, root: string, dir: string, maxBytes: number = MAX_UPLOAD_BYTES, secrets: string[] = []): Promise<Packed> {
  const packed = await pack(root, maxBytes, secrets);
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
    `git init -q -b main && git config user.name "OpenCode agent" && git config user.email opencode-agent@localhost` +
      ` && git add -f -A && git ${IDENTITY} commit -q --allow-empty -m ${shq(message)} && git update-ref ${BASE_REF} HEAD`,
    dir,
  );
}

/** One changed path, as `git diff --raw` reports it. Modes are git's: 100644, 100755, 120000 (symlink), 160000 (nested repo), 000000 (absent). */
export interface ChangedFile {
  path: string;
  oldMode: string;
  newMode: string;
}

export interface Changes {
  /** Tree of the sandbox working directory now. */
  tree: string;
  /** `git diff --stat` against the baseline; empty when nothing changed. */
  stat: string;
  patch: Buffer;
  files: ChangedFile[];
  /** Nested git repositories in the sandbox (folders with their own .git), which are left out: git cannot apply them to your checkout. */
  nested: string[];
}

/** Read `git diff --raw -z --no-renames` output: ":old new oldsha newsha status\0path\0" for each file. */
export function parseRaw(raw: Buffer): ChangedFile[] {
  const parts = splitNul(raw).map((b) => b.toString('utf8'));
  const files: ChangedFile[] = [];
  for (let i = 0; i + 1 < parts.length; i += 2) {
    const meta = /^:(\d+) (\d+) /.exec(parts[i] ?? '');
    if (meta) files.push({ oldMode: meta[1] as string, newMode: meta[2] as string, path: parts[i + 1] as string });
  }
  return files;
}

/** Diff options that make git's output the plain patch `git apply` reads, whatever the agent configured in the sandbox. */
const DIFF = '--no-ext-diff --no-textconv --no-color --no-renames --src-prefix=a/ --dst-prefix=b/';

/**
 * Changes in the sandbox since the baseline. Uses a scratch index, so the agent's own index is untouched, and
 * a scratch folder per call, so two calls at once do not mix their files.
 *
 * What the review shows and checks (`files`) is read here, on your machine, from the very patch bytes a pull
 * applies; nothing the sandbox says about the patch is trusted on its own.
 */
export async function changes(sandbox: Sandbox, dir: string): Promise<Changes> {
  const out = await sh(
    sandbox,
    [
      'set -e',
      `mkdir -p ${shq(STAGE)}`,
      `t=$(mktemp -d ${shq(`${STAGE}/c.XXXXXXXX`)})`,
      'idx=$(mktemp -u)',
      'export GIT_INDEX_FILE=$idx',
      `trap 'rm -f "$idx"' EXIT`,
      // A folder with its own .git is a repository of its own: git would record it as a pointer to a commit,
      // or refuse it when it has none, and git apply cannot create either. Leave them out and say so (one the
      // project ignores is left out by the ignore rules already).
      `find . \\( -path ./.git -o -path '*/node_modules' \\) -prune -o -name .git -printf '%h\\0' > "$t/found.nul"`,
      ': > "$t/nested.nul"',
      'ex=()',
      `while IFS= read -r -d '' d; do d=\${d#./}; git check-ignore -q --no-index -- "$d" && continue; ex+=(":(exclude,literal)$d"); printf '%s\\0' "$d" >> "$t/nested.nul"; done < "$t/found.nul"`,
      // Start from the baseline: a file it holds stays tracked even if the agent ignores it later.
      `git read-tree ${BASE_REF}`,
      'git add -A -- . "${ex[@]}"',
      'tree=$(git write-tree)',
      'echo "$t"',
      'echo "$tree"',
      `git -c core.quotePath=false diff ${DIFF} --stat=120 ${BASE_REF} "$tree"`,
      `git diff ${DIFF} --binary --full-index ${BASE_REF} "$tree" > "$t/changes.patch"`,
    ].join('\n'),
    dir,
  );
  const [scratch = '', tree = '', ...stat] = out.split('\n');
  const text = stat.join('\n').trim();
  if (!scratch.startsWith(`${STAGE}/c.`) || scratch.includes('..')) throw new Error('the sandbox answered the change listing oddly; try again');
  try {
    const nested = splitNul(Buffer.from(await sandbox.files.readBytes(`${scratch}/nested.nul`))).map((b) => b.toString('utf8').replace(/^\.\//, ''));
    if (!text) return { tree: tree.trim(), stat: '', patch: Buffer.alloc(0), files: [], nested };
    // Bytes, not text: a patch can carry bytes that are not UTF-8.
    const patch = Buffer.from(await sandbox.files.readBytes(`${scratch}/changes.patch`));
    return { tree: tree.trim(), stat: text, patch, files: patchFiles(patch), nested };
  } finally {
    await sh(sandbox, `rm -rf ${shq(scratch)}`).catch(() => undefined);
  }
}

/** A git C-quoted path ("a/x\\ty") read back to its bytes, as a latin1 string. */
function unquote(s: string): string {
  if (!s.startsWith('"')) return s;
  const bytes: number[] = [];
  const esc: { [c: string]: number } = { n: 10, t: 9, r: 13, '"': 34, '\\': 92, a: 7, b: 8, f: 12, v: 11 };
  for (let i = 1; i < s.length - 1; i += 1) {
    const c = s[i] as string;
    if (c !== '\\') {
      bytes.push(c.charCodeAt(0));
      continue;
    }
    const n = s[i + 1] as string;
    if (/[0-7]/.test(n)) {
      bytes.push(Number.parseInt(s.slice(i + 1, i + 4), 8));
      i += 3;
    } else {
      bytes.push(esc[n] ?? n.charCodeAt(0));
      i += 1;
    }
  }
  return Buffer.from(bytes).toString('latin1');
}

/** The path in a `diff --git a/P b/P` line (read as latin1, one character per byte; the caller decodes) (renames are refused, so both sides name the same file). */
function headerPath(line: string): string {
  const rest = line.slice('diff --git '.length);
  if (rest.startsWith('"')) {
    const end = rest.indexOf('" ', 1);
    return unquote(rest.slice(0, end + 1)).replace(/^a\//, '');
  }
  const len = (rest.length - 5) / 2;
  const a = rest.slice(2, 2 + len);
  if (!Number.isInteger(len) || rest.slice(0, 2) !== 'a/' || rest.slice(2 + len, 5 + len) !== ' b/' || rest.slice(5 + len) !== a) {
    throw new Error(`the patch from the sandbox names a file oddly (${line.slice(0, 200)}), so nothing was applied`);
  }
  return a;
}

/**
 * Every file a patch touches, with its old and new mode, read from the patch's own headers. A rename or copy
 * is refused: changes() asks git for none, so one means the patch is not what it should be.
 */
export function patchFiles(patch: Buffer): ChangedFile[] {
  const files: ChangedFile[] = [];
  const seen = new Set<string>();
  let cur: ChangedFile | undefined;
  let inHeader = false;
  // git writes a mode on the index line only when nothing else in the header names one: anything else is not git's.
  let named = false;
  let indexMode = false;
  const odd = (why: string) => new Error(`the patch from the sandbox is not one git wrote (${why}), so nothing was applied`);
  for (const line of patch.toString('latin1').split('\n')) {
    if (line.startsWith('diff --git ')) {
      if (line.includes('\r')) throw odd('a header line ends in CR');
      cur = { path: Buffer.from(headerPath(line), 'latin1').toString('utf8'), oldMode: '100644', newMode: '100644' };
      if (seen.has(cur.path)) throw odd(`${cur.path} twice`);
      seen.add(cur.path);
      files.push(cur);
      inHeader = true;
      named = false;
      indexMode = false;
      continue;
    }
    if (!cur || !inHeader) continue;
    if (line.includes('\r') && !/^(--- |\+\+\+ )/.test(line)) throw odd('a header line ends in CR');
    if (/^(new file mode|deleted file mode|old mode|new mode) /.test(line)) {
      if (indexMode) throw odd(`modes named twice for ${cur.path}`);
      named = true;
    }
    if (/^index [0-9a-f]+\.\.[0-9a-f]+ \d+$/.test(line)) {
      if (named) throw odd(`modes named twice for ${cur.path}`);
      indexMode = true;
    }
    let m: RegExpExecArray | null;
    if ((m = /^new file mode (\d{6})$/.exec(line))) {
      cur.oldMode = '000000';
      cur.newMode = m[1] as string;
    } else if ((m = /^deleted file mode (\d{6})$/.exec(line))) {
      cur.oldMode = m[1] as string;
      cur.newMode = '000000';
    } else if ((m = /^old mode (\d{6})$/.exec(line))) cur.oldMode = m[1] as string;
    else if ((m = /^new mode (\d{6})$/.exec(line))) cur.newMode = m[1] as string;
    else if ((m = /^index [0-9a-f]+\.\.[0-9a-f]+ (\d{6})$/.exec(line))) {
      cur.oldMode = m[1] as string;
      cur.newMode = m[1] as string;
    } else if (/^(rename|copy) (from|to) |^similarity index|^dissimilarity index/.test(line)) {
      throw new Error('the patch from the sandbox renames or copies a file, which this plugin never asks git for, so nothing was applied');
    } else if (/^(--- |\+\+\+ |@@|Binary files |GIT binary patch)/.test(line)) inHeader = false;
  }
  return files;
}

/**
 * Check, with git itself on your machine, that the files `git apply` would touch are exactly the files the
 * review read from the headers. The patch comes from a sandbox the agent controls (even its `git` could be
 * swapped), so a section without a `diff --git` header, or any other trick, must not slip past the review.
 */
export async function verifyPatch(patch: Buffer, files: ChangedFile[], cwd: string): Promise<void> {
  const out = await run('git', ['apply', '--numstat', '-z', '-'], { cwd, input: patch });
  const seen = new Set<string>();
  const parts = splitNul(out);
  for (let i = 0; i < parts.length; i += 1) {
    const rec = parts[i] as Buffer;
    const fields = rec.toString('latin1').split('\t');
    // "added\tdeleted\tpath", or for a rename "added\tdeleted\t" then old and new as the next two records.
    if (fields.length < 3 || fields[2] === '') {
      throw new Error('the patch from the sandbox renames or copies a file, which this plugin never asks git for, so nothing was applied');
    }
    seen.add(Buffer.from(fields.slice(2).join('\t'), 'latin1').toString('utf8'));
  }
  const listed = new Set(files.map((f) => f.path));
  const extra = [...seen].filter((p) => !listed.has(p));
  const missing = [...listed].filter((p) => !seen.has(p));
  if (extra.length || missing.length) {
    throw new Error(`the patch from the sandbox does not match its own file list (${[...extra, ...missing].slice(0, 3).join(', ')}), so nothing was applied`);
  }
}

/**
 * What `git apply` will leave on your machine, asked of git itself: the touched files, as they are in your
 * checkout now, go into a scratch index (blobs into a scratch object folder, so nothing is written to your
 * repository), the patch is applied to that index with `--cached`, and `git ls-files -s` reports each file's
 * mode afterwards. The review lists from this, so no reading of the patch text can disagree with git about a
 * symlink or an executable. Fails, applying nothing, where the real apply would conflict.
 */
export async function appliedModes(patch: Buffer, root: string, paths: string[]): Promise<ChangedFile[]> {
  const scratchDir = await mkdtemp(path.join(os.tmpdir(), 'opencode-prized-'));
  const env = { GIT_INDEX_FILE: path.join(scratchDir, 'index'), GIT_OBJECT_DIRECTORY: path.join(scratchDir, 'objects') };
  const git = (args: string[], input?: Buffer | string) => run('git', ['-c', 'core.hooksPath=/dev/null', ...args], { cwd: root, input, env });
  try {
    await mkdir(env.GIT_OBJECT_DIRECTORY, { recursive: true });
    const before = new Map<string, string>();
    const entries: Buffer[] = [];
    for (const p of paths) {
      const full = path.join(root, p);
      let st;
      try {
        st = lstatSync(full);
      } catch {
        continue; // not in your checkout: the patch creates it
      }
      let mode: string;
      let content: Buffer;
      if (st.isSymbolicLink()) {
        mode = '120000';
        content = Buffer.from(readlinkSync(full, { encoding: 'buffer' }));
      } else if (st.isFile()) {
        mode = st.mode & 0o111 ? '100755' : '100644';
        content = readFileSync(full);
      } else {
        throw new Error(`${p} is not a file in your checkout, so nothing was applied`);
      }
      const sha = (await git(['hash-object', '-w', '--stdin'], content)).toString().trim();
      before.set(p, mode);
      entries.push(Buffer.concat([Buffer.from(`${mode} ${sha}\t`), Buffer.from(p), Buffer.from([0])]));
    }
    if (entries.length) await git(['update-index', '-z', '--index-info'], Buffer.concat(entries));
    await git(['apply', '--cached', '--whitespace=nowarn', '-'], patch).catch((err: Error) => {
      throw new Error(`your files changed in a way that conflicts, so nothing was applied: ${err.message}`);
    });
    const after = new Map<string, string>();
    for (const rec of splitNul(await git(['ls-files', '-s', '-z']))) {
      const s = rec.toString('utf8');
      const tab = s.indexOf('\t');
      after.set(s.slice(tab + 1), s.slice(0, 6));
    }
    return paths.map((p) => ({ path: p, oldMode: before.get(p) ?? '000000', newMode: after.get(p) ?? '000000' }));
  } finally {
    await rm(scratchDir, { recursive: true, force: true });
  }
}

/** `git apply --stat` of the patch, on your machine: the summary a review shows is the patch's own. */
export async function localStat(patch: Buffer, cwd: string): Promise<string> {
  return (await run('git', ['apply', '--stat', '-'], { cwd, input: patch })).toString().trimEnd();
}

/** What a pull leaves behind, for the dialog and the exit note. */
export function leftBehind(c: Pick<Changes, 'nested'>): string {
  return c.nested.length
    ? `Not included: ${c.nested.slice(0, 5).join(', ')}${c.nested.length > 5 ? ', ...' : ''} (nested git repositories; delete their .git folder in the sandbox to include their files)`
    : '';
}

/**
 * Apply `changes` to the checkout at `root`, then move the sandbox baseline to them.
 *
 * With `branch`, the changes are committed on that local branch instead (created if needed), so the
 * commit holds the agent's changes only. That needs a clean working tree. Pushing and opening a pull
 * request stay with your own git.
 */
export async function pull(sandbox: Sandbox, dir: string, root: string, c: Changes, branch?: string): Promise<void> {
  // Hooks off for every git call here: what was just pulled could otherwise run as a commit hook (a new
  // .husky/pre-commit, a commitlint config a hook loads) on your machine.
  const git = (...args: string[]) => run('git', ['-c', 'core.hooksPath=/dev/null', ...args], { cwd: root });
  const apply = (...args: string[]) => run('git', ['-c', 'core.hooksPath=/dev/null', 'apply', '--whitespace=nowarn', ...args, '-'], { cwd: root, input: c.patch });
  if (branch) {
    // Untracked files are fine; edits to tracked ones would be swept into the branch's base.
    if ((await git('status', '--porcelain', '--untracked-files=no')).length > 0) throw new Error('commit or stash your local changes first');
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
    await git('commit', '-q', '--no-verify', '-m', `Changes from an OpenCode session in a Prized sandbox\n\n${c.stat}`).catch((err: Error) => {
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

/** Folders whose files tools run on their own: git hooks, direnv, editors, agents, CI, dev containers. Lower case. */
const AUTO_RUN_DIRS = [
  '.husky', '.githooks', '.opencode', '.pi', '.claude', '.cursor', '.vscode', '.idea', '.zed', '.devcontainer',
  '.github/workflows', '.circleci', '.cargo',
];
/** Files the same way, wherever they are in the project, lower case: hooks and their configs, env loaders, package
 *  manager settings, agent instructions OpenCode loads, and .gitignore (dropping .env from it uploads .env next time). */
const AUTO_RUN_FILES = new Set([
  '.envrc', '.mcp.json', '.gitmodules', '.gitignore', '.pre-commit-config.yaml', '.devcontainer.json', 'lefthook.yml', 'lefthook.yaml',
  '.lefthook.yml', 'mise.toml', '.mise.toml', '.npmrc', '.yarnrc', '.yarnrc.yml', '.pnpmfile.cjs', 'opencode.json', 'opencode.jsonc',
  'agents.md', 'claude.md', '.gitlab-ci.yml', '.lintstagedrc', '.lintstagedrc.json', '.lintstagedrc.js', '.lintstagedrc.cjs',
  '.lintstagedrc.mjs', '.lintstagedrc.yaml', '.lintstagedrc.yml', 'lint-staged.config.js', 'lint-staged.config.cjs',
  'lint-staged.config.mjs', 'commitlint.config.js', 'commitlint.config.cjs', 'commitlint.config.mjs', 'commitlint.config.ts',
  '.commitlintrc', '.commitlintrc.json', '.commitlintrc.js', '.commitlintrc.yml', '.huskyrc', '.huskyrc.json', '.huskyrc.js',
]);

// Case-insensitive: on macOS and Windows `.Husky/pre-commit` lands in `.husky`.
const runsByItself = (file: string): boolean => {
  const lower = file.toLowerCase();
  if (AUTO_RUN_FILES.has(lower.split('/').at(-1) ?? '')) return true;
  return AUTO_RUN_DIRS.some((dir) => `/${lower}`.includes(`/${dir}/`));
};

/**
 * What in the changes deserves a second look before it lands in your checkout: a symlink, an executable file (new,
 * made executable, or edited), or a file your tools run by themselves, at any depth. The changes are what the agent wrote after
 * reading your code, so look at them as you would a pull request from someone else.
 */
export function risks(files: ChangedFile[]): string[] {
  const found: string[] = [];
  for (const f of files) {
    if (f.newMode === '120000') found.push(`a symlink: ${f.path}`);
    // An executable is code the user runs later: a new one, one made executable, and an edit to one that already was
    // (a line slipped into a script) all get listed.
    else if (f.newMode === '100755') {
      found.push(f.oldMode === '100755' ? `a change to an executable file: ${f.path} (read the diff)` : `${f.oldMode === '000000' ? 'an executable' : 'made executable'}: ${f.path}`);
    }
    if (f.newMode !== '000000' && runsByItself(f.path)) found.push(`a file your tools run on their own: ${f.path}`);
  }
  return found;
}

/** The files `risks` has something to say about. */
export function riskyFiles(files: ChangedFile[]): string[] {
  return files.filter((f) => risks([f]).length > 0).map((f) => f.path);
}

/** The sections of the patch itself that touch `files`, capped at `maxLines`: what is shown is what is applied. */
export function diffOf(patch: Buffer, files: string[], maxLines = 200): string {
  if (files.length === 0) return '';
  const want = new Set(files);
  const lines: string[] = [];
  for (const part of patch.toString('latin1').split(/(?=^diff --git )/m)) {
    if (!part.startsWith('diff --git ')) continue;
    let p: string;
    try {
      p = Buffer.from(headerPath(part.slice(0, part.indexOf('\n'))), 'latin1').toString('utf8');
    } catch {
      continue;
    }
    if (want.has(p)) lines.push(...Buffer.from(part, 'latin1').toString('utf8').replace(/\n$/, '').split('\n'));
  }
  return lines.length > maxLines ? `${lines.slice(0, maxLines).join('\n')}\n... (${lines.length - maxLines} more lines)` : lines.join('\n');
}

/** The stat as the confirm dialog shows it: a few dozen files, then a count. */
export function capStat(stat: string, lines = 30): string {
  const all = stat.split('\n');
  if (all.length <= lines + 1) return stat;
  return [...all.slice(0, lines), ` ... and ${all.length - lines - 1} more files`, all.at(-1)].join('\n');
}
