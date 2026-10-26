// Offline tests: @prized/sdk is replaced by a fake whose exec runs the local bash, with a temp dir
// standing in for /home/prized, so the package's own shell helpers run for real.
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ made: [] as any[], root: "" }));

vi.mock("@prized/sdk", () => {
  class PrizedError extends Error {
    code: string;
    constructor(code: string, message?: string) {
      super(message ?? code);
      this.code = code;
    }
  }
  class CommandTimeoutError extends PrizedError {
    stdout: string;
    stderr: string;
    constructor(stdout: string, stderr: string) {
      super("command_timeout");
      this.stdout = stdout;
      this.stderr = stderr;
    }
  }
  class FakeSandbox {
    id: string;
    options: any;
    deleted = false;
    paused = false;
    calls: any[] = [];
    root: string;
    files: any;
    constructor(options: any) {
      this.id = `sbx_fake${state.made.length}`;
      this.options = options;
      this.root = join(state.root, this.id);
      mkdirSync(join(this.root, "work"), { recursive: true });
      const local = (p: string) => p.replace("/home/prized", this.root);
      this.files = {
        write: async (p: string, data: Uint8Array) => {
          mkdirSync(dirname(local(p)), { recursive: true });
          writeFileSync(local(p), data);
        },
        readBytes: async (p: string) => new Uint8Array(readFileSync(local(p))),
      };
      state.made.push(this);
    }
    static async create(options: any) {
      return new FakeSandbox(options);
    }
    static async connect(id: string) {
      const sb = state.made.find((s: any) => s.id === id && !s.deleted);
      if (!sb) throw new PrizedError("not_found");
      return sb;
    }
    static async deleteByLabels() {
      return state.made.filter((s: any) => !s.deleted).map((s: any) => s.id);
    }
    async exec(cmd: string | string[], opts: any = {}) {
      this.calls.push([cmd, opts]);
      if (this.deleted) throw new PrizedError("sandbox_deleted");
      const local = (p: string) => p.split("/home/prized").join(this.root);
      const argv = typeof cmd === "string" ? ["bash", "-c", local(cmd)] : cmd.map(local);
      const env = { PATH: process.env.PATH, HOME: this.root, ...(opts.env ?? {}) };
      if (env.PATH !== process.env.PATH) env.PATH = `${env.PATH}:${process.env.PATH}`;
      const r = spawnSync(argv[0], argv.slice(1), {
        cwd: local(opts.cwd ?? "/home/prized/work"),
        env,
        timeout: opts.timeoutMs,
        encoding: "utf8",
      });
      const back = (s: string) => (s ?? "").split(this.root).join("/home/prized");
      if (r.error && (r.error as any).code === "ETIMEDOUT") throw new CommandTimeoutError(back(r.stdout), back(r.stderr));
      return { stdout: back(r.stdout), stderr: back(r.stderr), exitCode: r.status ?? -1, durationMs: 1 };
    }
    failDeletes = 0;
    async delete() {
      if (this.failDeletes > 0) {
        this.failDeletes--;
        throw new PrizedError("network_error");
      }
      this.deleted = true;
    }
    async pause() {
      this.paused = true;
    }
    async resume() {
      this.paused = false;
    }
  }
  return { Sandbox: FakeSandbox, PrizedError, CommandTimeoutError };
});

const { PrizedSandbox, createPrizedSandboxFactory, createPrizedSandboxFactoryFromSandbox } = await import("../src/index.js");

beforeEach(() => {
  state.made.length = 0;
  state.root = mkdtempSync(join(tmpdir(), "prized-langchain-"));
});
afterEach(() => {
  rmSync(state.root, { recursive: true, force: true });
});

const enc = new TextEncoder();
const dec = new TextDecoder();

