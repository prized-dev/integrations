/**
 * Glob matching for the glob tool, the way OpenCode's own glob tool (ripgrep's --glob) reads a pattern:
 * a pattern without a slash matches a file name at any depth (`*.ts`), one with a slash matches the path
 * from the search folder (`src/**\/*.ts`). `*` stays within a folder, `**` crosses folders, `?` is one
 * character, `{a,b}` is either, `[abc]` a class.
 */

function escape(c: string): string {
  return /[\\^$.*+?()[\]{}|/]/.test(c) ? `\\${c}` : c;
}

function body(glob: string): string {
  let out = '';
  let depth = 0;
  for (let i = 0; i < glob.length; i += 1) {
    const c = glob[i] as string;
    if (c === '*') {
      if (glob[i + 1] === '*') {
        // `**/` is any number of folders (none included); `**` at the end is anything.
        if (glob[i + 2] === '/') {
          out += '(?:.*/)?';
          i += 2;
        } else {
          out += '.*';
          i += 1;
        }
      } else {
        out += '[^/]*';
      }
    } else if (c === '?') out += '[^/]';
    else if (c === '[') {
      const end = glob.indexOf(']', i + 2);
      if (end < 0) out += '\\[';
      else {
        let cls = glob.slice(i + 1, end).replaceAll('\\', '\\\\');
        if (cls.startsWith('!')) cls = `^${cls.slice(1)}`;
        out += `[${cls}]`;
        i = end;
      }
    } else if (c === '{') {
      depth += 1;
      out += '(?:';
    } else if (c === '}' && depth > 0) {
      depth -= 1;
      out += ')';
    } else if (c === ',' && depth > 0) out += '|';
    else if (c === '\\' && i + 1 < glob.length) {
      i += 1;
      out += escape(glob[i] as string);
    } else out += escape(c);
  }
  return out + ')'.repeat(depth);
}

/** A test for paths relative to the search folder, `/`-separated. */
export function globMatcher(pattern: string): (relPath: string) => boolean {
  const p = pattern.replace(/^\.\//, '');
  const anchored = p.includes('/');
  const re = new RegExp(`^${body(p.replace(/^\//, ''))}$`);
  return anchored ? (rel) => re.test(rel) : (rel) => re.test(rel.slice(rel.lastIndexOf('/') + 1)) || re.test(rel);
}
