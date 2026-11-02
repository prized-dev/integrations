// The plugin's record of the sandboxes Orca workspaces run on: which workspace, where its checkout
// is, the prized CLI's ssh files for it, and whether Orca has put it to sleep. No secrets.

import fs from "node:fs";
import path from "node:path";
import { UserError, paths, readJson, sleepSync, writeFileAtomic } from "./util.mjs";

const EMPTY = () => ({ version: 1, sandboxes: {} });

export function readState(env = process.env) {
  const file = paths(env).state;
  let state;
  try {
    state = readJson(file, EMPTY());
  } catch {
    // A broken file is set aside, never silently reused.
    fs.renameSync(file, `${file}.broken-${Date.now()}`);
    return EMPTY();
  }
  if (!state || typeof state.sandboxes !== "object" || Array.isArray(state.sandboxes)) return EMPTY();
  return state;
}

function lockIsStale(lock) {
  try {
    if (Date.now() - fs.statSync(lock).mtimeMs < 30_000) return false;
    const pid = Number(fs.readFileSync(path.join(lock, "pid"), "utf8"));
    if (!pid) return true;
    process.kill(pid, 0);
    return false;
  } catch (error) {
    return error.code !== "EPERM";
  }
}

// Run fn under a lock directory, so Orca's lifecycle calls, the cert hook and the proxy never lose
// each other's writes.
export function withLock(lock, fn, timeoutMs = 15_000) {
  fs.mkdirSync(path.dirname(lock), { recursive: true, mode: 0o700 });
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      fs.mkdirSync(lock);
      fs.writeFileSync(path.join(lock, "pid"), String(process.pid));
      break;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      if (lockIsStale(lock)) {
        // Rename first: of two processes clearing the same stale lock, only one wins the rename.
        const aside = `${lock}.stale-${process.pid}`;
        try {
          fs.renameSync(lock, aside);
          fs.rmSync(aside, { recursive: true, force: true });
        } catch {
          // another process cleared it first
        }
        continue;
      }
      if (Date.now() > deadline) throw new UserError(`${lock} is held: remove it if no prized-orca command is running`);
      sleepSync(50);
    }
  }
  try {
    return fn();
  } finally {
    fs.rmSync(lock, { recursive: true, force: true });
  }
}

// Read-modify-write the state under the lock. fn may return a value; the state is written either way.
export function updateState(fn, env = process.env) {
  const p = paths(env);
  return withLock(p.lock, () => {
    const state = readState(env);
    const result = fn(state);
    writeFileAtomic(p.state, `${JSON.stringify(state, null, 2)}\n`);
    return result;
  });
}

export function getEntry(id, env = process.env) {
  return readState(env).sandboxes[id] ?? null;
}

export function putEntry(id, patch, env = process.env) {
  return updateState((state) => {
    const now = new Date().toISOString();
    state.sandboxes[id] = { createdAt: now, ...(state.sandboxes[id] ?? {}), ...patch, updatedAt: now };
    return state.sandboxes[id];
  }, env);
}

export function dropEntry(id, env = process.env) {
  return updateState((state) => {
    const had = Boolean(state.sandboxes[id]);
    delete state.sandboxes[id];
    return had;
  }, env);
}
