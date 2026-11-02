#!/usr/bin/env node
// The agent pane. The first time for a mapping it creates the sandbox (or picks the box), copies the
// worktree in and starts the agent in a tmux session there; every time, it attaches to that session.
// Closing the pane pauses the sandbox (on-pane-closed.mjs), so the agent's memory and processes are
// kept for the next attach.

import { spawnSync } from "node:child_process";
import fs from "node:fs";

import {
  PluginError,
  cleanupArchive,
  envFile,
  explain,
  formatBytes,
  forwardNames,
  listWorktree,
  loadConfig,
  packWorktree,
  patchMapping,
  prized,
  prizedBin,
  prizedFailure,
  prizedOk,
  pickBox,
  readKey,
  readState,
  remoteDir,
  sandboxState,
  say,
  startSessionScript,
  unpackScript,
} from "./lib.mjs";

const ARCHIVE = "/home/prized/.herdr-worktree.tgz";
const ENV_FILE = "~/.herdr-prized.env";

function step(label, fn) {
  process.stdout.write(`  ${label}... `);
  const value = fn();
  say(typeof value === "string" && value ? value : "done");
  return value;
}

// Archives still on disk when the pane is closed mid-copy (herdr sends SIGHUP) are removed on the way out.
const pendingArchives = new Set();
for (const signal of ["SIGHUP", "SIGTERM", "SIGINT"]) {
  process.once(signal, () => {
    for (const archive of pendingArchives) cleanupArchive(archive);
    process.exit(128 + ({ SIGHUP: 1, SIGINT: 2, SIGTERM: 15 })[signal]);
  });
}

function packStep(mapping) {
  const listing = listWorktree(mapping.root);
  process.stdout.write(`  packing ${listing.files.length} files (${formatBytes(listing.bytes)})... `);
  const packed = packWorktree(mapping.root, listing.files);
  pendingArchives.add(packed.archive);
  say(formatBytes(packed.size));
  const secrets = listing.skipped.filter((s) => s.reason !== "submodule");
  if (secrets.length) {
    const shown = secrets.slice(0, 5).map((s) => s.path).join(", ");
    say(`  left out ${secrets.length} file(s) that may hold credentials: ${shown}${secrets.length > 5 ? ", ..." : ""}`);
  }
  const subs = listing.skipped.filter((s) => s.reason === "submodule");
  if (subs.length) say(`  submodules are not copied: ${subs.map((s) => s.path).join(", ")}`);
  return packed;
}

function sandboxExec(id, script) {
  return prizedOk(["sandbox", "exec", id, "--", script]);
}

function createSandbox(mapping, config) {
  const args = ["sandbox", "create", "--template", config.template, "--vcpu", String(config.vcpu), "--memory", String(config.memoryGb)];
  args.push("--label", "via=herdr", "--label", `herdr-mapping=${mapping.id}`);
  if (config.autoPause) args.push("--auto-pause", String(config.autoPause));
  if (config.maxRuntime) args.push("--max-runtime", String(config.maxRuntime));
  say(`  creating a sandbox (${config.template}, ${config.vcpu} vCPU, ${config.memoryGb} GB)`);
  const created = prized(args, { stderr: "inherit" });
  if (created.status !== 0) throw prizedFailure(args, created);
  const id = String(created.stdout).trim().split("\n").pop();
  if (!/^sbx_[0-9a-f]+$/.test(id)) throw new PluginError("prized sandbox create printed no sandbox id");
  say(`  sandbox ${id}`);
  return patchMapping(mapping.id, { sandboxId: id, status: "copying" });
}

// Copy the worktree in and hand over the agent's keys. Runs again on reconnect if it was cut short.
function fillSandbox(mapping, config) {
  const id = mapping.sandboxId;
  const packed = packStep(mapping);
  try {
    step("copying the worktree in", () => {
      prizedOk(["sandbox", "cp", packed.archive, `${id}:${ARCHIVE}`]);
      sandboxExec(id, unpackScript(remoteDir(mapping), ARCHIVE));
    });
  } finally {
    cleanupArchive(packed.archive);
    pendingArchives.delete(packed.archive);
  }
  const names = forwardNames(config);
  if (names.length) {
    step(`passing ${names.join(", ")} to the agent`, () => {
      // An empty file with mode 600 first: cp keeps the mode of a file it replaces.
      sandboxExec(id, `umask 077 && : > ${ENV_FILE}`);
      prizedOk(["sandbox", "cp", "-", `${id}:${ENV_FILE}`], { input: envFile(names) });
    });
  }
  return patchMapping(mapping.id, { status: "copied", copied: true });
}

