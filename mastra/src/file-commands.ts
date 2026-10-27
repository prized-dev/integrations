/**
 * The shell scripts `PrizedSandboxFilesystem` runs in the sandbox for what Prized's files API
 * doesn't do itself. The API moves a file's bytes (upload, download) under /home/prized; listing,
 * stat, mkdir, delete, copy and move are one command each, and every write is uploaded to a
 * staging file and put in place by one command, so it keeps file modes on every image and works
 * outside /home/prized too.
 *
 * Each builder takes absolute paths and returns one POSIX `sh` script; the caller maps the exit
 * codes in {@link EXIT} to typed errors. They need GNU coreutils and findutils, as Prized's
 * templates have (the SDK's own helpers use `find -printf` and `stat -c` too).
 *
 * `copyCommand`, `moveNoClobberCommand` and the link step of `linkFromStagingCommand` are adapted
 * from Mastra's `packages/_internals/workspace/src/file-commands.ts` (mastra-ai/mastra at
 * ce3004df, Apache-2.0, Copyright (c) 2025 Kepler Software, Inc.), the scripts behind
 * `E2BSandboxFilesystem`, so the two providers answer the same way. Changed here: `--` before
 * every path, exit codes shared with the rest of this file, and a no-clobber write that starts
 * from a staging file. `replaceFromStagingCommand` follows langchain-prized's staged upload:
 * a replaced file keeps its permission bits, as the files API itself does on newer sandbox images.
 */

/** Exit codes the scripts use for outcomes the caller turns into typed errors. Any other non-zero code is a plain failure. */
export const EXIT = {
  notFound: 20,
  isDirectory: 21,
  exists: 22,
  notDirectory: 23,
  destIsDirectory: 24,
  notEmpty: 25,
  notRegular: 26,
  tooLarge: 27,
  parentNotFound: 28,
  badLink: 29,
} as const;

export type ExitOutcome = keyof typeof EXIT;

