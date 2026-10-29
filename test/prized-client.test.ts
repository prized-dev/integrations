import { test, mock, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { createSdkPrizedClient, inPrizedHome, prizedFailure } from "../src/sandbox/prized-client.ts";
import { NonRetryableTurnError } from "../src/core/turn-error.ts";

class FakePrizedError extends Error {
  readonly code: string;
  constructor(code: string, message = code) {
    super(message);
    this.name = "PrizedError";
    this.code = code;
  }
}

class FakeCommandTimeoutError extends FakePrizedError {
  readonly result: { stdout: string; stderr: string; exitCode: number };
  constructor(result: { stdout: string; stderr: string }) {
    super("command_timeout", "The command ran past its timeout and was killed");
    this.result = { ...result, exitCode: -1 };
  }
}

const calls: unknown[][] = [];
const files = new Map<string, Uint8Array>();
let gone = false;
let createError: Error | undefined;
let execAnswer: (script: string) => { stdout: string; stderr: string; exitCode: number } | Error;
let writeError: Error | undefined;

class FakeSdkSandbox {
  readonly id: string;
  readonly info: Record<string, unknown>;
  readonly files = {
    readBytes: async (path: string): Promise<Uint8Array> => {
      calls.push(["readBytes", path]);
      const data = files.get(path);
      if (!data) throw new FakePrizedError("not_found", "no such file");
      return data;
    },
    write: async (path: string, data: Uint8Array): Promise<void> => {
      calls.push(["write", path]);
      if (writeError && !path.includes("/.qm-stage/")) throw writeError;
      files.set(path, data);
    },
  };
  constructor(id: string, labels: Record<string, string> = {}) {
    this.id = id;
    this.info = {
      id,
      state: "running",
      labels,
      expiresAt: new Date(2_000_000_000_000),
      errorReason: null,
      resources: { vcpu: 2, memoryMb: 4096, memoryGb: 4, diskGb: 10 },
    };
  }
  static async create(options: Record<string, unknown>): Promise<FakeSdkSandbox> {
    calls.push(["create", options]);
    if (createError) throw createError;
    return new FakeSdkSandbox("sbx_new", options.labels as Record<string, string>);
  }
  static async connect(id: string, options: Record<string, unknown>): Promise<FakeSdkSandbox> {
    calls.push(["connect", id, options]);
    if (gone) throw new FakePrizedError("not_found");
    return new FakeSdkSandbox(id);
  }
  static async list(options: Record<string, unknown>): Promise<FakeSdkSandbox[]> {
    calls.push(["list", options.labels]);
    return [new FakeSdkSandbox("sbx_listed", options.labels as Record<string, string>)];
  }
  static async deleteByLabels(labels: Record<string, string>): Promise<string[]> {
    calls.push(["deleteByLabels", labels]);
    return ["sbx_listed"];
  }
  async refresh(): Promise<this> {
    calls.push(["refresh", this.id]);
    if (gone) throw new FakePrizedError("not_found");
    return this;
  }
  async exec(script: string, options: Record<string, unknown>) {
    calls.push(["exec", script, options]);
    const answer = execAnswer(script);
    if (answer instanceof Error) throw answer;
    return { ...answer, durationMs: 1, timedOut: false, truncated: false };
  }
  async pause(options: Record<string, unknown>): Promise<void> {
    calls.push(["pause", this.id, options]);
  }
  async resume(): Promise<void> {
    calls.push(["resume", this.id]);
  }
  async keepAlive(): Promise<void> {
    calls.push(["keepAlive", this.id]);
  }
  async delete(): Promise<void> {
    calls.push(["delete", this.id]);
    if (gone) throw new FakePrizedError("not_found");
  }
}

mock.module("@prized/sdk", { namedExports: { Sandbox: FakeSdkSandbox } });

beforeEach(() => {
  calls.length = 0;
  files.clear();
  gone = false;
  createError = undefined;
  writeError = undefined;
  execAnswer = () => ({ stdout: "", stderr: "", exitCode: 0 });
});

const ops = (): string[] => calls.map((c) => String(c[0]));

test("create sends the agent template, PATH, labels and idle pause, and the answer becomes the info", async () => {
  const client = createSdkPrizedClient({ apiKey: "dcp_test" });
  const info = await client.create({ labels: { "qm-name": "qm-a" }, autoPauseMin: 60 });
  assert.deepEqual(calls[0], [
    "create",
    {
      apiKey: "dcp_test",
      template: "prized-agent-v1",
      envVars: { PATH: "/home/prized/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" },
      labels: { "qm-name": "qm-a" },
      autoPauseMin: 60,
    },
  ]);
  assert.equal(info.id, "sbx_new");
  assert.equal(info.cpus, 2);
  assert.equal(info.expiresAtMs, 2_000_000_000_000);
  await client.exec("sbx_new", "true", 5);
  assert.ok(!ops().includes("connect"));
});

test("a refused create reaches the turn as the API's own words", async () => {
  createError = new FakePrizedError("insufficient_balance", "Add balance to your workspace and try again.");
  const client = createSdkPrizedClient({ apiKey: "dcp_test" });
  await assert.rejects(
    client.create({ labels: {}, autoPauseMin: 60 }),
    (e: unknown) =>
      e instanceof NonRetryableTurnError &&
      e.message ===
        "Prized refused this agent computer (insufficient_balance): Add balance to your workspace and try again.",
  );
  const revoked = prizedFailure(new FakePrizedError("unauthorized", "This key was revoked."));
  assert.ok(revoked instanceof NonRetryableTurnError && /revoked/.test(revoked.message));
  const transient = new FakePrizedError("sandbox_capacity");
  assert.equal(prizedFailure(transient), transient);
  const plain = new Error("socket hang up");
  assert.equal(prizedFailure(plain), plain);
});

test("exec maps a timeout to exit 124 with what the command printed", async () => {
  execAnswer = () => new FakeCommandTimeoutError({ stdout: "partial", stderr: "" });
  const client = createSdkPrizedClient({ apiKey: "dcp_test" });
  const result = await client.exec("sbx_1", "sleep 100", 2);
  assert.deepEqual(result, { stdout: "partial", stderr: "", exitCode: 124, timedOut: true });
  assert.deepEqual((calls.find((c) => c[0] === "exec")![2] as Record<string, unknown>).timeoutMs, 2000);
  execAnswer = () => ({ stdout: "ok", stderr: "", exitCode: 3 });
  assert.deepEqual(await client.exec("sbx_1", "exit 3", 2), { stdout: "ok", stderr: "", exitCode: 3, timedOut: false });
});

test("a missing file reads as null, but a missing sandbox is an error", async () => {
  const client = createSdkPrizedClient({ apiKey: "dcp_test" });
  assert.equal(await client.readFileBytes("sbx_1", "/home/prized/workspace/nope"), null);
  assert.ok(ops().includes("refresh"));
  gone = true;
  await assert.rejects(client.readFileBytes("sbx_1", "/home/prized/workspace/nope"), /not_found/);
  await assert.rejects(client.readFileBytes("sbx_1", "/home/prized/workspace/nope"), /not_found/);
  assert.equal(calls.filter((c) => c[0] === "connect").length, 2);
});

test("files outside the home directory go through a staged copy", async () => {
  const client = createSdkPrizedClient({ apiKey: "dcp_test" });
  await client.writeFileBytes("sbx_1", "/tmp/agent-creds/token", Buffer.from("secret"));
  const staged = calls.find((c) => c[0] === "write")![1] as string;
  assert.match(staged, /^\/home\/prized\/\.qm-stage\//);
  const move = calls.find((c) => c[0] === "exec")![1] as string;
  assert.match(
    move,
    /mkdir -p '\/tmp\/agent-creds' && \(umask 077; cat '\/home\/prized\/\.qm-stage\/([^']+)' > '\/tmp\/agent-creds\/token'\) && rm -f '\/home\/prized\/\.qm-stage\/\1'/,
  );
  assert.ok(!calls.some((c) => c[0] === "exec" && String(c[1]).startsWith("rm -f")));

  calls.length = 0;
  execAnswer = (script) =>
    script.includes("exit 44") ? { stdout: "", stderr: "", exitCode: 44 } : { stdout: "", stderr: "", exitCode: 0 };
  assert.equal(await client.readFileBytes("sbx_1", "/etc/missing"), null);

  calls.length = 0;
  execAnswer = (script) => {
    const copy = /cp '\/etc\/hostname' '([^']+)'/.exec(script);
    if (copy) files.set(copy[1]!, Buffer.from("vm"));
    return { stdout: "", stderr: "", exitCode: 0 };
  };
  assert.equal(Buffer.from((await client.readFileBytes("sbx_1", "/etc/hostname"))!).toString(), "vm");
  assert.ok(calls.some((c) => c[0] === "exec" && String(c[1]).startsWith("rm -f '/home/prized/.qm-stage/")));

  calls.length = 0;
  execAnswer = () => ({ stdout: "", stderr: "mv: cannot move: Permission denied", exitCode: 1 });
  await assert.rejects(client.writeFileBytes("sbx_1", "/usr/local/bin/tool", Buffer.from("x")), /Permission denied/);
  assert.ok(calls.some((c) => c[0] === "exec" && String(c[1]).startsWith("rm -f '/home/prized/.qm-stage/")));
});

