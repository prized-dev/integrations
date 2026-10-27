"""Client for Devin's Outposts fleet API (``/opbeta/outposts``).

List, watch, get, claim (and renew), release and operator messages, as documented at
https://docs.devin.ai/cloud/outposts/reference. The API is early access, so entries are read
through ``Entry.parse``, which tolerates fields moving between ``status``/``spec``/``metadata``
and the top level.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("prized_outposts.devin.api")

PREFIX = "/opbeta/outposts"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class OutpostsError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Outposts API answered {status}: {detail[:300]}")
        self.status = status
        self.detail = detail


class ClaimConflict(OutpostsError):
    """409: another acceptor holds the session (or, on release, we no longer do)."""


class NotQueued(OutpostsError):
    """404: the session is not (or no longer) in the queue."""


def _pick(raw: dict[str, Any], section: str, *names: str) -> Any:
    for where in (raw.get(section) or {}, raw):
        if isinstance(where, dict):
            for name in names:
                if where.get(name) is not None:
                    return where[name]
    return None


@dataclass
class Entry:
    session_id: str
    outpost_id: str | None
    kind: str = "new"
    platform: str | None = "linux"
    remote_binary_sha: str | None = None
    image: str | None = None
    image_build: bool = False
    network_policy: dict[str, Any] = field(default_factory=dict)
    phase: str = "pending"
    acceptor_id: str | None = None
    claim_deadline: float | None = None
    session_status: str = "pending"
    connect_token: str | None = field(default=None, repr=False)
    gateway_url: str | None = None
    created_at: float | None = None
    updated_at: float | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Entry:
        sid = _pick(raw, "metadata", "session_id", "sessionId", "devin_id", "id")
        if not sid:
            raise ValueError("queue entry has no session id")
        return cls(
            session_id=str(sid),
            outpost_id=_pick(raw, "metadata", "outpost_id", "outpostId"),
            kind=str(_pick(raw, "spec", "kind") or "new"),
            platform=_pick(raw, "spec", "platform"),
            remote_binary_sha=_pick(raw, "spec", "remote_binary_sha", "remoteBinarySha"),
            image=_pick(raw, "spec", "image"),
            image_build=bool(_pick(raw, "spec", "image_build", "imageBuild")),
            network_policy=_pick(raw, "spec", "network_policy", "networkPolicy") or {},
            phase=str(_pick(raw, "status", "phase") or "pending"),
            acceptor_id=_pick(raw, "status", "acceptor_id", "acceptorId"),
            claim_deadline=_num(_pick(raw, "status", "claim_deadline", "claimDeadline")),
            session_status=str(_pick(raw, "status", "session_status", "sessionStatus") or "pending"),
            connect_token=_pick(raw, "status", "connect_token", "connectToken"),
            gateway_url=_pick(raw, "status", "gateway_url", "gatewayUrl"),
            created_at=_num(_pick(raw, "metadata", "created_at", "createdAt")),
            updated_at=_num(_pick(raw, "metadata", "updated_at", "updatedAt")),
        )


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def api_base(url: str) -> str:
    """``https://api.devin.ai``; also accepts the ``.../opbeta`` form some guides use."""
    url = url.rstrip("/")
    for suffix in (PREFIX, "/opbeta"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


class OutpostsAPI:
    def __init__(
        self,
        base_url: str,
        token: str,
        acceptor_id: str,
        *,
        http: httpx.Client | None = None,
    ) -> None:
        self.base = api_base(base_url)
        self.acceptor_id = acceptor_id
        self._token = token
        self.http = http or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "prized-devin-outposts"})

    def __repr__(self) -> str:  # never show the token
        return f"OutpostsAPI(base={self.base!r}, acceptor_id={self.acceptor_id!r})"

    def _url(self, path: str) -> str:
        return f"{self.base}{PREFIX}{path}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _check(self, resp: httpx.Response) -> httpx.Response:
        if resp.is_success:
            return resp
        detail = resp.text
        if resp.status_code == 409:
            raise ClaimConflict(409, detail)
        if resp.status_code == 404:
            raise NotQueued(404, detail)
        raise OutpostsError(resp.status_code, detail)

    # -- queue ---------------------------------------------------------------------------------

    def list(
        self,
        outpost: str,
        *,
        phase: str | None = None,
        acceptor_id: str | None = None,
        cursor: str | None = None,
        first: int | None = None,
    ) -> dict[str, Any]:
        params = {"outpost": outpost}
        for name, value in (
            ("phase", phase),
            ("acceptor_id", acceptor_id),
            ("cursor", cursor),
            ("first", first),
        ):
            if value is not None:
                params[name] = str(value)
        return self._check(self.http.get(self._url("/devins"), params=params, headers=self._headers())).json()

    def list_all(self, outpost: str, **filters: Any) -> tuple[list[Entry], str | None]:
        """Every entry (later pages win on duplicates) and the cursor to start a watch from."""
        seen: dict[str, Entry] = {}
        cursor = None
        for _ in range(1000):
            page = self.list(outpost, cursor=cursor, **filters)
            for raw in page.get("items") or []:
                try:
                    entry = Entry.parse(raw)
                except ValueError:
                    continue
                seen[entry.session_id] = entry
            cursor = page.get("cursor") or cursor
            if not page.get("has_next_page"):
                break
        return list(seen.values()), cursor

    def watch(
        self, outpost: str, cursor: str | None, stop: threading.Event
    ) -> Iterator[tuple[str, Entry, str | None]]:
        """Yield ``(type, entry, cursor)`` from one SSE stream until it ends (at most ~5 minutes)."""
        params = {"outpost": outpost, "watch": "true"}
        if cursor:
            params["cursor"] = cursor
        timeout = httpx.Timeout(30.0, connect=10.0, read=90.0)
        with self.http.stream(
            "GET", self._url("/devins"), params=params, headers=self._headers(), timeout=timeout
        ) as resp:
            self._check_stream(resp)
            data: list[str] = []
            for line in resp.iter_lines():
                if stop.is_set():
                    return
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "" and data:
                    payload, data = "\n".join(data), []
                    try:
                        event = json.loads(payload)
                        yield (
                            str(event.get("type", "MODIFIED")),
                            Entry.parse(event.get("object") or {}),
                            event.get("cursor"),
                        )
                    except (ValueError, AttributeError):
                        log.warning("skipping a watch event that could not be read")

    def _check_stream(self, resp: httpx.Response) -> None:
        if not resp.is_success:
            resp.read()
            self._check(resp)

    def get(self, session_id: str) -> Entry | None:
        try:
            return Entry.parse(
                self._check(self.http.get(self._url(f"/devins/{session_id}"), headers=self._headers())).json()
            )
        except NotQueued:
            return None

    def claim(self, session_id: str) -> Entry:
        """Claim, or renew a claim we hold. Raises ClaimConflict (409) or NotQueued (404)."""
        resp = self.http.post(
            self._url(f"/devins/{session_id}/claim"),
            json={"acceptor_id": self.acceptor_id},
            headers=self._headers(),
        )
        return Entry.parse(self._check(resp).json())

    def release(self, session_id: str) -> bool:
        """True if released; False if it was not ours any more (409) or is gone (404)."""
        try:
            self._check(
                self.http.post(
                    self._url(f"/devins/{session_id}/release"),
                    json={"acceptor_id": self.acceptor_id},
                    headers=self._headers(),
                )
            )
            return True
        except (ClaimConflict, NotQueued):
            return False

    def operator_message(self, session_id: str, message: str) -> bool:
        try:
            self._check(
                self.http.post(
                    self._url(f"/devins/{session_id}/operator-message"),
                    json={"message": message[:2000]},
                    headers=self._headers(),
                )
            )
            return True
        except OutpostsError as err:
            log.info("operator message to %s not shown (%s)", session_id, err.status)
            return False

    # -- outposts ------------------------------------------------------------------------------

    def outposts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = None
        for _ in range(100):
            params = {"after": after} if after else None
            page = self._check(self.http.get(self._url(""), params=params, headers=self._headers())).json()
            out.extend(page.get("items") or [])
            after = page.get("end_cursor")
            if not page.get("has_next_page") or not after:
                break
        return out

    def resolve_outpost(self, name_or_id: str) -> tuple[str, str | None]:
        """``(outpost_id, platform)`` for an outpost id or name."""
        for item in self.outposts():
            oid = _pick(item, "metadata", "outpost_id", "id")
            name = _pick(item, "spec", "name")
            if name_or_id in (oid, name):
                return str(oid), _pick(item, "spec", "platform")
        if name_or_id.startswith("outpost_"):
            return name_or_id, None
        raise OutpostsError(404, f"no outpost named {name_or_id!r} for this token")
