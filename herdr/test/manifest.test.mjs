// The manifest is what herdr validates and the marketplace indexes. These checks mirror herdr's own
// rules (docs/plugins.mdx and src/app/api/plugins/manifest.rs): required metadata, id characters,
// unique local ids, hookable event names, and commands that exist.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { PLUGIN } from "./helpers.mjs";

// Enough TOML for this manifest: top-level keys, [[tables]], strings, numbers and string arrays.
function parseManifest(text) {
  const top = {};
  let current = top;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const table = line.match(/^\[\[([a-z_]+)\]\]$/);
    if (table) {
      top[table[1]] ??= [];
      current = {};
      top[table[1]].push(current);
      continue;
    }
    const kv = line.match(/^([a-z_]+)\s*=\s*(.+)$/);
    assert.ok(kv, `unparsed manifest line: ${line}`);
    current[kv[1]] = JSON.parse(kv[2]);
  }
  return top;
}

const manifest = parseManifest(fs.readFileSync(path.join(PLUGIN, "herdr-plugin.toml"), "utf8"));

test("required metadata herdr and the marketplace read", () => {
  assert.equal(manifest.id, "prized.sandbox");
  assert.match(manifest.id, /^[A-Za-z0-9.:_-]+$/);
  assert.equal(typeof manifest.name, "string");
  assert.match(manifest.version, /^\d+\.\d+\.\d+$/);
  assert.match(manifest.min_herdr_version, /^0\.(8|9)\.\d+$/);
  assert.deepEqual(manifest.platforms, ["linux", "macos"]);
  assert.ok(manifest.description.length < 200);
});

test("local ids are unique and use herdr's characters (no dots)", () => {
  for (const kind of ["actions", "panes"]) {
    const ids = manifest[kind].map((x) => x.id);
    assert.equal(new Set(ids).size, ids.length, `${kind} ids repeat`);
    for (const id of ids) assert.match(id, /^[A-Za-z0-9:_-]+$/);
  }
});

test("every action is handled by action.mjs and every command file exists", async () => {
  const source = fs.readFileSync(path.join(PLUGIN, "src", "action.mjs"), "utf8");
  for (const action of manifest.actions) {
    assert.deepEqual(action.command, ["node", "src/action.mjs"]);
    assert.ok(source.includes(`"${action.id}"`) || source.includes(`  ${action.id},`), `action.mjs does not handle ${action.id}`);
    assert.ok(action.contexts.length > 0);
  }
  for (const item of [...manifest.panes, ...manifest.events]) {
    assert.equal(item.command[0], "node");
    assert.ok(fs.existsSync(path.join(PLUGIN, item.command[1])), `${item.command[1]} is missing`);
  }
});

test("the close hook listens to an event herdr lets plugins hook", () => {
  // herdr's plugin_hook_event_names() leaves out only high-volume events (pane.output_changed,
  // layout.updated, workspace.metadata_updated, pane.updated); pane.closed is hookable.
  assert.deepEqual(
    manifest.events.map((e) => e.on),
    ["pane.closed"],
  );
});

test("panes use placements herdr knows", () => {
  for (const pane of manifest.panes) assert.ok(["overlay", "popup", "split", "tab", "zoomed"].includes(pane.placement));
});

test("no build step: the plugin has no dependencies to install", () => {
  assert.equal(manifest.build, undefined);
  const pkg = JSON.parse(fs.readFileSync(path.join(PLUGIN, "package.json"), "utf8"));
  assert.equal(pkg.dependencies, undefined);
});
