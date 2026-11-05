/**
 * Paths on your machine and in the sandbox.
 *
 * OpenCode tells the model the project lives where you started it (/Users/you/app), so the model passes
 * those paths to the tools. The sandbox keeps the project at /home/prized/work/app. Paths under the
 * project map one to one; your home maps to the sandbox's home; anything else is a path in the sandbox
 * as given. Nothing here ever resolves to a file on your machine: the result is always a sandbox path.
 */

import os from 'node:os';
import path from 'node:path';

export const SANDBOX_HOME = '/home/prized';

/** Where the project is on your machine and in the sandbox. */
export interface Layout {
  /** The sandbox's copy of the project (the git root, or the start folder outside git). */
  dir: string;
  /** The sandbox folder that mirrors the folder OpenCode was started in. */
  cwd: string;
  /** The folder on your machine that `dir` mirrors. */
  hostDir: string;
  /** The folder OpenCode was started in. */
  hostCwd: string;
  /** Your git checkout, which a pull writes to; unset outside a git checkout. */
  hostRoot?: string;
}

/** Forward slashes and no drive letter, so a Windows path compares like a POSIX one. */
function norm(p: string, platform: string): string {
  return platform === 'win32' ? p.replace(/^[A-Za-z]:/, '').replaceAll('\\', '/') : p;
}

function under(p: string, base: string): string | undefined {
  if (p === base) return '';
  return p.startsWith(`${base}/`) ? p.slice(base.length + 1) : undefined;
}

/** The sandbox path for a path the model gave (absolute on your machine, absolute in the sandbox, `~/...`, or relative). */
export function toSandbox(
  given: string,
  layout: Layout,
  hostHome: string = os.homedir(),
  platform: string = process.platform,
): string {
  let p = given.trim();
  if (p === '') return layout.cwd;
  if (p === '~') return SANDBOX_HOME;
  if (p.startsWith('~/')) return path.posix.normalize(`${SANDBOX_HOME}/${p.slice(2)}`);
  const winAbs = platform === 'win32' && /^[A-Za-z]:[\\/]/.test(p);
  if (!p.startsWith('/') && !winAbs) return path.posix.normalize(path.posix.join(layout.cwd, p.replaceAll('\\', '/')));
  p = path.posix.normalize(norm(p, platform));
  const inProject = under(p, norm(layout.hostDir, platform));
  if (inProject !== undefined) return inProject ? `${layout.dir}/${inProject}` : layout.dir;
  // Already a sandbox path: the model read one in bash output.
  if (under(p, SANDBOX_HOME) !== undefined) return p;
  const inHome = under(p, norm(hostHome, platform));
  if (inHome !== undefined) return inHome ? `${SANDBOX_HOME}/${inHome}` : SANDBOX_HOME;
  return p;
}

/** How a sandbox path is shown to the model and in OpenCode: under the project, as the path on your machine. */
export function toHost(p: string, layout: Layout, platform: string = process.platform): string {
  const rel = under(p, layout.dir);
  if (rel === undefined) return p;
  if (!rel) return layout.hostDir;
  return platform === 'win32' ? path.win32.join(layout.hostDir, ...rel.split('/')) : `${layout.hostDir}/${rel}`;
}

/** A path relative to the project, for permission patterns and titles (as OpenCode's own tools give them). */
export function relative(sandboxPath: string, layout: Layout): string {
  const rel = under(sandboxPath, layout.dir);
  return rel === undefined ? sandboxPath : rel || '.';
}

/** The layout for a project: the sandbox mirrors your checkout, so the agent starts in the same subfolder you did. */
export function layoutFor(hostCwd: string, hostRoot: string | undefined): Layout {
  const hostDir = hostRoot ?? hostCwd;
  const name = path.basename(hostDir).replace(/[^\w.-]/g, '_') || 'workspace';
  const dir = `${SANDBOX_HOME}/work/${name === '.' || name === '..' ? 'workspace' : name}`;
  const rel = path.relative(hostDir, hostCwd).split(path.sep).join('/');
  const cwd = rel && !rel.startsWith('..') ? path.posix.join(dir, rel) : dir;
  return { dir, cwd, hostDir, hostCwd, hostRoot };
}
