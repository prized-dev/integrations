/**
 * String replacement for the edit tool, with the errors OpenCode's own edit tool gives (the model knows
 * them). An exact match comes first; then, as OpenCode does, matches that differ only in how lines are
 * indented or spaced, so an edit copied from read output with its whitespace slightly off still lands.
 */

type Replacer = (content: string, find: string) => Generator<string>;

const exact: Replacer = function* (_content, find) {
  yield find;
};

/** Every line equal once trimmed: yields the span of the file as it is written. */
const lineTrimmed: Replacer = function* (content, find) {
  const lines = content.split('\n');
  const want = find.split('\n');
  if (want.at(-1) === '') want.pop();
  if (want.length === 0) return;
  for (let i = 0; i + want.length <= lines.length; i += 1) {
    if (want.every((w, j) => (lines[i + j] as string).trim() === w.trim())) {
      yield lines.slice(i, i + want.length).join('\n');
    }
  }
};

const squash = (s: string) => s.replace(/\s+/g, ' ').trim();

/** Runs of whitespace count as one space. */
const whitespaceNormalized: Replacer = function* (content, find) {
  const want = squash(find);
  if (!want) return;
  const lines = content.split('\n');
  const n = find.split('\n').length;
  for (let i = 0; i + n <= lines.length; i += 1) {
    const block = lines.slice(i, i + n).join('\n');
    if (squash(block) === want) yield block;
  }
};

/** The same lines with a different common indent. */
const indentationFlexible: Replacer = function* (content, find) {
  const strip = (block: string) => {
    const ls = block.split('\n');
    const indents = ls.filter((l) => l.trim()).map((l) => (/^\s*/.exec(l) as RegExpExecArray)[0].length);
    const min = indents.length ? Math.min(...indents) : 0;
    return ls.map((l) => l.slice(Math.min(min, (/^\s*/.exec(l) as RegExpExecArray)[0].length))).join('\n');
  };
  const want = strip(find);
  const lines = content.split('\n');
  const n = find.split('\n').length;
  for (let i = 0; i + n <= lines.length; i += 1) {
    const block = lines.slice(i, i + n).join('\n');
    if (strip(block) === want) yield block;
  }
};

const trimmedBoundary: Replacer = function* (_content, find) {
  const t = find.trim();
  if (t && t !== find) yield t;
};

const REPLACERS = [exact, lineTrimmed, whitespaceNormalized, indentationFlexible, trimmedBoundary];

/** Replace `oldString` with `newString` in `content`; throws with OpenCode's messages when it cannot. */
export function replace(content: string, oldString: string, newString: string, replaceAll = false): string {
  if (oldString === newString) throw new Error('No changes to apply: oldString and newString are identical.');
  if (oldString === '') {
    throw new Error(
      'oldString cannot be empty when editing an existing file. Provide the exact text to replace, or use write for an intentional full-file replacement.',
    );
  }
  // A file with Windows line endings: match and write in its own style.
  const crlf = content.includes('\r\n');
  const text = crlf ? content.replaceAll('\r\n', '\n') : content;
  const find = oldString.replaceAll('\r\n', '\n');
  const put = newString.replaceAll('\r\n', '\n');
  const done = (s: string) => (crlf ? s.replaceAll('\n', '\r\n') : s);
  let found = false;
  for (const replacer of REPLACERS) {
    for (const search of replacer(text, find)) {
      const index = text.indexOf(search);
      if (index === -1) continue;
      found = true;
      if (replaceAll) return done(text.replaceAll(search, put));
      if (index !== text.lastIndexOf(search)) continue;
      return done(text.slice(0, index) + put + text.slice(index + search.length));
    }
  }
  if (!found) {
    throw new Error('Could not find oldString in the file. It must match exactly, including whitespace, indentation, and line endings.');
  }
  throw new Error('Found multiple matches for oldString. Provide more surrounding context to make the match unique.');
}
