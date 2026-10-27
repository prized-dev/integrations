"""A stand-in for Devin's side of Outposts, on 127.0.0.1.

The queue follows Cognition's own mock of its router (CognitionAI/devin-outpost-k8s,
tests/mock_server/mod.rs): keyset-paged list with an at-least-once cursor, an SSE watch with
keepalives that ends after a while, claim as a compare-and-swap (409 on conflict) where the same
acceptor renews, claims that expire unless the session is running, a fresh connect token per
claim, and release that answers 409 unless the caller holds the claim. On top of it:

- a "brain" the tests drive: start, suspend, wake and terminate sessions;
- a gateway the fake remote attaches to with its connect token and long-polls for "sleep";
- the static binary host (latest pointer, binary, .sha256);
- the partner token endpoint (/outposts/connection-token).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "cog_fake_outposts_token_000"


@dataclass
class Row:
    session_id: str
    outpost_id: str
    kind: str = "new"
    session_status: str = "pending"
    phase: str = "pending"
    acceptor_id: str | None = None
    claim_deadline: float | None = None
    remote_binary_sha: str | None = None
    network_policy: dict[str, Any] = field(default_factory=lambda: {"enabled": False, "allow": []})
    image_build: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    deleted: bool = False
    claims: int = 0
    tokens: list[str] = field(default_factory=list)
    action: str = "none"  # what the gateway tells an attached remote: none | sleep | terminate
    attached: int = 0
    messages: list[str] = field(default_factory=list)


class FakeOutposts:
    def __init__(
        self, *, claim_ttl: float = 300.0, watch_max: float = 5.0, remote_file: str | None = None
    ) -> None:
        self.rows: dict[str, Row] = {}
        self.lock = threading.Condition()
        self.claim_ttl = claim_ttl
        self.watch_max = watch_max
        self.remote_bytes = open(remote_file, "rb").read() if remote_file else b""
        self.remote_sha = "abc1234def"
        self.codes: dict[str, str] = {}  # one-time code -> code_challenge
        self.requests: list[tuple[str, str]] = []
        self._last = 0.0
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *a: None  # clients that hang up mid-answer (killed remotes)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        # What a claim hands the remote. Live tests use file:// (a real sandbox can't reach this machine).
        self.gateway_url = f"{self.url}/gateway"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- clock: strictly increasing, like the router's microsecond timestamps ----------------------

    def now(self) -> float:
        t = max(time.time(), self._last + 1e-6)
        self._last = t
        return t

    # -- the brain -----------------------------------------------------------------------------

    def enqueue(self, sid: str, outpost: str = "outpost_env-test", **kw: Any) -> Row:
        with self.lock:
            t = self.now()
            row = Row(sid, outpost, created_at=t, updated_at=t, **kw)
            self.rows[sid] = row
            self.lock.notify_all()
            return row

    def _touch(self, row: Row) -> None:
        row.updated_at = self.now()
        self.lock.notify_all()

    def suspend(self, sid: str) -> None:
        """Devin puts the session to sleep: the remote is told to exit; the row says suspended."""
        with self.lock:
            row = self.rows[sid]
            row.session_status = "suspended"
            row.action = "sleep"
            self._touch(row)

    def mark_running(self, sid: str) -> None:
        """What the gateway does when a remote attaches (live tests call it themselves)."""
        with self.lock:
            row = self.rows[sid]
            row.attached += 1
            if row.session_status == "pending":
                row.session_status = "running"
            self._touch(row)

    def wake(self, sid: str) -> None:
        """A new message wakes the session: it is queued again as a resume."""
        with self.lock:
            row = self.rows[sid]
            row.deleted = False
            row.kind = "resume"
            row.session_status = "pending"
            row.phase = "pending"
            row.acceptor_id = None
            row.claim_deadline = None
            row.action = "none"
            self._touch(row)

    def terminate(self, sid: str) -> None:
        with self.lock:
            row = self.rows[sid]
            row.session_status = "terminated"
            row.action = "terminate"
            self._touch(row)

    def vanish(self, sid: str) -> None:
        """The sweeper removes a released sleeper's row (absent != terminated)."""
        with self.lock:
            row = self.rows[sid]
            row.deleted = True
            self._touch(row)

    def row(self, sid: str) -> Row:
        with self.lock:
            return self.rows[sid]

    def wait_for(self, predicate: Any, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.lock:
            while not predicate():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.lock.wait(min(left, 0.2))
            return True

    # -- queue semantics ---------------------------------------------------------------------------

    def _expire(self) -> None:
        t = time.time()
        for row in self.rows.values():
            if (
                row.phase == "claimed"
                and not row.deleted
                and row.claim_deadline is not None
                and row.claim_deadline < t
                and row.session_status != "running"
            ):
                row.phase, row.acceptor_id, row.claim_deadline = "pending", None, None
                self._touch(row)

    @staticmethod
    def doc(row: Row, token: str | None = None, gateway: str | None = None) -> dict[str, Any]:
        return {
            "metadata": {
                "session_id": row.session_id,
                "outpost_id": row.outpost_id,
                "created_at": int(row.created_at),
                "updated_at": int(row.updated_at),
            },
            "spec": {
                "kind": row.kind,
                "platform": "linux",
                "remote_binary_sha": row.remote_binary_sha,
                "network_policy": row.network_policy,
                "image": None,
                "image_build": row.image_build,
                "image_target": None,
            },
            "status": {
                "phase": row.phase,
                "acceptor_id": row.acceptor_id,
                "claim_deadline": int(row.claim_deadline) if row.claim_deadline else None,
                "session_status": row.session_status,
                "connect_token": token,
                "gateway_url": gateway,
            },
        }

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                return

            def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
                data = (
                    body
                    if isinstance(body, bytes)
                    else (json.dumps(body) if ctype == "application/json" else str(body)).encode()
                )
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth(self) -> bool:
                if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                    return True
                self._send(401, {"detail": "Invalid token"})
                return False

            def _body(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type", "")
                if "x-www-form-urlencoded" in ctype:
                    return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {}

            def do_GET(self) -> None:  # noqa: N802
                url = urllib.parse.urlparse(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                fake.requests.append(("GET", url.path))
                if url.path.startswith("/static/"):
                    return self._static(url.path[len("/static/") :])
                if url.path == "/gateway/wait":
                    return self._gateway_wait(q)
                if not self._auth():
                    return
                if url.path == "/opbeta/outposts/devins":
                    return self._watch(q) if q.get("watch") == "true" else self._list(q)
                if url.path.startswith("/opbeta/outposts/devins/"):
                    sid = url.path.rsplit("/", 1)[1]
                    with fake.lock:
                        fake._expire()
                        row = fake.rows.get(sid)
                        if row is None or row.deleted:
                            return self._send(404, {"detail": "Session not found in queue"})
                        return self._send(200, fake.doc(row))
                if url.path in ("/opbeta/outposts", "/opbeta/outposts/"):
                    with fake.lock:
                        pools = sorted({r.outpost_id for r in fake.rows.values()} | {"outpost_env-test"})
                        items = [
                            {
                                "metadata": {"outpost_id": p, "account_id": "acc"},
                                "spec": {"name": p.replace("outpost_env-", ""), "platform": "linux"},
                                "status": {"queue_depth": 0, "active_claims": 0},
                            }
                            for p in pools
                        ]
                    return self._send(200, {"items": items, "end_cursor": None, "has_next_page": False})
                self._send(404, {"detail": "no route"})

            def do_POST(self) -> None:  # noqa: N802
                url = urllib.parse.urlparse(self.path)
                fake.requests.append(("POST", url.path))
                body = self._body()
                if url.path == "/outposts/connection-token":
                    return self._token(body)
                if url.path == "/gateway/attach":
                    return self._attach(body)
                if not self._auth():
                    return
                parts = url.path.split("/")
                if len(parts) == 6 and parts[3] == "devins":
                    sid, verb = parts[4], parts[5]
                    if verb == "claim":
                        return self._claim(sid, str(body.get("acceptor_id") or ""))
                    if verb == "release":
                        return self._release(sid, str(body.get("acceptor_id") or ""))
                    if verb == "operator-message":
                        with fake.lock:
                            row = fake.rows.get(sid)
                            if row is None or row.session_status == "terminated":
                                return self._send(404, {"detail": "not found"})
                            row.messages.append(str(body.get("message", "")))
                        return self._send(200, {"session_id": sid, "message": body.get("message")})
                self._send(404, {"detail": "no route"})

            def _list(self, q: dict[str, str]) -> None:
                with fake.lock:
                    fake._expire()
                    since = float(q["cursor"][3:]) if q.get("cursor", "").startswith("v1:") else None
                    rows = [
                        r
                        for r in fake.rows.values()
                        if not r.deleted
                        and (not q.get("outpost") or r.outpost_id == q["outpost"])
                        and (not q.get("phase") or r.phase == q["phase"])
                        and (not q.get("acceptor_id") or r.acceptor_id == q["acceptor_id"])
                        and (since is None or r.updated_at >= since)
                    ]
                    rows.sort(key=lambda r: r.updated_at)
                    first = int(q.get("first", 2))  # small pages, so paging is exercised
                    page = rows[:first]
                    more = len(page) == first and len(rows) > first
                    cursor = f"v1:{page[-1].updated_at}" if more else f"v1:{fake.now()}"
                    self._send(
                        200,
                        {
                            "items": [fake.doc(r) for r in page],
                            "cursor": cursor,
                            "has_next_page": more,
                            "total": len(rows),
                        },
                    )

            def _watch(self, q: dict[str, str]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                since = float(q["cursor"][3:]) if q.get("cursor", "").startswith("v1:") else 0.0
                emitted: dict[str, float] = {}
                end = time.monotonic() + fake.watch_max
                try:
                    while time.monotonic() < end:
                        with fake.lock:
                            fake._expire()
                            rows = sorted(
                                (
                                    r
                                    for r in fake.rows.values()
                                    if r.updated_at >= since
                                    and (not q.get("outpost") or r.outpost_id == q["outpost"])
                                ),
                                key=lambda r: r.updated_at,
                            )
                            chunks = []
                            for r in rows:
                                since = r.updated_at
                                if emitted.get(r.session_id) == r.updated_at:
                                    continue
                                emitted[r.session_id] = r.updated_at
                                ev = {
                                    "type": "DELETED" if r.deleted else "MODIFIED",
                                    "object": fake.doc(r),
                                    "cursor": f"v1:{r.updated_at}",
                                }
                                chunks.append(f"data: {json.dumps(ev)}\n\n")
                            if not chunks:
                                fake.lock.wait(0.2)
                        self.wfile.write(("".join(chunks) or ": keepalive\n\n").encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                self.close_connection = True

            def _claim(self, sid: str, acceptor: str) -> None:
                with fake.lock:
                    fake._expire()
                    row = fake.rows.get(sid)
                    if row is None or row.deleted:
                        return self._send(404, {"detail": "Session not found in queue"})
                    t = time.time()
                    renewal = row.phase == "claimed" and row.acceptor_id == acceptor
                    expired = (
                        row.phase == "claimed"
                        and row.claim_deadline is not None
                        and row.claim_deadline <= t
                        and row.session_status != "running"
                    )
                    if row.phase != "pending" and not expired and not renewal:
                        return self._send(409, {"detail": "Session is already claimed"})
                    row.phase, row.acceptor_id = "claimed", acceptor
                    row.claim_deadline = t + fake.claim_ttl
                    row.claims += 1
                    token = f"ctok-{sid}-{row.claims}"
                    row.tokens.append(token)
                    fake._touch(row)
                    return self._send(200, fake.doc(row, token, fake.gateway_url))

            def _release(self, sid: str, acceptor: str) -> None:
                with fake.lock:
                    row = fake.rows.get(sid)
                    if row is None or row.deleted:
                        return self._send(404, {"detail": "Session not found in queue"})
                    if row.phase != "claimed" or row.acceptor_id != acceptor:
                        return self._send(409, {"detail": "Session is not claimed by this acceptor"})
                    row.phase, row.acceptor_id, row.claim_deadline = "pending", None, None
                    fake._touch(row)
                    return self._send(200, fake.doc(row))

            def _attach(self, body: dict[str, Any]) -> None:
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                sid = str(body.get("session_id"))
                with fake.lock:
                    row = fake.rows.get(sid)
                    if row is None or token not in row.tokens:
                        return self._send(401, {"detail": "bad connect token"})
                    row.attached += 1
                    if row.session_status in ("pending", "running"):
                        row.session_status = "running"
                        fake._touch(row)
                    return self._send(200, {"ok": True})

            def _gateway_wait(self, q: dict[str, str]) -> None:
                sid = q.get("session_id", "")
                end = time.monotonic() + 2.0
                with fake.lock:
                    while time.monotonic() < end:
                        row = fake.rows.get(sid)
                        if row is not None and row.action != "none":
                            return self._send(200, {"action": row.action})
                        fake.lock.wait(0.1)
                return self._send(200, {"action": "none"})

            def _static(self, name: str) -> None:
                digest = hashlib.sha256(fake.remote_bytes).hexdigest()
                if name == "latest_linux_x64":
                    return self._send(200, fake.remote_sha + "\n", "text/plain")
                if name == f"devin-remote_{fake.remote_sha}_linux_x64":
                    return self._send(200, fake.remote_bytes, "application/octet-stream")
                if name == f"devin-remote_{fake.remote_sha}_linux_x64.sha256":
                    return self._send(200, digest + "\n", "text/plain")
                if name == "devin-remote_bad0bad0bad_linux_x64":
                    return self._send(200, b"#!/bin/sh\necho tampered\n", "application/octet-stream")
                if name == "devin-remote_bad0bad0bad_linux_x64.sha256":
                    return self._send(200, digest + "\n", "text/plain")
                self._send(404, "not found", "text/plain")

            def _token(self, body: dict[str, Any]) -> None:
                code, verifier = body.get("code", ""), body.get("code_verifier", "")
                import base64

                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                )
                if body.get("grant_type") != "authorization_code" or fake.codes.pop(code, None) != challenge:
                    return self._send(
                        400,
                        {"error": "invalid_grant", "error_description": "Invalid or expired connection code"},
                    )
                return self._send(
                    200,
                    {
                        "outpost_id": "outpost_env-connected",
                        "account_id": "acc",
                        "outpost_name": "prized",
                        "service_user_id": "su_1",
                        "api_base_url": fake.url,
                        "access_token": TOKEN,
                        "token_type": "bearer",
                    },
                )

        return H
