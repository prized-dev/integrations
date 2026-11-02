// The plugin end to end against stand-ins for `prized` and `herdr`: actions, the agent pane, the close
// hook, applying changes and deleting. The fake sandbox is a directory where the plugin's remote
// scripts really run (bash, tar, git), so the copy and the patch round trip are real.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  FAKE_AGENT_KEY,
  FAKE_PRIZED_KEY,
  PLUGIN,
  action,
  boxHome,
  db,
  everythingWritten,
  git,
  herdrCalls,
  prizedCalls,
  run,
  sandboxHome,
  state,
  world,
} from "./helpers.mjs";

const ctx = (w, pane = "w1:p1") => ({ focused_pane_id: pane, focused_pane_cwd: path.join(w.repo, "src") });

function startSandbox(w, env = {}) {
  const started = action(w, "start-sandbox", ctx(w));
  assert.equal(started.status, 0, started.out);
  const mapping = Object.values(state(w).mappings)[0];
  const pane = run(w, "agent-pane.mjs", {
    env: { PRIZED_HERDR_MAPPING: mapping.id, HERDR_PANE_ID: "w1:p2", ANTHROPIC_API_KEY: FAKE_AGENT_KEY, ...env },
    input: "\n", // the menu after the attach: close
  });
  return { mapping: state(w).mappings[mapping.id], pane };
}

test("start-sandbox opens the agent pane beside the invoking pane", () => {
  const w = world();
  const r = action(w, "start-sandbox", ctx(w));
  assert.equal(r.status, 0, r.out);
  const [mapping] = Object.values(state(w).mappings);
  assert.equal(mapping.kind, "sandbox");
  assert.equal(mapping.root, w.repo);
  assert.equal(mapping.session, `herdr-${mapping.id}`);
  const [open] = herdrCalls(w);
  assert.match(open, /^plugin pane open --plugin prized\.sandbox --entrypoint agent --placement split --target-pane w1:p1 --direction right/);
  assert.ok(open.includes(`--env PRIZED_HERDR_MAPPING=${mapping.id}`));
  assert.ok(open.includes("--env HERDR_AGENT=claude"));
  // A --cwd would make herdr start `node src/agent-pane.mjs` outside the plugin root (seen with herdr 0.9.3).
  assert.ok(!open.includes("--cwd"));
  assert.equal(prizedCalls(w).length, 0, "the action itself creates nothing");
});

test("the agent pane creates the sandbox, copies the worktree, starts the agent in tmux and attaches", () => {
  const w = world();
  const { mapping, pane } = startSandbox(w);
  assert.equal(pane.status, 0, pane.out);
  const id = mapping.sandboxId;
  assert.match(id, /^sbx_[0-9a-f]{16}$/);
  assert.equal(mapping.paneId, "w1:p2");

  const calls = prizedCalls(w).map((c) => c.args);
  const create = calls.find((a) => a[1] === "create");
  assert.deepEqual(create.slice(0, 8), ["sandbox", "create", "--template", "prized-agent-v1", "--vcpu", "2", "--memory", "4"]);
  assert.ok(create.join(" ").includes("--label via=herdr"));
  assert.ok(create.join(" ").includes(`--label herdr-mapping=${mapping.id}`));
  assert.ok(create.join(" ").includes("--auto-pause 30m"));
  assert.deepEqual(calls.at(-1), ["sandbox", "ssh", id, "-s", mapping.session]);

  // The copy: the worktree's files, a Git base to diff against, and no credentials.
  const work = path.join(sandboxHome(w, id), "work", "my-repo");
  assert.equal(fs.readFileSync(path.join(work, "src", "app.js"), "utf8"), "export const a = 1;\n");
  assert.ok(fs.existsSync(path.join(work, "notes.txt")));
  assert.ok(fs.existsSync(path.join(work, ".env.example")));
  for (const gone of [".env", "server.pem", "dist/bundle.js", "gone.txt"]) assert.ok(!fs.existsSync(path.join(work, gone)), gone);
  assert.match(git(work, "rev-parse", "refs/herdr/base"), /^[0-9a-f]{40}/);
  assert.ok(!fs.existsSync(path.join(sandboxHome(w, id), ".herdr-worktree.tgz")), "the archive is removed after unpacking");

  // The agent's key went over stdin into a private file, and nowhere else.
  const envFile = path.join(sandboxHome(w, id), ".herdr-prized.env");
  assert.equal(fs.readFileSync(envFile, "utf8"), `ANTHROPIC_API_KEY='${FAKE_AGENT_KEY}'\n`);
  assert.equal(fs.statSync(envFile).mode & 0o777, 0o600);
  const all = everythingWritten(w, pane.out);
  assert.ok(!all.includes(FAKE_AGENT_KEY), "the agent key leaked into a log, argv or the output");
  assert.ok(!all.includes(FAKE_PRIZED_KEY), "PRIZED_API_KEY leaked");
  assert.ok(prizedCalls(w).every((c) => c.hasKey), "every prized call got PRIZED_API_KEY from the environment");

  const tmux = fs.readFileSync(path.join(sandboxHome(w, id), ".tmux-calls"), "utf8");
  assert.match(tmux, new RegExp(`new-session -d -s ${mapping.session} -c .*/work/my-repo bash -lc .*; claude; exec bash -l`));
  assert.match(tmux, new RegExp(`set-option -t ${mapping.session} status off`));
  // `prized sandbox ssh` attaches from the login side: ~/.bashrc points it at the same tmux socket.
  const bashrc = fs.readFileSync(path.join(sandboxHome(w, id), ".bashrc"), "utf8");
  assert.match(bashrc.split("\n")[1], /^export TMUX_TMPDIR="\$HOME\/\.tmux";/);
  assert.match(pane.out, /Disconnected from sandbox sbx_.*still running/);
  assert.match(pane.out, /pauses the sandbox; memory and processes are kept/);
  assert.equal(mapping.status, "detached");
});

