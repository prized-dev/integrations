/**
 * OpenCode's file and shell tools, run in the session's Prized sandbox: bash, read, write, edit, glob,
 * grep and apply_patch. Same names, parameters and permission prompts as OpenCode's own, so the model,
 * your permission rules and the TUI's rendering work as before; only where they run changes. Nothing
 * here reads or writes a file on your machine, and your environment variables are not forwarded.
 */

import path from 'node:path';

import { CommandTimeoutError, type Sandbox } from '@prized/sdk';
import { createTwoFilesPatch } from 'diff';

import { replace } from './edit.ts';
import { globMatcher } from './glob.ts';
import { applyChunks, parsePatch } from './patch.ts';
import { SANDBOX_HOME, relative, toHost, toSandbox } from './paths.ts';
import type { Sandboxes, Target } from './sandboxes.ts';
import { STAGE, shq } from './workspace.ts';

/** What a tool call gets from OpenCode, whichever plugin API delivered it. */
export interface Call {
  sessionID: string;
  abort: AbortSignal;
  /** Live title and metadata for the TUI while the tool runs. */
  progress(update: { title?: string; metadata?: { [k: string]: unknown } }): void;
  /** OpenCode's permission prompt; rejects when the user (or a rule) says no. */
  ask(req: { permission: string; patterns: string[]; always: string[]; metadata: { [k: string]: unknown } }): Promise<void>;
}

export interface Result {
  title: string;
  output: string;
  metadata: { [k: string]: unknown };
  attachments?: Array<{ type: 'file'; mime: string; url: string }>;
}

export const DEFAULT_TIMEOUT_MS = 2 * 60 * 1000;
const READ_LIMIT = 2000;
const MAX_LINE = 2000;
const MAX_READ_BYTES = 50 * 1024;
/** Files above this go through sed rather than a whole download. */
const WHOLE_FILE_BYTES = 4 * 1024 * 1024;
const MAX_ATTACH_BYTES = 10 * 1024 * 1024;
const LIST_LIMIT = 100;
/** A result cap the model asked for (OpenCode 2's glob and grep take one), within 1 to 1000. */
const capOf = (limit?: number) => (limit && Number.isFinite(limit) && limit > 0 ? Math.min(Math.floor(limit), 1000) : LIST_LIMIT);
const MAX_OUTPUT_BYTES = 1024 * 1024;
const ATTACH_TYPES = new Map([
  ['.png', 'image/png'],
  ['.jpg', 'image/jpeg'],
  ['.jpeg', 'image/jpeg'],
  ['.gif', 'image/gif'],
  ['.webp', 'image/webp'],
  ['.pdf', 'application/pdf'],
]);

export const DESCRIPTIONS = {
  bash:
    'Runs a bash command in the Prized sandbox (a Linux microVM, user prized, no sudo) that holds a copy of the project, not on the user\'s machine. ' +
    'The working directory is the project folder; use `workdir` instead of `cd`. Background work (`cmd &`, a dev server) keeps running between calls, and the sandbox pauses between turns with its memory, so it is still running next time. ' +
    `\`timeout\` is in milliseconds (default ${DEFAULT_TIMEOUT_MS}). The user's environment variables are not there; install what you need (python3 -m venv for pip).`,
  read:
    'Reads a file or lists a folder in the Prized sandbox. `filePath` is absolute (a path in the project on the user\'s machine, or in the sandbox). ' +
    `Returns up to ${READ_LIMIT} lines from \`offset\` (1-indexed), each as "N: text"; images and PDFs come back as attachments.`,
  write: 'Writes a whole file in the Prized sandbox, creating folders as needed. `filePath` is absolute. Prefer edit for changes to an existing file, and read a file before you overwrite it.',
  edit:
    'Replaces `oldString` with `newString` in a file in the Prized sandbox. `oldString` must match the file (copy it from read output without the "N: " prefix) and be unique, unless `replaceAll` is set. ' +
    'With an empty `oldString` and a file that does not exist, creates it.',
  glob: 'Finds files in the Prized sandbox whose path matches a glob (`**/*.ts`, `src/**/*.{js,jsx}`), newest first, skipping what .gitignore ignores. Up to 100 results.',
  grep: 'Searches file contents in the Prized sandbox with a regular expression (Perl syntax), skipping what .gitignore ignores. `include` filters files by glob (`*.ts`). Up to 100 matches, with file and line.',
  apply_patch:
    'Edits files in the Prized sandbox with a patch:\n*** Begin Patch\n*** Add File: path\n+line\n*** Update File: path\n*** Move to: new/path (optional)\n@@ a line just before the change\n context\n-removed\n+added\n*** Delete File: path\n*** End Patch\nEvery line of an added file starts with +; paths are relative to the project or absolute.',
} as const;

