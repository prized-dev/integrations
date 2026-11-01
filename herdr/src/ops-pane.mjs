#!/usr/bin/env node
// A popup for the two operations that need a person's yes: applying the agent's changes to the local
// worktree, and deleting a sandbox for good.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  PluginError,
  advanceBaseScript,
  diffScript,
  explain,
  patchMapping,
  prized,
  prizedFailure,
  prizedOk,
  readKey,
  readLine,
  readState,
  remoteDir,
  say,
  updateState,
} from "./lib.mjs";

const PATCH = "/home/prized/.herdr-changes.patch";

function remote(mapping, script, options = {}) {
  return mapping.kind === "box"
    ? prizedOk(["exec", mapping.box, "--", script], options)
    : prizedOk(["sandbox", "exec", mapping.sandboxId, "--", script], options);
}

// The patch and the tree it was taken from. A sandbox writes the patch to a file and `sandbox cp`
// brings it back byte for byte; a box streams it straight back.
function fetchPatch(mapping, dir) {
  const local = path.join(dir, "changes.patch");
  if (mapping.kind === "box") {
    const out = remote(mapping, diffScript(remoteDir(mapping))).stdout;
    const nl = out.indexOf(10);
    fs.writeFileSync(local, out.subarray(nl + 1));
    return { tree: out.subarray(0, nl).toString().trim(), local };
  }
  const tree = String(remote(mapping, diffScript(remoteDir(mapping), PATCH)).stdout).trim();
  prizedOk(["sandbox", "cp", `${mapping.sandboxId}:${PATCH}`, local]);
  return { tree, local };
}

function git(root, args) {
  return spawnSync("git", ["-C", root, ...args], { encoding: "utf8" });
}

async function applyChanges(mapping) {
  if (mapping.kind === "sandbox" && !mapping.sandboxId) throw new PluginError("this sandbox is still being created");
  const where = mapping.kind === "box" ? `box ${mapping.box}` : `sandbox ${mapping.sandboxId}`;
  say(`Changes in ${where} since the copy (or the last apply)`);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "herdr-prized-patch-"));
  try {
    const { tree, local } = fetchPatch(mapping, dir);
    if (!/^[0-9a-f]{40,64}$/.test(tree)) throw new PluginError("could not read the remote tree");
    if (fs.statSync(local).size === 0) {
      say("No changes.");
      return;
    }
    const stat = git(mapping.root, ["apply", "--stat", "--binary", local]);
    say(stat.stdout.trimEnd());
    say("");
    const check = git(mapping.root, ["apply", "--check", "--binary", local]);
    if (check.status !== 0) {
      say("These changes do not apply cleanly to the local worktree, so nothing was changed:");
      say(check.stderr.trimEnd());
      return;
    }
    const answer = (await readLine(`Apply them to ${mapping.root}? [y/N] `)).trim().toLowerCase();
    if (answer !== "y" && answer !== "yes") {
      say("Nothing changed.");
      return;
    }
    const applied = git(mapping.root, ["apply", "--binary", local]);
    if (applied.status !== 0) throw new PluginError(`git apply failed:\n${applied.stderr.trimEnd()}`);
    remote(mapping, advanceBaseScript(remoteDir(mapping), tree));
    patchMapping(mapping.id, { lastAppliedAt: new Date().toISOString() });
    say("Applied. The next apply shows only what changes after this.");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function deleteTarget(mapping) {
  if (mapping.kind === "box") {
    say(`This stops the agent's tmux session ${mapping.session} on box ${mapping.box}.`);
    say(`The box and the files in ~/herdr/${mapping.dirName} stay.`);
    const answer = (await readLine("Stop it? [y/N] ")).trim().toLowerCase();
    if (answer !== "y" && answer !== "yes") return say("Nothing changed.");
    const run = prized(["ssh", mapping.box, "--kill", mapping.session]);
    if (run.status !== 0) say(`(the session was already gone: ${String(run.stderr).trim().split("\n").pop()})`);
  } else {
    if (mapping.sandboxId) {
      say(`This deletes sandbox ${mapping.sandboxId} for good: its files, memory and processes.`);
      say("Apply the agent's changes first if you want them.");
      const typed = (await readLine("Type DELETE to delete it: ")).trim();
      if (typed !== "DELETE") return say("Nothing changed.");
      const run = prized(["sandbox", "rm", mapping.sandboxId]);
      if (run.status !== 0 && run.status !== 5) throw prizedFailure(["sandbox", "rm"], run);
    }
  }
  updateState((state) => {
    delete state.mappings[mapping.id];
  });
  say(mapping.kind === "box" ? "Stopped." : "Deleted.");
}

async function main() {
  const id = process.env.PRIZED_HERDR_MAPPING;
  const mapping = id && readState().mappings[id];
  if (!mapping) throw new PluginError(`no mapping ${id ?? ""}`.trim());
  const op = process.env.PRIZED_HERDR_OP;
  if (op === "apply-changes") await applyChanges(mapping);
  else if (op === "delete") await deleteTarget(mapping);
  else throw new PluginError(`unknown operation ${op}`);
}

try {
  await main();
} catch (error) {
  say("");
  say(explain(error));
  process.exitCode = 1;
}
say("");
say("Press any key to close.");
await readKey();
