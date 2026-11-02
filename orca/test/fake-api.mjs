// A stand-in for the Prized control plane and a sandbox's edge, on 127.0.0.1, built from the
// documented API (https://prized.dev/docs/sandboxes-api). A "sandbox" is a directory on this machine:
// its home, where uploads land and commands run with the real bash and git.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

export const API_KEY = "dcp_00000000000000000000000000000000000000000000beef";
export const LOGIN_TOKEN = "fake-login-token";

export async function startFakeApi(root, options = {}) {
  const sandboxes = new Map();
  const idempotency = new Map();
  const requests = [];
  const opts = { maxRuntimeLimit: null, failExec: null, ...options };
  let seq = 0;

  const json = (res, status, body) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(body === undefined ? "" : JSON.stringify(body));
  };
  const view = (s) => ({
    id: s.id,
    state: s.state,
    template: s.template,
    resources: s.resources,
    labels: s.labels,
    autoPauseMin: s.autoPauseMin ?? null,
    maxRuntimeMin: s.maxRuntimeMin ?? null,
    toolboxBase: `${server.base}/edge/${s.id}`,
  });
  const readBody = (req) =>
    new Promise((resolve) => {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => resolve(Buffer.concat(chunks)));
    });

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const auth = req.headers.authorization ?? "";
    const who = auth === `Bearer ${API_KEY}` ? "key" : auth === `Bearer ${LOGIN_TOKEN}` ? "login" : "none";
    requests.push({ method: req.method, path: url.pathname, search: url.search, who, idem: req.headers["idempotency-key"] ?? null });
    if (who === "none") return json(res, 401, { error: "unauthorized" });
    const body = await readBody(req);
    const parts = url.pathname.split("/").filter(Boolean);

    if (url.pathname === "/api/v1/me") {
      return json(res, 200, { workspace: { id: "ws_test", name: "Test" }, user: { role: "owner" }, key: null });
    }
    if (req.method === "POST" && url.pathname === "/api/v1/sandboxes") {
      const input = JSON.parse(body.toString() || "{}");
      const idem = req.headers["idempotency-key"];
      if (idem && idempotency.has(idem)) {
        const prev = idempotency.get(idem);
        if (prev.body !== body.toString()) return json(res, 409, { error: "idempotency_conflict" });
        return json(res, 200, { sandbox: view(sandboxes.get(prev.id)) });
      }
      for (const k of Object.keys(input)) {
        if (!["template", "resources", "envVars", "labels", "autoPauseMin", "autoDeleteMin", "maxRuntimeMin"].includes(k)) {
          return json(res, 422, { error: "unknown_field", field: k });
        }
      }
      if (opts.maxRuntimeLimit && input.maxRuntimeMin > opts.maxRuntimeLimit) {
        return json(res, 429, {
          error: "sandbox_quota_exceeded",
          reason: "max_runtime",
          limit: opts.maxRuntimeLimit,
          tier: "free",
          message: `On the free tier a sandbox runs ${opts.maxRuntimeLimit} minutes in all.`,
        });
      }
      const id = `sbx_${(++seq).toString(16).padStart(16, "0")}`;
      const home = path.join(root, "sandboxes", id, "home");
      fs.mkdirSync(path.join(home, "work"), { recursive: true });
      const s = { id, state: "running", home, ...input, pauses: 0, resumes: 0 };
      sandboxes.set(id, s);
      if (idem) idempotency.set(idem, { id, body: body.toString() });
      return json(res, 201, { sandbox: view(s) });
    }
    if (parts[0] === "api" && parts[2] === "sandboxes" && parts[3]) {
      const s = sandboxes.get(parts[3]);
      if (!s || s.state === "deleted") return json(res, 404, { error: "not_found" });
      if (req.method === "GET" && parts.length === 4) return json(res, 200, { sandbox: view(s) });
      if (req.method === "DELETE" && parts.length === 4) {
        s.state = "deleted";
        return json(res, 202, { sandbox: { ...view(s), state: "deleting" } });
      }
      if (req.method === "POST" && parts[4] === "pause") {
        s.state = "paused";
        s.pauses++;
        return json(res, 200, { sandbox: view(s) });
      }
      if (req.method === "POST" && parts[4] === "resume") {
        s.state = "running";
        s.resumes++;
        return json(res, 200, { sandbox: view(s) });
      }
    }
    if (parts[0] === "edge" && parts[1]) {
      const s = sandboxes.get(parts[1]);
      if (!s || s.state === "deleted") return json(res, 410, { error: "sandbox_deleted" });
      if (s.state === "paused") {
        // the edge wakes a paused sandbox, as the docs say
        s.state = "running";
        s.resumes++;
      }
      if (parts[2] === "files" && parts[3] === "upload") {
        const dest = url.searchParams.get("path");
        if (!dest?.startsWith(`${s.home}/`)) return json(res, 400, { error: "bad_path" });
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        fs.writeFileSync(dest, body);
        return json(res, 200, { path: dest, bytes: body.length });
      }
      if (parts[2] === "process" && parts[3] === "execute") {
        const input = JSON.parse(body.toString());
        if (opts.failExec && opts.failExec.test(input.command)) return json(res, 200, { stdout: "", stderr: "injected failure", exitCode: 1 });
        const r = spawnSync("bash", ["-c", input.command], {
          cwd: path.join(s.home, "work"),
          encoding: "utf8",
          env: {
            PATH: process.env.PATH,
            HOME: s.home,
            GIT_CONFIG_NOSYSTEM: "1",
            GIT_CONFIG_GLOBAL: path.join(s.home, ".gitconfig"),
          },
          timeout: (input.timeout ?? 300) * 1000,
        });
        return json(res, 200, { stdout: r.stdout, stderr: r.stderr, exitCode: r.status ?? -1 });
      }
    }
    return json(res, 404, { error: "not_found" });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  server.base = `http://127.0.0.1:${server.address().port}`;
  return {
    base: server.base,
    apiUrl: `${server.base}/api/v1`,
    sandboxes,
    requests,
    opts,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}
