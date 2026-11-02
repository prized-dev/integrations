#!/usr/bin/env node
// The fake CLI's ProxyCommand, in place of `prized sandbox _stdio ID`: like the real bridge it
// refuses a sandbox that is not running (exit 3, "not running") and never wakes it; otherwise it
// hands the stream to a local sshd in inetd mode, set up like a sandbox guest (FAKE_SSHD_DIR).
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const id = process.argv[2];
const env = process.env;
if (env.FAKE_CLI_LOG) fs.appendFileSync(env.FAKE_CLI_LOG, `${JSON.stringify(["_bridge", id])}\n`);
const r = await fetch(`${env.FAKE_API}/api/v1/sandboxes/${id}`, { headers: { Authorization: "Bearer fake-login-token" } });
const state = r.status === 200 ? (await r.json()).sandbox.state : "gone";
if (state !== "running") {
  process.stderr.write(`prized: ${id} is not running; run prized sandbox ssh ${id} to resume it\n`);
  process.exit(3);
}
const child = spawn(env.FAKE_SSHD_BIN || "/usr/sbin/sshd", ["-i", "-f", path.join(env.FAKE_SSHD_DIR, "sshd_config")], { stdio: "inherit" });
child.on("exit", (code) => process.exit(code ?? 1));