/** Quote one word for a POSIX shell. */
export function shellQuote(word: string): string {
  return `'${word.replace(/'/g, `'\\''`)}'`;
}

const { notFound, isDirectory, exists, notDirectory, destIsDirectory, notEmpty, notRegular, tooLarge, parentNotFound, badLink } =
  EXIT;

/** `[ -e ]` is false for a broken symlink, which still occupies the path. */
const present = (variable: string) => `{ [ -e "$${variable}" ] || [ -L "$${variable}" ]; }`;

/** Fail unless `$variable`'s parent is a directory: `notDirectory` when something else is there, else `parentNotFound`. */
const requireParent = (variable: string) =>
  `d=$(dirname -- "$${variable}"); if [ ! -d "$d" ]; then if [ -e "$d" ] || [ -L "$d" ]; then exit ${notDirectory}; fi; exit ${parentNotFound}; fi`;

/**
 * Create `$variable`'s parent (`mkdir -p`) and set `d` to it. When that fails because the nearest
 * existing ancestor is not a directory (a file is where a directory should be), exit
 * `notDirectory`; any other failure is passed on as is.
 */
const makeParent = (variable: string) =>
  `d=$(dirname -- "$${variable}"); err=$(mkdir -p -- "$d" 2>&1) || { a=$d; while [ ! -e "$a" ] && [ "$a" != / ]; do a=$(dirname -- "$a"); done; [ -d "$a" ] || exit ${notDirectory}; printf '%s\n' "$err" >&2; exit 1; }`;

/** Stop before touching anything if the staged upload isn't there (the sandbox was replaced after the upload). */
const requireStaging = `[ -f "$src" ] || { echo 'the staged upload is gone (was the sandbox replaced?)' >&2; exit 1; }`;

/** The mode a new file gets from a command or the files API: 0666 less the umask. */
const NEW_FILE_MODE = `"$(printf %o $((0666 & ~0$(umask))))"`;

/**
 * One record per entry, NUL-separated so any file name survives: `<own type><type through links> <size>`,
 * then the path relative to the start, then the link target (empty when not a link). `%Y` is `N`
 * for a broken link and `L` for a loop.
 */
export const ENTRY_FORMAT = "'%y%Y %s\\0%P\\0%l\\0'";

/** A sibling temp name for `path`, hidden and unique per call, so an `ln` onto `path` stays on one filesystem. */
export function siblingTempPath(path: string): string {
  const cut = path.lastIndexOf('/');
  const suffix = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  return `${path.slice(0, cut + 1)}.${path.slice(cut + 1)}.prized-tmp-${suffix}`;
}

/** `<type> <size> <mtime>` of `path`, following symlinks like `fs.stat`. Exits `notFound`, also for a broken link. */
export function statCommand(path: string): string {
  return [`p=${shellQuote(path)}`, `[ -e "$p" ] || exit ${notFound}`, `find -H "$p" -maxdepth 0 -printf '%y %s %T@\\n'`].join('\n');
}

/** Exit 0 when `path` exists (through symlinks: a broken link doesn't count), 1 when not. */
export function existsCommand(path: string): string {
  return `p=${shellQuote(path)}\n[ -e "$p" ]`;
}

/**
 * The entries under the directory `path` (a link to one is followed), to `maxDepth` levels, in
 * {@link ENTRY_FORMAT}. Symlinks inside are listed, never followed; subdirectories that can't be
 * read are left out. Hidden entries are pruned
 * unless `includeHidden` (`.gitignore` files are kept with `keepGitignore`), and directories named
 * in `skipDirectoryNames` are neither listed nor entered.
 *
 * Exits: `notFound`, `notDirectory`.
 */
export function listCommand(
  path: string,
  options: { maxDepth?: number; includeHidden?: boolean; keepGitignore?: boolean; skipDirectoryNames?: string[] } = {},
): string {
  const depth =
    options.maxDepth !== undefined && Number.isFinite(options.maxDepth) ? ` -maxdepth ${Math.max(1, Math.floor(options.maxDepth))}` : '';
  const keep = options.keepGitignore ? ` ! \\( -type f -name .gitignore \\)` : '';
  const hidden = options.includeHidden === false ? ` \\( -name '.*'${keep} \\) -prune -o` : '';
  const skip = (options.skipDirectoryNames ?? []).filter(Boolean);
  const skipped = skip.length
    ? ` \\( -type d \\( ${skip.map(name => `-name ${shellQuote(name)}`).join(' -o ')} \\) \\) -prune -o`
    : '';
  return [
    `p=${shellQuote(path)}`,
    `if [ ! -d "$p" ]; then if [ -e "$p" ] || [ -L "$p" ]; then exit ${notDirectory}; fi; exit ${notFound}; fi`,
    `if [ ! -r "$p" ] || [ ! -x "$p" ]; then echo "find: '$p': Permission denied" >&2; exit 1; fi`,
    // A subdirectory that can't be read is left out, as the walk Mastra falls back to does.
    `find -H "$p" -mindepth 1${depth}${hidden}${skipped} -printf ${ENTRY_FORMAT} 2>/dev/null`,
    `exit 0`,
  ].join('\n');
}

/**
 * Delete a file or a symlink (the link itself, never what it points at).
 *
 * Exits: `notFound`, `isDirectory`.
 */
export function deleteFileCommand(path: string): string {
  return [
    `p=${shellQuote(path)}`,
    `if [ -L "$p" ]; then rm -f -- "$p"; exit; fi`,
    `[ -e "$p" ] || exit ${notFound}`,
    `[ -d "$p" ] && exit ${isDirectory}`,
    `rm -f -- "$p"`,
  ].join('\n');
}

/**
 * Create a directory; an existing one is fine. With `recursive` (the default) missing parents are
 * created; without it the parent must exist.
 *
 * Exits: `exists` (something else is there), `notDirectory` / `parentNotFound` (without `recursive`).
 */
export function mkdirCommand(path: string, recursive: boolean): string {
  return [
    `p=${shellQuote(path)}`,
    `if [ -d "$p" ]; then exit 0; fi`,
    `${present('p')} && exit ${exists}`,
    ...(recursive
      ? [`mkdir -p -- "$p"`]
      : [
          requireParent('p'),
          `mkdir -- "$p" 2>/dev/null && exit 0`,
          // Lost a race: fine if a directory won it.
          `[ -d "$p" ] && exit 0`,
          `${present('p')} && exit ${exists}`,
          `mkdir -- "$p"`,
        ]),
  ].join('\n');
}

/**
 * Remove a directory: an empty one, or with `recursive` everything under it. A symlink to a
 * directory is refused without `recursive`, and with it only the link is removed.
 *
 * Exits: `notFound`, `notDirectory`, `notEmpty`.
 */
export function rmdirCommand(path: string, recursive: boolean): string {
  return [
    `p=${shellQuote(path)}`,
    `${present('p')} || exit ${notFound}`,
    ...(recursive
      ? [`if [ ! -L "$p" ] && [ ! -d "$p" ]; then exit ${notDirectory}; fi`, `rm -rf -- "$p"`]
      : [
          `if [ -L "$p" ] || [ ! -d "$p" ]; then exit ${notDirectory}; fi`,
          `out=$(rmdir -- "$p" 2>&1) && exit 0`,
          `[ -n "$(ls -A -- "$p" 2>/dev/null)" ] && exit ${notEmpty}`,
          `printf '%s\\n' "$out" >&2`,
          `exit 1`,
        ]),
  ].join('\n');
}

/**
 * Hard-link `$tmp` to `$dest` and delete `$tmp`, failing with `exists` when something is at
 * `$dest`. `ln` onto a directory (or a link to one) would create the link inside it and succeed,
 * so that case is undone and reported as taken too. (Mastra's `linkTempIntoPlace`.)
 */
function linkTempIntoPlace(): string[] {
  return [
    `ln -- "$tmp" "$dest" 2>/dev/null || { rm -f -- "$tmp"; ${present('dest')} && exit ${exists}; exit 1; }`,
    `if [ -d "$dest" ]; then rm -f -- "$dest/$(basename -- "$tmp")" "$tmp"; exit ${exists}; fi`,
    `rm -f -- "$tmp"`,
  ];
}

/**
 * Copy `src` to `dest` inside the sandbox, creating `dest`'s parent. A file source copies its
 * content (a symlinked source is followed); an existing destination file keeps its mode, as with
 * `cp`. A directory source with `recursive` copies its contents into `dest`, keeping symlinks
 * inside the tree as links. With `overwrite: false` the destination is claimed atomically: a
 * directory with an exclusive `mkdir`, a file by copying to a temp name and hard-linking it into
 * place (`link(2)` fails when the destination exists).
 *
 * Exits: `notFound` (no source), `isDirectory` (directory source without `recursive`),
 * `destIsDirectory` (a file onto a directory or a link to one), `exists`, `notDirectory` (a file
 * where the destination's parent should be).
 */
export function copyCommand(src: string, dest: string, options: { recursive?: boolean; overwrite?: boolean } = {}): string {
  const noClobber = options.overwrite === false;
  return [
    `src=${shellQuote(src)}`,
    `dest=${shellQuote(dest)}`,
    // A broken symlink has nothing to copy.
    `[ -e "$src" ] || exit ${notFound}`,
    makeParent('dest'),
    `if [ -d "$src" ]; then`,
    options.recursive ? `  :` : `  exit ${isDirectory}`,
    noClobber ? `  mkdir -- "$dest" 2>/dev/null || exit ${exists}` : `  mkdir -p -- "$dest" || exit 1`,
    `  cp -R -P -- "$src"/. "$dest"/ || exit 1`,
    `  exit 0`,
    `fi`,
    noClobber ? `${present('dest')} && exit ${exists}` : `if [ -d "$dest" ]; then exit ${destIsDirectory}; fi`,
    ...(noClobber
      ? [`tmp=${shellQuote(siblingTempPath(dest))}`, `cp -- "$src" "$tmp" || { rm -f -- "$tmp"; exit 1; }`, ...linkTempIntoPlace()]
      : [`cp -- "$src" "$dest" || exit 1`]),
  ].join('\n');
}

/**
 * Move `src` to `dest`, replacing a file at `dest` (rename semantics: `mv -T`, so nothing moves
 * into a directory that happens to be at `dest`), creating `dest`'s parent.
 *
 * Exits: `notFound`, `destIsDirectory` (a non-directory onto a directory), `notDirectory`.
 */
export function moveCommand(src: string, dest: string): string {
  return [
    `src=${shellQuote(src)}`,
    `dest=${shellQuote(dest)}`,
    `${present('src')} || exit ${notFound}`,
    `if [ -d "$dest" ] && [ ! -L "$dest" ] && { [ -L "$src" ] || [ ! -d "$src" ]; }; then exit ${destIsDirectory}; fi`,
    makeParent('dest'),
    `mv -fT -- "$src" "$dest"`,
  ].join('\n');
}

/**
 * Move `src` to `dest` without replacing anything at `dest`, creating `dest`'s parent. `-T`: a
 * directory that appears at `dest` after the check is not moved into. A skipped `mv -n` is
 * detected by the source surviving, whatever its exit code.
 *
 * Exits: `notFound`, `exists`, `notDirectory`.
 */
export function moveNoClobberCommand(src: string, dest: string): string {
  return [
    `src=${shellQuote(src)}`,
    `dest=${shellQuote(dest)}`,
    `${present('src')} || exit ${notFound}`,
    `${present('dest')} && exit ${exists}`,
    makeParent('dest'),
    // coreutils 9.2+ exits nonzero when -n skips; older versions exit 0.
    `mv -nT -- "$src" "$dest" 2>/dev/null || { ${present('src')} && ${present('dest')} && exit ${exists}; exit 1; }`,
    `${present('src')} && exit ${exists}`,
    `exit 0`,
  ].join('\n');
}

/**
 * Put the staged upload `staging` at `dest`, whole or not at all, and delete `staging` whatever
 * happens. A symlink at `dest` is written through, as a shell's `>` would: the file it names gets
 * the bytes and the link stays. The bytes go to a temp file beside that file and are renamed over
 * it, so a copy that fails (a full disk, say) leaves the old file as it was. The mode is the files
 * API's: a regular file it replaces lends its permission bits (read just before the rename;
 * setuid, setgid and sticky are not carried over), so a 0755 script stays executable, and a new
 * file gets 0666 less the umask. Without `recursive` the parent must exist.
 *
 * Exits: `isDirectory`, `notRegular` (a FIFO, socket or device), `badLink` (a link that can't be
 * resolved), `notDirectory` / `parentNotFound`.
 */
export function replaceFromStagingCommand(staging: string, dest: string, options: { recursive?: boolean } = {}): string {
  return [
    `src=${shellQuote(staging)}`,
    `dst=${shellQuote(dest)}`,
    `trap 'rm -f -- "$src"' EXIT`,
    requireStaging,
    `if [ -L "$dst" ]; then dst=$(readlink -f -- "$dst") || exit ${badLink}; fi`,
    `[ -d "$dst" ] && exit ${isDirectory}`,
    // A FIFO, socket or device is not replaced by a regular file (the files API refuses it too).
    `if [ -e "$dst" ] && [ ! -f "$dst" ]; then exit ${notRegular}; fi`,
    options.recursive === false ? requireParent('dst') : makeParent('dst'),
    `tmp=$(mktemp -- "$d/.prized-upload-XXXXXXXXXX") || exit 1`,
    `trap 'rm -f -- "$tmp" "$src"' EXIT`,
    `cp -- "$src" "$tmp" || exit 1`,
    // One lstat: the type and the bits come from the same file.
    `if r=$(stat -c %f -- "$dst" 2>/dev/null) && [ $((0x$r & 0170000)) -eq 32768 ]; then m=$((0x$r & 0777)); else m=$((0666 & ~0$(umask))); fi`,
    `chmod "$(printf %o "$m")" -- "$tmp" || exit 1`,
    `mv -fT -- "$tmp" "$dst"`,
  ].join('\n');
}

/**
 * Publish the staged upload `staging` at `dest` only if nothing is there, then delete `staging`.
 * The bytes are copied beside `dest` (so `ln` stays on one filesystem) and hard-linked into place:
 * `link(2)` is the atomic exclusivity check. The new file gets 0666 less the umask, as a new file
 * from the files API does. With `recursive` (the default) missing parents are created.
 *
 * Exits: `exists`, `notDirectory` / `parentNotFound` (dest's parent).
 */
export function linkFromStagingCommand(staging: string, dest: string, options: { recursive?: boolean } = {}): string {
  return [
    `src=${shellQuote(staging)}`,
    `dest=${shellQuote(dest)}`,
    `trap 'rm -f -- "$src"' EXIT`,
    requireStaging,
    options.recursive === false ? requireParent('dest') : makeParent('dest'),
    `${present('dest')} && exit ${exists}`,
    `tmp=$(mktemp -- "$d/.prized-upload-XXXXXXXXXX") || exit 1`,
    `trap 'rm -f -- "$tmp" "$src"' EXIT`,
    `cp -- "$src" "$tmp" || exit 1`,
    `chmod ${NEW_FILE_MODE} -- "$tmp" || exit 1`,
    ...linkTempIntoPlace(),
  ].join('\n');
}

/**
 * Append the staged upload `staging` to `dest` and delete it. Only the new bytes crossed the
 * network; `dest` (and its parent) is created when missing, and an existing file keeps its mode.
 *
 * Exits: `isDirectory`, `notRegular`, `notDirectory`.
 */
export function appendFromStagingCommand(staging: string, dest: string): string {
  return [
    `src=${shellQuote(staging)}`,
    `dest=${shellQuote(dest)}`,
    `trap 'rm -f -- "$src"' EXIT`,
    requireStaging,
    `[ -d "$dest" ] && exit ${isDirectory}`,
    // Appending to a FIFO would block until something reads it.
    `if [ -e "$dest" ] && [ ! -f "$dest" ]; then exit ${notRegular}; fi`,
    makeParent('dest'),
    `cat -- "$src" >> "$dest"`,
  ].join('\n');
}

/**
 * Copy the regular file `src` to `staging` (under the files API's root) so it can be downloaded.
 * Refuses a file over `maxBytes` before copying anything, printing its size.
 *
 * Exits: `isDirectory`, `notFound`, `notRegular`, `tooLarge`.
 */
export function stageForReadCommand(src: string, staging: string, maxBytes: number): string {
  return [
    `src=${shellQuote(src)}`,
    `dst=${shellQuote(staging)}`,
    `[ -d "$src" ] && exit ${isDirectory}`,
    `[ -e "$src" ] || exit ${notFound}`,
    `[ -f "$src" ] || exit ${notRegular}`,
    `size=$(stat -L -c %s -- "$src") || exit 1`,
    `if [ "$size" -gt ${Math.floor(maxBytes)} ]; then echo "$size"; exit ${tooLarge}; fi`,
    `mkdir -p -- "$(dirname -- "$dst")" || exit 1`,
    `cat -- "$src" > "$dst" || { rm -f -- "$dst"; exit 1; }`,
  ].join('\n');
}

/** The modification time (`%T@`, following links) of `path`, or nothing when it doesn't exist. */
export function mtimeCommand(path: string): string {
  return [`p=${shellQuote(path)}`, `if [ -e "$p" ]; then find -H "$p" -maxdepth 0 -printf '%T@\\n'; fi`, `exit 0`].join('\n');
}

/** Exit 0 when `path` is a directory (through links). */
export function isDirectoryCommand(path: string): string {
  return `[ -d ${shellQuote(path)} ]`;
}

/** Remove a file; a missing one is fine. */
export function removeCommand(path: string): string {
  return `rm -f -- ${shellQuote(path)}`;
}

/** `find`'s `%T@` (seconds with a fraction) as a Date, rounded to the millisecond, the same way every time. */
export function parseFindTime(text: string): Date {
  const [whole = '0', fraction = ''] = text.trim().split('.');
  const ms = Number(whole) * 1000 + Math.round(Number(`${fraction}000000000`.slice(0, 9)) / 1e6);
  return new Date(Number.isFinite(ms) ? ms : 0);
}