describe("PrizedSandbox (offline)", () => {
  it("creates with labels, shape and auto-pause, and writes initial files", async () => {
    const sb = await PrizedSandbox.create({
      template: "prized-agent-v1",
      resources: { vcpu: 2, memoryGb: 4 },
      labels: { team: "x" },
      envVars: { A: "1" },
      initialFiles: { "hello.txt": "hi", "/home/prized/abs.txt": "abs" },
    });
    const o = state.made[0].options;
    expect(o.template).toBe("prized-agent-v1");
    expect(o.resources).toEqual({ vcpu: 2, memoryGb: 4 });
    expect(o.labels).toEqual({ team: "x", "created-by": "prized-langchain" });
    expect(o.autoPauseMin).toBe(10);
    expect(o.envVars).toEqual({ A: "1" });
    const [a, b] = await sb.downloadFiles(["/home/prized/work/hello.txt", "/home/prized/abs.txt"]);
    expect(dec.decode(a.content!)).toBe("hi");
    expect(dec.decode(b.content!)).toBe("abs");
  });

  it("formats stdout, stderr and exit codes", async () => {
    const sb = await PrizedSandbox.create();
    expect(await sb.execute("echo out; echo err >&2; exit 3")).toEqual({
      output: "out\n<stderr>err</stderr>",
      exitCode: 3,
      truncated: false,
    });
    expect((await sb.execute("pwd")).output).toBe("/home/prized/work\n");
  });

  it("turns a timeout into exit code 124 with what was printed", async () => {
    const sb = await PrizedSandbox.create({ timeout: 1 });
    const r = await sb.execute("echo started; sleep 5");
    expect(r.exitCode).toBe(124);
    expect(r.output).toContain("Command timed out after 1 seconds");
  });

  it("uploads atomically, keeps an existing file's mode, and maps errors per file", async () => {
    const sb = await PrizedSandbox.create();
    const w = "/home/prized/work";
    await sb.execute(`mkdir -p ${w}/dir && printf old > ${w}/run.sh && chmod 755 ${w}/run.sh && printf ro > ${w}/ro.txt && chmod 444 ${w}/ro.txt`);
    const res = await sb.uploadFiles([
      [`${w}/a/b/new.txt`, enc.encode("new")],
      [`${w}/run.sh`, enc.encode("#!/bin/sh\necho ran\n")],
      [`${w}/dir`, enc.encode("x")],
      [`${w}/ro.txt`, enc.encode("x")],
      ["relative.txt", enc.encode("x")],
    ]);
    expect(res.map((r) => r.error)).toEqual([null, null, "is_directory", "permission_denied", "invalid_path"]);
    expect((await sb.execute(`./run.sh; stat -c %a ${w}/run.sh ${w}/a/b/new.txt`)).output).toBe("ran\n755\n644\n");
    expect(execFileSync("ls", ["-A", join(state.made[0].root, ".cache/prized-langchain")]).toString()).toBe("");
  });

  it("downloads several files with per-file errors and cleans its staging", async () => {
    const sb = await PrizedSandbox.create();
    const w = "/home/prized/work";
    await sb.uploadFiles([[`${w}/bin.dat`, new Uint8Array([0, 255, 10])]]);
    await sb.execute(`printf secret > ${w}/locked && chmod 000 ${w}/locked && mkdir -p ${w}/d && ln -s ${w}/bin.dat ${w}/link`);
    const res = await sb.downloadFiles([`${w}/bin.dat`, `${w}/missing`, `${w}/d`, `${w}/locked`, "rel", `${w}/link`]);
    expect(res.map((r) => r.error)).toEqual([null, "file_not_found", "is_directory", "permission_denied", "invalid_path", null]);
    expect(Array.from(res[0].content!)).toEqual([0, 255, 10]);
    expect(Array.from(res[5].content!)).toEqual([0, 255, 10]);
    expect(execFileSync("ls", ["-A", join(state.made[0].root, ".cache/prized-langchain")]).toString()).toBe("");
  });

  it("writes through a symlink to the file it names", async () => {
    const sb = await PrizedSandbox.create();
    const w = "/home/prized/work";
    await sb.execute(`printf a > ${w}/target && ln -s ${w}/target ${w}/alias`);
    await sb.uploadFiles([[`${w}/alias`, enc.encode("b")]]);
    expect((await sb.execute(`cat ${w}/target; test -L ${w}/alias && echo link`)).output).toBe("blink\n");
  });

  it("deletes only a sandbox it created, once", async () => {
    const owned = await PrizedSandbox.create();
    await owned.close();
    await owned.close();
    expect(state.made[0].deleted).toBe(true);
    const keep = await PrizedSandbox.create();
    const attached = await PrizedSandbox.fromId(keep.id);
    await attached.close();
    expect(state.made[1].deleted).toBe(false);
    expect(attached.isRunning).toBe(false);
    await expect(attached.initialize()).rejects.toThrow(/closed/);
  });

  it("lets close() be retried after a failed delete", async () => {
    const sb = await PrizedSandbox.create();
    state.made[0].failDeletes = 1;
    await expect(sb.close()).rejects.toThrow(/network_error/);
    expect(state.made[0].deleted).toBe(false);
    await sb.close();
    expect(state.made[0].deleted).toBe(true);
  });

  it("refuses use before initialize and bad options", async () => {
    const sb = new PrizedSandbox();
    expect(sb.id).toBe("");
    await expect(sb.execute("true")).rejects.toThrow(/not initialized/);
    expect(() => new PrizedSandbox({ timeout: 0 })).toThrow(/timeout/);
    expect(() => new PrizedSandbox({ cwd: "rel" })).toThrow(/cwd/);
  });

  it("reports a deleted sandbox as SANDBOX_NOT_FOUND", async () => {
    const sb = await PrizedSandbox.create();
    state.made[0].deleted = true;
    await expect(sb.execute("true")).rejects.toMatchObject({ code: "SANDBOX_NOT_FOUND" });
  });

  it("pauses, resumes and deletes on request; factories", async () => {
    const sb = await PrizedSandbox.create();
    await sb.stop();
    expect(state.made[0].paused).toBe(true);
    await sb.start();
    expect(state.made[0].paused).toBe(false);
    expect(createPrizedSandboxFactoryFromSandbox(sb)({} as never)).toBe(sb);
    const made = await createPrizedSandboxFactory({ labels: { f: "1" } })();
    expect(made.id).toBe("sbx_fake1");
    await sb.kill();
    expect(state.made[0].deleted).toBe(true);
    expect(await PrizedSandbox.deleteAll({ f: "1" })).toBe(1);
  });

  it("runs the agent's read, write, edit, ls, grep and glob through the base class", async () => {
    const sb = await PrizedSandbox.create();
    const w = "/home/prized/work";
    expect((await sb.write(`${w}/src/app.py`, "print('hi')\n")).error).toBeFalsy();
    const edited = await sb.edit(`${w}/src/app.py`, "hi", "there");
    expect(edited.error).toBeFalsy();
    const read = await sb.read(`${w}/src/app.py`);
    expect(JSON.stringify(read)).toContain("there");
    expect(JSON.stringify(await sb.ls(`${w}/src`))).toContain("app.py");
    expect(JSON.stringify(await sb.grep("there", w))).toContain("app.py");
    expect(JSON.stringify(await sb.glob("**/*.py", w))).toContain("app.py");
  });
});
