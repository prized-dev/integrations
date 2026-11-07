"""A stand-in for Claude Managed Agents' side of a self-hosted environment, on 127.0.0.1.

Built from the API reference and guides (fetched 2026-11-06):

- https://platform.claude.com/docs/en/api/beta/environments/work (poll, ack, heartbeat, stop, stats)
- https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-workers
- https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-security
- https://platform.claude.com/docs/en/managed-agents/webhooks

and from how Anthropic's own workers use it (anthropic Python SDK 2.6.0 ``lib/environments``;
anthropic-cli's ``sandboxes/dispatcher``). What it does:

- **Auth.** The work queue (poll, ack, stats) takes only the environment key, as a Bearer token. A
  claimed item carries a ``secret``: base64url JSON holding a ``sessions_token`` that authorizes
  that one item's heartbeat and stop and its session's reads, and nothing else.
- **The queue.** Poll validates ``block_ms`` (1-999) and long-polls; an item claimed but not acked
  within ``reclaim_older_than_ms`` (default 5000) is handed out again, with a fresh secret. Ack
  moves it to ``starting``; a second ack is a 409. Polls by worker id feed ``workers_polling``.
- **Leases.** Heartbeats use ``expected_last_heartbeat`` (``NO_HEARTBEAT`` first): a stale value is
  a 412 carrying the server's state. A lease not renewed within its TTL is lost: the item goes back
  to the queue and its token stops working. Stop moves an active item to ``stopping`` (``force``:
  to ``stopped``); stopping a stopped item is a 409.
- **Sessions.** ``GET /v1/sessions/{id}`` answers 404 ``not_found_error`` "Session not found: <id>"
  once a session is deleted.
- **Webhooks.** Signed with the Standard Webhooks library Anthropic's SDK verifies with; up to 3
  attempts with backoff (scaled down here), the same event id each time, a fresh timestamp each
  attempt; a 3xx disables the endpoint.

What it doesn't model: the session event stream and tool calls. Those run between ``ant`` and the
API and never pass through the orchestrator; here the stub ``ant`` (tests/bin/ant) asks
``/_stub/...`` for the "tool calls" to run and reports their results.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

import httpx
import standardwebhooks
import uvicorn

ENV_ID = "env_mock0001"
ENV_KEY = "sk-ant-oat01-ENVIRONMENT-KEY-mock-0001"
WEBHOOK_KEY = "whsec_" + base64.b64encode(b"m" * 32).decode()
BETA = "managed-agents-2026-04-01"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass
class Item:
    id: str
    session_id: str
    kind: str = "session"
    state: str = "queued"
    created: float = field(default_factory=time.time)
    claimed_at: float | None = None
    claims: int = 0
    acked_at: float | None = None
    token: str | None = None
    last_heartbeat: str | None = None
    lease_until: float | None = None
    heartbeats: int = 0
    stopped_by: str | None = None
    no_secret: bool = False  # hand it out with "secret": null


@dataclass
class Session:
    id: str
    status: str = "idle"
    metadata: dict[str, str] = field(default_factory=dict)
    deleted: bool = False
    commands: list[str] = field(default_factory=list)  # "tool calls" the stub ant runs, in order
    results: list[dict[str, Any]] = field(default_factory=list)
    stub_env: list[dict[str, Any]] = field(default_factory=list)  # what each stub ant saw


class MockCMA:
    def __init__(self, *, lease_ttl: float = 2.0, retry_delays: tuple[float, ...] = (0.05, 0.2)) -> None:
        self.lock = threading.RLock()
        self.items: dict[str, Item] = {}
        self.sessions: dict[str, Session] = {}
        self.ids = itertools.count(1)
        self.lease_ttl = lease_ttl
        self.retry_delays = retry_delays
        self.requests: list[tuple[str, str, str]] = []  # (method, path, credential kind)
        self.pollers: dict[str, float] = {}
        self.empty_poll_status = 200  # 200 with a null body, or 204
        self.fail_next: dict[str, list[int]] = {}  # "ack" / "poll" / "stop" / "session": statuses to answer first
        self.reuse_work_ids = False  # the dispatcher says a work item's id can be its session's id
        # webhooks
        self.endpoint: str | None = None
        self.endpoint_disabled: str | None = None
        self.deliveries: list[tuple[str, str, int | None]] = []  # (event id, event type, status or None)
        self._hook = standardwebhooks.Webhook(WEBHOOK_KEY)
        self._threads: list[threading.Thread] = []
        # the server
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self._server = uvicorn.Server(
            uvicorn.Config(self.app, interface="asgi3", lifespan="off", log_level="warning", access_log=False)
        )
        self._loop_thread = threading.Thread(target=self._serve, daemon=True)
        self._loop_thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("the mock CMA server did not start")
            time.sleep(0.01)

    def _serve(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        reaper = asyncio.create_task(self._reap_leases())
        try:
            await self._server.serve(sockets=[self._sock])
        finally:
            reaper.cancel()

    def close(self) -> None:
        self._server.should_exit = True
        self._loop_thread.join(10)
        for t in self._threads:
            t.join(10)

    # -- test controls -------------------------------------------------------------------------

    def new_session(self, *, metadata: dict[str, str] | None = None) -> Session:
        with self.lock:
            sid = f"sesn_{next(self.ids):04d}mock"
            s = self.sessions[sid] = Session(sid, metadata=dict(metadata or {}))
            return s

    def run(self, sid: str, *commands: str, webhook: bool = True) -> Item | None:
        """A user message: the session runs ``commands`` as tool calls. A worker that still holds
        the session's lease takes them; otherwise a work item is queued (and the run-started
        webhook fires either way, as it does at every transition to running)."""
        with self.lock:
            s = self.sessions[sid]
            s.commands.extend(commands)
            s.status = "running"
            held = any(
                i.session_id == sid and i.state in ("starting", "active") and i.lease_until for i in self.items.values()
            )
            item = None if held else self._enqueue(sid)
        if webhook:
            self.emit("session.status_run_started", sid)
        return item

    def healthcheck(self) -> Item:
        with self.lock:
            return self._enqueue("", kind="healthcheck")

    def terminate(self, sid: str, *, webhook: bool = True) -> None:
        with self.lock:
            self.sessions[sid].status = "terminated"
            for i in self.items.values():
                if i.session_id == sid and i.state in ("queued", "starting", "active"):
                    i.state = "stopping" if i.state == "active" else "stopped"
        if webhook:
            self.emit("session.status_terminated", sid)

    def delete(self, sid: str, *, webhook: bool = True) -> None:
        with self.lock:
            self.sessions[sid].deleted = True
            for i in self.items.values():
                if i.session_id == sid and i.state != "stopped":
                    i.state = "stopped"
        if webhook:
            self.emit("session.deleted", sid)

    def _enqueue(self, sid: str, *, kind: str = "session") -> Item:
        wid = sid if (self.reuse_work_ids and sid) else f"work_{next(self.ids):04d}mock"
        item = Item(wid, sid, kind=kind)
        old = self.items.get(wid)
        if old is not None and old.state != "stopped":
            raise AssertionError(f"{wid} is still {old.state}")
        self.items[wid] = item
        return item

    def item(self, wid: str) -> Item:
        with self.lock:
            return self.items[wid]

    def items_of(self, sid: str) -> list[Item]:
        with self.lock:
            return [i for i in self.items.values() if i.session_id == sid]

    # -- webhooks ------------------------------------------------------------------------------

    def emit(
        self,
        event_type: str,
        resource_id: str,
        *,
        duplicate: bool = False,
        stale: bool = False,
        key: str | None = None,
        wait: bool = False,
    ) -> str:
        """Deliver an event to the registered endpoint, in the background, the way Anthropic does."""
        event_id = f"whe_{secrets.token_hex(8)}"
        body = json.dumps(
            {
                "type": "event",
                "id": event_id,
                "created_at": now_iso(),
                "data": {
                    "type": event_type,
                    "id": resource_id,
                    "organization_id": "00000000-0000-4000-8000-000000000001",
                    "workspace_id": "00000000-0000-4000-8000-000000000002",
                },
            },
            separators=(",", ":"),
        )
        if self.endpoint is None or self.endpoint_disabled:
            self.deliveries.append((event_id, event_type, None))
            return event_id
        t = threading.Thread(
            target=self._deliver, args=(event_id, event_type, body, duplicate, stale, key), daemon=True
        )
        self._threads.append(t)
        t.start()
        if wait:
            t.join(30)
        return event_id

    def _deliver(
        self, event_id: str, event_type: str, body: str, duplicate: bool, stale: bool, key: str | None
    ) -> None:
        hook = standardwebhooks.Webhook(key) if key else self._hook
        attempts = 1 + len(self.retry_delays)
        done = False
        with httpx.Client(timeout=10) as http:
            for n in range(attempts):
                if n:
                    time.sleep(self.retry_delays[n - 1])
                stamp = datetime.now(timezone.utc)
                if stale:
                    stamp = datetime.fromtimestamp(stamp.timestamp() - 600, timezone.utc)
                headers = {
                    "webhook-id": event_id,
                    "webhook-timestamp": str(int(stamp.timestamp())),
                    "webhook-signature": hook.sign(event_id, stamp, body),
                    "content-type": "application/json",
                    "user-agent": "Anthropic-Webhooks/mock",
                }
                try:
                    r = http.post(self.endpoint or "", content=body.encode(), headers=headers, follow_redirects=False)
                    status: int | None = r.status_code
                except httpx.HTTPError:
                    status = None
                self.deliveries.append((event_id, event_type, status))
                if status is not None and 300 <= status < 400:
                    self.endpoint_disabled = "auto-disabled: endpoint URL returned a redirect (3xx)"
                    return
                if status is not None and 200 <= status < 300:
                    done = True
                    if not duplicate:
                        return
                    duplicate = False  # the same event once more, as a retry would
                    continue
            if not done:
                return

    # -- the HTTP API --------------------------------------------------------------------------

    async def app(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        query = {k: v[-1] for k, v in parse_qs(scope.get("query_string", b"").decode()).items()}
        try:
            status, doc = await self.route(scope["method"], scope["path"], query, headers, body)
        except _Err as err:
            status, doc = (
                err.status,
                {"type": "error", "error": {"type": err.kind, "message": err.message, **err.extra}},
            )
        out = b"" if doc is None and status == 204 else json.dumps(doc).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"request-id", b"req_mock")],
            }
        )
        await send({"type": "http.response.body", "body": out})

    def _credential(self, headers: dict[str, str]) -> tuple[str, str]:
        auth = headers.get("authorization", "")
        if headers.get("x-api-key"):
            return "api_key", headers["x-api-key"]
        if not auth.startswith("Bearer "):
            raise _Err(401, "authentication_error", "missing credentials")
        token = auth[len("Bearer ") :]
        if token == ENV_KEY:
            return "environment_key", token
        with self.lock:
            for i in self.items.values():
                if i.token and secrets.compare_digest(i.token, token):
                    return "sessions_token", token
        raise _Err(401, "authentication_error", "invalid bearer token")

    def _item_for_token(self, token: str) -> Item | None:
        for i in self.items.values():
            if i.token == token:
                return i
        return None

    def _maybe_fail(self, what: str) -> None:
        queue = self.fail_next.get(what)
        if queue:
            status = queue.pop(0)
            raise _Err(status, "api_error" if status >= 500 else "invalid_request_error", f"injected {status}")

    async def route(
        self, method: str, path: str, query: dict[str, str], headers: dict[str, str], body: bytes
    ) -> tuple[int, Any]:
        parts = path.strip("/").split("/")
        if parts[0] == "_stub":
            return self._stub(method, parts, headers, body)
        kind, token = self._credential(headers)
        with self.lock:
            self.requests.append((method, path, kind))
        if BETA not in headers.get("anthropic-beta", ""):
            raise _Err(400, "invalid_request_error", f"this endpoint needs the anthropic-beta header {BETA}")
        # /v1/environments/{env}/work/...
        if parts[:2] == ["v1", "environments"] and len(parts) >= 4 and parts[3] == "work":
            if parts[2] != ENV_ID:
                raise _Err(404, "not_found_error", f"Environment not found: {parts[2]}")
            if parts[4:] == ["poll"] and method == "GET":
                self._only(kind, "environment_key")
                self._maybe_fail("poll")
                return await self._poll(query, headers)
            if parts[4:] == ["stats"] and method == "GET":
                self._only(kind, "environment_key", "api_key")
                return 200, self._stats()
            if len(parts) == 6 and method == "POST":
                wid, verb = parts[4], parts[5]
                with self.lock:
                    item = self.items.get(wid)
                    if item is None:
                        raise _Err(404, "not_found_error", f"Work not found: {wid}")
                    if verb == "ack":
                        self._only(kind, "environment_key")
                        self._maybe_fail("ack")
                        return self._ack(item)
                    if verb == "heartbeat":
                        self._own(kind, token, item)
                        return self._heartbeat(item, query)
                    if verb == "stop":
                        if kind != "environment_key":
                            self._own(kind, token, item)
                        self._maybe_fail("stop")
                        return self._stop(item, body, kind)
        if parts[:2] == ["v1", "sessions"] and len(parts) == 3 and method == "GET":
            sid = parts[2]
            with self.lock:
                if kind == "sessions_token":
                    owner = self._item_for_token(token)
                    if owner is None or owner.session_id != sid:
                        raise _Err(403, "permission_error", "this token is for another session")
                elif kind != "environment_key":
                    raise _Err(403, "permission_error", "not with this credential in the mock")
                self._maybe_fail("session")
                s = self.sessions.get(sid)
                if s is None or s.deleted:
                    raise _Err(404, "not_found_error", f"Session not found: {sid}")
                return 200, {
                    "type": "session",
                    "id": s.id,
                    "status": s.status,
                    "metadata": s.metadata,
                    "environment_id": ENV_ID,
                    "resources": [],
                }
        raise _Err(404, "not_found_error", f"no route {method} {path}")

    @staticmethod
    def _only(kind: str, *allowed: str) -> None:
        if kind not in allowed:
            raise _Err(403, "permission_error", f"{kind} can't call this endpoint")

    def _own(self, kind: str, token: str, item: Item) -> None:
        if kind == "environment_key":
            return
        if kind != "sessions_token" or item.token != token:
            raise _Err(403, "permission_error", "this token is for another work item")

    async def _poll(self, query: dict[str, str], headers: dict[str, str]) -> tuple[int, Any]:
        block = query.get("block_ms")
        if block is not None and not (block.isdigit() and 1 <= int(block) <= 999):
            raise _Err(400, "invalid_request_error", "block_ms must be 1-999")
        reclaim = query.get("reclaim_older_than_ms", "5000")
        if not reclaim.isdigit() or int(reclaim) < 1:
            raise _Err(400, "invalid_request_error", "reclaim_older_than_ms must be at least 1")
        with self.lock:
            self.pollers[headers.get("anthropic-worker-id", "-")] = time.time()
        deadline = time.time() + (int(block) / 1000 if block else 0)
        while True:
            with self.lock:
                item = self._claimable(int(reclaim) / 1000)
                if item is not None:
                    item.claimed_at = time.time()
                    item.claims += 1
                    item.token = f"sesstok_{secrets.token_hex(12)}"
                    payload = {"sessions_token": item.token, "ingress_token": "unused", "version": 1}
                    secret = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
                    return 200, self._doc(item, secret=None if item.no_secret else secret)
            if time.time() >= deadline:
                return self.empty_poll_status, None
            await asyncio.sleep(0.02)

    def _claimable(self, reclaim_s: float) -> Item | None:
        now = time.time()
        queued = [
            i
            for i in self.items.values()
            if i.state == "queued" and (i.claimed_at is None or (i.acked_at is None and now - i.claimed_at > reclaim_s))
        ]
        return min(queued, key=lambda i: i.created) if queued else None

    def _ack(self, item: Item) -> tuple[int, Any]:
        # A worker may heartbeat before the ack lands (Anthropic's dispatcher acks only after the
        # sandbox has the secret, by which time its worker may be running), so an active item
        # takes the ack too. A stopped one, or one never claimed, is a conflict.
        if item.claimed_at is None or item.state in ("stopping", "stopped"):
            raise _Err(409, "conflict_error", f"work {item.id} is {item.state}, not claimed")
        if item.acked_at is not None:
            raise _Err(409, "conflict_error", f"work {item.id} is acknowledged already")
        item.acked_at = time.time()
        if item.state == "queued":
            item.state = "starting"
        return 200, self._doc(item)

    def _heartbeat(self, item: Item, query: dict[str, str]) -> tuple[int, Any]:
        expected = query.get("expected_last_heartbeat", "NO_HEARTBEAT")
        current = item.last_heartbeat or "NO_HEARTBEAT"
        if item.state == "queued" and item.claimed_at is None:
            expected = "LEASE-GONE"  # the lease lapsed and the item went back to the queue
        if expected != current:
            raise _Err(
                412,
                "precondition_failed",
                "expected_last_heartbeat does not match",
                details={
                    "current_state": {"state": item.state, "ttl_seconds": self.lease_ttl, "last_heartbeat": current}
                },
            )
        item.heartbeats += 1
        item.last_heartbeat = now_iso()
        extended = item.state not in ("stopping", "stopped")
        if extended:
            item.lease_until = time.time() + self.lease_ttl
            if item.state in ("queued", "starting"):
                item.state = "active"
        return 200, {
            "type": "work_heartbeat",
            "last_heartbeat": item.last_heartbeat,
            "lease_extended": extended,
            "state": item.state,
            "ttl_seconds": self.lease_ttl,
        }

    def _stop(self, item: Item, body: bytes, kind: str) -> tuple[int, Any]:
        force = bool(json.loads(body or b"{}").get("force"))
        if item.state == "stopped":
            raise _Err(409, "conflict_error", f"work {item.id} is already stopped")
        item.state = "stopped" if force or item.state != "active" else "stopping"
        item.lease_until = None
        item.stopped_by = kind
        return 200, self._doc(item)

    def _stats(self) -> dict[str, Any]:
        with self.lock:
            queued = [i for i in self.items.values() if i.state == "queued"]
            return {
                "type": "work_queue_stats",
                "depth": sum(1 for i in queued if i.claimed_at is None),
                "pending": sum(1 for i in queued if i.claimed_at is not None),
                "oldest_queued_at": None,
                "workers_polling": sum(1 for t in self.pollers.values() if time.time() - t < 30),
            }

    @staticmethod
    def _doc(item: Item, *, secret: str | None = None) -> dict[str, Any]:
        return {
            "type": "work",
            "id": item.id,
            "environment_id": ENV_ID,
            "state": item.state,
            "data": {"type": item.kind, "id": item.session_id},
            "metadata": {},
            "secret": secret,
            "created_at": now_iso(),
            "acknowledged_at": None,
            "latest_heartbeat_at": item.last_heartbeat,
            "started_at": None,
            "stop_requested_at": None,
            "stopped_at": None,
        }

    async def _reap_leases(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            with self.lock:
                for i in self.items.values():
                    if i.state in ("starting", "active") and i.lease_until and time.time() > i.lease_until:
                        # The lease lapsed: back to the queue; the old token stops working.
                        i.state, i.claimed_at, i.acked_at, i.lease_until, i.token = "queued", None, None, None, None
                        i.last_heartbeat = None

    # -- the stub ant's side channel (stands in for the session event stream) -------------------

    def _stub(self, method: str, parts: list[str], headers: dict[str, str], body: bytes) -> tuple[int, Any]:
        token = headers.get("authorization", "")[len("Bearer ") :]
        with self.lock:
            item = self._item_for_token(token)
            if item is None:
                raise _Err(401, "authentication_error", "stub: unknown token")
            s = self.sessions[item.session_id]
            if parts[1:] == ["env"] and method == "POST":
                s.stub_env.append(json.loads(body))
                return 200, {}
            if parts[1:] == ["next"] and method == "GET":
                if s.status == "terminated" or s.deleted:
                    return 200, {"stop": True}
                if s.commands:
                    return 200, {"command": s.commands.pop(0)}
                return 200, {"idle": True}
            if parts[1:] == ["result"] and method == "POST":
                s.results.append(json.loads(body))
                if not s.commands:
                    s.status = "idle"
                    idled = True
                else:
                    idled = False
            else:
                raise _Err(404, "not_found_error", "stub: no route")
        if idled:
            self.emit("session.status_idled", s.id)
        return 200, {}


class _Err(Exception):
    def __init__(self, status: int, kind: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.kind, self.message, self.extra = status, kind, message, extra


@contextlib.contextmanager
def running(**kw: Any) -> Any:
    m = MockCMA(**kw)
    try:
        yield m
    finally:
        m.close()
