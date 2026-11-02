// An in-memory stand-in for the Prized Sandbox API and a sandbox's edge, behind a `fetch`. Commands
// are recognised by what they run (install, daemon start, pair); each step can be told to fail.

export const FAKE_KEY = `dcp_${"a".repeat(48)}`;
export const AGENT_KEY = "sk-ant-test-FAKE-do-not-leak";

export function pairingUrl(serverId = "srv_test123") {
  const offer = { v: 2, serverId, daemonPublicKeyB64: Buffer.alloc(32, 7).toString("base64"), relay: { endpoint: "relay.paseo.sh:443", useTls: true } };
  return `https://app.paseo.sh/#offer=${Buffer.from(JSON.stringify(offer)).toString("base64url")}`;
}

export function fakePrized({ fail = {}, workspace = "acme" } = {}) {
  const sandboxes = new Map();
  const idempotency = new Map();
  const processes = new Map();
  const requests = [];
  let n = 0;

  const json = (status, body) => new Response(body === undefined ? "" : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

  async function fetchImpl(url, init = {}) {
    const u = new URL(url);
    const method = init.method ?? "GET";
    const headers = init.headers ?? {};
    const body = init.body ? JSON.parse(init.body) : undefined;
    requests.push({ method, path: u.pathname + u.search, headers, body });
    if (headers.Authorization !== `Bearer ${FAKE_KEY}`) return json(401, { error: "unauthorized", message: "bad key" });

    if (u.host === "api.prized.dev") {
      const path = u.pathname.replace("/api/v1", "");
      if (path === "/me") return json(200, { workspace: { id: "ws1", name: workspace }, access: { sandboxes: "write" } });
      if (path === "/sandboxes" && method === "POST") {
        if (fail.create) return json(409, { error: "insufficient_balance", message: "The balance is empty." });
        const key = headers["Idempotency-Key"];
        if (key && idempotency.has(key)) return json(200, { sandbox: sandboxes.get(idempotency.get(key)) });
        const id = `sbx_${(++n).toString(16).padStart(16, "0")}`;
        const sandbox = { id, state: "running", toolboxBase: `https://term.prized.dev/v1/sandbox/${id}`, expiresAt: "2026-11-03T00:00:00Z", labels: body.labels, request: body };
        sandboxes.set(id, sandbox);
        if (key) idempotency.set(key, id);
        return json(201, { sandbox });
      }
      const m = path.match(/^\/sandboxes\/([^/]+)(?:\/(pause|resume))?$/);
      if (m) {
        const sandbox = sandboxes.get(m[1]);
        if (!sandbox || sandbox.state === "deleted") return json(404, { error: "not_found", message: "no sandbox" });
        if (method === "GET") return json(200, { sandbox });
        if (method === "DELETE") {
          sandbox.state = "deleted";
          return json(202, { sandbox });
        }
        if (m[2] === "pause") {
          sandbox.state = "paused";
          sandbox.expiresAt = null;
          return json(200, { sandbox });
        }
        if (m[2] === "resume") {
          sandbox.state = "running";
          sandbox.expiresAt = "2026-11-03T00:00:00Z";
          sandbox.resumes = (sandbox.resumes ?? 0) + 1;
          return json(200, { sandbox });
        }
      }
      return json(404, { error: "no_route" });
    }

    if (u.host === "term.prized.dev") {
      const m = u.pathname.match(/^\/v1\/sandbox\/([^/]+)\/process(?:\/(.+))?$/);
      const sandbox = m && sandboxes.get(m[1]);
      if (!sandbox) return json(404, { error: "not_found" });
      if (sandbox.state === "paused") sandbox.state = "running";
      if (method === "POST" && m[2] === "start") {
        const command = body.command;
        const id = `p_${(++n).toString(16).padStart(12, "0")}`;
        let result = { exitCode: 0, stdout: "", stderr: "" };
        if (command.includes("npm install -g")) {
          result = fail.install ? { exitCode: 1, stdout: "", stderr: "npm ERR! network unreachable" } : { exitCode: 0, stdout: "0.14.0\nclaude: /usr/local/bin/claude\n", stderr: "" };
          sandbox.installs = (sandbox.installs ?? 0) + 1;
        } else if (command.includes("daemon start")) {
          result = fail.start ? { exitCode: 1, stdout: "", stderr: "daemon did not start" } : { exitCode: 0, stdout: "running\n", stderr: "" };
          sandbox.starts = (sandbox.starts ?? 0) + 1;
        } else if (command.includes("daemon pair")) {
          result = { exitCode: 0, stdout: JSON.stringify({ relayEnabled: true, url: fail.pairBad ? "https://evil.example/#nothing" : pairingUrl(), qr: "...", connectionUri: "x" }), stderr: "" };
        }
        sandbox.commands = [...(sandbox.commands ?? []), command];
        processes.set(id, result);
        return json(201, { process: { processId: id, running: true } });
      }
      if (method === "GET" && m[2]) {
        const r = processes.get(decodeURIComponent(m[2]));
        if (!r) return json(404, { error: "process_not_found" });
        return json(200, { process: { running: false, exitCode: r.exitCode, stdout: r.stdout, stderr: r.stderr } });
      }
    }
    return json(404, { error: "no_route" });
  }

  // `fail` is live: a test can switch a failure off and retry.
  return { fetch: fetchImpl, sandboxes, requests, fail };
}