test("forwardEnv [] sends no keys, and only set variables are ever sent", () => {
  const w = world({ config: { forwardEnv: [] } });
  const { mapping } = startSandbox(w);
  assert.ok(!fs.existsSync(path.join(sandboxHome(w, mapping.sandboxId), ".herdr-prized.env")));
  const w2 = world({ config: { agent: "codex" } });
  const { mapping: m2 } = startSandbox(w2); // ANTHROPIC_API_KEY is set, codex's OPENAI_API_KEY is not
  assert.ok(!fs.existsSync(path.join(sandboxHome(w2, m2.sandboxId), ".herdr-prized.env")));
});

test("closing the agent pane pauses the sandbox, and only that pane does", () => {
  const w = world();
  const { mapping } = startSandbox(w);
  const other = run(w, "on-pane-closed.mjs", { env: { HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ event: "pane.closed", data: { pane_id: "w1:p9" } }) } });
  assert.equal(other.status, 0, other.out);
  assert.equal(db(w).sandboxes[mapping.sandboxId].state, "running");

  const closed = run(w, "on-pane-closed.mjs", { env: { HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ event: "pane.closed", data: { pane_id: "w1:p2" } }) } });
  assert.equal(closed.status, 0, closed.out);
  assert.equal(db(w).sandboxes[mapping.sandboxId].state, "paused");
  assert.ok(prizedCalls(w).some((c) => c.args.join(" ") === `sandbox pause ${mapping.sandboxId}`));
  assert.ok(!prizedCalls(w).some((c) => c.args[1] === "rm"), "never deleted on close");
  const after = state(w).mappings[mapping.id];
  assert.equal(after.status, "paused");
  assert.equal(after.paneId, null);
  assert.ok(herdrCalls(w).some((l) => l.startsWith("notification show Prized --body Paused")));
});

test("the hook also reads the closed pane from the invocation context", () => {
  const w = world();
  const { mapping } = startSandbox(w);
  run(w, "on-pane-closed.mjs", { env: { HERDR_PLUGIN_CONTEXT_JSON: JSON.stringify({ focused_pane_id: "w1:p2" }) } });
  assert.equal(db(w).sandboxes[mapping.sandboxId].state, "paused");
});

test("onClose keep leaves the sandbox running", () => {
  const w = world({ config: { onClose: "keep" } });
  const { mapping, pane } = startSandbox(w);
  assert.match(pane.out, /close this pane \(keeps running\)/);
  run(w, "on-pane-closed.mjs", { env: { HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ data: { pane_id: "w1:p2" } }) } });
  assert.equal(db(w).sandboxes[mapping.sandboxId].state, "running");
});

