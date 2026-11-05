/**
 * The apply_patch format OpenCode gives GPT models in place of edit and write:
 *
 *   *** Begin Patch
 *   *** Add File: path        (then + lines)
 *   *** Delete File: path
 *   *** Update File: path     (optional "*** Move to: path", then @@ hunks of ' ', '-', '+' lines)
 *   *** End Patch
 *
 * Parsing and applying happen here, on text: the tool reads and writes the files in the sandbox.
 */

export type Hunk =
  | { type: 'add'; path: string; contents: string }
  | { type: 'delete'; path: string }
  | { type: 'update'; path: string; movePath?: string; chunks: Chunk[] };

export interface Chunk {
  /** The text after `@@`, a line to find before the change (may be empty). */
  context: string;
  old: string[];
  new: string[];
  /** `*** End of File`: the change is at the end of the file. */
  eof: boolean;
}

export function parsePatch(text: string): Hunk[] {
  const lines = text.replaceAll('\r\n', '\n').trim().split('\n');
  const begin = lines.findIndex((l) => l.trim() === '*** Begin Patch');
  const end = lines.findLastIndex((l) => l.trim() === '*** End Patch');
  if (begin < 0 || end <= begin) throw new Error('apply_patch verification failed: the patch must start with *** Begin Patch and end with *** End Patch');
  const hunks: Hunk[] = [];
  let i = begin + 1;
  const header = (l: string, h: string) => (l.startsWith(h) ? l.slice(h.length).trim() : undefined);
  while (i < end) {
    const line = lines[i] as string;
    let p: string | undefined;
    if ((p = header(line, '*** Add File:')) !== undefined) {
      i += 1;
      const body: string[] = [];
      while (i < end && !(lines[i] as string).startsWith('*** ')) {
        const l = lines[i] as string;
        if (!l.startsWith('+')) throw new Error(`apply_patch verification failed: in ${p}, every line of an added file starts with +`);
        body.push(l.slice(1));
        i += 1;
      }
      hunks.push({ type: 'add', path: p, contents: body.length ? `${body.join('\n')}\n` : '' });
    } else if ((p = header(line, '*** Delete File:')) !== undefined) {
      hunks.push({ type: 'delete', path: p });
      i += 1;
    } else if ((p = header(line, '*** Update File:')) !== undefined) {
      i += 1;
      let movePath: string | undefined;
      const move = header(lines[i] ?? '', '*** Move to:');
      if (move !== undefined) {
        movePath = move;
        i += 1;
      }
      const chunks: Chunk[] = [];
      let cur: Chunk | undefined;
      while (i < end && !/^\*\*\* (Add|Delete|Update) File:/.test(lines[i] as string)) {
        const l = lines[i] as string;
        if (l.startsWith('@@')) {
          cur = { context: l.slice(2).trim(), old: [], new: [], eof: false };
          chunks.push(cur);
        } else if (l.trim() === '*** End of File') {
          if (cur) cur.eof = true;
        } else {
          if (!cur) {
            cur = { context: '', old: [], new: [], eof: false };
            chunks.push(cur);
          }
          const tag = l[0];
          const rest = l.slice(1);
          if (tag === '+') cur.new.push(rest);
          else if (tag === '-') cur.old.push(rest);
          else if (tag === ' ' || l === '') {
            cur.old.push(rest);
            cur.new.push(rest);
          } else throw new Error(`apply_patch verification failed: in ${p}, a hunk line must start with ' ', '-' or '+': ${l}`);
        }
        i += 1;
      }
      if (chunks.length === 0) throw new Error(`apply_patch verification failed: no changes for ${p}`);
      hunks.push({ type: 'update', path: p, movePath, chunks });
    } else if (line.trim() === '') {
      i += 1;
    } else {
      throw new Error(`apply_patch verification failed: expected a file header (*** Add File, *** Delete File, *** Update File), got: ${line}`);
    }
  }
  if (hunks.length === 0) throw new Error('apply_patch verification failed: the patch changes no files');
  return hunks;
}

/** Where `want` starts in `lines` at or after `from`: exactly, then ignoring trailing, then all surrounding whitespace. */
function seek(lines: string[], want: string[], from: number, eof: boolean): number {
  if (want.length === 0) return eof ? lines.length : from;
  const tries: Array<(s: string) => string> = [(s) => s, (s) => s.trimEnd(), (s) => s.trim()];
  for (const f of tries) {
    const starts = eof ? [lines.length - want.length] : [];
    for (let i = from; i + want.length <= lines.length; i += 1) starts.push(i);
    for (const i of starts) {
      if (i < from || i < 0) continue;
      if (want.every((w, j) => f(lines[i + j] as string) === f(w))) return i;
    }
  }
  return -1;
}

/** The new text of a file after an update hunk's chunks. */
export function applyChunks(path: string, content: string, chunks: Chunk[]): string {
  const crlf = content.includes('\r\n');
  const lines = (crlf ? content.replaceAll('\r\n', '\n') : content).split('\n');
  const trailing = lines.at(-1) === '';
  if (trailing) lines.pop();
  let at = 0;
  for (const c of chunks) {
    if (c.context) {
      const ctx = seek(lines, [c.context], at, false);
      if (ctx < 0) throw new Error(`apply_patch verification failed: failed to find context '${c.context}' in ${path}`);
      at = ctx + 1;
    }
    let old = c.old;
    let neu = c.new;
    let idx = seek(lines, old, at, c.eof);
    // A trailing empty line in the hunk is often the file's final newline.
    if (idx < 0 && old.at(-1) === '') {
      old = old.slice(0, -1);
      if (neu.at(-1) === '') neu = neu.slice(0, -1);
      idx = seek(lines, old, at, c.eof);
    }
    if (idx < 0) throw new Error(`apply_patch verification failed: failed to find expected lines in ${path}:\n${c.old.join('\n')}`);
    lines.splice(idx, old.length, ...neu);
    at = idx + neu.length;
  }
  // Keep the file's own ending: a final newline only if it had one (an empty file counts as having one).
  const out = `${lines.join('\n')}${trailing && lines.length ? '\n' : ''}`;
  return crlf ? out.replaceAll('\n', '\r\n') : out;
}