function chooseBox(mapping) {
  const listed = prizedOk(["box", "ls", "--json"]);
  let boxes = [];
  try {
    boxes = JSON.parse(String(listed.stdout)).boxes ?? [];
  } catch {
    throw new PluginError("prized box ls --json printed something that is not JSON");
  }
  return pickBox(boxes).then((name) => patchMapping(mapping.id, { box: name }));
}

function fillBox(mapping) {
  say(`Prized box ${mapping.box} for ${mapping.root}`);
  const packed = packStep(mapping);
  try {
    step(`copying the worktree to ~/herdr/${mapping.dirName}`, () => {
      prizedOk(["exec", mapping.box, "--", unpackScript(remoteDir(mapping), "-")], { input: fs.readFileSync(packed.archive) });
    });
  } finally {
    cleanupArchive(packed.archive);
    pendingArchives.delete(packed.archive);
  }
  return patchMapping(mapping.id, { status: "copied", copied: true });
}

function ensureSession(mapping) {
  const script = startSessionScript(remoteDir(mapping), mapping.session, mapping.agentCommand, mapping.kind);
  if (mapping.kind === "box") prizedOk(["exec", mapping.box, "--", script]);
  else prizedOk(["sandbox", "exec", mapping.sandboxId, "--", script]);
}

function attach(mapping) {
  const args = mapping.kind === "box" ? ["ssh", mapping.box, "-s", mapping.session] : ["sandbox", "ssh", mapping.sandboxId, "-s", mapping.session];
  const run = spawnSync(prizedBin(), args, { stdio: "inherit" });
  if (run.error) throw run.error;
  return run.status ?? 1;
}

async function main() {
  const id = process.env.PRIZED_HERDR_MAPPING;
  if (!id) throw new PluginError("PRIZED_HERDR_MAPPING is not set: open this pane from a Prized action");
  let mapping = readState().mappings[id];
  if (!mapping) throw new PluginError(`no mapping ${id}`);
  const config = loadConfig();
  mapping = patchMapping(id, { paneId: process.env.HERDR_PANE_ID ?? mapping.paneId });

  if (mapping.kind === "sandbox") {
    if (!mapping.sandboxId) {
      say(`Prized sandbox for ${mapping.root}`);
      mapping = createSandbox(mapping, config);
    } else {
      const state = sandboxState(mapping.sandboxId);
      if (state === "deleted" || state === "error") {
        patchMapping(id, { status: state });
        throw new PluginError(`sandbox ${mapping.sandboxId} is ${state}`, "start a new one with \"Start agent in a Prized sandbox\"");
      }
      if (state === "paused" || state === "pausing") say(`Resuming ${mapping.sandboxId}: memory and processes pick up where they stopped.`);
    }
    // `copied`, not the status: a pause or a closed pane changes the status of a copy cut short.
    if (!mapping.copied) mapping = fillSandbox(mapping, config);
  } else {
    if (!mapping.box) mapping = await chooseBox(mapping);
    if (!mapping.copied) mapping = fillBox(mapping);
  }

  for (;;) {
    step(`starting ${mapping.agentCommand || "a shell"} in tmux session ${mapping.session}`, () => ensureSession(mapping));
    mapping = patchMapping(id, { status: "attached", lastError: null });
    const where = mapping.kind === "box" ? `box ${mapping.box}` : `sandbox ${mapping.sandboxId}`;
    const code = attach(mapping);
    mapping = patchMapping(id, { status: "detached" });
    say("");
    if (code !== 0) {
      say(`The connection to ${where} ended (exit ${code}).`);
      if (mapping.kind === "sandbox") say("A shell in a sandbox opens with your `prized login`; an API key cannot open one.");
    } else {
      say(`Disconnected from ${where}. The agent is still running there.`);
    }
    const closing = mapping.kind === "sandbox" && config.onClose === "pause" ? "close this pane (pauses the sandbox; memory and processes are kept)" : "close this pane (keeps running)";
    say(`r: reconnect   ${mapping.kind === "sandbox" ? "p: pause now   " : ""}any other key: ${closing}`);
    const key = await readKey();
    if (key === "r") continue;
    if (key === "p" && mapping.kind === "sandbox") {
      const run = prized(["sandbox", "pause", mapping.sandboxId, "--wait"]);
      if (run.status !== 0) throw prizedFailure(["sandbox", "pause"], run);
      patchMapping(id, { status: "paused" });
      say(`Paused ${mapping.sandboxId}.`);
    }
    return;
  }
}

try {
  await main();
} catch (error) {
  const id = process.env.PRIZED_HERDR_MAPPING;
  if (id) {
    try {
      patchMapping(id, { lastError: String(error?.message ?? error) });
    } catch {
      // the mapping is gone: nothing to record
    }
  }
  say("");
  say(`Could not start the Prized agent: ${explain(error)}`);
  say("Press any key to close this pane.");
  await readKey();
  process.exitCode = 1;
}
