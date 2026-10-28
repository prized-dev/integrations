"""An in-process fake of the Prized Sandbox API for the tests.

Adapted from langchain-prized's fake. The control plane keeps sandboxes in memory
and walks them through the documented states, and holds an answer for
`?wait=` as the API does. The edge wakes a paused sandbox for a request,
runs each command with a real `bash -c` on this machine, and serves files
from a real directory that stands in for that sandbox's `/home/prized`, with
the edge's path rules and error codes: absolute,
no `..`, inside that root, no symlinks, regular files only, and new files
written whole with mode 0600 (an existing file is replaced, so it loses its
mode, as on the real edge).

Every sandbox gets its own home directory, made at the create and removed
once it is deleted, so a replacement sandbox really starts empty. The package
under test sends the real `/home/prized` paths: the fake maps that prefix to
the sandbox's directory in commands, `cwd`, `env`, and file paths, and maps it
back in command output. Requests travel through `httpx.MockTransport`, so no
socket is opened.
"""

from __future__ import annotations

import itertools
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

# Prized's own domain, so the client trusts the fake's edge beside its API
# (requests never leave httpx.MockTransport).
API_ORIGIN = "https://api.prized.dev"
EDGE_ORIGIN = "https://edge.prized.dev"
API_KEY = "dcp_test_key"
PRIZED_HOME = "/home/prized"
TEMPLATES = frozenset({"prized-base-v1", "prized-agent-v1"})
CREATE_FIELDS = frozenset(
    {
        "template",
        "resources",
        "envVars",
        "labels",
        "autoPauseMin",
        "autoDeleteMin",
        "maxRuntimeMin",
    }
)
STREAM_CAP = 4 * 1024 * 1024
HOLD_POLLS = 5
"""How many state steps a held answer waits through before it answers as is."""
WAIT_FOR = {"": "running", "resume": "running", "pause": "paused"}
"""The `wait` value each route takes: create, resume, pause."""
BASH = shutil.which("bash") or "/bin/bash"
"""Found once, as the edge runs bash whatever PATH a command sets."""


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    json: Any = None
    size: int = 0


@dataclass
class Scripted:
    method: str
    pattern: re.Pattern[str]
    response: httpx.Response | Exception
    times: int = 1
    command: str | None = None
    """Only exec requests whose command contains this."""
    after: bool = False
    """Handle the request first, then answer with `response`: the answer is lost."""


@dataclass
class FakeSandbox:
    id: str
    template: str
    body: dict[str, Any]
    home: Path
    state: str = "starting"
    polls_left: int = 0
    env_vars: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        resources = {"vcpu": 2, "memoryMb": 4096, "diskGb": 10}
        resources.update(self.body.get("resources") or {})
        return {
            "id": self.id,
            "state": self.state,
            "template": self.template,
            "resources": resources,
            "durability": "ephemeral",
            "labels": dict(self.labels),
            "autoPauseMin": self.body.get("autoPauseMin"),
            "autoDeleteMin": self.body.get("autoDeleteMin"),
            "expiresAt": "2026-10-14T19:02:11Z",
            "errorReason": "boot failed" if self.state == "error" else None,
            "toolboxBase": f"{EDGE_ORIGIN}/v1/sandbox/{self.id}",
            "createdAt": "2026-10-14T17:02:11Z",
            "lastActiveAt": "2026-10-14T17:02:11Z",
        }

    def local(self, path: str) -> str:
        """The stand-in path for a sandbox path under /home/prized."""
        if path == PRIZED_HOME or path.startswith(f"{PRIZED_HOME}/"):
            return str(self.home) + path[len(PRIZED_HOME) :]
        return path

    def to_local(self, text: str) -> str:
        return text.replace(PRIZED_HOME, str(self.home))

    def to_sandbox(self, text: str) -> str:
        return text.replace(str(self.home), PRIZED_HOME)


def _error(status: int, code: str, **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"error": code, **extra})


def _answer(response: httpx.Response | Exception) -> httpx.Response:
    if isinstance(response, Exception):
        raise response
    # A fresh copy each time: a response is read once.
    return httpx.Response(response.status_code, headers=response.headers, content=response.content)


