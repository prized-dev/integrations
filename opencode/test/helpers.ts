import { execFileSync } from 'node:child_process';
import { chmodSync, mkdirSync, mkdtempSync, realpathSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import type { Call } from '../src/tools.ts';

/** A git checkout in a temp folder: app.py, bin/run (executable), .env (ignored), all committed. */
export function makeRepo(): string {
  const dir = realpathSync(mkdtempSync(path.join(tmpdir(), 'oc-repo-')));
  const git = (...a: string[]) => execFileSync('git', a, { cwd: dir, env: { ...process.env, GIT_CONFIG_NOSYSTEM: '1' } });
  git('init', '-q', '-b', 'main');
  git('config', 'user.name', 'Test');
  git('config', 'user.email', 'test@example.com');
  writeFileSync(path.join(dir, 'app.py'), 'def greet():\n    print("Hi")\n');
  writeFileSync(path.join(dir, '.gitignore'), '.env\nnode_modules/\n');
  writeFileSync(path.join(dir, '.env'), 'SECRET=dont-upload-me\n');
  mkdirSync(path.join(dir, 'bin'));
  writeFileSync(path.join(dir, 'bin', 'run'), '#!/bin/sh\necho run\n');
  chmodSync(path.join(dir, 'bin', 'run'), 0o755);
  mkdirSync(path.join(dir, 'src'));
  writeFileSync(path.join(dir, 'src', 'util.ts'), 'export const add = (a: number, b: number) => a + b;\n');
  git('add', '-A');
  git('commit', '-q', '-m', 'init');
  return dir;
}

export const gitIn = (dir: string, ...a: string[]): string => execFileSync('git', a, { cwd: dir }).toString();

/** A tool call as OpenCode makes it, recording what it asked and showed. */
export function makeCall(sessionID = 'ses_root', deny?: string): Call & { asks: Array<{ permission: string; patterns: string[] }>; updates: unknown[] } {
  const asks: Array<{ permission: string; patterns: string[] }> = [];
  const updates: unknown[] = [];
  return {
    sessionID,
    abort: new AbortController().signal,
    asks,
    updates,
    progress: (u) => updates.push(u),
    ask: async (req) => {
      asks.push({ permission: req.permission, patterns: req.patterns });
      if (req.permission === deny) throw new Error(`The user rejected permission to use this specific tool call: ${req.permission}`);
    },
  };
}
