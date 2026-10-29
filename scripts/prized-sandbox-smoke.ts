#!/usr/bin/env node
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createPrizedSandbox } from "../src/sandbox/prized-sandbox.ts";
import { createSdkPrizedClient } from "../src/sandbox/prized-client.ts";
import { pollProcess } from "../src/sandbox/process-poll.ts";
import { supportsProcessSessions } from "../src/sandbox/sandbox.ts";
import { createLocalWorkspaceStore } from "../src/workspace/workspace-store.ts";
import { createMemoryMap } from "../src/persistence/durable-map.ts";
import { createMemoryAdvisoryLock } from "../src/persistence/advisory-lock.ts";
import { scopeId } from "../src/types.ts";

const apiKey = process.env.PRIZED_API_KEY;
const labels = Object.fromEntries(
  (process.env.PRIZED_SMOKE_LABELS ?? "")
    .split(",")
    .map((pair) => pair.trim().split("="))
    .filter((pair): pair is [string, string] => pair.length === 2 && !!pair[0]),
);

const log = (...a: unknown[]) => console.log("[prized-smoke]", ...a);
const timed = async <T>(what: string, work: () => Promise<T>): Promise<T> => {
  const started = Date.now();
  const result = await work();
  log(`${what}: ${Date.now() - started} ms`);
  return result;
};