test("a pane closed while the sandbox was being created still pauses it, found by its label", () => {
  const w = world();
  action(w, "start-sandbox", ctx(w));
  const [m] = Object.values(state(w).mappings);
  const pane = run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: m.id, HERDR_PANE_ID: "w1:p2", FAKE_CREATE_THEN_DIE: "1" }, input: "\n" });
  assert.equal(pane.status, 1);
  assert.equal(state(w).mappings[m.id].sandboxId, null);
  run(w, "on-pane-closed.mjs", { env: { HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ data: { pane_id: "w1:p2" } }) } });
  const [sbx] = Object.values(db(w).sandboxes);
  assert.equal(sbx.state, "paused");
  assert.equal(state(w).mappings[m.id].sandboxId, sbx.id);
});

test("reconnect resumes the paused sandbox and lands in the same session", () => {
  const w = world();
  const { mapping } = startSandbox(w);
  run(w, "on-pane-closed.mjs", { env: { HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ data: { pane_id: "w1:p2" } }) } });
  const r = action(w, "reconnect", { focused_pane_cwd: w.repo, focused_pane_id: "w1:p7" });
  assert.equal(r.status, 0, r.out);
  assert.ok(herdrCalls(w).some((l) => l.includes("--entrypoint agent") && l.includes(`PRIZED_HERDR_MAPPING=${mapping.id}`)));
  const before = prizedCalls(w).length;
  const pane = run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: mapping.id, HERDR_PANE_ID: "w1:p8" }, input: "\n" });
  assert.equal(pane.status, 0, pane.out);
  assert.match(pane.out, /Resuming sbx_.*memory and processes pick up/);
  const calls = prizedCalls(w).slice(before).map((c) => c.args[1]);
  assert.ok(!calls.includes("create") && !calls.includes("cp"), "no second sandbox, no second copy");
  assert.equal(db(w).sandboxes[mapping.sandboxId].resumes, 1);
  assert.equal(state(w).mappings[mapping.id].paneId, "w1:p8");
  const tmux = fs.readFileSync(path.join(sandboxHome(w, mapping.sandboxId), ".tmux-calls"), "utf8");
  assert.equal(tmux.match(/new-session/g).length, 1, "the existing session is reused");
  const bashrc = fs.readFileSync(path.join(sandboxHome(w, mapping.sandboxId), ".bashrc"), "utf8");
  assert.equal(bashrc.match(/# herdr-prized/g).length, 1, "the ~/.bashrc lines are written once");
});

test("r reconnects from the menu, and p pauses before closing", () => {
  const w = world();
  action(w, "start-sandbox", ctx(w));
  const [m] = Object.values(state(w).mappings);
  const pane = run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: m.id, HERDR_PANE_ID: "w1:p2" }, input: "r\np\n" });
  assert.equal(pane.status, 0, pane.out);
  const id = state(w).mappings[m.id].sandboxId;
  assert.equal(db(w).sandboxes[id].attaches, 2);
  assert.equal(db(w).sandboxes[id].state, "paused");
});

test("a failed attach explains that a sandbox shell needs prized login", () => {
  const w = world();
  const { pane } = startSandbox(w, { FAKE_SSH_EXIT: "8" });
  assert.match(pane.out, /connection to sandbox .* ended \(exit 8\)/);
  assert.match(pane.out, /prized login/);
});

