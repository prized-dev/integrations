"""An in-process stand-in for the Prized sandbox API, served through ``httpx.MockTransport``.

The control plane keeps sandboxes in a dict. The edge runs each exec with the
host's real ``bash``, with the sandbox's filesystem root mapped to a temp dir
(``/home/prized`` -> ``<fs>/home/prized``, ``tar -C /`` -> ``tar -C <fs>``), so tests
exercise the actual scripts the backend sends, not string matches.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import httpx

API = "https://api.test/api/v1"
EDGE = "https://edge.test/v1/sandbox"
REMOTE_HOME = "/home/prized"


class FakePrized:
    def __init__(self, fs: Path, *, expires_at: str = "2999-01-01T00:00:00Z"):
        self.fs = fs  # stands in for the sandbox's /
        self.root = fs / "home" / "prized"
        self.root.mkdir(parents=True, exist_ok=True)
        self.sandboxes: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.exec_scripts: list[str] = []
        self.creates: list[dict] = []
        self.expires_at = expires_at
        self.exec_answers: list[httpx.Response] = []  # queued canned answers, served before real runs

    # --- helpers ---
    def local(self, text: str) -> str:
        return text.replace(" -C / ", f" -C {self.fs} ").replace(REMOTE_HOME, str(self.root))

    def add(self, sid: str, state: str = "running", labels: dict | None = None,
            expires_at: str | None = None) -> dict:
        sbx = {"id": sid, "state": state, "labels": labels or {}, "toolboxBase": f"{EDGE}/{sid}",
               "expiresAt": expires_at or self.expires_at, "template": "prized-base-v1"}
        self.sandboxes[sid] = sbx
        return sbx

    def paths(self, method: str | None = None) -> list[str]:
        return [r.url.path for r in self.requests if method is None or r.method == method]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- routes ---
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, path = str(request.url), request.url.path
        if url.startswith(EDGE):
            sid, _, route = path.removeprefix("/v1/sandbox/").partition("/")
            sbx = self.sandboxes.get(sid)
            if sbx is None or sbx["state"] in ("deleting", "deleted"):
                return httpx.Response(404, json={"error": "not_found"})
            if sbx["state"] == "paused":  # the edge wakes a paused sandbox
                sbx["state"] = "running"
            return getattr(self, "edge_" + route.replace("/", "_"))(request)
        route = path.removeprefix("/api/v1")
        if route == "/sandboxes" and request.method == "POST":
            body = json.loads(request.content)
            self.creates.append({"body": body, "headers": dict(request.headers), "params": dict(request.url.params)})
            sbx = self.add(f"sbx_{uuid.uuid4().hex[:16]}", labels=body.get("labels"))
            return httpx.Response(201, json={"sandbox": sbx})
        sid, _, action = route.removeprefix("/sandboxes/").partition("/")
        sbx = self.sandboxes.get(sid)
        if sbx is None or sbx["state"] == "deleted":
            return httpx.Response(404, json={"error": "not_found"})
        if request.method == "GET":
            return httpx.Response(200, json={"sandbox": sbx})
        if request.method == "DELETE":
            sbx["state"] = "deleted"
            return httpx.Response(202, json={"sandbox": sbx})
        sbx["state"] = {"pause": "paused", "resume": "running"}[action]
        return httpx.Response(200, json={"sandbox": sbx})

    def edge_process_execute(self, request: httpx.Request) -> httpx.Response:
        doc = json.loads(request.content)
        self.exec_scripts.append(doc["command"])
        if self.exec_answers:
            return self.exec_answers.pop(0)
        proc = subprocess.run(["bash", "-c", self.local(doc["command"])], capture_output=True, text=True,
                              timeout=doc.get("timeout", 300), cwd=self.root, stdin=subprocess.DEVNULL)
        back = lambda text: text.replace(str(self.root), REMOTE_HOME)  # noqa: E731 — answer in sandbox paths
        return httpx.Response(200, json={"stdout": back(proc.stdout), "stderr": back(proc.stderr),
                                         "exitCode": proc.returncode, "durationMs": 1})

    def edge_files_upload(self, request: httpx.Request) -> httpx.Response:
        remote = request.url.params["path"]
        if not remote.startswith(REMOTE_HOME + "/"):
            return httpx.Response(403, json={"error": "path_outside_root"})
        dest = Path(self.local(remote))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(request.content)
        return httpx.Response(200, json={"path": remote, "size": len(request.content)})

    def edge_files_download(self, request: httpx.Request) -> httpx.Response:
        src = Path(self.local(request.url.params["path"]))
        if not src.is_file():
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(200, content=src.read_bytes())
