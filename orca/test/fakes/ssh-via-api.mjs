#!/usr/bin/env node
// For test/live.mjs only: an `ssh` that runs the remote command in the real sandbox through the
// API's exec route instead of over ssh (the API half of the plugin is what a live run checks; the
// ssh half is checked against a local sshd in lifecycle.test.mjs). Like the real bridge it never
// wakes a sandbox: a sandbox that is not running is refused (Prized-Auto-Resume: false).
const argv = process.argv.slice(2);
const dash = argv.indexOf("--");
const dest = argv[dash + 1] ?? "";
const command = argv.slice(dash + 2).join(" ");
const id = dest.split("@").pop();
const API = process.env.FAKE_LIVE_API;
const auth = { Authorization: `Bearer ${process.env.FAKE_LOGIN_BEARER}` };
const r = await fetch(`${API}/sandboxes/${id}`, { headers: auth });
const sandbox = r.status === 200 ? (await r.json()).sandbox : null;
if (sandbox?.state !== "running") {
  process.stderr.write(`ssh-via-api: ${id} is ${sandbox?.state ?? "gone"}\n`);
  process.exit(255);
}
const x = await fetch(`${sandbox.toolboxBase}/process/execute`, {
  method: "POST",
  headers: { ...auth, "Content-Type": "application/json", "Prized-Auto-Resume": "false" },
  body: JSON.stringify({ command: command || "true", timeout: 60 }),
});
const out = await x.json();
process.stdout.write(out.stdout ?? "");
process.stderr.write(out.stderr ?? "");
process.exit(x.status === 200 ? out.exitCode : 255);