async function main(): Promise<void> {
  if (!apiKey) throw new Error("PRIZED_API_KEY is required");
  const client = createSdkPrizedClient({ apiKey });
  const prefix = `qms${randomBytes(3).toString("hex")}`;
  const sandbox = createPrizedSandbox(createLocalWorkspaceStore(mkdtempSync(join(tmpdir(), "prized-smoke-ws-"))), {
    client,
    namePrefix: prefix,
    labels,
    store: createMemoryMap(),
    advisoryLock: createMemoryAdvisoryLock(),
    onError: (e) => log("reported", e.code, e.message),
  });
  if (!supportsProcessSessions(sandbox)) throw new Error("prized sandbox lacks process sessions");
  const scope = scopeId("personal", "smoke");
  const layers = [{ scopeId: scope, mountPath: "/", mode: "rw" as const }];

  try {
    const handle = await timed("cold provision (incl. gh and AWS CLI install)", () =>
      sandbox.provision(layers, { env: { QM_SMOKE: "1" } }),
    );
    assert.equal(handle.coldStart, true);

    const r = await sandbox.run(handle, "pwd; echo QM_SMOKE=$QM_SMOKE; whoami; uname -sr");
    assert.equal(r.code, 0, `run exited ${r.code}: ${r.stderr}`);
    assert.match(r.stdout, /^\/home\/prized\/workspace$/m);
    assert.match(r.stdout, /QM_SMOKE=1/);
    log("run:", JSON.stringify(r.stdout));

    const binaryName = (entry: string) => entry.trim().split(/\s+/)[0] ?? "";
    const present = (sandbox.profile.spec?.tools ?? []).map(binaryName);
    const absent = (sandbox.profile.spec?.notInstalled ?? []).map(binaryName);
    const plumbing = [
      "sh",
      "bash",
      "timeout",
      "tar",
      "base64",
      "sha256sum",
      "setsid",
      "mkfifo",
      "cut",
      "find",
      "xargs",
    ];
    const probe = [...new Set([...plumbing, ...present, ...absent])];
    const tools = await sandbox.run(
      handle,
      `for t in ${probe.join(" ")}; do printf '%s=%s ' $t $(command -v $t >/dev/null 2>&1 && echo yes || echo NO); done`,
    );
    log("tools:", tools.stdout.trim());
    const onPath = new Set(
      tools.stdout
        .trim()
        .split(/\s+/)
        .filter((pair) => pair.endsWith("=yes"))
        .map((pair) => pair.split("=")[0]),
    );
    for (const t of plumbing) assert.ok(onPath.has(t), `qm plumbing needs ${t}`);
    for (const t of present) assert.ok(onPath.has(t), `the profile advertises ${t}, which the guest lacks`);
    for (const t of absent) assert.ok(!onPath.has(t), `the profile says ${t} is absent, but the guest has it`);

    const latencies: number[] = [];
    for (let i = 0; i < 10; i++) {
      const started = Date.now();
      assert.equal((await sandbox.run(handle, "true")).code, 0);
      latencies.push(Date.now() - started);
    }
    latencies.sort((a, b) => a - b);
    log(`run("true") x10: median ${latencies[5]} ms, max ${latencies[9]} ms`);

    await sandbox.writeFile(handle, "notes/hello.txt", "written by qm");
    assert.equal(await sandbox.readFile(handle, "notes/hello.txt"), "written by qm");
    assert.equal(await sandbox.readFile(handle, "notes/missing.txt"), null);
    const big = randomBytes(3 * 1024 * 1024);
    await timed("write 3 MiB", () => sandbox.writeFileBytes(handle, "big.bin", big));
    const back = await timed("read 3 MiB", () => sandbox.readFileBytes(handle, "big.bin"));
    assert.ok(back && Buffer.from(back).equals(big), "binary round trip differs");
    const sum = await sandbox.run(handle, "sha256sum big.bin | cut -d' ' -f1");
    assert.equal(sum.stdout.trim(), (await import("node:crypto")).createHash("sha256").update(big).digest("hex"));
    assert.deepEqual(await sandbox.listDir(handle, "notes"), ["notes/hello.txt"]);
    await sandbox.importFiles!(handle, [{ path: "imported/a.txt", data: Buffer.from("a") }]);
    assert.equal(await sandbox.readFile(handle, "imported/a.txt"), "a");
    const exported = await sandbox.exportFiles!(handle, { include: ["workspace"] });
    assert.ok(
      exported.some((entry) => entry.path.endsWith("notes/hello.txt")),
      "export misses a workspace file",
    );
    log("file round trips OK, export has", exported.length, "entries");

    const outside = "/tmp/qm-smoke/outside.txt";
    await client.writeFileBytes((await client.list({ "qm-deployment": prefix }))[0]!.id, outside, Buffer.from("tmp"));
    const outsideBack = await sandbox.run(handle, `cat ${outside}`);
    assert.equal(outsideBack.stdout, "tmp");
    log("write outside the home directory OK");

    const slow = await sandbox.run(handle, "echo started; sleep 20", { timeoutMs: 3000 });
    assert.equal(slow.timedOut, true);
    assert.equal(slow.code, 124);
    log("timeout OK:", JSON.stringify(slow.stdout));

    const once = await sandbox.startProcess(handle, "for i in 1 2 3; do echo tick $i; sleep 1; done");
    const ticks = await pollProcess(sandbox, handle, once.processId, { deadlineMs: 20_000, waitMs: 2_000 });
    assert.equal(ticks.status.state, "exited");
    assert.match(ticks.output, /tick 3/);
    log("process session OK");

    const counter = await sandbox.startProcess(
      handle,
      "i=0; while true; do i=$((i+1)); echo $i > counter.txt; sleep 1; done",
    );
    await new Promise((resolve) => setTimeout(resolve, 3000));
    await timed("teardown (pause)", () => sandbox.teardown(handle, {}));
    const parked = await sandbox.computerStatus!(scope);
    log("status after teardown:", parked.machine, parked.lifecycleState);
    assert.equal(parked.lifecycleState, "paused");
    await new Promise((resolve) => setTimeout(resolve, 5000));
    const resumed = await timed("warm provision (resume)", () => sandbox.provision(layers, { env: { QM_SMOKE: "1" } }));
    assert.equal(resumed.coldStart, false);
    const first = Number((await sandbox.readFile(resumed, "counter.txt"))?.trim());
    await new Promise((resolve) => setTimeout(resolve, 2500));
    const second = Number((await sandbox.readFile(resumed, "counter.txt"))?.trim());
    const listed = await sandbox.listProcesses(resumed);
    log(
      `counter ${first} -> ${second}; processes:`,
      listed.map((p) => `${p.processId.slice(0, 8)} ${p.status.state}`),
    );
    assert.ok(second > first, "the background process did not keep counting after resume");
    assert.ok(first < 8, "the counter ran while the computer was paused");
    assert.equal(listed.find((p) => p.processId === counter.processId)?.status.state, "running");
    assert.equal(await sandbox.readFile(resumed, "notes/hello.txt"), "written by qm");
    log("files and a running process survived pause and resume");
    await sandbox.signalProcess(resumed, counter.processId, "KILL");

    await sandbox.teardown(resumed, { keepWarm: true });
    const warm = await sandbox.computerStatus!(scope);
    assert.equal(warm.lifecycleState, "running");
    assert.equal(warm.guestResponsive, true);
    log("keepWarm teardown left it running:", warm.machine);

    const scratch = await timed("scratch provision", () =>
      sandbox.provision(layers, { scratch: { key: `smoke-${prefix}` } }),
    );
    assert.equal((await sandbox.run(scratch, "echo scratch")).stdout.trim(), "scratch");
    await sandbox.teardown(scratch);
    const scratchLeft = await client.list({ "qm-deployment": prefix, "qm-kind": "scratch" });
    assert.deepEqual(
      scratchLeft.filter((s) => s.state !== "deleting"),
      [],
      "the scratch computer outlived its last lease",
    );
    log("scratch computer deleted at release");

    await timed("destroy scope", () => sandbox.destroyScope!(scope));
    const left = (await client.list({ "qm-deployment": prefix })).filter((s) => s.state !== "deleting");
    assert.deepEqual(left, [], "destroyScope left a computer behind");
    log("ALL CHECKS PASSED");
  } finally {
    const swept = await client.deleteByLabels({ "qm-deployment": prefix }).catch((e: unknown) => {
      log("final sweep failed:", e);
      return [];
    });
    log("final sweep deleted", swept.length);
  }
}

main().catch((e: unknown) => {
  console.error("[prized-smoke] FAILED", e);
  process.exit(1);
});