test("apply-changes brings the agent's work back after a yes, and only once", () => {
  const w = world();
  const { mapping } = startSandbox(w);
  const work = path.join(sandboxHome(w, mapping.sandboxId), "work", "my-repo");
  fs.writeFileSync(path.join(work, "src", "app.js"), "export const a = 2;\n");
  fs.writeFileSync(path.join(work, "src", "new.js"), "export const b = 1;\n");
  fs.writeFileSync(path.join(work, "logo.bin"), Buffer.from([0, 1, 2, 255, 254]));
  git(work, "add", "-A");
  git(work, "commit", "-qm", "the agent committed some of it");
  fs.rmSync(path.join(work, "notes.txt"));

  const opened = action(w, "apply-changes", { focused_pane_id: "w1:p2", focused_pane_cwd: w.repo });
  assert.equal(opened.status, 0, opened.out);
  assert.ok(herdrCalls(w).some((l) => l.includes("--entrypoint ops --placement popup") && l.includes("PRIZED_HERDR_OP=apply-changes")));

  const no = run(w, "ops-pane.mjs", { env: { PRIZED_HERDR_MAPPING: mapping.id, PRIZED_HERDR_OP: "apply-changes" }, input: "n\n\n" });
  assert.match(no.out, /src\/app\.js/);
  assert.match(no.out, /Nothing changed/);
  assert.equal(fs.readFileSync(path.join(w.repo, "src", "app.js"), "utf8"), "export const a = 1;\n");

  const yes = run(w, "ops-pane.mjs", { env: { PRIZED_HERDR_MAPPING: mapping.id, PRIZED_HERDR_OP: "apply-changes" }, input: "y\n\n" });
  assert.equal(yes.status, 0, yes.out);
  assert.match(yes.out, /Applied/);
  assert.equal(fs.readFileSync(path.join(w.repo, "src", "app.js"), "utf8"), "export const a = 2;\n");
  assert.equal(fs.readFileSync(path.join(w.repo, "src", "new.js"), "utf8"), "export const b = 1;\n");
  assert.deepEqual([...fs.readFileSync(path.join(w.repo, "logo.bin"))], [0, 1, 2, 255, 254]);
  assert.ok(!fs.existsSync(path.join(w.repo, "notes.txt")));
  assert.ok(fs.existsSync(path.join(w.repo, ".env")), "local files the copy left out are untouched");

  const again = run(w, "ops-pane.mjs", { env: { PRIZED_HERDR_MAPPING: mapping.id, PRIZED_HERDR_OP: "apply-changes" }, input: "\n" });
  assert.match(again.out, /No changes\./);
});

test("apply-changes changes nothing when the patch does not apply", () => {
  const w = world();
  const { mapping } = startSandbox(w);
  const work = path.join(sandboxHome(w, mapping.sandboxId), "work", "my-repo");
  fs.writeFileSync(path.join(work, "README.md"), "remote edit\n");
  fs.writeFileSync(path.join(w.repo, "README.md"), "local edit\n");
  const r = run(w, "ops-pane.mjs", { env: { PRIZED_HERDR_MAPPING: mapping.id, PRIZED_HERDR_OP: "apply-changes" }, input: "y\n\n" });
  assert.match(r.out, /do not apply cleanly/);
  assert.equal(fs.readFileSync(path.join(w.repo, "README.md"), "utf8"), "local edit\n");
});

test("delete needs the word DELETE, then removes the sandbox and the mapping", () => {
  const w = world();
  const { mapping } = startSandbox(w);
  const env = { PRIZED_HERDR_MAPPING: mapping.id, PRIZED_HERDR_OP: "delete" };
  const no = run(w, "ops-pane.mjs", { env, input: "delete\n\n" });
  assert.match(no.out, /Nothing changed/);
  assert.equal(db(w).sandboxes[mapping.sandboxId].state, "running");
  const yes = run(w, "ops-pane.mjs", { env, input: "DELETE\n\n" });
  assert.match(yes.out, /Deleted\./);
  assert.equal(db(w).sandboxes[mapping.sandboxId].state, "deleted");
  assert.equal(state(w).mappings[mapping.id], undefined);
});

test("pause action pauses the pane's sandbox; a sandbox deleted elsewhere is reported on reconnect", () => {
  const w = world();
  const { mapping } = startSandbox(w);
  // Invoked from the agent pane, which herdr runs from the plugin's folder.
  const r = action(w, "pause", { focused_pane_id: "w1:p2", focused_pane_cwd: PLUGIN }, { HERDR_PLUGIN_ROOT: PLUGIN });
  assert.equal(r.status, 0, r.out);
  assert.equal(db(w).sandboxes[mapping.sandboxId].state, "paused");
  const d = db(w);
  d.sandboxes[mapping.sandboxId].state = "deleted";
  fs.writeFileSync(path.join(w.prizedDir, "db.json"), JSON.stringify(d));
  const pane = run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: mapping.id }, input: "\n" });
  assert.equal(pane.status, 1);
  assert.match(pane.out, /is deleted/);
});

