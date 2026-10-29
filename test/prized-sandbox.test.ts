import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createPrizedSandbox, type StoredPrizedSandbox } from "../src/sandbox/prized-sandbox.ts";
import { sandboxScopeName } from "../src/sandbox/exec-sandbox-base.ts";
import { supportsBlobStaging, supportsProcessSessions, type Sandbox } from "../src/sandbox/sandbox.ts";
import { pollProcess } from "../src/sandbox/process-poll.ts";
import { createLocalWorkspaceStore } from "../src/workspace/workspace-store.ts";
import { createMemoryMap, type DurableMap } from "../src/persistence/durable-map.ts";
import { createMemoryAdvisoryLock } from "../src/persistence/advisory-lock.ts";
import { NonRetryableTurnError } from "../src/core/turn-error.ts";
import { scopeId } from "../src/types.ts";
import { createFakePrized, type FakePrized } from "./support/fake-prized.ts";

let fake: FakePrized;
let store: DurableMap<StoredPrizedSandbox>;
let errors: Array<{ code: string; message: string }>;
const scope = scopeId("personal", "tester");
const layers = [{ scopeId: scope, mountPath: "/", mode: "rw" as const }];
const name = sandboxScopeName("qmt", scope);

function make(extra: Partial<Parameters<typeof createPrizedSandbox>[1]> = {}): Sandbox {
  return createPrizedSandbox(createLocalWorkspaceStore(mkdtempSync(join(tmpdir(), "prized-ws-"))), {
    client: fake.client,
    store,
    advisoryLock: createMemoryAdvisoryLock(),
    namePrefix: "qmt",
    onError: (e) => errors.push({ code: e.code, message: e.message }),
    ...extra,
  });
}

beforeEach(() => {
  fake = createFakePrized();
  store = createMemoryMap();
  errors = [];
});
afterEach(() => fake.cleanup());

const opsOn = (id: string): string[] => fake.calls.filter((c) => c.id === id).map((c) => c.op);

test("provision creates one labelled computer per scope and runs commands with env in the workspace", async () => {
  const sandbox = make({ labels: { team: "growth" }, keepWarmSec: 1800 });
  const h = await sandbox.provision(layers, { env: { MY_VAR: "v1" } });
  assert.equal(h.coldStart, true);
  assert.equal(h.rootDir, "/home/prized/workspace");
  const [computer] = fake.named(name);
  assert.ok(computer);
  assert.deepEqual(computer.labels, { team: "growth", "qm-deployment": "qmt", "qm-name": name, "qm-kind": "computer" });
  assert.equal(computer.autoPauseMin, 30);
  assert.equal((await store.get(scope))?.sandboxId, computer.id);
  const r = await sandbox.run(h, "pwd; echo VAR=$MY_VAR");
  assert.equal(r.code, 0);
  assert.match(r.stdout, /workspace/);
  assert.match(r.stdout, /VAR=v1/);
});

test("streams, exit codes and timeouts are exact", async () => {
  const sandbox = make();
  const h = await sandbox.provision(layers);
  const r = await sandbox.run(h, "echo out; echo err >&2; exit 3");
  assert.equal(r.code, 3);
  assert.equal(r.stdout.trim(), "out");
  assert.equal(r.stderr.trim(), "err");
  const slow = await sandbox.run(h, "sleep 5", { timeoutMs: 1000 });
  assert.equal(slow.timedOut, true);
  assert.equal(slow.code, 124);
});

test("file roundtrip incl. large binary, empty and missing files", async () => {
  const sandbox = make();
  const h = await sandbox.provision(layers);
  await sandbox.writeFile(h, "a/b.txt", "hello\n");
  assert.equal(await sandbox.readFile(h, "a/b.txt"), "hello\n");
  assert.equal(await sandbox.readFile(h, "nope.txt"), null);
  const big = Buffer.alloc(1300 * 1024);
  for (let i = 0; i < big.length; i++) big[i] = (i * 13) % 256;
  await sandbox.writeFileBytes(h, "big.bin", big);
  const back = await sandbox.readFileBytes(h, "big.bin");
  assert.ok(back && Buffer.from(back).equals(big));
  await sandbox.writeFileBytes(h, "empty.bin", Buffer.alloc(0));
  assert.equal((await sandbox.readFileBytes(h, "empty.bin"))?.length, 0);
  const listed = await sandbox.listDir(h, "a");
  assert.deepEqual(listed, ["a/b.txt"]);
});