class FakePrized:
    """The fake API. `handle` is the `httpx.MockTransport` handler."""

    def __init__(self, root: Path, *, start_polls: int = 1, base_path: str | None = None) -> None:
        """Create the fake.

        Args:
            root: A real directory; each sandbox's home is a directory in it.
            start_polls: How many reads a starting sandbox answers `starting`
                before it is `running`.
            base_path: The PATH commands get before any env.
        """
        self.root = root.resolve()
        self.start_polls = start_polls
        self.base_path = base_path or os.environ.get("PATH", "/usr/bin:/bin")
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.requests: list[Recorded] = []
        self.scripted: list[Scripted] = []
        self.idempotency: dict[str, str] = {}
        self.boot_fails = False
        # An API from before `?wait=` answers at once, whatever the query.
        self.honor_wait = True
        # The edge's limit on one upload or download.
        self.max_transfer = 100 * 1024 * 1024
        self.exec_hooks: list[tuple[str, Callable[[FakeSandbox], None]]] = []
        self.lock = threading.Lock()
        self._ids = itertools.count(1)

    # --- test helpers ---------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def script(
        self,
        method: str,
        pattern: str,
        response: httpx.Response | Exception,
        times: int = 1,
        *,
        command: str | None = None,
    ) -> None:
        """Answer the next `times` matching requests with `response` (or raise it)."""
        self.scripted.append(Scripted(method, re.compile(pattern), response, times, command))

    def lose(
        self,
        method: str,
        pattern: str,
        times: int = 1,
        *,
        command: str | None = None,
        answer: httpx.Response | Exception | None = None,
    ) -> None:
        """Handle the next `times` matching requests, then lose their answers.

        The API acts (creates, pauses, deletes, runs the command) and the
        client sees `answer` instead: by default a read timeout, as when the
        connection drops on the way back.
        """
        lost = answer if answer is not None else httpx.ReadTimeout("the answer was lost")
        self.scripted.append(
            Scripted(method, re.compile(pattern), lost, times, command, after=True)
        )

    def before_exec(self, marker: str, action: Callable[[FakeSandbox], None]) -> None:
        """Run `action` once, just before the next command containing `marker` runs."""
        self.exec_hooks.append((marker, action))

    def calls(self, method: str, pattern: str) -> list[Recorded]:
        rx = re.compile(pattern)
        return [r for r in self.requests if r.method == method and rx.search(r.path)]

    def commands(self) -> list[str]:
        return [
            r.json["command"]
            for r in self.calls("POST", r"/process/execute$")
            if isinstance(r.json, dict)
        ]

    def sandbox(self, sandbox_id: str) -> FakeSandbox:
        return self.sandboxes[sandbox_id]

    def add_sandbox(self, state: str = "running", template: str = "prized-base-v1") -> str:
        with self.lock:
            return self._new_sandbox(template, {"template": template}, state=state).id

    def live(self) -> list[str]:
        return [s.id for s in self.sandboxes.values() if s.state not in {"deleting", "deleted"}]

    # --- the handler ----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        query = dict(request.url.params)
        body = request.read()
        recorded = Recorded(
            method=request.method,
            path=parts.path,
            query=query,
            headers={k.lower(): v for k, v in request.headers.items()},
            size=len(body),
        )
        if request.headers.get("content-type", "").startswith("application/json") and body:
            recorded.json = json.loads(body)
        lost: Scripted | None = None
        with self.lock:
            self.requests.append(recorded)
            for item in self.scripted:
                if item.method != request.method or not item.pattern.search(parts.path):
                    continue
                command = recorded.json.get("command") if isinstance(recorded.json, dict) else None
                if item.command is not None and item.command not in str(command):
                    continue
                item.times -= 1
                if item.times <= 0:
                    self.scripted.remove(item)
                if item.after:
                    lost = item
                    break
                return _answer(item.response)
        answer = self._route(request, parts, query, body, recorded)
        if lost is not None:
            return _answer(lost.response)
        return answer

    def _route(
        self,
        request: httpx.Request,
        parts: Any,
        query: dict[str, str],
        body: bytes,
        recorded: Recorded,
    ) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {API_KEY}":
            return _error(401, "unauthorized")
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin == API_ORIGIN:
            return self._control_plane(
                request.method, parts.path, recorded.json, recorded.headers, query
            )
        if origin == EDGE_ORIGIN:
            wake = recorded.headers.get("prized-auto-resume", "").lower() != "false"
            return self._edge(request.method, parts.path, query, body, recorded.json, wake=wake)
        return _error(404, "not_found")

    # --- control plane --------------------------------------------------------

    def _new_sandbox(self, template: str, body: dict[str, Any], *, state: str) -> FakeSandbox:
        sandbox_id = f"sbx_{next(self._ids):016x}"
        home = self.root / sandbox_id
        (home / "work").mkdir(parents=True)
        sandbox = FakeSandbox(
            id=sandbox_id,
            template=template,
            body=body,
            home=home,
            state=state,
            polls_left=self.start_polls,
            env_vars=dict(body.get("envVars") or {}),
            labels=dict(body.get("labels") or {}),
        )
        self.sandboxes[sandbox.id] = sandbox
        return sandbox

    def _advance(self, sandbox: FakeSandbox) -> None:
        if sandbox.state == "starting":
            if sandbox.polls_left > 0:
                sandbox.polls_left -= 1
            elif self.boot_fails:
                sandbox.state = "error"
            else:
                sandbox.state = "running"
        elif sandbox.state in {"pausing", "resizing"}:
            sandbox.state = "paused"
        elif sandbox.state == "deleting":
            sandbox.state = "deleted"
            shutil.rmtree(sandbox.home, ignore_errors=True)

    def _hold(self, sandbox: FakeSandbox, want: str | None, settled: int) -> int:
        """Walk a held sandbox toward `want`; return the status to answer with."""
        if want is None or not self.honor_wait:
            return settled
        for _ in range(HOLD_POLLS):
            if sandbox.state == want:
                return 201 if settled == 201 else 200
            if sandbox.state not in {"starting", "pausing"}:
                break
            self._advance(sandbox)
        if sandbox.state == want:
            return 201 if settled == 201 else 200
        return settled

    def _control_plane(  # noqa: C901, PLR0911, PLR0912
        self, method: str, path: str, doc: Any, headers: dict[str, str], query: dict[str, str]
    ) -> httpx.Response:
        prefix = "/api/v1/sandboxes"
        if not path.startswith(prefix):
            return _error(404, "not_found")
        rest = path.removeprefix(prefix).strip("/")
        sandbox_id, _, verb = rest.partition("/")
        want = query.get("wait")
        if want is not None or "waitTimeoutMs" in query:
            if method != "POST" or verb not in WAIT_FOR or want != WAIT_FOR[verb]:
                return _error(400, "bad_request", field="wait")
            ms = query.get("waitTimeoutMs", "8000")
            if not ms.isdigit() or int(ms) > 8000:
                return _error(400, "bad_request", field="waitTimeoutMs")
        with self.lock:
            if method == "POST" and rest == "":
                answer = self._create(doc, headers.get("idempotency-key"))
                if answer.status_code != 201:
                    return answer
                created = self.sandboxes[answer.json()["sandbox"]["id"]]
                status = self._hold(created, want, 201)
                return httpx.Response(status, json={"sandbox": created.document()})
            sandbox = self.sandboxes.get(sandbox_id)
            if sandbox is None or sandbox.state == "deleted":
                return _error(404, "not_found")
            if method == "GET" and verb == "":
                self._advance(sandbox)
                if sandbox.state == "deleted":
                    return _error(404, "not_found")
                return httpx.Response(200, json={"sandbox": sandbox.document()})
            if method == "DELETE" and verb == "":
                if sandbox.state != "deleting":
                    sandbox.state = "deleting"
                return httpx.Response(202, json={"sandbox": sandbox.document()})
            if method == "POST" and verb == "pause":
                if sandbox.state == "paused":
                    return httpx.Response(200, json={"sandbox": sandbox.document()})
                if sandbox.state not in {"running", "starting"}:
                    return _error(409, "sandbox_not_running", state=sandbox.state)
                sandbox.state = "pausing"
                status = self._hold(sandbox, want, 202)
                return httpx.Response(status, json={"sandbox": sandbox.document()})
            if method == "POST" and verb == "resume":
                if sandbox.state in {"paused", "pausing"}:
                    sandbox.state = "starting"
                    sandbox.polls_left = self.start_polls
                status = self._hold(sandbox, want, 202)
                return httpx.Response(status, json={"sandbox": sandbox.document()})
        return _error(404, "not_found")

    def _create(self, doc: Any, key: str | None) -> httpx.Response:
        if not isinstance(doc, dict):
            return _error(400, "bad_request")
        unknown = sorted(set(doc) - CREATE_FIELDS)
        if unknown:
            return _error(422, "unknown_field", field=unknown[0])
        template = doc.get("template")
        if template not in TEMPLATES:
            return _error(404, "template_not_found")
        if key and key in self.idempotency:
            existing = self.sandboxes[self.idempotency[key]]
            return httpx.Response(200, json={"sandbox": existing.document()})
        sandbox = self._new_sandbox(template, doc, state="starting")
        if key:
            self.idempotency[key] = sandbox.id
        return httpx.Response(201, json={"sandbox": sandbox.document()})

    # --- the edge -------------------------------------------------------------

    def _edge(  # noqa: PLR0911
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: bytes,
        doc: Any,
        *,
        wake: bool,
    ) -> httpx.Response:
        match = re.fullmatch(r"/v1/sandbox/([^/]+)(/.*)", path)
        if match is None:
            return _error(404, "not_found")
        with self.lock:
            sandbox = self.sandboxes.get(match.group(1))
            if sandbox is None or sandbox.state in {"deleting", "deleted"}:
                return _error(404, "not_found")
            # The edge wakes a paused sandbox and waits for a starting one,
            # unless the request says not to; a resizing one it leaves alone.
            if wake and sandbox.state in {"paused", "pausing", "starting"}:
                sandbox.state = "running"
                sandbox.polls_left = 0
            if sandbox.state != "running":
                return _error(409, "sandbox_not_running")
        route = match.group(2)
        if method == "POST" and route == "/process/execute":
            return self._execute(sandbox, doc)
        if method == "POST" and route == "/files/upload":
            return self._upload(sandbox, query.get("path"), body)
        if method == "GET" and route == "/files/download":
            return self._download(sandbox, query.get("path"))
        return _error(404, "not_found")

    def _execute(self, sandbox: FakeSandbox, doc: Any) -> httpx.Response:  # noqa: C901, PLR0911
        if not isinstance(doc, dict):
            return _error(400, "bad_json")
        command = doc.get("command")
        if not isinstance(command, str) or not command:
            return _error(400, "missing_command")
        cwd = doc.get("cwd", f"{PRIZED_HOME}/work")
        if not isinstance(cwd, str) or not cwd.startswith("/"):
            return _error(400, "cwd_not_absolute")
        timeout = doc.get("timeout", 300)
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            return _error(400, "bad_timeout")
        env = {
            "PATH": self.base_path,
            "HOME": str(sandbox.home),
            "LANG": "C.UTF-8",
            "USER": "prized",
        }
        env.update(sandbox.env_vars)
        extra = doc.get("env") or {}
        if not isinstance(extra, dict) or not all(
            isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in extra
        ):
            return _error(400, "bad_env")
        env.update({k: sandbox.to_local(str(v)) for k, v in extra.items()})
        for hook in list(self.exec_hooks):
            if hook[0] in command:
                self.exec_hooks.remove(hook)
                hook[1](sandbox)
        try:
            proc = subprocess.Popen(
                [BASH, "-c", sandbox.to_local(command)],
                cwd=sandbox.local(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return _error(400, "spawn_failed", detail=str(exc))
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            timed_out = True
        answer: dict[str, Any] = {
            "stdout": sandbox.to_sandbox(out[:STREAM_CAP].decode("utf-8", "replace")),
            "stderr": sandbox.to_sandbox(err[:STREAM_CAP].decode("utf-8", "replace")),
            "exitCode": -1 if timed_out else proc.returncode,
            "durationMs": 1,
        }
        if timed_out:
            answer["timedOut"] = True
        return httpx.Response(200, json=answer)

    def _check_path(  # noqa: PLR0911
        self, sandbox: FakeSandbox, raw: str | None
    ) -> tuple[Path | None, httpx.Response | None]:
        """The edge's path rules, in its order."""
        if not raw:
            return None, _error(400, "missing_path")
        if not raw.startswith("/"):
            return None, _error(400, "path_not_absolute")
        if "\x00" in raw or ".." in raw.split("/"):
            return None, _error(400, "invalid_path")
        clean = posixpath.normpath(raw)
        if clean != PRIZED_HOME and not clean.startswith(PRIZED_HOME + "/"):
            return None, _error(403, "path_outside_root")
        root = str(sandbox.home)
        local = sandbox.local(clean)
        parts = Path(local).relative_to(root).parts
        current = Path(root)
        for index, part in enumerate(parts):
            current /= part
            if current.is_symlink():
                return None, _error(400, "symlink_not_allowed")
            if not current.exists():
                break
            if index < len(parts) - 1 and not current.is_dir():
                return None, _error(400, "not_directory")
        return Path(local), None

    def _upload(self, sandbox: FakeSandbox, raw: str | None, body: bytes) -> httpx.Response:
        path, problem = self._check_path(sandbox, raw)
        if problem is not None or path is None:
            return problem or _error(400, "missing_path")
        if path.exists() and not path.is_file():
            return _error(400, "unsupported_file_type")
        if len(body) > self.max_transfer:
            return _error(413, "body_too_large")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            temp.replace(path)
        except PermissionError:
            return _error(403, "permission_denied")
        return httpx.Response(200, json={"path": raw, "size": len(body)})

    def _download(self, sandbox: FakeSandbox, raw: str | None) -> httpx.Response:
        path, problem = self._check_path(sandbox, raw)
        if problem is not None or path is None:
            return problem or _error(400, "missing_path")
        if not path.exists():
            return _error(404, "not_found")
        if not path.is_file():
            return _error(400, "not_regular_file")
        if path.stat().st_size > self.max_transfer:
            return _error(413, "file_too_large")
        try:
            data = path.read_bytes()
        except PermissionError:
            return _error(403, "permission_denied")
        return httpx.Response(
            200, content=data, headers={"Content-Type": "application/octet-stream"}
        )