test("on a box: pick the box, stream the copy over exec, attach with prized ssh, and never pause it", () => {
  const boxes = [
    { hostname: "kiwi", observedState: "running", tier: "nano" },
    { hostname: "mango", observedState: "running", tier: "flow" },
  ];
  const w = world();
  const r = action(w, "start-box", ctx(w), { FAKE_BOXES: JSON.stringify(boxes) });
  assert.equal(r.status, 0, r.out);
  const [m] = Object.values(state(w).mappings);
  assert.equal(m.box, null);
  const pane = run(w, "agent-pane.mjs", {
    env: { PRIZED_HERDR_MAPPING: m.id, HERDR_PANE_ID: "w1:p3", FAKE_BOXES: JSON.stringify(boxes), ANTHROPIC_API_KEY: FAKE_AGENT_KEY },
    input: "2\n\n",
  });
  assert.equal(pane.status, 0, pane.out);
  const mapping = state(w).mappings[m.id];
  assert.equal(mapping.box, "mango");
  const work = path.join(boxHome(w, "mango"), "herdr", mapping.dirName);
  assert.equal(fs.readFileSync(path.join(work, "README.md"), "utf8"), "hello\n");
  assert.ok(!fs.existsSync(path.join(work, ".env")));
  assert.ok(!fs.existsSync(path.join(boxHome(w, "mango"), ".herdr-prized.env")), "no keys are sent to a box");
  const calls = prizedCalls(w);
  assert.ok(calls.some((c) => c.args[0] === "exec" && c.args[1] === "mango" && c.stdinBytes > 0));
  assert.deepEqual(calls.at(-1).args, ["ssh", "mango", "-s", mapping.session]);
  run(w, "on-pane-closed.mjs", { env: { HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ data: { pane_id: "w1:p3" } }) } });
  assert.ok(!prizedCalls(w).some((c) => c.args.includes("pause")));
  assert.ok(!everythingWritten(w, pane.out).includes(FAKE_AGENT_KEY));
});

test("add-machine wakes the box, links the agents onto the path and runs herdr machine add", () => {
  const boxes = [{ hostname: "mango", observedState: "paused", tier: "flow" }];
  const w = world();
  const r = action(w, "add-machine", {}, {});
  assert.equal(r.status, 0, r.out);
  assert.ok(herdrCalls(w)[0].includes("--entrypoint machine --placement popup"));
  const pane = run(w, "machine-pane.mjs", { env: { FAKE_BOXES: JSON.stringify(boxes) }, input: "\n\n\n" });
  assert.equal(pane.status, 0, pane.out);
  assert.ok(prizedCalls(w).some((c) => c.args.join(" ") === "box resume mango --wait"));
  const ssh = fs.readFileSync(w.sshLog, "utf8");
  assert.match(ssh, /BatchMode=yes .* mango true/);
  assert.match(ssh, /sudo -n ln -s/);
  assert.ok(herdrCalls(w).includes("machine add mango --label mango"));
  assert.match(pane.out, /mango is in the sidebar/);
});

test("add-machine stops before herdr when plain ssh cannot reach the box", () => {
  const w = world();
  const pane = run(w, "machine-pane.mjs", { env: { FAKE_SSH_STATUS: "255" }, input: "\n" });
  assert.equal(pane.status, 1);
  assert.match(pane.out, /prized init --box mango/);
  assert.ok(!herdrCalls(w).some((l) => l.startsWith("machine add")));
});

test("missing CLI and a signed-out CLI get hints a person can act on", () => {
  const w = world();
  action(w, "start-sandbox", ctx(w));
  const [m] = Object.values(state(w).mappings);
  const missing = run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: m.id, PRIZED_BIN: path.join(w.dir, "no-such-prized") }, input: "\n" });
  assert.match(missing.out, /prized CLI is not installed/);
  assert.match(missing.out, /prized\.dev\/docs\/quickstart\?via=herdr/);
  const signedOut = run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: m.id, FAKE_PRIZED_EXIT: "8" }, input: "\n" });
  assert.match(signedOut.out, /not signed in/);
  assert.match(signedOut.out, /prized login/);
});