/** The files API reaches /home/prized only; anything else goes through a copy there. */
const inHome = (p: string) => p === SANDBOX_HOME || p.startsWith(`${SANDBOX_HOME}/`);
const scratch = () => `${STAGE}/io-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

async function run(sb: Sandbox, script: string): Promise<void> {
  const r = await sb.exec(script, { timeoutMs: 60_000 });
  if (r.exitCode !== 0) throw new Error((r.stderr || r.stdout).trim() || `exit ${r.exitCode}`);
}

export const io = {
  async exists(sb: Sandbox, p: string): Promise<boolean> {
    if (inHome(p)) return sb.files.exists(p);
    return (await sb.exec(`test -e ${shq(p)}`, { timeoutMs: 30_000 })).exitCode === 0;
  },
  async read(sb: Sandbox, p: string): Promise<Buffer> {
    if (inHome(p)) return Buffer.from(await sb.files.readBytes(p));
    const tmp = scratch();
    await run(sb, `mkdir -p ${shq(STAGE)} && cp -- ${shq(p)} ${shq(tmp)}`);
    try {
      return Buffer.from(await sb.files.readBytes(tmp));
    } finally {
      await sb.exec(`rm -f ${shq(tmp)}`, { timeoutMs: 30_000 }).catch(() => undefined);
    }
  },
  async write(sb: Sandbox, p: string, data: string | Uint8Array): Promise<void> {
    if (inHome(p)) {
      await sb.files.write(p, data);
      return;
    }
    const tmp = scratch();
    await sb.files.write(tmp, data);
    await run(sb, `mkdir -p -- ${shq(path.posix.dirname(p))} && mv -f -- ${shq(tmp)} ${shq(p)}`).catch(async (err) => {
      await sb.exec(`rm -f ${shq(tmp)}`, { timeoutMs: 30_000 }).catch(() => undefined);
      throw err;
    });
  },
  async remove(sb: Sandbox, p: string): Promise<void> {
    if (inHome(p)) await sb.files.remove(p);
    else await run(sb, `rm -f -- ${shq(p)}`);
  },
};

/** Text a tool may rewrite: UTF-8 only, so an edit never mangles bytes it does not understand. */
function text(bytes: Buffer, shown: string): string {
  const s = bytes.toString('utf8');
  if (!Buffer.from(s, 'utf8').equals(bytes)) throw new Error(`${shown} is not UTF-8 text, so it was left alone; change it with bash.`);
  return s;
}

const tail = (s: string, max: number) => (Buffer.byteLength(s) > max ? `...output truncated...\n\n${s.slice(-max)}` : s);

export class Tools {
  constructor(private readonly sandboxes: Sandboxes) {}

  private async target(call: Call): Promise<Target> {
    return this.sandboxes.target(call.sessionID);
  }

  /** Metadata for OpenCode: JSON values only (OpenCode 2 refuses a result whose metadata holds `undefined`). */
  private meta(t: Target, extra: { [k: string]: unknown } = {}): { [k: string]: unknown } {
    const json = JSON.parse(JSON.stringify(extra)) as { [k: string]: unknown };
    return { ...json, prized: { sandboxId: t.sandboxId, owner: t.owner } };
  }

  async bash(args: { command: string; timeout?: number; workdir?: string; description?: string; background?: boolean }, call: Call): Promise<Result> {
    if (typeof args.command !== 'string' || !args.command.trim()) throw new Error('command is required');
    if (args.timeout !== undefined && !(Number.isFinite(args.timeout) && args.timeout >= 0)) {
      throw new Error(`Invalid timeout value: ${args.timeout}. Timeout must be a positive number.`);
    }
    // One pattern per simple command, as OpenCode's own bash tool asks, so a rule such as "git push*": "deny"
    // also catches `make && git push`. (A rough split; it only shapes the prompt, the command runs in the sandbox.)
    const parts = args.command.split(/&&|\|\||;|\||\n/).map((x) => x.trim()).filter(Boolean);
    const patterns = parts.length ? parts : [args.command];
    const always = [...new Set(patterns.map((x) => `${x.split(/\s+/)[0]} *`))];
    await call.ask({ permission: 'bash', patterns, always, metadata: { command: args.command } });
    const t = await this.target(call);
    const cwd = args.workdir ? toSandbox(args.workdir, t.layout) : t.layout.cwd;
    if (args.background) {
      // OpenCode 2's shell can start a command in the background: here it runs detached, its output in a file.
      const log = `${scratch()}.log`;
      const r = await t.use((sb) =>
        sb.exec(`mkdir -p ${shq(STAGE)} && cd ${shq(cwd)} && { setsid nohup bash -c ${shq(args.command)} > ${shq(log)} 2>&1 < /dev/null & echo $!; }`, { timeoutMs: 30_000 }),
      );
      if (r.exitCode !== 0) throw new Error((r.stderr || r.stdout).trim() || `could not start the command (exit ${r.exitCode})`);
      const pid = r.stdout.trim();
      return {
        title: args.description ?? args.command,
        output: `Started in the background in the sandbox (pid ${pid}). Its output goes to ${log}; read it with bash when you need it, and stop it with kill ${pid}.`,
        metadata: this.meta(t, { output: '', exit: null, background: true, pid, log }),
      };
    }
    const timeout = args.timeout ?? DEFAULT_TIMEOUT_MS;
    let out = '';
    let last = 0;
    const onChunk = (chunk: string) => {
      out += chunk;
      if (out.length > MAX_OUTPUT_BYTES * 2) out = out.slice(-MAX_OUTPUT_BYTES);
      const now = Date.now();
      if (now - last > 250) {
        last = now;
        call.progress({ metadata: { output: out.slice(-30_000) } });
      }
    };
    call.progress({ title: args.description ?? args.command, metadata: { output: '' } });
    let code: number | null = null;
    const notes: string[] = [];
    try {
      const r = await t.use((sb) =>
        sb.exec(args.command, { cwd, timeoutMs: timeout, signal: call.abort, onStdout: onChunk, onStderr: onChunk }),
      );
      code = r.exitCode;
    } catch (err) {
      if (err instanceof CommandTimeoutError) {
        notes.push(
          `shell tool terminated command after exceeding timeout ${timeout} ms. If this command is expected to take longer and is not waiting for interactive input, retry with a larger timeout value in milliseconds.`,
        );
      } else if (call.abort.aborted) {
        notes.push('User aborted the command');
      } else {
        throw err;
      }
    }
    if (code !== null && code !== 0) notes.push(`exit code ${code}`);
    let output = tail(out, MAX_OUTPUT_BYTES) || '(no output)';
    if (notes.length) output += `\n\n<shell_metadata>\n${notes.join('\n')}\n</shell_metadata>`;
    return { title: args.description ?? args.command, output, metadata: this.meta(t, { output: out.slice(-30_000), exit: code, description: args.description }) };
  }

  async read(args: { filePath: string; offset?: number; limit?: number }, call: Call): Promise<Result> {
    if (typeof args.filePath !== 'string' || !args.filePath) throw new Error('filePath is required');
    const t = await this.target(call);
    const p = toSandbox(args.filePath, t.layout);
    const shown = toHost(p, t.layout);
    await call.ask({ permission: 'read', patterns: [relative(p, t.layout)], always: ['*'], metadata: {} });
    const offset = args.offset || 1;
    const limit = args.limit || READ_LIMIT;
    const title = relative(p, t.layout);
    return t.use(async (sb) => {
      const kind = (await sb.exec(`if [ -d ${shq(p)} ]; then echo dir; elif [ -f ${shq(p)} ]; then stat -c 'file %s' -- ${shq(p)}; else echo none; fi`, { timeoutMs: 30_000 })).stdout.trim();
      if (kind === 'none' || kind === '') throw new Error(`File not found: ${shown}`);
      if (kind === 'dir') {
        const r = await sb.exec(`cd ${shq(p)} && ls -1Ap --group-directories-first`, { timeoutMs: 30_000 });
        const items = r.stdout.split('\n').filter(Boolean);
        const sliced = items.slice(offset - 1, offset - 1 + limit);
        const more = offset - 1 + sliced.length < items.length;
        const output = [
          `<path>${shown}</path>`,
          '<type>directory</type>',
          '<entries>',
          sliced.join('\n'),
          more ? `\n(Showing ${sliced.length} of ${items.length} entries. Use 'offset' parameter to read beyond entry ${offset + sliced.length})` : `\n(${items.length} entries)`,
          '</entries>',
        ].join('\n');
        return { title, output, metadata: this.meta(t, { preview: sliced.slice(0, 20).join('\n'), truncated: more }) };
      }
      const size = Number(kind.split(' ')[1]);
      const mime = ATTACH_TYPES.get(path.posix.extname(p).toLowerCase());
      if (mime) {
        if (size > MAX_ATTACH_BYTES) throw new Error(`${shown} is ${Math.round(size / 1024 / 1024)} MB, too big to attach`);
        const bytes = await io.read(sb, p);
        const msg = mime === 'application/pdf' ? 'PDF read successfully' : 'Image read successfully';
        return { title, output: msg, metadata: this.meta(t, { preview: msg, truncated: false }), attachments: [{ type: 'file', mime, url: `data:${mime};base64,${bytes.toString('base64')}` }] };
      }
      let text: string;
      let total: number;
      let startLine = offset;
      if (size <= WHOLE_FILE_BYTES) {
        const bytes = await io.read(sb, p);
        if (bytes.subarray(0, 4096).includes(0)) throw new Error(`Cannot read binary file: ${shown}`);
        text = bytes.toString('utf8');
        const all = text.split('\n');
        if (all.at(-1) === '') all.pop();
        total = all.length;
        text = all.slice(offset - 1, offset - 1 + limit).join('\n');
      } else {
        // A big file: only the lines asked for come over.
        // awk counts a last line without a newline, as the split above does.
        const r = await sb.exec(`awk 'END{print NR}' ${shq(p)}; sed -n '${offset},${offset + limit - 1}p' ${shq(p)}`, { timeoutMs: 60_000, fullOutput: true });
        const nl = r.stdout.indexOf('\n');
        total = Number(r.stdout.slice(0, nl));
        text = r.stdout.slice(nl + 1).replace(/\n$/, '');
        startLine = offset;
      }
      if (total < offset && !(total === 0 && offset === 1)) throw new Error(`Offset ${offset} is out of range for this file (${total} lines)`);
      const raw = text === '' && total === 0 ? [] : text.split('\n');
      const lines: string[] = [];
      let bytes = 0;
      let cut = false;
      for (const line of raw) {
        const l = line.length > MAX_LINE ? `${line.slice(0, MAX_LINE)}... (line truncated to ${MAX_LINE} chars)` : line;
        bytes += Buffer.byteLength(l) + 1;
        if (bytes > MAX_READ_BYTES) {
          cut = true;
          break;
        }
        lines.push(l);
      }
      const lastLine = startLine + lines.length - 1;
      let output = `<path>${shown}</path>\n<type>file</type>\n<content>\n${lines.map((l, i) => `${i + startLine}: ${l}`).join('\n')}`;
      if (cut) output += `\n\n(Output capped at ${MAX_READ_BYTES / 1024} KB. Showing lines ${startLine}-${lastLine}. Use offset=${lastLine + 1} to continue.)`;
      else if (lastLine < total) output += `\n\n(Showing lines ${startLine}-${lastLine} of ${total}. Use offset=${lastLine + 1} to continue.)`;
      else output += `\n\n(End of file - total ${total} lines)`;
      output += '\n</content>';
      return { title, output, metadata: this.meta(t, { preview: lines.slice(0, 20).join('\n'), truncated: cut || lastLine < total }) };
    });
  }

  private async readText(sb: Sandbox, p: string, shown: string): Promise<string | undefined> {
    if (!(await io.exists(sb, p))) return undefined;
    return text(await io.read(sb, p), shown);
  }

  async write(args: { content: string; filePath: string }, call: Call): Promise<Result> {
    if (typeof args.filePath !== 'string' || !args.filePath) throw new Error('filePath is required');
    if (typeof args.content !== 'string') throw new Error('content is required');
    const t = await this.target(call);
    const p = toSandbox(args.filePath, t.layout);
    const shown = toHost(p, t.layout);
    return t.use(async (sb) => {
      const old = await this.readText(sb, p, shown);
      const diff = createTwoFilesPatch(shown, shown, old ?? '', args.content);
      await call.ask({ permission: 'edit', patterns: [relative(p, t.layout)], always: ['*'], metadata: { filepath: shown, diff } });
      await io.write(sb, p, args.content);
      return { title: relative(p, t.layout), output: 'Wrote file successfully.', metadata: this.meta(t, { filepath: shown, exists: old !== undefined, diff }) };
    });
  }

  async edit(args: { filePath: string; oldString: string; newString: string; replaceAll?: boolean }, call: Call): Promise<Result> {
    if (typeof args.filePath !== 'string' || !args.filePath) throw new Error('filePath is required');
    if (args.oldString === args.newString) throw new Error('No changes to apply: oldString and newString are identical.');
    const t = await this.target(call);
    const p = toSandbox(args.filePath, t.layout);
    const shown = toHost(p, t.layout);
    return t.use(async (sb) => {
      const old = await this.readText(sb, p, shown);
      let next: string;
      if (old === undefined) {
        if (args.oldString !== '') throw new Error(`File ${shown} not found`);
        next = args.newString;
      } else {
        next = replace(old, args.oldString, args.newString, args.replaceAll === true);
      }
      const diff = createTwoFilesPatch(shown, shown, old ?? '', next);
      await call.ask({ permission: 'edit', patterns: [relative(p, t.layout)], always: ['*'], metadata: { filepath: shown, diff } });
      await io.write(sb, p, next);
      return { title: relative(p, t.layout), output: 'Edit applied successfully.', metadata: this.meta(t, { diff, filediff: { file: shown } }) };
    });
  }

  async glob(args: { pattern: string; path?: string; limit?: number }, call: Call): Promise<Result> {
    if (typeof args.pattern !== 'string' || !args.pattern) throw new Error('pattern is required');
    await call.ask({ permission: 'glob', patterns: [args.pattern], always: ['*'], metadata: { pattern: args.pattern, path: args.path } });
    const t = await this.target(call);
    const base = args.path ? toSandbox(args.path, t.layout) : t.layout.cwd;
    const match = globMatcher(args.pattern);
    return t.use(async (sb) => {
      const r = await sb.exec(listScript(base), { timeoutMs: 60_000, fullOutput: true });
      if (r.exitCode !== 0) throw new Error(r.stderr.trim() || `No such folder: ${toHost(base, t.layout)}`);
      const hits: Array<{ mtime: number; file: string }> = [];
      for (const rec of r.stdout.split('\0')) {
        const tab = rec.indexOf('\t');
        if (tab < 0) continue;
        const rel = rec.slice(tab + 1).replace(/^\.\//, '');
        if (match(rel)) hits.push({ mtime: Number(rec.slice(0, tab)), file: rel });
      }
      hits.sort((a, b) => b.mtime - a.mtime);
      const cap = capOf(args.limit);
      const shown = hits.slice(0, cap).map((h) => toHost(path.posix.join(base, h.file), t.layout));
      const output = shown.length
        ? `${shown.join('\n')}${hits.length > cap ? `\n\n(Results are truncated: showing first ${cap} results. Consider using a more specific path or pattern.)` : ''}`
        : 'No files found';
      return { title: relative(base, t.layout), output, metadata: this.meta(t, { count: hits.length, truncated: hits.length > cap }) };
    });
  }

  async grep(args: { pattern: string; path?: string; include?: string; literal?: boolean; caseSensitive?: boolean; limit?: number }, call: Call): Promise<Result> {
    if (typeof args.pattern !== 'string' || !args.pattern) throw new Error('pattern is required');
    await call.ask({ permission: 'grep', patterns: [args.pattern], always: ['*'], metadata: { pattern: args.pattern, path: args.path, include: args.include } });
    const t = await this.target(call);
    const base = args.path ? toSandbox(args.path, t.layout) : t.layout.cwd;
    return t.use(async (sb) => {
      const r = await sb.exec(grepScript(base, args.pattern, args.include, { literal: args.literal, ignoreCase: args.caseSensitive === false }), { timeoutMs: 120_000, fullOutput: true });
      const cap = capOf(args.limit);
      if (r.exitCode === 2 || (r.exitCode !== 0 && r.exitCode !== 1)) throw new Error(r.stderr.trim() || `grep failed (${r.exitCode})`);
      const byFile = new Map<string, string[]>();
      let count = 0;
      for (const line of r.stdout.split('\n')) {
        const m = /^(.*?)\0(\d+)\0(.*)$/.exec(line);
        if (!m) continue;
        count += 1;
        if (count > cap) continue;
        const file = toHost(path.posix.join(base, (m[1] as string).replace(/^\.\//, '')), t.layout);
        const text = (m[3] as string).length > MAX_LINE ? `${(m[3] as string).slice(0, MAX_LINE)}...` : (m[3] as string);
        const list = byFile.get(file) ?? [];
        list.push(`  Line ${m[2]}: ${text}`);
        byFile.set(file, list);
      }
      if (count === 0) return { title: args.pattern, output: 'No files found', metadata: this.meta(t, { matches: 0, truncated: false }) };
      const out = [`Found ${count} matches${count > cap ? ` (showing first ${cap})` : ''}`];
      for (const [file, lines] of byFile) out.push(`${file}:`, ...lines, '');
      if (count > cap) out.push(`(Results truncated: showing ${cap} of ${count} matches. Consider using a more specific path or pattern.)`);
      return { title: args.pattern, output: out.join('\n').trimEnd(), metadata: this.meta(t, { matches: count, truncated: count > cap }) };
    });
  }

  async applyPatch(args: { patchText: string }, call: Call): Promise<Result> {
    if (typeof args.patchText !== 'string' || !args.patchText) throw new Error('patchText is required');
    const hunks = parsePatch(args.patchText);
    const t = await this.target(call);
    return t.use(async (sb) => {
      const plan: Array<{ type: 'add' | 'update' | 'delete' | 'move'; from: string; to: string; before: string; after: string }> = [];
      for (const h of hunks) {
        const from = toSandbox(h.path, t.layout);
        if (h.type === 'add') {
          plan.push({ type: 'add', from, to: from, before: (await this.readText(sb, from, toHost(from, t.layout))) ?? '', after: h.contents });
        } else if (h.type === 'delete') {
          const before = await this.readText(sb, from, toHost(from, t.layout));
          if (before === undefined) throw new Error(`apply_patch verification failed: ${toHost(from, t.layout)} does not exist`);
          plan.push({ type: 'delete', from, to: from, before, after: '' });
        } else {
          const before = await this.readText(sb, from, toHost(from, t.layout));
          if (before === undefined) throw new Error(`apply_patch verification failed: ${toHost(from, t.layout)} does not exist`);
          const to = h.movePath ? toSandbox(h.movePath, t.layout) : from;
          plan.push({ type: h.movePath ? 'move' : 'update', from, to, before, after: applyChunks(toHost(from, t.layout), before, h.chunks) });
        }
      }
      const files = plan.map((f) => {
        const diff = createTwoFilesPatch(toHost(f.from, t.layout), toHost(f.to, t.layout), f.before, f.after);
        const added = diff.split('\n').filter((l) => l.startsWith('+') && !l.startsWith('+++')).length;
        const removed = diff.split('\n').filter((l) => l.startsWith('-') && !l.startsWith('---')).length;
        return { filePath: toHost(f.from, t.layout), relativePath: relative(f.to, t.layout), type: f.type, diff, additions: added, deletions: removed, movePath: f.type === 'move' ? toHost(f.to, t.layout) : undefined };
      });
      const rel = plan.map((f) => relative(f.to, t.layout));
      const diff = files.map((f) => f.diff).join('\n');
      await call.ask({ permission: 'edit', patterns: rel, always: ['*'], metadata: { filepath: rel.join(', '), diff, files } });
      for (const f of plan) {
        if (f.type === 'delete') await io.remove(sb, f.from);
        else {
          await io.write(sb, f.to, f.after);
          if (f.type === 'move') await io.remove(sb, f.from);
        }
      }
      const letter = { add: 'A', update: 'M', move: 'M', delete: 'D' } as const;
      const output = `Success. Updated the following files:\n${plan.map((f) => `${letter[f.type]} ${relative(f.to, t.layout)}`).join('\n')}`;
      return { title: rel.join(', '), output, metadata: this.meta(t, { diff, files }) };
    });
  }
}

/** Files under `base` with their modification time, "mtime\tpath\0": git's view (ignored files left out) inside a repository, else find's. */
export function listScript(base: string): string {
  return [
    `cd ${shq(base)} || exit 1`,
    'if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then',
    "  git ls-files -z --cached --others --exclude-standard -- . | xargs -0 -r stat --printf '%Y\\t%n\\0' 2>/dev/null",
    'else',
    "  find . -type d \\( -name .git -o -name node_modules \\) -prune -o -type f -printf '%T@\\t%p\\0'",
    'fi',
    'exit 0',
  ].join('\n');
}

/** Brace expansion for one level of `{a,b}`, which git's pathspecs do not do. */
function braces(glob: string): string[] {
  const m = /\{([^{}]*)\}/.exec(glob);
  if (!m) return [glob];
  return (m[1] as string).split(',').flatMap((alt) => braces(glob.slice(0, m.index) + alt + glob.slice(m.index + m[0].length)));
}

/** Matches as "path\0line\0text": git grep inside a repository (untracked files too, ignored ones not), else grep -r. */
export function grepScript(base: string, pattern: string, include?: string, opts: { literal?: boolean; ignoreCase?: boolean } = {}): string {
  const specs = include ? braces(include).map((g) => shq(`:(glob)${g.includes('/') ? g : `**/${g}`}`)).join(' ') : '.';
  const incs = include ? braces(include).map((g) => `--include=${shq(g.split('/').at(-1) as string)}`).join(' ') : '';
  const pat = shq(pattern);
  // -F for a literal pattern, -P (Perl, like ripgrep) otherwise; -i when the model asked for case-insensitive.
  const mode = `${opts.literal ? '-F' : '-P'}${opts.ignoreCase ? ' -i' : ''}`;
  const fallback = `${opts.literal ? '-F' : '-E'}${opts.ignoreCase ? ' -i' : ''}`;
  return [
    `cd ${shq(base)} || exit 2`,
    'if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then',
    '  err=$(mktemp); trap \'rm -f "$err"\' EXIT',
    `  git -c core.quotePath=false grep -I -n -z --untracked --exclude-standard ${mode} -e ${pat} -- ${specs} 2>"$err"; s=$?`,
    // A git built without PCRE: extended regular expressions instead.
    `  if [ $s -gt 1 ] && grep -qi 'perl\\|pcre' "$err"; then git -c core.quotePath=false grep -I -n -z --untracked --exclude-standard ${fallback} -e ${pat} -- ${specs}; s=$?; else cat "$err" >&2; fi`,
    '  exit $s',
    'fi',
    `grep -r -I -n -Z ${mode} ${incs} --exclude-dir=.git --exclude-dir=node_modules -e ${pat} . | sed 's/^\\([^\\x00]*\\)\\x00\\([0-9]*\\):/\\1\\x00\\2\\x00/'`,
  ].join('\n');
}
