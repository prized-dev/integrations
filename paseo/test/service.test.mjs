// The daemon side end to end against an in-memory Prized API (test/fake-prized.mjs): settings, create
// (sandbox, install, daemon, pairing), stop, resume, retry, delete, and what never leaks.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { installScript, pairScript, parsePairingUrl, startDaemonScript } from "../server/bootstrap.ts";
import { PrizedApi, PrizedApiError } from "../server/prized-api.ts";
import { HostService } from "../server/service.ts";
import { AGENT_KEY, FAKE_KEY, fakePrized, pairingUrl } from "./fake-prized.mjs";

const dirs = [];
process.on("exit", () => dirs.forEach((d) => fs.rmSync(d, { recursive: true, force: true })));

function setup({ fail, env = {} } = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "paseo-prized-test-"));
  dirs.push(stateDir);
  const prized = fakePrized({ fail });
  const logs = [];
  const service = new HostService({
    stateDir,
    env,
    api: (key) => new PrizedApi(key, { fetch: prized.fetch, sleep: async () => {} }),
    log: (line) => logs.push(line),
  });
  return { service, prized, stateDir, logs };
}

function fakeHosts() {
  const calls = [];
  return {
    calls,
    async add(input) {
      calls.push(["add", input]);
      return { registrationId: "reg_1", status: "connecting" };
    },
    async remove(id) {
      calls.push(["remove", id]);
    },
  };
}

async function readyHost(t = setup()) {
  await t.service.saveSettings({ prizedApiKey: FAKE_KEY, anthropicApiKey: AGENT_KEY });
  const { hostId } = await t.service.create("demo");
  await t.service.settled();
  return { ...t, hostId };
}

function everything(stateDir, ...extra) {
  return [...fs.readdirSync(stateDir).filter((f) => f !== "secrets.json").map((f) => fs.readFileSync(path.join(stateDir, f), "utf8")), ...extra].join("\n");
}

test("settings: the key is checked, kept private, and never read back", async () => {
  const t = setup();
  await assert.rejects(t.service.saveSettings({ prizedApiKey: "nope" }), /not a Prized API key/);
  await assert.rejects(t.service.saveSettings({ prizedApiKey: `dcp_${"b".repeat(48)}` }), /did not accept/);
  const saved = await t.service.saveSettings({ prizedApiKey: FAKE_KEY, anthropicApiKey: AGENT_KEY, vcpu: 4, memoryGb: 8 });
  assert.equal(saved.workspace, "acme");
  const status = await t.service.status(false);
  assert.deepEqual(status.settings, { apiKeySource: "saved", workspace: "acme", anthropicKeySaved: true, openaiKeySaved: false, vcpu: 4, memoryGb: 8, maxRuntimeHours: 24 });
  const secrets = path.join(t.stateDir, "secrets.json");
  assert.equal(fs.statSync(secrets).mode & 0o777, 0o600);
  assert.equal(fs.statSync(t.stateDir).mode & 0o777, 0o700);
  assert.ok(!JSON.stringify(status).includes(FAKE_KEY) && !JSON.stringify(status).includes(AGENT_KEY));
});

test("PRIZED_API_KEY from the daemon's environment works with nothing saved", async () => {
  const t = setup({ env: { PRIZED_API_KEY: FAKE_KEY } });
  assert.equal((await t.service.status(false)).settings.apiKeySource, "environment");
  const { hostId } = await t.service.create();
  await t.service.settled();
  assert.equal((await t.service.status(false)).hosts.find((h) => h.id === hostId).phase, "ready");
});