test("process sessions work end to end", async () => {
  const sandbox = make();
  assert.ok(supportsProcessSessions(sandbox));
  if (!supportsProcessSessions(sandbox)) return;
  const h = await sandbox.provision(layers);
  const { processId } = await sandbox.startProcess(h, "echo one; echo two");
  const { output, status } = await pollProcess(sandbox, h, processId, { deadlineMs: 5_000, waitMs: 100 });
  assert.equal(status.state, "exited");
  assert.match(output, /one/);
  assert.match(output, /two/);
});

test("teardown pauses the computer and the next turn resumes the same one with its files", async () => {
  const sandbox = make();
  const first = await sandbox.provision(layers);
  await sandbox.writeFile(first, "notes.txt", "kept");
  const [computer] = fake.named(name);
  await sandbox.teardown(first);
  assert.equal(computer!.state, "paused");
  const second = await sandbox.provision(layers);
  assert.equal(second.coldStart, false);
  assert.equal(computer!.state, "running");
  assert.equal(fake.named(name).length, 1);
  assert.equal(await sandbox.readFile(second, "notes.txt"), "kept");
  assert.deepEqual(
    opsOn(computer!.id).filter((op) => op === "pause" || op === "resume"),
    ["pause", "resume"],
  );
});

test("keepWarm teardown keeps the computer running and counts as activity", async () => {
  const sandbox = make();
  const h = await sandbox.provision(layers);
  const [computer] = fake.named(name);
  await sandbox.teardown(h, { keepWarm: true });
  assert.equal(computer!.state, "running");
  assert.ok(opsOn(computer!.id).includes("keepAlive"));
  assert.ok(!opsOn(computer!.id).includes("pause"));
});

test("a failed pause at teardown is reported, not thrown, and leaves the computer to its idle pause", async () => {
  const sandbox = make();
  const h = await sandbox.provision(layers);
  const [computer] = fake.named(name);
  computer!.state = "deleted";
  await sandbox.teardown(h);
  assert.deepEqual(
    errors.map((e) => e.code),
    ["pause_failed"],
  );
});

test("a second core adopts the computer by its labels instead of creating another", async () => {
  const a = make();
  await a.provision(layers);
  const b = make({ store: createMemoryMap() });
  const h = await b.provision(layers);
  assert.equal(h.coldStart, false);
  assert.equal(fake.named(name).length, 1);
  assert.equal(fake.calls.filter((c) => c.op === "create").length, 1);
});

test("a computer deleted out of band is refused, not silently replaced, until the scope is retired", async () => {
  const sandbox = make();
  await sandbox.provision(layers);
  const [lost] = fake.named(name);
  await fake.client.delete(lost!.id);
  await assert.rejects(
    sandbox.provision(layers),
    (e: unknown) => e instanceof NonRetryableTurnError && /no longer exists.*Retire this sandbox/.test(e.message),
  );
  assert.deepEqual(fake.named(name), []);
  assert.deepEqual(
    errors.map((e) => e.code),
    ["computer_lost"],
  );
  await sandbox.destroyScope!(scope);
  assert.equal((await sandbox.provision(layers)).coldStart, true);
  assert.equal(fake.named(name).length, 1);
});

test("a computer in error is refused and kept, since it may recover", async () => {
  const sandbox = make();
  await sandbox.provision(layers);
  const [broken] = fake.named(name);
  broken!.state = "error";
  broken!.errorReason = "runtime_degraded";
  await assert.rejects(
    sandbox.provision(layers),
    (e: unknown) =>
      e instanceof NonRetryableTurnError && /is in error \(runtime_degraded\)\. Try again later/.test(e.message),
  );
  assert.deepEqual(
    errors.map((e) => e.code),
    ["computer_error"],
  );
  assert.notEqual(broken!.state, "deleted");
  assert.ok(!opsOn(broken!.id).includes("delete"));
  broken!.state = "running";
  assert.equal((await sandbox.provision(layers)).coldStart, false);
});

