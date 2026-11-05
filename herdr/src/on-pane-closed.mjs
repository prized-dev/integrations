#!/usr/bin/env node
// herdr runs this on every pane.closed event. When the closed pane was a Prized agent pane on a
// sandbox, the sandbox is paused, not deleted: its memory, processes and files are kept, and only
// storage is billed until the next reconnect resumes it. Boxes are left running. Set
// "onClose": "keep" in config.json to leave sandboxes running too.

import { explain, loadConfig, notify, pluginContext, prized, readState, updateState } from "./lib.mjs";

function closedPaneId(env = process.env) {
  try {
    const event = JSON.parse(env.HERDR_PLUGIN_EVENT_JSON || "{}");
    const id = event?.data?.pane_id ?? event?.pane_id;
    if (id) return String(id);
  } catch {
    // fall back to the invocation context
  }
  return pluginContext(env).focused_pane_id ?? null;
}

// Sandboxes a mapping may own: its recorded id, or (when the pane closed while the create was still
// running) whatever carries its label.
function sandboxesOf(mapping) {
  if (mapping.sandboxId) return [mapping.sandboxId];
  const listed = prized(["sandbox", "ls", "--label", `herdr-mapping=${mapping.id}`, "--json"]);
  if (listed.status !== 0) return [];
  try {
    return (JSON.parse(String(listed.stdout)).sandboxes ?? []).map((s) => s.id);
  } catch {
    return [];
  }
}

try {
  const pane = closedPaneId();
  const config = loadConfig();
  const mappings = pane ? Object.values(readState().mappings).filter((m) => m.paneId === pane) : [];
  for (const mapping of mappings) {
    if (mapping.kind !== "sandbox" || config.onClose !== "pause") {
      updateState((state) => {
        if (state.mappings[mapping.id]) state.mappings[mapping.id].paneId = null;
      });
      continue;
    }
    const ids = sandboxesOf(mapping);
    const paused = [];
    for (const id of ids) {
      const run = prized(["sandbox", "pause", id]);
      if (run.status === 0) paused.push(id);
      else process.stderr.write(`pause ${id} failed (exit ${run.status}): ${String(run.stderr).trim().split("\n").pop()}\n`);
    }
    updateState((state) => {
      const current = state.mappings[mapping.id];
      if (!current) return;
      current.paneId = null;
      if (!current.sandboxId && ids.length === 1) current.sandboxId = ids[0];
      if (paused.length) current.status = "paused";
      current.updatedAt = new Date().toISOString();
    });
    if (paused.length) {
      notify("Prized", `Paused ${paused.join(", ")}. Memory and processes are kept; reconnect to resume.`);
      process.stdout.write(`${JSON.stringify({ event: "pane.closed", pane, mapping: mapping.id, paused })}\n`);
    }
  }
} catch (error) {
  process.stderr.write(`prized.sandbox pane.closed: ${explain(error)}\n`);
  process.exitCode = 1;
}