test("files under the home directory go straight to the files API", async () => {
  const client = createSdkPrizedClient({ apiKey: "dcp_test" });
  await client.writeFileBytes("sbx_1", "/home/prized/workspace/a.txt", Buffer.from("a"));
  assert.deepEqual(
    calls.filter((c) => c[0] === "write" || c[0] === "exec"),
    [["write", "/home/prized/workspace/a.txt"]],
  );
  assert.equal(inPrizedHome("/home/prized/workspace/../.ssh/key"), true);
  assert.equal(inPrizedHome("/home/prized/../root/x"), false);
  assert.equal(inPrizedHome("/home/prizedfoo/x"), false);
});

test("a write through a symlink that leaves the home falls back to the staged copy", async () => {
  writeError = new FakePrizedError("symlink_not_allowed");
  const client = createSdkPrizedClient({ apiKey: "dcp_test" });
  await client.writeFileBytes("sbx_1", "/home/prized/.aws/credentials", Buffer.from("k"));
  const writes = calls.filter((c) => c[0] === "write").map((c) => String(c[1]));
  assert.equal(writes[0], "/home/prized/.aws/credentials");
  assert.match(writes[1]!, /^\/home\/prized\/\.qm-stage\//);
  assert.match(String(calls.find((c) => c[0] === "exec")![1]), /cat '[^']+' > '\/home\/prized\/\.aws\/credentials'/);
  writeError = new FakePrizedError("permission_denied");
  await assert.rejects(client.writeFileBytes("sbx_1", "/home/prized/locked", Buffer.from("k")), /permission_denied/);
});

test("pause does not wait by default, and delete of a sandbox already gone succeeds", async () => {
  const client = createSdkPrizedClient({ apiKey: "dcp_test" });
  await client.pause("sbx_1");
  await client.pause("sbx_1", { wait: true });
  assert.deepEqual(
    calls.filter((c) => c[0] === "pause").map((c) => c[2]),
    [{ wait: false }, { wait: true }],
  );
  gone = true;
  await client.delete("sbx_2");
  assert.equal(await client.get("sbx_2"), null);
});
