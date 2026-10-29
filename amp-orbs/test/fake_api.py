#!/usr/bin/env python3
"""A stand-in for the Prized Sandbox API on 127.0.0.1, for test.sh: the routes dispatch.sh uses.

    fake_api.py STATE_DIR    serves until killed; writes its port to STATE_DIR/port

Control plane under /api/v1, the edge under /edge/<id> (each sandbox's toolboxBase). A sandbox's
home is STATE_DIR/<id>/home, where /process/execute runs commands with bash, an environment of only
PATH (sandbox-bin first), HOME, STUB_* settings and the request's env, as a real sandbox has none of
this machine's. Every request is appended to STATE_DIR/requests.jsonl and the sandboxes are written
to STATE_DIR/state.json after each one.

STATE_DIR/faults.json, if present, is a list of {method, path, times?, and status (with body? and
retryAfter?), drop, dropAfter, delay or delayAnswer}: the next `times` requests whose method matches
and whose path contains `path` get that answer instead, no answer (drop: nothing done; dropAfter:
done, then the answer lost), or their answer that many seconds late (delay: before the request is
handled; delayAnswer: after). STATE_DIR/seed.json, read at start, is a
list of sandboxes to begin with. A sandbox's `slow` field (reads left) holds it in its state until
it has been read that many times, then it moves to `target`; FAKE_SLOW_* set that for creates,
resumes and pauses. Creates and forks honour Idempotency-Key as the real API does. FAKE_DELETE_PAGE
caps how many sandboxes one delete by label takes.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(sys.argv[1])
HERE = Path(__file__).resolve().parent
KEY = os.environ.get("FAKE_PRIZED_KEY", "dcp_000000000000000000000000000000test")
SANDBOXES = {}
IDEMPOTENT = {}
CLOCK = [0]
CREATE_FIELDS = {"template", "resources", "envVars", "labels", "autoPauseMin", "autoDeleteMin", "maxRuntimeMin"}
EXEC_FIELDS = {"command", "cwd", "env", "timeout"}
PORT = 0


def save():
    tmp = STATE / "state.json.tmp"
    tmp.write_text(json.dumps(list(SANDBOXES.values()), indent=1))
    os.replace(tmp, STATE / "state.json")


def created_at():
    CLOCK[0] += 1
    return f"2026-10-28T12:00:{CLOCK[0]:02d}Z"


def public(s):
    s = {k: v for k, v in s.items() if k not in ("slow", "target")}
    s["toolboxBase"] = f"http://127.0.0.1:{PORT}/edge/{s['id']}"
    return s


def home(sid):
    return STATE / sid / "home"


def fault(method, path):
    f = STATE / "faults.json"
    if not f.exists():
        return None
    faults = json.loads(f.read_text())
    for x in faults:
        if x.get("method", method) == method and x["path"] in path and x.get("times", 1) > 0:
            x["times"] = x.get("times", 1) - 1
            f.write_text(json.dumps(faults))
            return x
    return None


def settle(s):
    """A slow sandbox moves on to its target after `slow` reads."""
    if s.get("slow", 0) > 0:
        s["slow"] -= 1
        if s["slow"] == 0:
            s["state"] = s.pop("target")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def answer(self, status, body=None, headers=()):
        time.sleep(getattr(self, "delay_answer", 0))
        save()
        if getattr(self, "drop_after", False):
            self.close_connection = True
            self.connection.shutdown(2)
            return None
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def handle_one(self, method):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = {"unparsed": raw.decode(errors="replace")}
        with open(STATE / "requests.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "method": method, "path": url.path, "query": url.query, "body": body,
                                "idem": self.headers.get("Idempotency-Key"),
                                "auth": self.headers.get("Authorization") == f"Bearer {KEY}"}) + "\n")
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self.answer(401, {"error": "unauthorized"})
        f = fault(method, url.path)
        if f and f.get("delay"):
            time.sleep(f["delay"])
        elif f and f.get("delayAnswer"):
            self.delay_answer = f["delayAnswer"]
        elif f and f.get("dropAfter"):
            self.drop_after = True
        elif f and f.get("drop"):
            self.close_connection = True
            self.connection.shutdown(2)
            return None
        elif f:
            hdrs = [("Retry-After", str(f["retryAfter"]))] if "retryAfter" in f else []
            return self.answer(f["status"], f.get("body", {"error": "injected"}), hdrs)
        idem = self.headers.get("Idempotency-Key")
        if idem and method == "POST":
            if idem in IDEMPOTENT:
                first_body, first_answer = IDEMPOTENT[idem]
                if first_body != body:
                    return self.answer(409, {"error": "idempotency_conflict"})
                return self.answer(200, first_answer)
            self.idem = (idem, body)
        parts = [p for p in url.path.split("/") if p]
        if parts[:2] == ["api", "v1"] and parts[2:3] == ["sandboxes"]:
            return self.control(method, parts[3:], query, body)
        if parts[:1] == ["edge"] and len(parts) == 4 and parts[2:] == ["process", "execute"] and method == "POST":
            return self.execute(parts[1], body)
        return self.answer(404, {"error": "not_found"})

    def control(self, method, rest, query, body):
        labels = [v.split("=", 1) for v in query.get("label", [])]

        def selected():
            return [s for s in SANDBOXES.values() if all(s["labels"].get(k) == v for k, v in labels)]

        if not rest and method == "GET":
            return self.answer(200, {"sandboxes": [public(s) for s in reversed(selected())]})
        if not rest and method == "DELETE":
            if not labels:
                return self.answer(400, {"error": "label_required"})
            ids = [s["id"] for s in selected()]
            page = int(os.environ.get("FAKE_DELETE_PAGE", "250"))
            more = len(ids) > page
            for i in ids[:page]:
                SANDBOXES.pop(i)
            return self.answer(202, {"count": len(ids[:page]), "ids": ids[:page], "more": more})
        if not rest and method == "POST":
            unknown = set(body or {}) - CREATE_FIELDS
            if unknown:
                return self.answer(422, {"error": "unknown_field", "field": sorted(unknown)[0]})
            if not (body or {}).get("template"):
                return self.answer(400, {"error": "bad_request", "field": "template"})
            sid = "sbx_" + secrets.token_hex(8)
            s = {"id": sid, "state": "running", "template": body["template"], "labels": body.get("labels") or {},
                 "resources": body.get("resources") or {}, "maxRuntimeMin": body.get("maxRuntimeMin"),
                 "autoDeleteMin": body.get("autoDeleteMin"), "autoPauseMin": body.get("autoPauseMin"),
                 "envVars": sorted((body.get("envVars") or {}).keys()), "createdAt": created_at()}
            slow = int(os.environ.get("FAKE_SLOW_CREATE", "0"))
            if slow:
                s.update(state="starting", slow=slow, target="running")
            SANDBOXES[sid] = s
            (home(sid) / "work").mkdir(parents=True)
            self.remember({"sandbox": public(s)})
            return self.answer(201, {"sandbox": public(s)})
        s = SANDBOXES.get(rest[0])
        if s is None:
            return self.answer(404, {"error": "not_found"})
        if len(rest) == 1 and method == "GET":
            settle(s)
            return self.answer(200, {"sandbox": public(s)})
        if len(rest) == 1 and method == "DELETE":
            SANDBOXES.pop(s["id"])
            return self.answer(202, {"sandbox": dict(public(s), state="deleting")})
        if len(rest) == 2 and method == "POST" and rest[1] == "fork":
            unknown = set(body or {}) - {"count", "labels", "maxRuntimeMin"}
            if unknown:
                return self.answer(422, {"error": "unknown_field", "field": sorted(unknown)[0]})
            if s["state"] not in ("running", "paused"):
                return self.answer(409, {"error": "parent_not_running", "state": s["state"]})
            if os.environ.get("FAKE_FORK_FULL") == "1":
                return self.answer(409, {"error": "fork_capacity", "fits": 0})
            cid = "sbx_" + secrets.token_hex(8)
            c = dict(s, id=cid, labels=body.get("labels", s["labels"]), forkedFrom=s["id"], createdAt=created_at(),
                     maxRuntimeMin=body.get("maxRuntimeMin", s.get("maxRuntimeMin")), state="running")
            slow = int(os.environ.get("FAKE_SLOW_CREATE", "0"))
            if slow:
                c.update(state="starting", slow=slow, target="running")
            SANDBOXES[cid] = c
            shutil.copytree(home(s["id"]), home(cid), symlinks=True)
            fork = {"id": "frk_" + secrets.token_hex(8), "parentId": s["id"], "count": 1, "sandboxIds": [cid]}
            self.remember({"fork": fork, "sandboxes": [public(c)]})
            return self.answer(202, {"fork": fork, "sandboxes": [public(c)]})
        if len(rest) == 2 and method == "POST" and rest[1] in ("pause", "resume"):
            target, moving = ("paused", "pausing") if rest[1] == "pause" else ("running", "starting")
            if s["state"] == "resizing":
                return self.answer(409, {"error": "sandbox_not_running", "state": "resizing"})
            degraded = s["state"] == "error" and str(s.get("errorReason") or "").startswith("runtime_degraded")
            if s["state"] == "error" and not (degraded and rest[1] == "pause"):
                return self.answer(409, {"error": "sandbox_not_running"})
            slow = int(os.environ.get("FAKE_SLOW_" + rest[1].upper(), "0"))
            if slow:
                s.update(state=moving, slow=slow, target=target)
            else:
                s["state"] = target
            waited = query.get("wait") == [target] and s["state"] == target
            return self.answer(200 if waited else 202, {"sandbox": public(s)})
        return self.answer(404, {"error": "not_found"})

    def remember(self, answer):
        if getattr(self, "idem", None):
            IDEMPOTENT[self.idem[0]] = (self.idem[1], answer)

    def execute(self, sid, body):
        s = SANDBOXES.get(sid)
        if s is None:
            return self.answer(404, {"error": "not_found"})
        unknown = set(body or {}) - EXEC_FIELDS
        if unknown:
            return self.answer(422, {"error": "unknown_field", "field": sorted(unknown)[0]})
        if s["state"] in ("paused", "pausing"):
            s["state"] = "running"
            s["woken"] = s.get("woken", 0) + 1
        h = home(sid)
        env = {"PATH": f"{HERE}/sandbox-bin:/usr/bin:/bin", "HOME": str(h), "STUB_AMP_SRC": str(HERE / "stub-amp")}
        env.update({k: v for k, v in os.environ.items() if k.startswith("STUB_")})
        env.update(body.get("env") or {})
        t0 = time.time()
        try:
            p = subprocess.run(["bash", "-c", body["command"]], cwd=body.get("cwd") or h / "work", env=env,
                               capture_output=True, text=True, timeout=body.get("timeout", 300), stdin=subprocess.DEVNULL)
            out = {"stdout": p.stdout, "stderr": p.stderr, "exitCode": p.returncode}
        except subprocess.TimeoutExpired:
            out = {"stdout": "", "stderr": "", "exitCode": -1, "timedOut": True}
        out["durationMs"] = int((time.time() - t0) * 1000)
        return self.answer(200, out)

    def do_GET(self):
        self.handle_one("GET")

    def do_POST(self):
        self.handle_one("POST")

    def do_DELETE(self):
        self.handle_one("DELETE")


def main():
    global PORT
    seed = STATE / "seed.json"
    if seed.exists():
        for s in json.loads(seed.read_text()):
            s.setdefault("createdAt", created_at())
            SANDBOXES[s["id"]] = s
            (home(s["id"]) / "work").mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    PORT = srv.server_address[1]
    save()
    (STATE / "port").write_text(str(PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
