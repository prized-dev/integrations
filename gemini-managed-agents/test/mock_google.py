#!/usr/bin/env python3
"""A stand-in for Google's side of a self-hosted environment, for test.sh.

Usage: mock_google.py QUEUE.json STATE_DIR

Two parts, both written from the gemini-api CLI's source (internal/cli/custom/envruntime/poller.go,
v0.4.6), not from its wire protocol:

1. The poller's handoff, as `gemini-api environments poll --provision-cmd CMD` does it: for each
   interaction in QUEUE.json it runs `sh -c CMD` in a process group of its own, with the v1 lease
   JSON on stdin, Gemini and Google credentials removed from the environment, stdout thrown away
   and stderr kept. A command still running at "provision_timeout" seconds has its whole process
   group killed. A failed or killed command is tried again after a backoff, up to "max_tries".
   An item with "after_file" is handed out once a file matching that glob (under STATE_DIR) exists.

2. An HTTP server on loopback that the stub worker in a fake sandbox calls with its lease token
   (POST /v1beta/interactions/ID:attach and :complete; the real worker opens a stream instead), and
   that answers Prized's POST /api/v1/sandboxes/ID/resume for fake-prized's sandboxes.

It writes STATE_DIR/port, results.json (every try of every interaction), attaches.json and
completes.json. Lease tokens and keys are never written to its logs.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

queue = json.loads(Path(sys.argv[1]).read_text())
state = Path(sys.argv[2])
HERE = Path(__file__).resolve().parent
items = {i["id"]: i for i in queue["items"]}
prized_key = queue.get("prized_key")
lock = threading.Lock()
issued = {}  # interaction id -> the lease tokens handed out for it
results, attaches, completes = {}, {}, {}
# What the CLI strips from a provision command's environment (envruntime/shell.go).
SCRUBBED = {
    "GEMINI_ENVIRONMENT_KEY", "ENVIRONMENT_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_ACCESS_TOKEN",
    "ACCESS_TOKEN", "GOOGLE_OAUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN", "GEMINI_LEASE_TOKEN", "LEASE_TOKEN",
}


def write(name, doc):
    tmp = state / (name + ".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, state / name)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, code, doc):
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        path = self.path.split("?")[0]
        m = re.fullmatch(r"/api/v1/sandboxes/(sbx_[0-9a-f]+)/resume", path)
        if m:
            if self.headers.get("Authorization", "") != f"Bearer {prized_key}":
                return self.answer(401, {"error": "unauthorized"})
            return self.prized_resume(m.group(1))
        m = re.fullmatch(r"/v1beta/interactions/([^/:]+):(attach|complete)", path)
        if not m:
            return self.answer(404, {"error": {"status": "NOT_FOUND"}})
        iid, verb = m.groups()
        token = self.headers.get("x-goog-lease-token", "")
        # A worker must come with its interaction's lease token and nothing stronger.
        stronger = any(self.headers.get(h) for h in ("x-goog-api-key", "x-goog-environment-key", "Authorization"))
        ok = token in issued.get(iid, []) and not items.get(iid, {}).get("reject_attach")
        with lock:
            if verb == "attach":
                attaches.setdefault(iid, []).append({"t": time.time(), "ok": ok, "stronger_credential": stronger})
                write("attaches.json", attaches)
            elif ok:
                completes[iid] = time.time()
                write("completes.json", completes)
        if not ok:
            return self.answer(403, {"error": {"status": "PERMISSION_DENIED"}})
        return self.answer(200, {})

    def prized_resume(self, sid):
        meta = state / "prized" / sid / "meta.json"
        if not meta.exists() or json.loads(meta.read_text())["state"] in ("deleted", "deleting"):
            return self.answer(404, {"error": "not_found"})
        with lock:
            doc = json.loads(meta.read_text())
            doc["resume_calls"] = doc.get("resume_calls", 0) + 1
            meta.write_text(json.dumps(doc))
        if doc.get("resume_fails"):
            reason = doc.get("resume_reason", "running_vcpu")
            if reason == "invalid_resources":  # the shape can't run on any host right now
                return self.answer(422, {"error": "invalid_resources", "field": "host_capacity"})
            return self.answer(429, {"error": "sandbox_quota_exceeded", "reason": reason})
        # This call is the mock's own, not the script's: keep it out of what calls.log says about keys.
        env = {k: v for k, v in os.environ.items() if k.upper() not in SCRUBBED}
        env["FAKE_PRIZED_ROOT"] = str(state / "prized")
        subprocess.run([str(HERE / "fake-prized"), "sandbox", "resume", sid], env=env, check=True)
        return self.answer(200, {"sandbox": {"id": sid, "state": "running"}})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
write("port", port)
threading.Thread(target=server.serve_forever, daemon=True).start()


def provision_env():
    keep = queue.get("leak_credentials")  # an older poller that did not scrub: the script must cope
    env = {k: v for k, v in os.environ.items() if keep or k.upper() not in SCRUBBED}
    env["PRIZED_API_URL"] = f"http://127.0.0.1:{port}/api/v1"
    return env


def dispatch(item):
    gate = item.get("after_file")
    while gate and not any(state.glob(gate)):
        time.sleep(0.1)
    iid = item["id"]
    for n in range(1, int(item.get("max_tries", queue.get("max_tries", 3))) + 1):
        token = item["token"] if n == 1 else f"{item['token']}.try{n}"
        with lock:
            issued.setdefault(iid, []).append(token)
        payload = {
            "schema_version": "v1", "interaction_id": iid, "environment_id": item["environment"],
            "lease_token": token, "server_url": f"http://127.0.0.1:{port}",
        }
        if "custom_config" in item:
            payload["environment_config"] = {"custom_config": item["custom_config"]}
        start = time.time()
        proc = subprocess.Popen(
            ["sh", "-c", queue["provision_cmd"]], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=provision_env(), cwd=state, start_new_session=True,
        )
        chunks = []
        reader = threading.Thread(target=lambda: chunks.append(proc.stderr.read()), daemon=True)
        reader.start()
        try:
            proc.stdin.write(json.dumps(payload).encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        timed_out = False
        try:
            code = proc.wait(timeout=float(queue.get("provision_timeout", 120)))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            code = proc.wait()
        end = time.time()
        reader.join(timeout=5)  # the CLI gives the command's pipes 5 s after it exits (cmd.WaitDelay)
        stderr = b"".join(c for c in chunks if c).decode(errors="replace").replace(token, "[REDACTED]")
        with lock:
            results.setdefault(iid, []).append({
                "exit": code, "timed_out": timed_out, "start": start, "end": end,
                "stderr_held": reader.is_alive(), "stderr": stderr[-4000:],
            })
            write("results.json", results)
        if code == 0:
            return
        time.sleep(min(0.5 * 2 ** (n - 1), 15))


threads = [threading.Thread(target=dispatch, args=(i,), daemon=True) for i in queue["items"]]
for t in threads:
    t.start()
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
while True:
    time.sleep(1)
