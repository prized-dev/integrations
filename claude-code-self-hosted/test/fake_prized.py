"""A fake Prized sandbox API for spawn-runner's offline tests.

Each sandbox is a directory (ROOT/<id>/home stands in for /home/prized). Commands run with bash -c, HOME set to that
directory and PATH starting with the test's sandbox-bin (stub claude and curl), so no real Claude Code or network.
DELETE ends the runner's process group (run.sh's pid file). Implements what the hook uses: POST /api/v1/sandboxes (Idempotency-Key replay, ?wait=running), GET /api/v1/sandboxes
(?label=k=v), DELETE /api/v1/sandboxes/{id}, and the toolbox routes /process/execute and /files/upload under
/tb/{id}. Control: ROOT/fail.json maps a route name ("create", "upload", "exec") to a list of HTTP codes to answer
before working normally ([503] once, then fine). Every request is appended to ROOT/requests.jsonl, the bearer
replaced by whether it matched KEY.

Own-box mode (spawn-runner PRIZED_RUN_ON=own-box): ROOT/team.json is GET /api/v1/team/status, its boxes' observedState
and desiredState read live from ROOT/box/<id>/{state,desired} (default running). Each box is ROOT/box/<id>/home; its
edge is /edge/v1/box/<id>/ (exec, synchronous under sh -c or detached in its own session; upload). POST
/api/v1/boxes/<id>/wake turns a sleeping box running after ROOT/box/<id>/wake_after seconds (default 1). Box files:
noclaude (no stub claude on its PATH), old-agent (keepAwake refused). Fail routes: team, wake, me, exec, upload, start.
"""
import http.server, json, os, signal, subprocess, sys, threading, time, urllib.parse, uuid

PORT, ROOT, KEY, SBIN = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
lock = threading.Lock()
sandboxes = {}  # id -> dict
idem = {}       # key -> (body, response)


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t or time.time()))


def fail_for(route):
    p = os.path.join(ROOT, "fail.json")
    with lock:
        try:
            f = json.load(open(p))
        except Exception:
            return None
        codes = f.get(route) or []
        if not codes:
            return None
        code = codes.pop(0)
        json.dump(f, open(p, "w"))
        return code


def box_dir(bid):
    return os.path.join(ROOT, "box", bid)


def box_get(bid, name, default="running"):
    try:
        return open(os.path.join(box_dir(bid), name)).read().strip() or default
    except OSError:
        return default


def box_set(bid, name, value):
    open(os.path.join(box_dir(bid), name), "w").write(value)


def box_obj(bid):
    return {"id": bid, "observedState": box_get(bid, "state"), "desiredState": box_get(bid, "desired")}


def wake(bid):
    with lock:
        if box_get(bid, "state") == "running" or box_get(bid, "state") == "waking":
            box_set(bid, "desired", "running")
            return
        box_set(bid, "desired", "running")
        box_set(bid, "state", "waking")
        open(os.path.join(box_dir(bid), "wakes"), "a").write(now_iso() + "\n")
    after = float(box_get(bid, "wake_after", "1"))

    def up():
        time.sleep(after)
        if box_get(bid, "state") == "waking" and box_get(bid, "desired") == "running":
            box_set(bid, "state", "running")
    threading.Thread(target=up, daemon=True).start()