test("create: one sandbox, Paseo installed, the daemon started and paired, the link kept private", async () => {
  const t = await readyHost();
  const { hosts } = await t.service.status(false);
  const host = hosts.find((h) => h.id === t.hostId);
  assert.equal(host.phase, "ready");
  assert.equal(host.paired, true);
  assert.equal(host.label, "demo");
  const [sandbox] = t.prized.sandboxes.values();
  assert.deepEqual(sandbox.request.resources, { vcpu: 2, memoryMb: 4096 });
  assert.deepEqual(sandbox.request.labels, { via: "paseo", "paseo-host": t.hostId });
  assert.equal(sandbox.request.template, "prized-agent-v1");
  assert.equal(sandbox.request.maxRuntimeMin, 24 * 60);
  assert.deepEqual(sandbox.request.envVars, { ANTHROPIC_API_KEY: AGENT_KEY });
  const create = t.prized.requests.find((r) => r.method === "POST" && r.path.startsWith("/api/v1/sandboxes"));
  assert.equal(create.headers["Idempotency-Key"], `paseo-host-${t.hostId}`);
  assert.equal(sandbox.commands.length, 3);
  assert.match(sandbox.commands[0], /npm install -g .*@getpaseo\/cli@0\.14\.0/);
  assert.match(sandbox.commands[1], /paseo daemon start/);
  assert.match(sandbox.commands[2], /paseo daemon pair --relay --json/);
  // The pairing link opens the daemon: only an explicit reveal hands it out.
  const all = everything(t.stateDir, JSON.stringify(await t.service.status(true)), t.logs.join("\n"));
  assert.ok(!all.includes("#offer="), "the pairing link leaked into status or a non-secret file");
  assert.ok(!all.includes(FAKE_KEY) && !all.includes(AGENT_KEY), "a key leaked");
  assert.equal((await t.service.revealPairing(t.hostId)).url, pairingUrl());
  // No command carries a key: agent keys travel as the sandbox's environment variables.
  assert.ok(sandbox.commands.every((c) => !c.includes(AGENT_KEY) && !c.includes(FAKE_KEY)));
});

test("stop pauses (never deletes), resume brings the same sandbox back with its daemon", async () => {
  const t = await readyHost();
  const [sandbox] = t.prized.sandboxes.values();
  await t.service.act({ hostId: t.hostId, action: "stop" });
  await t.service.settled();
  assert.equal(sandbox.state, "paused");
  assert.equal((await t.service.status(false)).hosts[0].phase, "paused");
  await assert.rejects(t.service.act({ hostId: t.hostId, action: "stop" }), /Only a ready host/);
  await t.service.act({ hostId: t.hostId, action: "resume" });
  await t.service.settled();
  assert.equal(sandbox.state, "running");
  assert.equal(sandbox.resumes, 1);
  assert.equal(sandbox.starts, 2, "resume checks the daemon (and starts it only if it is gone)");
  assert.equal(t.prized.sandboxes.size, 1);
  assert.equal((await t.service.status(false)).hosts[0].phase, "ready");
});

test("a failed install shows why, and Retry finishes on the same sandbox", async () => {
  const t = setup({ fail: { install: true } });
  await t.service.saveSettings({ prizedApiKey: FAKE_KEY });
  const { hostId } = await t.service.create();
  await t.service.settled();
  const failed = (await t.service.status(false)).hosts[0];
  assert.equal(failed.phase, "failed");
  assert.match(failed.error, /Installing Paseo in the sandbox failed \(exit 1\): npm ERR! network unreachable/);
  t.prized.fail.install = false; // the network is back
  await t.service.act({ hostId, action: "retry" });
  await t.service.settled();
  assert.equal((await t.service.status(false)).hosts[0].phase, "ready");
  assert.equal(t.prized.sandboxes.size, 1, "Retry reuses the sandbox");
  assert.equal([...t.prized.sandboxes.values()][0].installs, 2);
});

test("a create refused by Prized fails with Prized's reason, and Retry creates it once the cause is fixed", async () => {
  const t = setup({ fail: { create: true } });
  await t.service.saveSettings({ prizedApiKey: FAKE_KEY });
  const { hostId } = await t.service.create();
  await t.service.settled();
  const failed = (await t.service.status(false)).hosts[0];
  assert.equal(failed.phase, "failed");
  assert.equal(failed.error, "The balance is empty.");
  t.prized.fail.create = false;
  await t.service.act({ hostId, action: "retry" });
  await t.service.settled();
  assert.equal((await t.service.status(false)).hosts[0].phase, "ready");
  // A retry sends the same idempotency key, so a create whose answer was lost is found, not doubled.
  const keys = t.prized.requests.filter((r) => r.method === "POST" && r.path.startsWith("/api/v1/sandboxes")).map((r) => r.headers["Idempotency-Key"]);
  assert.deepEqual(keys, [`paseo-host-${hostId}`, `paseo-host-${hostId}`]);
});

