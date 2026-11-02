#!/usr/bin/env node
// Every manifest action runs this file; HERDR_PLUGIN_ACTION_ID says which. Actions have no terminal,
// so anything that shows progress or asks a question opens a pane. Output here goes to herdr's
// plugin log (`herdr plugin log list --plugin prized.sandbox`).

import {
  PluginError,
  agentOf,
  contextCwd,
  explain,
  findMapping,
  loadConfig,
  newId,
  notify,
  openPane,
  patchMapping,
  pluginContext,
  prized,
  prizedFailure,
  readState,
  safeName,
  updateState,
  worktreeRoot,
} from "./lib.mjs";
import path from "node:path";

function result(action, status, extra = {}) {
  process.stdout.write(`${JSON.stringify({ action, status, ...extra })}\n`);
}

function mappingFor(context) {
  const root = tryRoot(context);
  const mapping = findMapping(readState(), context, root);
  if (!mapping) {
    throw new PluginError("no Prized sandbox or box session for this pane or worktree", "start one with \"Start agent in a Prized sandbox\"");
  }
  return mapping;
}

function tryRoot(context) {
  try {
    return worktreeRoot(contextCwd(context));
  } catch {
    return null;
  }
}

function start(kind) {
  return (context) => {
    const config = loadConfig();
    const root = worktreeRoot(contextCwd(context));
    const id = newId();
    const agent = agentOf(config);
    const mapping = {
      id,
      kind,
      root,
      dirName: kind === "box" ? `${safeName(path.basename(root))}-${id}` : safeName(path.basename(root)),
      session: `herdr-${id}`,
      agentCommand: agent.command,
      herdrAgent: agent.herdr,
      sandboxId: null,
      box: kind === "box" ? config.box : null, // null: the pane asks which box
      paneId: null,
      status: "new",
      createdAt: new Date().toISOString(),
    };
    updateState((state) => {
      state.mappings[id] = mapping;
    });
    openPane("agent", context, { placement: "split", env: paneEnv(mapping) });
    result(`start-${kind}`, "opened", { mapping: id });
  };
}

function paneEnv(mapping) {
  const env = { PRIZED_HERDR_MAPPING: mapping.id };
  if (mapping.herdrAgent) env.HERDR_AGENT = mapping.herdrAgent;
  return env;
}

function reconnect(context) {
  const mapping = mappingFor(context);
  openPane("agent", context, { placement: "split", env: paneEnv(mapping) });
  result("reconnect", "opened", { mapping: mapping.id, sandbox: mapping.sandboxId, box: mapping.box });
}

function pause(context) {
  const mapping = mappingFor(context);
  if (mapping.kind !== "sandbox") {
    result("pause", "skipped", { reason: "boxes pause on their own when idle; this plugin leaves them running" });
    return;
  }
  if (!mapping.sandboxId) throw new PluginError("this sandbox is still being created");
  const run = prized(["sandbox", "pause", mapping.sandboxId, "--wait"]);
  if (run.status !== 0) throw prizedFailure(["sandbox", "pause"], run);
  patchMapping(mapping.id, { status: "paused" });
  notify("Prized", `Paused ${mapping.sandboxId}. Its memory and processes are kept; reconnect to resume.`);
  result("pause", "paused", { mapping: mapping.id, sandbox: mapping.sandboxId });
}

function ops(op) {
  return (context) => {
    const mapping = mappingFor(context);
    openPane("ops", context, { placement: "popup", env: { PRIZED_HERDR_MAPPING: mapping.id, PRIZED_HERDR_OP: op } });
    result(op, "opened", { mapping: mapping.id });
  };
}

function addMachine(context) {
  openPane("machine", context, { placement: "popup" });
  result("add-machine", "opened");
}

export const ACTIONS = {
  "start-sandbox": start("sandbox"),
  "start-box": start("box"),
  reconnect,
  "apply-changes": ops("apply-changes"),
  pause,
  delete: ops("delete"),
  "add-machine": addMachine,
};

const action = process.env.HERDR_PLUGIN_ACTION_ID || "";
try {
  const run = ACTIONS[action];
  if (!run) throw new PluginError(`unknown action "${action}"`);
  await run(pluginContext());
} catch (error) {
  process.stderr.write(`prized.sandbox ${action}: ${explain(error)}\n`);
  try {
    notify("Prized", explain(error).split("\n")[0]);
  } catch {
    // herdr itself is unreachable: the log line above is all we can do
  }
  process.exitCode = 1;
}
