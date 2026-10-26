#!/usr/bin/env python3
"""A stand-in for Anthropic's environment work queue, enough for `ant beta:worker poll --on-work`.

Usage: mock_api.py QUEUE.json STATE_DIR
Serves the work items in QUEUE.json one at a time: the next is handed out only once the one before
it was stopped, the way a session's follow-up arrives after its run, or, for an item with
"after_file", once a file matching that glob (under STATE_DIR) exists. It also answers Prized's
POST /api/v1/sandboxes/ID/resume for fake-prized's sandboxes (STATE_DIR/prized). It records every request
(never the key itself) in STATE_DIR/requests.log and each stop's time in STATE_DIR/stops.json, and
writes the port it listens on to STATE_DIR/port.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

queue = json.loads(Path(sys.argv[1]).read_text())
state = Path(sys.argv[2])
env_id, env_key = queue["environment_id"], queue["environment_key"]
prized_key = queue.get("prized_key")
HERE = Path(__file__).resolve().parent
items = queue["items"]
lock = threading.Lock()
handed = 0
stops = {}


def work_doc(item):
    blank = ""
    return {
        "id": item["id"], "type": "work", "environment_id": env_id, "state": "queued",
        "data": {"type": "session", "id": item["session"]}, "secret": item.get("secret", blank),
        "metadata": {}, "created_at": "2026-10-25T00:00:00Z", "acknowledged_at": blank,
        "latest_heartbeat_at": blank, "started_at": blank, "stop_requested_at": blank, "stopped_at": blank,
    }


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

    def handle_any(self):
        global handed
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        auth = self.headers.get("Authorization", "") + self.headers.get("X-Api-Key", "")
        authed = env_key in auth or (prized_key and auth == f"Bearer {prized_key}" and self.path.startswith("/api/v1/"))
        with open(state / "requests.log", "a") as f:
            f.write(json.dumps({"t": time.time(), "method": self.command, "path": self.path, "authed": authed}) + "\n")
        if not authed:
            return self.answer(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
        path = self.path.split("?")[0]
        m = re.fullmatch(r"/api/v1/sandboxes/(sbx_[0-9a-f]+)/resume", path)
        if m and self.command == "POST":  # Prized's resume, on fake-prized's sandboxes
            return self.prized_resume(m.group(1))
        prefix = f"/v1/environments/{env_id}/work"
        if path == prefix + "/poll" and self.command == "GET":
            with lock:
                gate = items[handed].get("after_file") if handed < len(items) else None
                if gate:  # released once a file matching this glob exists, stopped or not
                    ready = any(state.glob(gate))
                else:
                    ready = handed < len(items) and (handed == 0 or items[handed - 1]["id"] in stops)
                if ready:
                    item = items[handed]
                    handed += 1
                    return self.answer(200, work_doc(item))
            time.sleep(0.2)
            return self.answer(200, {})
        m = re.fullmatch(prefix + r"/([^/]+)/(ack|stop|heartbeat)", path)
        if m and self.command == "POST":
            work_id, verb = m.groups()
            if verb == "stop":
                with lock:
                    stops.setdefault(work_id, time.time())
                    (state / "stops.json").write_text(json.dumps(stops))
            item = next(i for i in items if i["id"] == work_id)
            return self.answer(200, work_doc(item))
        return self.answer(404, {"type": "error", "error": {"type": "not_found_error", "message": path}})

    def prized_resume(self, sid):
        meta = state / "prized" / sid / "meta.json"
        if not meta.exists() or json.loads(meta.read_text())["state"] in ("deleted", "deleting"):
            return self.answer(404, {"error": "not_found"})
        doc = json.loads(meta.read_text())
        doc["resume_calls"] = doc.get("resume_calls", 0) + 1
        meta.write_text(json.dumps(doc))
        if doc.get("resume_fails"):
            reason = doc.get("resume_reason", "running_vcpu")
            if reason == "invalid_resources":  # the shape can't run on any host right now
                return self.answer(422, {"error": "invalid_resources", "field": "host_capacity"})
            return self.answer(429, {"error": "sandbox_quota_exceeded", "reason": reason})
        env = dict(os.environ, FAKE_PRIZED_ROOT=str(state / "prized"))
        subprocess.run([str(HERE / "fake-prized"), "sandbox", "resume", sid], env=env, check=True)
        return self.answer(200, {"sandbox": {"id": sid, "state": "running"}})

    do_GET = do_POST = handle_any


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
(state / "port").write_text(str(server.server_address[1]))
server.serve_forever()
