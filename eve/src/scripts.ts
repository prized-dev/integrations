/**
 * The shell scripts the connection runs in the sandbox. Each takes its paths from environment
 * variables (`EVE_SRC`, `EVE_DST`, ...) rather than inline, so no path is ever quoted into a
 * command, and each reports what happened through its exit code.
 */

/** Exit codes the scripts use; anything else is a failure with the reason on stderr. */
export const EXIT = {
  missing: 3,
  isDirectory: 4,
  notRegular: 5,
  notEmpty: 6,
} as const;

/** `[ -e ]` is false for a broken symlink, which still occupies the path. */
const present = (variable: string): string => `{ [ -e "$${variable}" ] || [ -L "$${variable}" ]; }`;

/** Create the directory `EVE_DST`. */
export const MKDIR = 'mkdir -p -- "$EVE_DST"';

/**
 * Copy the file `EVE_SRC` to `EVE_DST` (a staging file the files API can read), following links
 * as any process would. Exits `missing` when nothing is there (a broken link included, as Node's
 * ENOENT), `isDirectory` for a directory.
 */
export const STAGE_READ = [
  `[ -e "$EVE_SRC" ] || exit ${EXIT.missing}`,
  `[ -d "$EVE_SRC" ] && exit ${EXIT.isDirectory}`,
  `[ -f "$EVE_SRC" ] || exit ${EXIT.notRegular}`,
  'mkdir -p -- "$(dirname -- "$EVE_DST")" || exit 1',
  'cat -- "$EVE_SRC" > "$EVE_DST" || { rm -f -- "$EVE_DST"; exit 1; }',
].join('\n');

/**
 * Put the uploaded staging file `EVE_SRC` in place at `EVE_DST`, creating missing parents. `cat`
 * writes through a link and keeps an existing file's mode. The staging file is removed either way.
 */
export const STAGE_WRITE = [
  'trap \'rm -f -- "$EVE_SRC"\' EXIT',
  `[ -d "$EVE_DST" ] && exit ${EXIT.isDirectory}`,
  'mkdir -p -- "$(dirname -- "$EVE_DST")" || exit 1',
  'cat -- "$EVE_SRC" > "$EVE_DST" || exit 1',
].join('\n');

/**
 * Remove `EVE_DST`. `EVE_FORCE=1` ignores a missing path; `EVE_RECURSIVE=1` removes a non-empty
 * directory (without it, only an empty one goes). A link is removed, never what it points to.
 */
export const REMOVE = [
  `if ! ${present('EVE_DST')}; then [ "$EVE_FORCE" = 1 ] && exit 0; exit ${EXIT.missing}; fi`,
  'if [ -d "$EVE_DST" ] && [ ! -L "$EVE_DST" ]; then',
  '  if [ "$EVE_RECURSIVE" = 1 ]; then rm -rf -- "$EVE_DST" || exit 1; exit 0; fi',
  `  rmdir -- "$EVE_DST" 2>/dev/null || exit ${EXIT.notEmpty}`,
  '  exit 0',
  'fi',
  'rm -f -- "$EVE_DST" || exit 1',
].join('\n');

/** Whether `EVE_SRC` exists, following links (a broken link doesn't): exit 0 if so. */
export const EXISTS = '[ -e "$EVE_SRC" ]';
