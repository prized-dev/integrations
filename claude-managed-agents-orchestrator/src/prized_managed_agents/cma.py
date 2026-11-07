"""The slice of the Claude Managed Agents API the orchestrator calls, with the environment key.

Endpoints (https://platform.claude.com/docs/en/api/beta/environments/work):

- ``GET  /v1/environments/{env}/work/poll``      claim the next work item (with its ``secret``)
- ``POST /v1/environments/{env}/work/{id}/ack``  take it off the queue, once a sandbox holds it
- ``POST /v1/environments/{env}/work/{id}/stop`` end it (``force`` skips the worker's confirmation)
- ``GET  /v1/environments/{env}/work/stats``     queue depth; also our start-up check of the key
- ``GET  /v1/sessions/{id}``                     a session's status and metadata

Plain httpx rather than the anthropic SDK: an SDK client reads ``ANTHROPIC_API_KEY`` from the
environment and a key wins over a bearer token, so a stray organization key on this host would be
sent in place of the environment key. Here the environment key is the only credential, always as
``Authorization: Bearer``, and it is never put in an error or a log line.

The heartbeat and the session's event stream are the worker's business (``ant beta:worker run`` in
the sandbox, on the work item's own secret); nothing here calls them.
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import __version__

API_VERSION = "2026-10-21"
BETA = "managed-agents-2026-04-01"
TRANSIENT = frozenset({408, 429, 500, 502, 503, 504, 529})


class CMAError(Exception):
    """A refused or failed call. ``status`` is None when nothing came back."""

    def __init__(
        self, what: str, status: int | None, kind: str = "", message: str = "", retry_after: str | None = None
    ) -> None:
        self.what, self.status, self.kind, self.message = what, status, kind, message
        self.retry_after = retry_after
        detail = f"HTTP {status}" if status is not None else "no answer"
        if kind or message:
            detail += f" ({kind}{': ' if kind and message else ''}{message[:200]})"
        super().__init__(f"{what}: {detail}")

    @property
    def transient(self) -> bool:
        return self.status is None or self.status in TRANSIENT

    @property
    def auth(self) -> bool:
        return self.status in (401, 403)


@dataclass(frozen=True)
class WorkItem:
    id: str
    session_id: str
    kind: str  # "session"; the queue also sends "healthcheck" items
    environment_id: str
    secret: str | None = field(repr=False)  # the per-session credential; never log it

    @classmethod
    def parse(cls, doc: Any) -> WorkItem | None:
        if not isinstance(doc, dict) or not doc.get("id"):
            return None  # an empty poll
        data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
        return cls(
            id=str(doc["id"]),
            session_id=str(data.get("id") or ""),
            kind=str(data.get("type") or ""),
            environment_id=str(doc.get("environment_id") or ""),
            secret=doc.get("secret") if isinstance(doc.get("secret"), str) and doc.get("secret") else None,
        )


@dataclass(frozen=True)
class Session:
    id: str
    status: str  # rescheduling, running, idle or terminated
    metadata: dict[str, str]


class CMA:
    def __init__(
        self,
        base_url: str,
        environment_id: str,
        environment_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        worker_id: str | None = None,
        timeout: float = 30.0,
        retries: int = 4,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.environment_id = environment_id
        self._key = environment_key
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
        # The API asks for an id unique to each polling worker; the hostname alone isn't.
        self.worker_id = worker_id or f"prized-managed-agents-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.retries = retries
        self.sleep = sleep

    async def close(self) -> None:
        if self._own_http:
            await self.http.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "anthropic-version": API_VERSION,
            "anthropic-beta": BETA,
            "user-agent": f"prized-managed-agents/{__version__}",
        }

    async def _call(
        self,
        what: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        retries: int | None = None,
        ok: tuple[int, ...] = (),
    ) -> tuple[int, Any]:
        """One call, retried on no answer, 408, 429 and 5xx with backoff (Retry-After honoured up to
        20 s). Any other 4xx raises at once. Statuses in ``ok`` are returned instead of raised."""
        attempts = (self.retries if retries is None else retries) + 1
        last: CMAError | None = None
        for attempt in range(attempts):
            if attempt:
                await self.sleep(self._backoff(attempt, last))
            try:
                r = await self.http.request(
                    method,
                    self.base + path,
                    params=params,
                    json=body,
                    headers={**self._headers(), **(headers or {})},
                )
            except httpx.HTTPError as err:
                last = CMAError(what, None, type(err).__name__)
                continue
            if r.status_code in ok or r.is_success:
                return r.status_code, _json(r)
            kind, message = _error(r)
            last = CMAError(what, r.status_code, kind, message, r.headers.get("retry-after"))
            if r.status_code not in TRANSIENT:
                raise last
        assert last is not None
        raise last

    @staticmethod
    def _backoff(attempt: int, last: CMAError | None) -> float:
        after = last.retry_after if last is not None else None
        if after:
            try:
                return min(20.0, max(0.0, float(after)))
            except ValueError:
                pass
        base = min(8.0, 0.5 * 2 ** (attempt - 1))
        return base / 2 + random.uniform(0, base / 2)

    # -- the work queue ------------------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        _, doc = await self._call("work stats", "GET", f"/v1/environments/{self.environment_id}/work/stats")
        return doc if isinstance(doc, dict) else {}

    async def poll(self, *, block_ms: int | None, reclaim_older_than_ms: int) -> WorkItem | None:
        """Claim the next work item, or None. ``block_ms`` (1-999) long-polls; None returns at once.
        An item claimed and never acked comes back after ``reclaim_older_than_ms``."""
        params: dict[str, Any] = {"reclaim_older_than_ms": reclaim_older_than_ms}
        if block_ms is not None:
            params["block_ms"] = max(1, min(999, block_ms))
        _, doc = await self._call(
            "work poll",
            "GET",
            f"/v1/environments/{self.environment_id}/work/poll",
            params=params,
            headers={"Anthropic-Worker-ID": self.worker_id},
            retries=1,  # the caller's loop is the retry
        )
        return WorkItem.parse(doc)

    async def ack(self, work_id: str) -> bool:
        """True once the item is off the queue. A 409 (it is past ``queued``: acked already, or
        stopped) also counts: either way there is nothing left to ack."""
        status, _ = await self._call(
            "work ack",
            "POST",
            f"/v1/environments/{self.environment_id}/work/{work_id}/ack",
            ok=(409,),
            retries=3,
        )
        return status < 300 or status == 409

    async def stop(self, work_id: str, *, force: bool = True) -> None:
        """End the work item. 409 means it has stopped already."""
        await self._call(
            "work stop",
            "POST",
            f"/v1/environments/{self.environment_id}/work/{work_id}/stop",
            body={"force": force},
            ok=(409,),
        )

    # -- sessions -------------------------------------------------------------------------------

    async def session(self, session_id: str) -> Session | None:
        """The session, or None when the API says it no longer exists. Only a 404 whose message is
        exactly ``Session not found: <id>`` counts as gone (Anthropic's own dispatcher draws the
        same line): a proxy's 404, or an organization without the beta, must not delete anything."""
        status, doc = await self._call("get session", "GET", f"/v1/sessions/{session_id}", ok=(404,), retries=2)
        if status == 404:
            err = doc.get("error") if isinstance(doc, dict) else None
            if (
                isinstance(err, dict)
                and err.get("type") == "not_found_error"
                and err.get("message") == f"Session not found: {session_id}"
            ):
                return None
            raise CMAError("get session", 404, str(err.get("type") if isinstance(err, dict) else ""), "")
        if not isinstance(doc, dict):
            raise CMAError("get session", status, "bad_response", "not a JSON object")
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        return Session(
            id=str(doc.get("id") or session_id),
            status=str(doc.get("status") or ""),
            metadata={str(k): str(v) for k, v in meta.items()},
        )


def _json(r: httpx.Response) -> Any:
    if r.status_code == 204 or not r.content:
        return None
    try:
        return r.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _error(r: httpx.Response) -> tuple[str, str]:
    doc = _json(r)
    err = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict):
        return str(err.get("type") or ""), str(err.get("message") or "")
    return "", ""