test("one operation at a time per host", async () => {
  const t = await readyHost();
  await t.service.act({ hostId: t.hostId, action: "stop" });
  await assert.rejects(t.service.act({ hostId: t.hostId, action: "stop" }), /already running|Only a ready host/);
  await t.service.settled();
});

test("delete needs the sandbox id typed, removes the sandbox, the link and Paseo's saved host", async () => {
  const t = await readyHost();
  const hosts = fakeHosts();
  await t.service.addHost(t.hostId, hosts);
  assert.deepEqual(hosts.calls[0], ["add", { connection: pairingUrl(), label: "demo", metadata: { prizedSandbox: [...t.prized.sandboxes.keys()][0] }, sourceKey: `prized-sandbox-${t.hostId}` }]);
  assert.equal((await t.service.status(false)).hosts[0].addedAsHost, true);
  const [sandbox] = t.prized.sandboxes.values();
  await assert.rejects(t.service.act({ hostId: t.hostId, action: "delete", confirm: "yes" }, hosts), new RegExp(`Type ${sandbox.id}`));
  assert.equal(sandbox.state, "running");
  await t.service.act({ hostId: t.hostId, action: "delete", confirm: sandbox.id }, hosts);
  await t.service.settled();
  assert.equal(sandbox.state, "deleted");
  assert.deepEqual(hosts.calls[1], ["remove", "reg_1"]);
  assert.deepEqual((await t.service.status(false)).hosts, []);
  assert.ok(!fs.readFileSync(path.join(t.stateDir, "secrets.json"), "utf8").includes("#offer="));
});

test("a sandbox deleted elsewhere (or at the end of its run time) shows as gone and can be removed", async () => {
  const t = await readyHost();
  const [sandbox] = t.prized.sandboxes.values();
  sandbox.state = "deleted";
  const host = (await t.service.status(true)).hosts[0];
  assert.equal(host.phase, "gone");
  await t.service.act({ hostId: t.hostId, action: "forget" });
  assert.deepEqual((await t.service.status(false)).hosts, []);
});

test("work cut short by a plugin restart is marked failed, with Retry", async () => {
  const t = await readyHost();
  const file = path.join(t.stateDir, "hosts.json");
  const hosts = JSON.parse(fs.readFileSync(file, "utf8"));
  hosts.hosts[t.hostId].phase = "installing";
  fs.writeFileSync(file, JSON.stringify(hosts));
  const restarted = new HostService({ stateDir: t.stateDir, env: {}, api: (key) => new PrizedApi(key, { fetch: t.prized.fetch, sleep: async () => {} }) });
  const host = (await restarted.status(false)).hosts[0];
  assert.equal(host.phase, "failed");
  assert.match(host.error, /Interrupted/);
  await restarted.act({ hostId: t.hostId, action: "retry" });
  await restarted.settled();
  assert.equal((await restarted.status(false)).hosts[0].phase, "ready");
  assert.equal(t.prized.sandboxes.size, 1, "no second sandbox");
});

test("a pairing link that is not a Paseo offer is refused", async () => {
  const t = setup({ fail: { pairBad: true } });
  await t.service.saveSettings({ prizedApiKey: FAKE_KEY });
  await t.service.create();
  await t.service.settled();
  assert.equal((await t.service.status(false)).hosts[0].phase, "failed");
  assert.throws(() => parsePairingUrl('{"url":"http://app.paseo.sh/#offer=e30"}'), /not https/);
  assert.throws(() => parsePairingUrl("not json"), /did not return JSON/);
});

test("no key, no host", async () => {
  const t = setup();
  await assert.rejects(t.service.create(), /Add a Prized API key/);
});

test("API errors never carry the key", async () => {
  const leaky = async () => new Response(JSON.stringify({ error: "bad", message: `echo ${FAKE_KEY}` }), { status: 400 });
  const api = new PrizedApi(FAKE_KEY, { fetch: leaky });
  await assert.rejects(api.me(), (e) => e instanceof PrizedApiError && !e.message.includes(FAKE_KEY));
});

test("the sandbox scripts are valid bash", () => {
  for (const script of [installScript(), startDaemonScript(), pairScript()]) {
    const r = spawnSync("bash", ["-n"], { input: script, encoding: "utf8" });
    assert.equal(r.status, 0, r.stderr);
  }
});