test("an action outside a Git worktree says so and notifies", () => {
  const w = world();
  const r = action(w, "start-sandbox", { focused_pane_cwd: w.home });
  assert.equal(r.status, 1);
  assert.match(r.out, /not inside a Git worktree/);
  assert.ok(herdrCalls(w).some((l) => l.startsWith("notification show Prized")));
});

// ------------------------------------------------------------------ fixes from the first review

test("a copy cut short is finished on reconnect, even after the sandbox was paused", () => {
  const w = world();
  action(w, "start-sandbox", ctx(w));
  const [m] = Object.values(state(w).mappings);
  const first = run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: m.id, HERDR_PANE_ID: "w1:p2", FAKE_FAIL_SCRIPT: "tar xzf" }, input: "\n" });
  assert.equal(first.status, 1);
  run(w, "on-pane-closed.mjs", { env: { HERDR_PLUGIN_EVENT_JSON: JSON.stringify({ data: { pane_id: "w1:p2" } }) } });
  assert.equal(state(w).mappings[m.id].status, "paused");
  assert.notEqual(state(w).mappings[m.id].copied, true);
  const again = run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: m.id, HERDR_PANE_ID: "w1:p3" }, input: "\n" });
  assert.equal(again.status, 0, again.out);
  const id = state(w).mappings[m.id].sandboxId;
  assert.ok(fs.existsSync(path.join(sandboxHome(w, id), "work", "my-repo", "README.md")));
  assert.equal(state(w).mappings[m.id].copied, true);
});

test("a file tracked despite .gitignore is in the base, so the agent's edit to it comes back", () => {
  const w = world();
  fs.writeFileSync(path.join(w.repo, "dist", "keep.js"), "keep 1\n");
  git(w.repo, "add", "-f", "dist/keep.js");
  git(w.repo, "commit", "-qm", "keep");
  const { mapping } = startSandbox(w);
  const work = path.join(sandboxHome(w, mapping.sandboxId), "work", "my-repo");
  assert.ok(!fs.existsSync(path.join(work, "dist", "bundle.js")), "an ignored, untracked file is still left out");
  fs.writeFileSync(path.join(work, "dist", "keep.js"), "keep 2\n");
  const r = run(w, "ops-pane.mjs", { env: { PRIZED_HERDR_MAPPING: mapping.id, PRIZED_HERDR_OP: "apply-changes" }, input: "y\n\n" });
  assert.match(r.out, /Applied/);
  assert.equal(fs.readFileSync(path.join(w.repo, "dist", "keep.js"), "utf8"), "keep 2\n");
});

test("apply-changes leaves the agent's own index alone", () => {
  const w = world();
  const { mapping } = startSandbox(w);
  const work = path.join(sandboxHome(w, mapping.sandboxId), "work", "my-repo");
  fs.writeFileSync(path.join(work, "scratch.txt"), "agent's scratch\n");
  run(w, "ops-pane.mjs", { env: { PRIZED_HERDR_MAPPING: mapping.id, PRIZED_HERDR_OP: "apply-changes" }, input: "n\n\n" });
  assert.equal(git(work, "diff", "--cached", "--name-only"), "");
  assert.match(git(work, "status", "--porcelain"), /^\?\? scratch\.txt/m);
});

test("if the remote base cannot be moved after a local apply, the next apply finishes it and offers nothing twice", () => {
  const w = world();
  const { mapping } = startSandbox(w);
  const work = path.join(sandboxHome(w, mapping.sandboxId), "work", "my-repo");
  fs.writeFileSync(path.join(work, "src", "app.js"), "export const a = 3;\n");
  const env = { PRIZED_HERDR_MAPPING: mapping.id, PRIZED_HERDR_OP: "apply-changes" };
  const failed = run(w, "ops-pane.mjs", { env: { ...env, FAKE_FAIL_SCRIPT: "commit-tree" }, input: "y\n\n" });
  assert.match(failed.out, /Applied to this worktree/);
  assert.match(failed.out, /the changes are in this worktree, but sandbox .* could not be told so/);
  assert.equal(fs.readFileSync(path.join(w.repo, "src", "app.js"), "utf8"), "export const a = 3;\n");
  assert.ok(state(w).mappings[mapping.id].pendingTree);
  const next = run(w, "ops-pane.mjs", { env, input: "y\n\n" });
  assert.match(next.out, /Finishing the last apply/);
  assert.match(next.out, /No changes\./);
  assert.equal(state(w).mappings[mapping.id].pendingTree, null);
});

