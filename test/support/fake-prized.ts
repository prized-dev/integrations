import { execFile } from "node:child_process";
import { randomBytes, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { PRIZED_TOOLS_ENSURE } from "../../src/sandbox/prized-sandbox.ts";
import {
  PRIZED_HOME_DIR,
  type PrizedClient,
  type PrizedCreateOptions,
  type PrizedSandboxInfo,
  type PrizedState,
} from "../../src/sandbox/prized-client.ts";

const execFileAsync = promisify(execFile);
const RUN_TIME_MS = 24 * 3600_000;

interface FakeSandbox {
  id: string;
  state: PrizedState;
  labels: Record<string, string>;
  autoPauseMin: number;
  autoDeleteMin?: number;
  maxRuntimeMin?: number;
  expiresAtMs: number | null;
  errorReason: string | null;
}

export interface FakePrizedCall {
  op: string;
  id?: string;
  labels?: Record<string, string>;
}

export function createFakePrized() {
  const root = mkdtempSync(join(tmpdir(), "fake-prized-"));
  const sandboxes = new Map<string, FakeSandbox>();
  const calls: FakePrizedCall[] = [];
  const commands: string[] = [];
  const refusals = new Map<string, Error>();
  const toolInstalls: string[] = [];
  let toolsFail = false;
  const failing: string[] = [];
  const home = (id: string): string => join(root, id);
  const mapped = (id: string, path: string): string =>
    path.replace(/^\/tmp\//, `${home(id)}/tmp/`).replace(new RegExp(`^${PRIZED_HOME_DIR}(?=/|$)`), home(id));
  const matches = (sandbox: FakeSandbox, labels: Record<string, string>): boolean =>
    Object.entries(labels).every(([key, value]) => sandbox.labels[key] === value);
  const info = (sandbox: FakeSandbox): PrizedSandboxInfo => ({
    id: sandbox.id,
    state: sandbox.state,
    labels: { ...sandbox.labels },
    expiresAtMs: sandbox.expiresAtMs,
    errorReason: sandbox.errorReason,
    cpus: 4,
    memoryMb: 8192,
    diskGb: 10,
  });
  const live = (id: string): FakeSandbox => {
    const sandbox = sandboxes.get(id);
    if (!sandbox || sandbox.state === "deleted")
      throw Object.assign(new Error(`${id} not found`), { code: "not_found" });
    return sandbox;
  };
  const refused = (op: string): void => {
    const error = refusals.get(op);
    if (error) throw error;
  };
  const wake = (id: string): FakeSandbox => {
    const sandbox = live(id);
    if (sandbox.state === "error") throw new Error(`${id} is in error`);
    if (sandbox.state === "paused" || sandbox.state === "pausing") {
      refused("resume");
      sandbox.state = "running";
      sandbox.expiresAtMs = Date.now() + RUN_TIME_MS;
    }
    return sandbox;
  };
  const execute = async (id: string, script: string, timeoutSec: number) => {
    mkdirSync(join(home(id), "tmp"), { recursive: true });
    const file = join(root, `run-${randomUUID()}.sh`);
    writeFileSync(
      file,
      script
        .replace(/\/tmp\//g, `${home(id)}/tmp/`)
        .replace(new RegExp(`${PRIZED_HOME_DIR}(?![A-Za-z0-9_-])`, "g"), home(id)),
    );
    try {
      const result = await execFileAsync("/bin/bash", [file], {
        encoding: "utf8",
        env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin", HOME: home(id), COPYFILE_DISABLE: "1" },
        maxBuffer: 16 * 1024 * 1024,
        timeout: Math.min(timeoutSec, 30) * 1000,
      });
      return { stdout: result.stdout, stderr: result.stderr, exitCode: 0, timedOut: false };
    } catch (error) {
      const failed = error as Error & { code?: number | string; killed?: boolean; stdout?: string; stderr?: string };
      if (failed.killed)
        return { stdout: failed.stdout ?? "", stderr: failed.stderr ?? "", exitCode: 124, timedOut: true };
      if (typeof failed.code !== "number") throw new Error(failed.message, { cause: error });
      return { stdout: failed.stdout ?? "", stderr: failed.stderr ?? "", exitCode: failed.code, timedOut: false };
    } finally {
      rmSync(file, { force: true });
    }
  };

  const client: PrizedClient = {
    async create(createOpts: PrizedCreateOptions) {
      calls.push({ op: "create", labels: createOpts.labels });
      refused("create");
      const id = `sbx_${randomBytes(8).toString("hex")}`;
      const sandbox: FakeSandbox = {
        id,
        state: "running",
        labels: { ...createOpts.labels },
        autoPauseMin: createOpts.autoPauseMin,
        ...(createOpts.autoDeleteMin ? { autoDeleteMin: createOpts.autoDeleteMin } : {}),
        ...(createOpts.maxRuntimeMin ? { maxRuntimeMin: createOpts.maxRuntimeMin } : {}),
        expiresAtMs: Date.now() + RUN_TIME_MS,
        errorReason: null,
      };
      mkdirSync(join(home(id), "work"), { recursive: true });
      sandboxes.set(id, sandbox);
      return info(sandbox);
    },
    async get(id) {
      calls.push({ op: "get", id });
      const sandbox = sandboxes.get(id);
      return sandbox && sandbox.state !== "deleted" ? info(sandbox) : null;
    },
    async list(labels) {
      calls.push({ op: "list", labels });
      return [...sandboxes.values()].filter((s) => s.state !== "deleted" && matches(s, labels)).map(info);
    },
    async resume(id) {
      calls.push({ op: "resume", id });
      wake(id);
    },
    async pause(id) {
      calls.push({ op: "pause", id });
      const sandbox = live(id);
      sandbox.state = "paused";
      sandbox.expiresAtMs = null;
    },
    async keepAlive(id) {
      calls.push({ op: "keepAlive", id });
      live(id);
    },
    async delete(id) {
      calls.push({ op: "delete", id });
      const sandbox = sandboxes.get(id);
      if (!sandbox) return;
      sandbox.state = "deleted";
      rmSync(home(id), { recursive: true, force: true });
    },
    async deleteByLabels(labels) {
      calls.push({ op: "deleteByLabels", labels });
      const ids = [...sandboxes.values()].filter((s) => s.state !== "deleted" && matches(s, labels)).map((s) => s.id);
      for (const id of ids) await client.delete(id);
      return ids;
    },
    async exec(id, script, timeoutSec) {
      wake(id);
      if (script === PRIZED_TOOLS_ENSURE) {
        toolInstalls.push(id);
        return toolsFail
          ? { stdout: "", stderr: "curl: (6) Could not resolve host: github.com", exitCode: 6, timedOut: false }
          : { stdout: "gh version 2.93.0\naws-cli/2.34.54\n", stderr: "", exitCode: 0, timedOut: false };
      }
      commands.push(script);
      if (failing.some((fragment) => script.includes(fragment)))
        return { stdout: "", stderr: "injected failure", exitCode: 1, timedOut: false };
      return execute(id, script, timeoutSec);
    },
    async readFileBytes(id, absPath) {
      wake(id);
      const path = mapped(id, absPath);
      return existsSync(path) ? new Uint8Array(readFileSync(path)) : null;
    },
    async writeFileBytes(id, absPath, data) {
      wake(id);
      const path = mapped(id, absPath);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, data);
    },
  };

  return {
    client,
    sandboxes,
    calls,
    commands,
    home,
    named(name: string): FakeSandbox[] {
      return [...sandboxes.values()].filter((s) => s.state !== "deleted" && s.labels["qm-name"] === name);
    },
    toolInstalls,
    failToolInstall(fail = true) {
      toolsFail = fail;
    },
    failCommands(fragment: string) {
      failing.push(fragment);
    },
    refuse(op: "create" | "resume", error?: Error) {
      if (error) refusals.set(op, error);
      else refusals.delete(op);
    },
    cleanup() {
      rmSync(root, { recursive: true, force: true });
    },
  };
}

export type FakePrized = ReturnType<typeof createFakePrized>;