test("a second core finds a labelled computer in error and refuses instead of creating a duplicate", async () => {
  await make().provision(layers);
  const [broken] = fake.named(name);
  broken!.state = "error";
  await assert.rejects(make({ store: createMemoryMap() }).provision(layers), /is in error/);
  assert.equal(fake.calls.filter((c) => c.op === "create").length, 1);
});

test("computers ask for the longest run time, since each resume restarts the clock", async () => {
  const sandbox = make();
  await sandbox.provision(layers);
  assert.equal(fake.named(name)[0]!.maxRuntimeMin, 7 * 24 * 60);
  const scratch = await sandbox.provision(layers, { scratch: { key: "job-runtime" } });
  assert.equal(fake.named(scratch.id)[0]!.maxRuntimeMin, undefined);
});

test("a failed provision pauses the computer it woke", async () => {
  const sandbox = make();
  const h = await sandbox.provision(layers);
  await sandbox.teardown(h);
  const [computer] = fake.named(name);
  fake.failCommands("mkdir -p '/home/prized/workspace'");
  await assert.rejects(sandbox.provision(layers), /provision prep failed/);
  assert.equal(computer!.state, "paused");
});

test("a failed provision leaves alone a computer another turn kept running", async () => {
  const sandbox = make();
  const h = await sandbox.provision(layers);
  await sandbox.teardown(h, { keepWarm: true });
  const [computer] = fake.named(name);
  fake.failCommands("mkdir -p '/home/prized/workspace'");
  await assert.rejects(sandbox.provision(layers), /provision prep failed/);
  assert.equal(computer!.state, "running");
  assert.ok(!opsOn(computer!.id).includes("pause"));
});

test("layer tools bound for /usr/local install under ~/.local, since the guest has no root", async () => {
  const sandbox = make({
    layerToolFiles: () => [
      { to: "/usr/local/bin/hello-tool", mode: "0755", content: "#!/bin/sh\necho hello\n" },
      { to: "/usr/local/lib/hello/data.txt", mode: "0644", content: "data\n" },
    ],
  });
  const h = await sandbox.provision(layers);
  const [computer] = fake.named(name);
  const listed = await sandbox.run(
    h,
    "ls -l /home/prized/.local/bin/hello-tool /home/prized/.local/lib/hello/data.txt",
  );
  assert.equal(listed.code, 0, listed.stderr);
  assert.match(listed.stdout, /-rwxr-xr-x.*hello-tool/);
  assert.ok(!fake.commands.some((command) => command.includes("/usr/local/bin/hello-tool")));
  assert.ok(computer);
});

test("a refused create records nothing and the next provision retries", async () => {
  const sandbox = make();
  fake.refuse("create", new NonRetryableTurnError("Prized refused this agent computer: add balance"));
  await assert.rejects(sandbox.provision(layers), (e: unknown) => e instanceof NonRetryableTurnError);
  assert.equal(await store.get(scope), null);
  fake.refuse("create");
  assert.equal((await sandbox.provision(layers)).coldStart, true);
});

test("destroyScope deletes the computer and its record without booting one, and tolerates absence", async () => {
  const sandbox = make();
  await sandbox.destroyScope!(scope);
  assert.equal(fake.calls.filter((c) => c.op === "create").length, 0);
  await sandbox.provision(layers);
  await sandbox.destroyScope!(scope);
  assert.deepEqual(fake.named(name), []);
  assert.equal(await store.get(scope), null);
  await sandbox.destroyScope!(scope);
});

test("a stale adapter deletes another adapter's replacement for the same scope", async () => {
  const a = make({ store: createMemoryMap() });
  const b = make({ store: createMemoryMap() });
  await a.provision(layers);
  await b.destroyScope!(scope);
  await b.provision(layers);
  assert.equal(fake.named(name).length, 1);
  await a.destroyScope!(scope);
  assert.deepEqual(fake.named(name), []);
});