class H(http.server.BaseHTTPRequestHandler):
    def send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        authed = self.headers.get("Authorization") == "Bearer " + KEY
        rec = {"m": self.command, "p": u.path, "q": q, "authed": authed,
               "idem": self.headers.get("Idempotency-Key"), "len": len(body)}
        if u.path.endswith("/exec") and body:
            try:
                b = json.loads(body)
                rec.update({"cmd": b.get("command", "")[:400], "detached": b.get("detached"), "keepAwake": b.get("keepAwake")})
            except Exception:
                pass
        with open(os.path.join(ROOT, "requests.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        if not authed:
            return self.send(401, {"error": "unauthorized", "message": "No key, or a revoked one"})
        parts = u.path.strip("/").split("/")
        if parts[:3] == ["api", "v1", "sandbox-templates"]:
            rt = os.environ.get("FAKE_RUNTIME_MIN", "1440")
            rt = 1440 if rt == "none" else int(rt)
            return self.send(200, {"templates": [{"id": t, "defaultMaxRuntimeMin": rt} for t in ("prized-agent-v1", "prized-base-v1")]})
        if parts[:3] == ["api", "v1", "sandboxes"]:
            return self.sandboxes_route(parts[3:], q, body)
        if parts[0] == "tb" and len(parts) >= 3:
            return self.toolbox(parts[1], "/".join(parts[2:]), q, body)
        if parts[:3] == ["api", "v1", "me"]:
            code = fail_for("me")
            if code:
                return self.send(code, {"error": "injected"})
            return self.send(200, {"edge": {"url": f"http://127.0.0.1:{PORT}/edge"}, "user": {"role": "owner"}})
        if parts[:4] == ["api", "v1", "team", "status"]:
            return self.team_status()
        if parts[:3] == ["api", "v1", "boxes"] and len(parts) >= 4:
            return self.box_route(parts[3], parts[4:], body)
        if parts[:3] == ["edge", "v1", "box"] and len(parts) == 5:
            return self.edge(parts[3], parts[4], q, body)
        self.send(404, {"error": "not_found"})

    do_GET = do_POST = do_DELETE = do_PUT = handle_any

    def injected(self, route, edge=False):
        code = fail_for(route)
        if isinstance(code, dict):
            self.send(code["code"], code["body"])
            return True
        if code:
            self.send(code, {"ok": False, "error": {"code": "injected", "message": f"injected {code}"}} if edge
                      else {"error": "injected", "message": f"injected {code}"})
            return True
        return False

    def team_status(self):
        if self.injected("team"):
            return
        t = json.load(open(os.path.join(ROOT, "team.json")))
        for b in t.get("boxes", []):
            b["observedState"] = box_get(b["id"], "state", b.get("observedState", "running"))
            b["desiredState"] = box_get(b["id"], "desired", b.get("desiredState", "running"))
        return self.send(200, t)

    def box_route(self, bid, rest, body):
        d = box_dir(bid)
        if not os.path.isdir(d):
            return self.send(404, {"error": "not_found"})
        if self.command == "POST" and rest == ["wake"]:
            if self.injected("wake"):
                return
            if box_get(bid, "desired", "running") == "terminated":
                return self.send(410, {"error": "box_deleted", "message": "the box is being deleted"})
            wake(bid)
            return self.send(202, {"box": box_obj(bid)})
        if self.command == "GET" and not rest:
            return self.send(200, {"box": box_obj(bid)})
        self.send(404, {"error": "not_found"})

    def edge(self, bid, route, q, body):
        d = box_dir(bid)
        if not os.path.isdir(d):
            return self.send(404, {"ok": False, "error": {"code": "not_found", "message": "no such box"}})
        home = os.path.join(d, "home")
        if route == "exec" and self.command == "POST":
            if self.injected("exec", edge=True):
                return
            req = json.loads(body)
            if box_get(bid, "state") != "running":  # the edge wakes a sleeping box before a command, as termd does
                wake(bid)
                for _ in range(3000):
                    if box_get(bid, "state") == "running":
                        break
                    time.sleep(0.1)
            path = "/usr/bin:/bin" if os.path.exists(os.path.join(d, "noclaude")) else SBIN + ":/usr/bin:/bin"
            env = {"HOME": home, "PATH": path, "LANG": "C.UTF-8", "FAKE_ROOT": ROOT}
            if req.get("detached"):
                if req.get("keepAwake") and os.path.exists(os.path.join(d, "old-agent")):
                    return self.send(409, {"ok": False, "error": {"code": "agent_upgrade_required", "message":
                        "cannot confirm keep-awake protection; the running box agent must advertise the runs capability; no process started"}})
                if self.injected("start", edge=True):
                    return
                p = subprocess.Popen(["sh", "-c", req["command"]], cwd=home, env=env, start_new_session=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=open(os.path.join(d, "detached.err"), "a"))
                with open(os.path.join(d, "pids"), "a") as f:
                    f.write(f"{p.pid}\n")
                return self.send(200, {"ok": True, "type": "exec.started", "processId": "p_" + uuid.uuid4().hex[:8],
                                       "pid": p.pid, "cwd": "~", "startedAt": now_iso()})
            delay = fail_for("exec_delay")
            p = subprocess.run(["sh", "-c", req["command"]], cwd=home, env=env, capture_output=True, text=True,
                               timeout=req.get("timeoutSeconds", 30))
            if delay:
                time.sleep(delay)
            return self.send(200, {"ok": True, "type": "exec.result", "exitCode": p.returncode, "signal": "",
                                   "stdout": p.stdout, "stderr": p.stderr, "timedOut": False})
        if route == "upload" and self.command == "PUT":
            if self.injected("upload", edge=True):
                return
            if box_get(bid, "state") != "running":
                return self.send(409, {"ok": False, "error": {"code": "box_not_running", "message": "the box is asleep"}})
            p = q["path"][0]
            assert not p.startswith("/"), p
            dst = os.path.join(home, p[2:] if p.startswith("~/") else p)
            if not os.path.isdir(os.path.dirname(dst)):
                return self.send(404, {"ok": False, "error": {"code": "no_such_path", "message": "directory " + os.path.dirname(dst)}})
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, int(q.get("mode", ["0644"])[0], 8))
            os.write(fd, body)
            os.close(fd)
            return self.send(200, {"ok": True, "type": "file.uploaded", "path": dst, "size": len(body)})
        self.send(404, {"ok": False, "error": {"code": "not_found", "message": "no such route"}})

    def log_message(self, *a):
        pass

    def sandboxes_route(self, rest, q, body):
        if self.command == "POST" and not rest:
            code = fail_for("create")
            if isinstance(code, dict):  # {"code": 422, "body": {...}}: a real error body
                return self.send(code["code"], code["body"])
            if code:
                return self.send(code, {"error": "injected", "message": f"injected {code}"})
            key = self.headers.get("Idempotency-Key")
            with lock:
                if key in idem:
                    prev_body, resp = idem[key]
                    if prev_body != body:
                        return self.send(409, {"error": "idempotency_conflict", "message": "key used for another request"})
                    return self.send(200, resp)
                req = json.loads(body)
                sid = "sbx_" + uuid.uuid4().hex[:16]
                home = os.path.join(ROOT, sid, "home")
                os.makedirs(os.path.join(home, "work"))
                if os.environ.get("FAKE_SEED_SETSID_FAIL"):  # the start's setsid fails once in each new sandbox
                    open(os.path.join(home, ".setsid-fail"), "w").close()
                created = time.time() - float(os.environ.get("FAKE_AGE", "0"))
                sb = {"id": sid, "state": "running", "template": req.get("template"), "labels": req.get("labels", {}),
                      "resources": req.get("resources"), "autoDeleteMin": req.get("autoDeleteMin"),
                      "createdAt": now_iso(created), "toolboxBase": f"http://127.0.0.1:{PORT}/tb/{sid}",
                      "maxRuntimeMin": req.get("maxRuntimeMin"),
                      "expiresAt": None if os.environ.get("FAKE_RUNTIME_MIN") == "none" else
                      now_iso(created + 60 * int(req.get("maxRuntimeMin") or 1440))}
                sandboxes[sid] = sb
                resp = {"sandbox": sb}
                if key:
                    idem[key] = (body, resp)
                json.dump(sb, open(os.path.join(ROOT, sid, "sandbox.json"), "w"))
            return self.send(201, resp)
        if self.command == "GET" and not rest:
            want = [l.split("=", 1) for l in q.get("label", [])]
            out = [s for s in sandboxes.values() if s["state"] != "deleted" and all(s["labels"].get(k) == v for k, v in want)]
            return self.send(200, {"sandboxes": out})
        if self.command == "GET" and len(rest) == 1:
            sb = sandboxes.get(rest[0])
            if not sb or sb["state"] == "deleted":
                return self.send(404, {"error": "not_found"})
            return self.send(200, {"sandbox": sb})
        if self.command == "DELETE" and len(rest) == 1:
            sb = sandboxes.get(rest[0])
            if not sb or sb["state"] == "deleted":
                return self.send(404, {"error": "not_found"})
            sb["state"] = "deleted"
            open(os.path.join(ROOT, rest[0], "deleted"), "w").write(now_iso())
            try:  # end the sandbox's runner: run.sh is a process group leader (setsid) and wrote its pid
                pidf = os.path.join(ROOT, rest[0], "home", ".claude-runner", "pid")
                if os.path.isfile(pidf):  # never open a FIFO a test left there
                    pid = int(open(pidf).read())
                    if pid > 1:
                        os.killpg(pid, 15)
            except Exception:
                pass
            return self.send(202, {"sandbox": sb})
        if self.command == "POST" and len(rest) == 2 and rest[1] == "state":  # test-only: set a sandbox's state
            st, _, why = body.decode().partition(":")
            sandboxes[rest[0]]["state"] = st
            sandboxes[rest[0]]["errorReason"] = why or None
            return self.send(200, {"sandbox": sandboxes[rest[0]]})
        self.send(404, {"error": "not_found"})

    def toolbox(self, sid, route, q, body):
        sb = sandboxes.get(sid)
        if not sb or sb["state"] == "deleted":
            return self.send(410, {"error": "sandbox_deleted"})
        home = os.path.join(ROOT, sid, "home")
        if route == "files/upload":
            code = fail_for("upload")
            if code:
                return self.send(code, {"error": "injected"})
            path = q["path"][0]
            assert path.startswith("/home/prized/"), path
            dst = os.path.join(home, path[len("/home/prized/"):])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "wb").write(body)
            delay = fail_for("upload_delay")  # seconds to sit on the answer
            if delay:
                time.sleep(delay)
            return self.send(200, {"path": path, "size": len(body)})
        if route == "process/execute":
            code = fail_for("exec")
            if code:
                return self.send(code, {"error": "injected"})
            delay = fail_for("exec_delay")  # seconds to sit on the answer after running the command

            req = json.loads(body)
            env = {"HOME": home, "PATH": SBIN + ":/usr/bin:/bin", "FAKE_SBX": sid, "FAKE_ROOT": ROOT, "LANG": "C.UTF-8"}
            p = subprocess.run(["bash", "-c", req["command"]], cwd=os.path.join(home, "work"),
                               env=env, capture_output=True, text=True, timeout=req.get("timeout", 300))
            if delay:
                time.sleep(delay)
            return self.send(200, {"stdout": p.stdout, "stderr": p.stderr, "exitCode": p.returncode, "durationMs": 1})
        self.send(404, {"error": "not_found"})


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