test("delete on a box kills the session with -y, and keeps the mapping when that fails", () => {
  const w = world({ config: { box: "mango" } });
  action(w, "start-box", ctx(w));
  const [m] = Object.values(state(w).mappings);
  run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: m.id, HERDR_PANE_ID: "w1:p3" }, input: "\n" });
  const env = { PRIZED_HERDR_MAPPING: m.id, PRIZED_HERDR_OP: "delete" };
  const broken = run(w, "ops-pane.mjs", { env: { ...env, FAKE_PRIZED_EXIT: "3" }, input: "y\n\n" });
  assert.equal(broken.status, 1);
  assert.ok(state(w).mappings[m.id], "a failed kill keeps the mapping");
  const ok = run(w, "ops-pane.mjs", { env, input: "y\n\n" });
  assert.match(ok.out, /Stopped\./);
  assert.ok(prizedCalls(w).some((c) => c.args.join(" ") === `ssh mango --kill ${m.session} -y`));
  assert.equal(state(w).mappings[m.id], undefined);
});

test("delete finds a sandbox whose create was cut short by its label", () => {
  const w = world();
  action(w, "start-sandbox", ctx(w));
  const [m] = Object.values(state(w).mappings);
  run(w, "agent-pane.mjs", { env: { PRIZED_HERDR_MAPPING: m.id, FAKE_CREATE_THEN_DIE: "1" }, input: "\n" });
  const [sbx] = Object.values(db(w).sandboxes);
  const r = run(w, "ops-pane.mjs", { env: { PRIZED_HERDR_MAPPING: m.id, PRIZED_HERDR_OP: "delete" }, input: "DELETE\n\n" });
  assert.match(r.out, new RegExp(`deletes sandbox ${sbx.id}`));
  assert.equal(db(w).sandboxes[sbx.id].state, "deleted");
});

test("a custom command keeps the agent's key and herdr's agent hint", () => {
  const w = world({ config: { command: "claude --dangerously-skip-permissions" } });
  const { mapping } = startSandbox(w);
  assert.ok(herdrCalls(w)[0].includes("--env HERDR_AGENT=claude"));
  assert.ok(fs.existsSync(path.join(sandboxHome(w, mapping.sandboxId), ".herdr-prized.env")));
  const tmux = fs.readFileSync(path.join(sandboxHome(w, mapping.sandboxId), ".tmux-calls"), "utf8");
  assert.match(tmux, /; claude --dangerously-skip-permissions; exec bash -l/);
});

test("a recorded pane id from another worktree is not trusted, and agent panes are", () => {
  const w = world();
  const { mapping } = startSandbox(w); // paneId w1:p2, worktree w.repo
  const other = path.join(w.dir, "other");
  git(w.dir, "init", "-q", other);
  // herdr reused w1:p2 for a pane in another repository: the action must not reach this sandbox.
  const r = action(w, "pause", { focused_pane_id: "w1:p2", focused_pane_cwd: other });
  assert.equal(r.status, 1);
  assert.match(r.out, /no Prized sandbox or box session for this pane or worktree/);
  assert.equal(db(w).sandboxes[mapping.sandboxId].state, "running");
  // The agent pane itself runs from the plugin's folder: there the pane id decides.
  const fromPane = action(w, "pause", { focused_pane_id: "w1:p2", focused_pane_cwd: PLUGIN }, { HERDR_PLUGIN_ROOT: PLUGIN });
  assert.equal(fromPane.status, 0, fromPane.out);
  assert.equal(db(w).sandboxes[mapping.sandboxId].state, "paused");
  // And start-sandbox from an agent pane refuses to copy the plugin's own folder.
  const start = action(w, "start-sandbox", { focused_pane_id: "w1:p2", focused_pane_cwd: PLUGIN }, { HERDR_PLUGIN_ROOT: PLUGIN });
  assert.equal(start.status, 1);
  assert.match(start.out, /plugin's own/);
});