test("scratch computers are disposable and deleted when the last lease ends", async () => {
  const sandbox = make();
  const a = await sandbox.provision(layers, { scratch: { key: "job-1" } });
  const b = await sandbox.provision(layers, { scratch: { key: "job-1" } });
  assert.equal(a.scratch, true);
  assert.equal(a.id, b.id);
  const scratch = fake.named(a.id);
  assert.equal(scratch.length, 1);
  assert.equal(scratch[0]!.labels["qm-kind"], "scratch");
  assert.equal(scratch[0]!.autoDeleteMin, 24 * 60);
  await sandbox.teardown(a);
  assert.equal(fake.named(a.id).length, 1);
  await sandbox.teardown(b);
  assert.deepEqual(fake.named(a.id), []);
});

test("status reports running, paused and missing computers without waking a paused one", async () => {
  const sandbox = make();
  assert.equal((await sandbox.computerStatus!(scope)).provisioned, false);
  const h = await sandbox.provision(layers);
  const running = await sandbox.computerStatus!(scope);
  assert.equal(running.lifecycleState, "running");
  assert.equal(running.guestResponsive, true);
  assert.equal(running.recovery?.strategy, "provider_pause");
  await sandbox.teardown(h);
  const [computer] = fake.named(name);
  const commandsBefore = fake.commands.length;
  const paused = await sandbox.computerStatus!(scope);
  assert.equal(paused.lifecycleState, "paused");
  assert.equal(paused.provisioned, true);
  assert.equal(computer!.state, "paused");
  assert.equal(fake.commands.length, commandsBefore);
  await fake.client.delete(computer!.id);
  assert.equal((await sandbox.computerStatus!(scope)).provisioned, false);
});

test("the profile parks on teardown and reports the shape the computer was given", async () => {
  const sandbox = make({ extraTools: ["kubectl"] });
  assert.equal(sandbox.profile.backend, "prized");
  assert.equal(sandbox.profile.parksOnTeardown, true);
  assert.equal(sandbox.profile.writablePersistence, "provider_managed");
  for (const tool of ["claude", "codex", "gh", "aws", "kubectl"])
    assert.ok(sandbox.profile.spec?.tools?.includes(tool));
  assert.ok(!sandbox.profile.spec?.notInstalled?.includes("kubectl"));
  assert.equal(supportsBlobStaging(sandbox), false);
  assert.equal(sandbox.profile.spec?.cpus, undefined);
  await sandbox.provision(layers);
  assert.equal(sandbox.profile.spec?.cpus, 4);
});

test("gh and the AWS CLI install on a new computer, and once installed are not checked again", async () => {
  const sandbox = make();
  const h = await sandbox.provision(layers);
  const [computer] = fake.named(name);
  assert.deepEqual(fake.toolInstalls, [computer!.id]);
  assert.equal((await store.get(scope))?.toolsReady, true);
  await sandbox.teardown(h);
  await sandbox.provision(layers);
  assert.deepEqual(fake.toolInstalls, [computer!.id]);
  const scratch = await sandbox.provision(layers, { scratch: { key: "job-tools" } });
  assert.equal(fake.toolInstalls.length, 2);
  assert.equal(fake.toolInstalls[1], fake.named(scratch.id)[0]!.id);
});

test("a computer adopted by label checks its tools, which installs nothing when they are there", async () => {
  await make().provision(layers);
  const [computer] = fake.named(name);
  const other = createMemoryMap<StoredPrizedSandbox>();
  await make({ store: other }).provision(layers);
  assert.deepEqual(fake.toolInstalls, [computer!.id, computer!.id]);
  assert.equal((await other.get(scope))?.toolsReady, true);
});

test("a failed tool install is reported and the computer still serves the turn", async () => {
  fake.failToolInstall();
  const sandbox = make();
  const h = await sandbox.provision(layers);
  assert.equal(h.coldStart, true);
  assert.equal((await sandbox.run(h, "echo ok")).stdout.trim(), "ok");
  assert.deepEqual(
    errors.map((e) => e.code),
    ["tools_install_failed"],
  );
  assert.match(errors[0]!.message, /Could not resolve host/);
  assert.notEqual((await store.get(scope))?.toolsReady, true);
  fake.failToolInstall(false);
  await sandbox.teardown(h);
  await sandbox.provision(layers);
  assert.equal(fake.toolInstalls.length, 2);
  assert.equal((await store.get(scope))?.toolsReady, true);
});
