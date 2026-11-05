#!/usr/bin/env python3
"""Arena relay: many viewers fork one live contestant, one fork call per window.

A viewer asks the relay for a copy of a contestant. The relay holds the request
for up to --window seconds, then forks the contestant once with `count` = the
viewers it has room for. It mints each copy a port token and hands each viewer
its copy id and live URL once the copy answers on it (--ready probe, the
default), or once the API's `?wait=running` hold says it runs (--ready hold).
Every copy is deleted after --ttl seconds. Viewers past the room wait for the
next window, or get `full` when no room frees up in time.

Why batch: separate forks of one sandbox each take their own checkpoint of it,
one at a time, so 25 at once take up to 18-23 s. One `count: 25` fork takes one
checkpoint for all of them.

No copy is left behind. Each one carries the labels relay=<name>,
relay_run=<run> and relay_batch=<batch>, and maxRuntimeMin as a server-side
backstop. Any error after a fork may have made copies deletes the batch by
label. A reconcile pass deletes anything under this run that the relay does not
hold. Shutdown deletes the run, and startup deletes what a previous run under the
same --name left.

Run:  PRIZED_API_KEY=... python relay.py --contestant sbx_... --label agent=me
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import math
import os
import random
import secrets
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
from prized import AsyncSandbox

VERSION = "0.1.0"
SERVER_HOLD_MS = 8000  # the API holds a fork answer at most 8 s from arrival
GONE = ("deleting", "deleted")
MAX_TRIES = 3  # forks a viewer waits through before a 502
PROBE_SLOTS = 12  # probes in flight at once, across every copy
PROBE_EVERY = 0.2  # seconds between one copy's probes; a 429 waits its Retry-After (or 1 s)


def mono() -> float:
    return time.monotonic()


def iso(wall: float) -> str:
    return dt.datetime.fromtimestamp(wall, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- the API: the SDK where it has the call, raw HTTP where it doesn't -------


class Api:
    """SDK 0.1.2 for list and delete-by-label. Raw calls for what 0.1.2 lacks:
    fork with ?wait=running and maxRuntimeMin, port tokens, and a single DELETE
    without a handle."""

    def __init__(
        self, base_url: str, api_key: str, client: httpx.AsyncClient, probe_client: Optional[httpx.AsyncClient] = None
    ) -> None:
        self.base = base_url.rstrip("/")
        self.client = client
        self.probe_client = probe_client or client
        # The preview proxy rate-limits (429) when 36 copies are polled at once,
        # so probes go out a few at a time.
        self.probe_slots = asyncio.Semaphore(PROBE_SLOTS)
        self.headers = {"Authorization": f"Bearer {api_key}", "User-Agent": f"prized-arena-relay/{VERSION}"}
        self.sdk = {"api_key": api_key, "base_url": self.base, "http_client": client}

    def _u(self, path: str) -> str:
        return f"{self.base}/api/v1{path}"

    async def fork(
        self, parent: str, count: int, labels: Dict[str, str], max_runtime_min: int, key: str, timeout: float,
        hold: bool = True,
    ) -> Tuple[int, Dict[str, Any]]:
        r = await self.client.post(
            self._u(f"/sandboxes/{parent}/fork"),
            params={"wait": "running", "waitTimeoutMs": str(SERVER_HOLD_MS)} if hold else None,
            json={"count": count, "labels": labels, "maxRuntimeMin": max_runtime_min},
            headers={**self.headers, "Idempotency-Key": key},
            timeout=timeout,
        )
        return r.status_code, _doc(r)

    async def get(self, sid: str) -> Tuple[int, Dict[str, Any]]:
        r = await self.client.get(self._u(f"/sandboxes/{sid}"), headers=self.headers, timeout=10)
        return r.status_code, _doc(r)

    async def token(self, sid: str, port: int, expires_in: int) -> Tuple[int, Dict[str, Any]]:
        r = await self.client.post(
            self._u(f"/sandboxes/{sid}/ports/{port}/token"),
            json={"expiresIn": expires_in},
            headers=self.headers,
            timeout=10,
        )
        return r.status_code, _doc(r)

    async def probe(self, url: str) -> Tuple[int, Optional[float]]:
        """GET a copy's live URL (its token in the query, no API key): the
        status, and Retry-After in seconds when the proxy sent one."""
        async with self.probe_slots:
            r = await self.probe_client.get(url, timeout=3)
        retry = None
        with contextlib.suppress(TypeError, ValueError):
            retry = float(r.headers.get("retry-after"))
        return r.status_code, retry

    async def delete(self, sid: str) -> bool:
        """True once the sandbox is deleted or going (404 counts: it is gone)."""
        r = await self.client.delete(self._u(f"/sandboxes/{sid}"), headers=self.headers, timeout=15)
        return r.status_code < 300 or r.status_code == 404

    async def delete_labels(self, labels: Dict[str, str]) -> List[str]:
        return await AsyncSandbox.delete_by_labels(labels, **self.sdk)

    async def list_labels(self, labels: Dict[str, str]) -> List[Tuple[str, str, Dict[str, str]]]:
        found = await AsyncSandbox.list(labels=labels, **self.sdk)
        out = []
        for sb in found:
            out.append((sb.id, sb.state, dict(sb.labels)))
            await sb.close()
        return out


def _doc(r: httpx.Response) -> Dict[str, Any]:
    with contextlib.suppress(ValueError):
        d = r.json()
        if isinstance(d, dict):
            return d
    return {}


# --- outcomes ----------------------------------------------------------------


class Full(Exception):
    """No room for this viewer in time. retry_after: seconds until a copy frees."""

    def __init__(self, reason: str, retry_after: Optional[float] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class ForkFailed(Exception):
    """final: retrying will not help (the API refused for good), so the viewer
    gets 502 now instead of waiting for room."""

    def __init__(self, msg: str, final: bool = False) -> None:
        super().__init__(msg)
        self.final = final


class NoRoom(Exception):
    """The API reported room for 0 copies (fork_capacity fits or quota admitted)."""

    def __init__(self, error: str, room: int) -> None:
        super().__init__(f"{error}: room {room}")
        self.error = error
        self.room = room


@dataclass
class Viewer:
    vid: str
    arrived: float
    deadline: float
    fut: "asyncio.Future[Copy]"
    tries: int = 0


@dataclass
class Copy:
    id: str
    contestant: str
    batch: str
    batch_size: int
    url: str
    browser_url: Optional[str]
    born: float  # monotonic: when its fork POST went out
    ready: float  # monotonic: running and its URL minted
    expires: float  # monotonic
    expires_wall: float
    viewer: str = ""


@dataclass
class Config:
    contestants: List[str]
    window: float = 1.0
    cap: int = 36
    ttl: float = 300.0
    queue_max: float = 10.0
    max_pending: int = 200
    port: int = 8080
    name: str = "arena-relay"
    labels: Dict[str, str] = field(default_factory=dict)
    fork_timeout: float = 30.0
    reconcile_every: float = 15.0
    no_room_backoff: float = 2.0
    events: Optional[str] = None
    ready: str = "probe"  # probe: no hold, the relay polls each copy's URL; hold: ?wait=running
    probe_path: str = "/"
    fault_token: float = 0.0  # tests: share of port-token mints made to fail
    fault_batch: int = 0  # tests: fail the Nth batch after its fork answered


# --- one contestant's queue and windows --------------------------------------


class Batcher:
    def __init__(self, relay: "Relay", cid: str) -> None:
        self.r = relay
        self.cid = cid
        self.pending: Deque[Viewer] = deque()
        self.window: Optional[asyncio.Task] = None
        self.wake = asyncio.Event()
        self.inflight = 0
        self.no_room_until = 0.0
        self.retry: Optional[asyncio.TimerHandle] = None

    def live(self) -> List[Copy]:
        return [c for c in self.r.live.values() if c.contestant == self.cid]

    def room(self) -> int:
        return self.r.cfg.cap - len(self.live()) - self.inflight

    def _frees_by(self, t: float) -> int:
        return sum(1 for c in self.live() if c.expires <= t)

    async def request(self, vid: str) -> Copy:
        cfg = self.r.cfg
        now = mono()
        self._expire(now)
        if self.r.closing:
            raise Full("shutting_down")
        if len(self.pending) >= cfg.max_pending:
            raise Full("queue_full", cfg.window)
        deadline = now + cfg.queue_max
        # Answer `full` at once when the room, plus every copy that frees
        # before this viewer's deadline, is already spoken for by the queue.
        if self.room() + self._frees_by(deadline) - len(self.pending) <= 0:
            later = sorted(c.expires for c in self.live() if c.expires > deadline)
            raise Full("contestant_full", round(later[0] - now, 1) if later else None)
        v = Viewer(vid, now, deadline, asyncio.get_running_loop().create_future())
        self.pending.append(v)
        self.arm()
        try:
            return await v.fut
        except asyncio.CancelledError:
            # The viewer left. Still queued: drop it. Served meanwhile: delete
            # its copy now rather than at its TTL.
            with contextlib.suppress(ValueError):
                self.pending.remove(v)
            if v.fut.done() and not v.fut.cancelled() and v.fut.exception() is None:
                self.r.spawn(self.r.release(v.fut.result().id, "viewer_left"))
            raise

    def arm(self) -> None:
        """Open a window if none is open; flush early once the queue fills the room."""
        if not self.pending or self.r.closing:
            return
        if self.window is None or self.window.done():
            self.window = asyncio.ensure_future(self._window())
        if len(self.pending) >= max(1, self.room()):
            self.wake.set()

    async def _window(self) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.wake.wait(), self.r.cfg.window)
        self.wake.clear()
        self.window = None  # arrivals from here on open the next window
        task = asyncio.current_task()
        self.r.flushing.add(task)
        try:
            await self._flush()
        except Exception as exc:  # a bug: _flush has already put its viewers back
            self.r.log("flush_error", contestant=self.cid, error=repr(exc))
        finally:
            self.r.flushing.discard(task)
            self._retry_later()

    def _expire(self, now: float) -> None:
        keep: Deque[Viewer] = deque()
        for v in self.pending:
            if v.fut.done():
                continue
            if now >= v.deadline:
                later = sorted(c.expires for c in self.live())
                v.fut.set_exception(Full("no_room_in_time", round(later[0] - now, 1) if later else None))
            else:
                keep.append(v)
        self.pending = keep

    def _retry_later(self) -> None:
        """Queued viewers remain: look again when room may have freed."""
        if not self.pending or self.r.closing:
            return
        now = mono()
        times = [v.deadline for v in self.pending] + [c.expires for c in self.live()]
        if self.no_room_until > now:
            times.append(self.no_room_until)
        delay = max(0.05, min(times) - now) if times else self.r.cfg.window
        delay = min(delay, self.r.cfg.window * 2)
        if self.retry is not None:
            self.retry.cancel()
        self.retry = asyncio.get_running_loop().call_later(delay, self.arm)

    async def _flush(self) -> None:
        now = mono()
        self._expire(now)
        if not self.pending:
            return
        room = 0 if now < self.no_room_until else self.room()
        group: List[Viewer] = []
        while self.pending and len(group) < room:
            v = self.pending.popleft()
            if not v.fut.done():
                group.append(v)
        if not group:
            self._retry_later()
            return
        n = len(group)
        batch = f"b{self.r.run}-{next(self.r.batch_seq)}"
        self.inflight += n
        self.r.inflight_batches.add(batch)
        got = 0  # copies this batch has handed over
        nxt = 0  # the next viewer of the group to serve
        failed: Optional[ForkFailed] = None

        def hand(c: Copy) -> None:
            # Each copy goes out the moment it is ready, to the first viewer of
            # the group still waiting, else to the head of the queue. The
            # inflight count drops as the live count rises, in the same step.
            nonlocal got, nxt
            if got < n:
                got += 1
                self.inflight -= 1
            while nxt < len(group) and group[nxt].fut.done():
                nxt += 1
            if nxt < len(group):
                nxt += 1
                self.r.assign(group[nxt - 1], c)
                return
            while self.pending and self.pending[0].fut.done():
                self.pending.popleft()
            if self.pending and not self.r.closing:
                self.r.assign(self.pending.popleft(), c)
            else:
                self.r.spawn(self.r.release_unheld(c, "no_viewer"))

        try:
            await self.r.fork_batch(self.cid, batch, n, hand)
        except NoRoom as exc:
            self.no_room_until = mono() + self.r.cfg.no_room_backoff
            self.r.log("no_room", contestant=self.cid, batch=batch, asked=n, error=exc.error, room=exc.room)
        except ForkFailed as exc:
            failed = exc
            self.r.log("fork_failed", contestant=self.cid, batch=batch, asked=n, error=str(exc), final=exc.final)
        finally:
            # Whatever happened (a bug or a cancellation too), every viewer of
            # the group not served is answered or queued again.
            self.inflight -= n - got
            self.r.inflight_batches.discard(batch)
            for v in reversed(group[nxt:]):
                if v.fut.done():
                    continue
                v.tries += 1
                if self.r.closing:
                    v.fut.set_exception(Full("shutting_down"))
                elif failed is not None and (failed.final or v.tries >= MAX_TRIES):
                    v.fut.set_exception(failed)
                else:
                    self.pending.appendleft(v)
            self._expire(mono())


# --- the relay ---------------------------------------------------------------


class Relay:
    def __init__(self, cfg: Config, api: Api) -> None:
        self.cfg = cfg
        self.api = api
        self.run = secrets.token_hex(3)
        self.batch_seq = iter(range(1, 1 << 30))
        self.batchers = {cid: Batcher(self, cid) for cid in cfg.contestants}
        self.live: Dict[str, Copy] = {}
        self.doomed: Dict[str, Tuple[str, int, float]] = {}  # copy id -> (reason, tries, next try): retried by the reaper
        self.deleting: set = set()
        self.deleted: set = set()
        self.inflight_batches: set = set()
        self.flushing: set = set()
        self.tasks: set = set()
        self.closing = False
        self.batch_n = 0
        self.max_runtime_min = int(math.ceil(cfg.ttl / 60.0)) + 2
        self.labels = {
            **cfg.labels,
            "role": "copy",
            "relay": cfg.name,
            "relay_run": self.run,
        }
        self.stats: Dict[str, float] = {
            "viewers": 0, "served": 0, "full": 0, "failed": 0, "left": 0,
            "batches": 0, "fork_calls": 0, "copies_forked": 0, "copies_deleted": 0,
            "delete_retries": 0, "orphans_deleted": 0, "copy_seconds": 0.0,
        }
        self._events = open(cfg.events, "a", buffering=1) if cfg.events else None
        self._bg: List[asyncio.Task] = []

    # bookkeeping

    def log(self, ev: str, **kw: Any) -> None:
        """Best effort: a full disk or a closed stderr never stops the relay."""
        try:
            line = json.dumps({"t": round(time.time(), 3), "ev": ev, **kw}, default=str)
            if self._events is not None:
                self._events.write(line + "\n")
            if ev in ("fork_failed", "no_room", "flush_error", "orphan", "delete_failed", "sweep", "startup", "closed"):
                print(line, file=sys.stderr, flush=True)
        except Exception:
            pass

    def spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # forking

    async def fork_batch(self, cid: str, batch: str, n: int, hand: Callable[[Copy], None]) -> None:
        """Fork `n` copies (fewer if the API reports less room) and pass each
        to `hand` once it is ready, with its URL minted. Every copy of the
        batch is either handed out or deleted, whatever happens."""
        cfg = self.cfg
        labels = {**self.labels, "relay_batch": batch}
        born = mono()
        want = n
        tries: List[Dict[str, Any]] = []
        doc: Dict[str, Any] = {}
        self.stats["batches"] += 1
        self.batch_n += 1
        nth = self.batch_n
        while True:
            self.stats["fork_calls"] += 1
            t = mono()
            try:
                status, doc = await self.api.fork(
                    cid, want, labels, self.max_runtime_min, f"{batch}-{want}", cfg.fork_timeout, cfg.ready == "hold"
                )
            except BaseException as exc:
                # No answer, or cancelled mid-call: the copies may exist. The
                # sweep runs on its own task, so the viewers go back at once.
                self.spawn(self._purge_batch(batch, "fork_unanswered"))
                if isinstance(exc, Exception):
                    raise ForkFailed(f"no answer: {exc!r}") from exc
                raise
            tries.append({"count": want, "status": status, "ms": round((mono() - t) * 1000), "error": doc.get("error")})
            if status in (200, 201, 202):
                break
            room = None
            if status == 409 and doc.get("error") == "fork_capacity":
                room = _int(doc.get("fits"))
            elif status == 429 and doc.get("error") == "sandbox_quota_exceeded":
                room = _int(doc.get("admitted"))
            if room is not None:
                if room <= 0 or len(tries) >= 3:
                    self.log("batch", contestant=cid, batch=batch, asked=n, got=0, tries=tries)
                    raise NoRoom(doc["error"], room)
                want = min(want - 1, room)  # the API's room, never more
                continue
            self.log("batch", contestant=cid, batch=batch, asked=n, got=0, tries=tries)
            if status >= 500 or status == 429:
                # A 5xx may come after the copies were made; try again next window.
                self.spawn(self._purge_batch(batch, f"fork_{status}"))
                raise ForkFailed(f"{status} {doc.get('error')}")
            # Any other refusal (contestant gone or paused, bad key): it will
            # not change in a second, so the viewers hear it now.
            raise ForkFailed(f"{status} {doc.get('error')}", final=True)
        answered = mono()
        sandboxes = [s for s in (doc.get("sandboxes") or []) if isinstance(s, dict) and s.get("id")]
        ids = [s["id"] for s in sandboxes]
        self.stats["copies_forked"] += len(ids)
        trace: Dict[str, Any] = {"mint_s": [], "url_s": [], "mint_refusals": {}, "probe_answers": {}}
        handed: List[str] = []

        def ready_one(s: Dict[str, Any], minted: Dict[str, Any]) -> None:
            now = mono()
            handed.append(s["id"])
            hand(Copy(
                id=s["id"], contestant=cid, batch=batch, batch_size=len(ids), url=minted["url"],
                browser_url=minted.get("browserUrl"), born=born, ready=now,
                expires=now + cfg.ttl, expires_wall=time.time() + cfg.ttl,
            ))

        async def probe_one(s: Dict[str, Any]) -> None:
            ready_one(s, await self._mint_and_probe(s["id"], born, born + cfg.fork_timeout, trace))

        try:
            if cfg.fault_batch and nth == cfg.fault_batch:
                raise ForkFailed("fault injected after the fork answered")
            if cfg.ready == "probe":
                await asyncio.gather(*(probe_one(s) for s in sandboxes), return_exceptions=True)
            else:
                running = await self._until_running(sandboxes, born + cfg.fork_timeout)
                minted = await asyncio.gather(*(self._mint(s["id"]) for s in running), return_exceptions=True)
                for s, m in zip(running, minted):
                    if not isinstance(m, BaseException):
                        ready_one(s, m)
        except BaseException as exc:
            # Copies already handed out are the relay's now (TTL, shutdown);
            # every other copy of this batch goes.
            rest = [sid for sid in ids if sid not in handed]
            self.spawn(self._purge_ids(rest, None if handed else batch, "batch_error"))
            self.log("batch", contestant=cid, batch=batch, asked=n, got=len(handed), tries=tries, error=repr(exc))
            if isinstance(exc, Exception) and not isinstance(exc, ForkFailed):
                raise ForkFailed(repr(exc)) from exc
            raise
        bad = [sid for sid in ids if sid not in handed]
        for sid in bad:
            self.spawn(self._delete(sid, "not_ready", born))
        self.log(
            "batch", contestant=cid, batch=batch, asked=n, got=len(handed), bad=len(bad), tries=tries,
            fork_ms=round((answered - born) * 1000), last_ready_ms=round((mono() - born) * 1000),
            states=_count(s.get("state") for s in sandboxes),
            **({"trace": _trace_summary(trace)} if cfg.ready == "probe" else {}),
        )

    async def _until_running(self, sandboxes: List[Dict[str, Any]], deadline: float) -> List[Dict[str, Any]]:
        """The copies that run. A copy the hold answered while still starting
        is read again until it runs, fails, or the deadline passes."""
        states = {s["id"]: s.get("state") for s in sandboxes}
        while True:
            waiting = [sid for sid, st in states.items() if st == "starting"]
            if not waiting or mono() >= deadline:
                break
            await asyncio.sleep(0.25)
            for sid, (status, d) in zip(waiting, await asyncio.gather(*(self.api.get(sid) for sid in waiting))):
                d = d.get("sandbox", d)
                states[sid] = d.get("state") if status == 200 else "gone"
        return [s for s in sandboxes if states[s["id"]] == "running"]

    async def _mint_and_probe(self, sid: str, born: float, deadline: float, trace: Dict[str, Any]) -> Dict[str, Any]:
        """Probe mode: mint the copy's URL as soon as the API allows, then poll
        the URL until the copy itself answers 2xx/3xx on --probe-path. The trace
        gathers when each step came through and what the misses answered."""
        while True:
            try:
                minted = await self._mint(sid)
                break
            except ForkFailed as exc:
                if mono() >= deadline or exc.final:
                    raise
                key = str(exc).rsplit(": ", 1)[-1][:40]
                trace["mint_refusals"][key] = trace["mint_refusals"].get(key, 0) + 1
                await asyncio.sleep(0.25)
        trace["mint_s"].append(mono() - born)
        url = _with_path(minted["url"], self.cfg.probe_path)
        last: Any = None
        while mono() < deadline:
            pause = PROBE_EVERY
            try:
                last, retry_after = await self.api.probe(url)
                if last == 429:
                    pause = min(5.0, retry_after if retry_after is not None else 1.0)
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            trace["probe_answers"][str(last)] = trace["probe_answers"].get(str(last), 0) + 1
            if isinstance(last, int) and last < 400:
                trace["url_s"].append(mono() - born)
                return minted
            await asyncio.sleep(pause)
        raise ForkFailed(f"{sid} never answered on its URL: {last}")

    async def _mint(self, sid: str) -> Dict[str, Any]:
        if self.cfg.fault_token and random.random() < self.cfg.fault_token:
            raise ForkFailed("fault injected: token mint", final=True)
        expires_in = int(self.cfg.ttl) + 60
        last: Any = None
        for _ in range(2):
            try:
                status, d = await self.api.token(sid, self.cfg.port, expires_in)
            except httpx.HTTPError as exc:
                last = exc
                continue
            if status < 300 and d.get("url"):
                return d
            last = f"{status} {d.get('error')}"
            if status in (401, 403, 404):  # the copy is gone, or the key can't: no retry helps
                raise ForkFailed(f"port token for {sid}: {last}", final=True)
            if status < 500:
                break
        raise ForkFailed(f"port token for {sid}: {last}")

    # handing out and taking back

    def assign(self, v: Viewer, c: Copy) -> None:
        c.viewer = v.vid
        self.live[c.id] = c
        if v.fut.done():  # the viewer left while its copy was forked
            self.spawn(self.release(c.id, "viewer_left"))
        else:
            v.fut.set_result(c)

    async def release_unheld(self, c: Copy, reason: str) -> None:
        await self._delete(c.id, reason, c.born)

    async def release(self, sid: str, reason: str) -> None:
        c = self.live.pop(sid, None)
        if c is None:
            return
        await self._delete(sid, reason, c.born)
        b = self.batchers.get(c.contestant)
        if b is not None:
            b.arm()

    async def _delete(self, sid: str, reason: str, born: Optional[float] = None) -> None:
        if sid in self.deleting:
            return
        self.deleting.add(sid)
        ok = False
        try:
            with contextlib.suppress(Exception):
                ok = await self.api.delete(sid)
        finally:
            self.deleting.discard(sid)
        if ok:
            self.doomed.pop(sid, None)
            if sid not in self.deleted:
                self.deleted.add(sid)
                self.stats["copies_deleted"] += 1
            if born is not None:
                self.stats["copy_seconds"] += mono() - born
            self.log("deleted", id=sid, reason=reason)
        else:
            # The reaper tries again in 0.5 s, 1 s, 2 s, 4 s, then every 5 s.
            tries = self.doomed[sid][1] + 1 if sid in self.doomed else 1
            self.doomed[sid] = (reason, tries, mono() + min(5.0, 0.25 * 2 ** tries))
            self.stats["delete_retries"] += 1
            self.log("delete_failed", id=sid, reason=reason, tries=tries)

    async def _sweep(self, batch: str, reason: str, delay: float = 0.0) -> None:
        await asyncio.sleep(delay)
        with contextlib.suppress(Exception):
            gone = await self.api.delete_labels({"relay_run": self.run, "relay_batch": batch})
            self.stats["copies_deleted"] += len(set(gone) - self.deleted)
            self.deleted.update(gone)
            self.log("sweep", batch=batch, reason=reason, deleted=gone)

    async def _purge_batch(self, batch: str, reason: str) -> None:
        """A fork with no answer: delete every copy of its batch by label now,
        and again in a few seconds in the background, in case the fork was
        still committing. The reconcile pass catches anything later still."""
        await self._sweep(batch, reason)
        self.spawn(self._sweep(batch, reason + "_again", 3.0))

    async def _purge_ids(self, ids: List[str], batch: Optional[str], reason: str) -> None:
        """A fork that answered: its answer listed every copy. The label sweep
        too only when no copy of the batch was handed out."""
        await asyncio.gather(*(self._delete(sid, reason) for sid in ids))
        if batch is not None:
            await self._sweep(batch, reason)

    # background passes

    async def reaper(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            now = mono()
            for c in [c for c in self.live.values() if c.expires <= now]:
                self.spawn(self.release(c.id, "ttl"))
            for sid, (reason, _, due) in list(self.doomed.items()):
                if due <= now:
                    self.spawn(self._delete(sid, reason))

    async def reconciler(self) -> None:
        """Delete anything under this run that the relay does not hold."""
        while True:
            await asyncio.sleep(self.cfg.reconcile_every)
            with contextlib.suppress(Exception):
                await self.reconcile()

    async def reconcile(self) -> int:
        found = await self.api.list_labels({"relay": self.cfg.name, "relay_run": self.run})
        orphans = [
            sid for sid, state, labels in found
            if state not in GONE and sid not in self.live and sid not in self.deleting and sid not in self.doomed
            and labels.get("relay_batch") not in self.inflight_batches
        ]
        for sid in orphans:
            self.stats["orphans_deleted"] += 1
            self.log("orphan", id=sid)
            await self._delete(sid, "orphan")
        return len(orphans)

    async def startup(self) -> None:
        gone = await self.api.delete_labels({"relay": self.cfg.name})
        self.log("startup", run=self.run, swept_previous=gone)
        self._bg = [asyncio.ensure_future(self.reaper()), asyncio.ensure_future(self.reconciler())]

    async def close(self, grace: float = 2.0) -> Dict[str, Any]:
        """Stop taking viewers, delete every copy of this run, give in-flight
        forks `grace` seconds and then cancel them (each one's copies are swept
        by label as it unwinds), and sweep and list until nothing of the run is
        left. Call it while the HTTP client is open."""
        self.closing = True
        run = {"relay": self.cfg.name, "relay_run": self.run}
        for b in self.batchers.values():
            if b.retry is not None:
                b.retry.cancel()
            for v in b.pending:
                if not v.fut.done():
                    v.fut.set_exception(Full("shutting_down"))
            b.pending.clear()
            if b.window is not None and b.window not in self.flushing:
                b.window.cancel()
        # First the copies already out, in case a SIGKILL follows the SIGTERM.
        with contextlib.suppress(Exception):
            await self.api.delete_labels(run)
        if self.flushing:
            _, slow = await asyncio.wait(list(self.flushing), timeout=grace)
            for t in slow:  # their own cleanup runs as they unwind
                t.cancel()
            if slow:
                await asyncio.wait(slow, timeout=10)
        for t in self._bg:
            t.cancel()
        await asyncio.gather(*(self.release(sid, "shutdown") for sid in list(self.live)), return_exceptions=True)
        if self.tasks:
            await asyncio.wait(list(self.tasks), timeout=20)
        swept: List[str] = []
        left: List[str] = []
        for attempt in range(6):
            with contextlib.suppress(Exception):
                swept += await self.api.delete_labels(run)
            try:
                left = [s[0] for s in await self.api.list_labels(run) if s[1] not in GONE]
            except Exception as exc:
                left = [f"unknown: {exc!r}"]
            if not left:
                break
            await asyncio.sleep(2.0)
        out = {"run": self.run, "swept_at_close": swept, "left_not_deleting": left, **self.stats}
        self.log("closed", **out)
        return out

    def snapshot(self) -> Dict[str, Any]:
        return {
            "run": self.run, "live": len(self.live), "doomed": len(self.doomed),
            "inflight_batches": len(self.inflight_batches),
            "contestants": {
                cid: {"pending": len(b.pending), "live": len(b.live()), "inflight": b.inflight, "room": b.room()}
                for cid, b in self.batchers.items()
            },
            **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.stats.items()},
        }


async def _finish(coro: Any) -> None:
    """Run cleanup to its end even if the caller is cancelled meanwhile."""
    task = asyncio.ensure_future(coro)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        raise


def _int(x: Any) -> int:
    try:
        return max(0, int(x))
    except (TypeError, ValueError):
        return 0


def _trace_summary(trace: Dict[str, Any]) -> Dict[str, Any]:
    def span(xs: List[float]) -> Optional[List[float]]:
        return [round(min(xs), 2), round(sorted(xs)[len(xs) // 2], 2), round(max(xs), 2)] if xs else None
    return {"mint_s_min_p50_max": span(trace["mint_s"]), "url_s_min_p50_max": span(trace["url_s"]),
            "mint_refusals": trace["mint_refusals"], "probe_answers": trace["probe_answers"]}


def _with_path(url: str, path: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, path, p.query, ""))


def _count(xs: Any) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return out


# --- HTTP: a few routes, JSON, one request per connection --------------------


class Http:
    def __init__(self, relay: Relay) -> None:
        self.r = relay

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        path = ""
        try:
            try:
                method, path, body = await asyncio.wait_for(_read_request(reader), 10)
            except LengthRequired:
                await _write(writer, 411, {"error": "length_required"})
                return
            except Exception:
                return
            answer = await self.route(method, path, body, reader)
            if answer is None:
                return
            status, doc = answer
            try:
                await _write(writer, status, doc)
            except Exception:
                # The viewer is gone: a copy it never got goes back now, not at its TTL.
                if status == 200 and isinstance(doc, dict) and doc.get("copyId"):
                    self.r.stats["left"] += 1
                    await self.r.release(doc["copyId"], "viewer_left")
                raise
        except Exception as exc:
            self.r.log("http_error", path=path, error=repr(exc))
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def route(self, method: str, path: str, body: bytes, reader: asyncio.StreamReader) -> Optional[Tuple[int, Any]]:
        parts = [p for p in path.split("?")[0].split("/") if p]
        if method == "GET" and parts == ["healthz"]:
            return 200, {"ok": not self.r.closing}
        if method == "GET" and parts == ["v1", "stats"]:
            return 200, self.r.snapshot()
        if method == "DELETE" and len(parts) == 3 and parts[:2] == ["v1", "copies"]:
            if parts[2] not in self.r.live:
                return 404, {"error": "not_found"}
            await self.r.release(parts[2], "viewer_done")
            return 204, None
        if method == "POST" and len(parts) == 4 and parts[0] == "v1" and parts[1] == "contestants" and parts[3] == "copies":
            return await self.copy(parts[2], body, reader)
        return 404, {"error": "not_found"}

    async def copy(self, cid: str, body: bytes, reader: asyncio.StreamReader) -> Optional[Tuple[int, Any]]:
        b = self.r.batchers.get(cid)
        if b is None:
            return 404, {"error": "unknown_contestant"}
        vid = ""
        with contextlib.suppress(ValueError, AttributeError):
            vid = str(json.loads(body or b"{}").get("viewer") or "")[:64]
        vid = vid or secrets.token_hex(4)
        self.r.stats["viewers"] += 1
        arrived = mono()
        work = asyncio.ensure_future(b.request(vid))
        gone = asyncio.ensure_future(_hangup(reader))
        try:
            await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:  # our own cancellation too: the request goes with us
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            gone_now = gone.done()
            gone.cancel()
        if work.cancelled() or gone_now:
            if not work.cancelled() and work.exception() is None:
                await self.r.release(work.result().id, "viewer_left")  # hung up as its copy came
            self.r.stats["left"] += 1
            self.r.log("viewer", viewer=vid, contestant=cid, outcome="left", wait_ms=round((mono() - arrived) * 1000))
            return None
        try:
            c = work.result()
        except Full as exc:
            self.r.stats["full"] += 1
            self.r.log("viewer", viewer=vid, contestant=cid, outcome="full", reason=exc.reason, wait_ms=round((mono() - arrived) * 1000))
            return 503, {"error": "full", "reason": exc.reason, "retryAfterS": exc.retry_after}
        except Exception as exc:
            self.r.stats["failed"] += 1
            self.r.log("viewer", viewer=vid, contestant=cid, outcome="failed", error=repr(exc))
            return 502, {"error": "fork_failed", "reason": str(exc)[:200]}
        self.r.stats["served"] += 1
        self.r.log(
            "viewer", viewer=vid, contestant=cid, outcome="served", copy=c.id, batch=c.batch, batch_size=c.batch_size,
            wait_ms=round((mono() - arrived) * 1000), window_ms=round((c.born - arrived) * 1000),
            fork_ms=round((c.ready - c.born) * 1000),
        )
        return 200, {
            "copyId": c.id, "contestant": cid, "url": c.url, "browserUrl": c.browser_url,
            "expiresAt": iso(c.expires_wall), "ttlS": self.r.cfg.ttl, "batch": c.batch, "batchSize": c.batch_size,
            "waitMs": round((mono() - arrived) * 1000),
        }


_REASONS = {
    200: "OK", 204: "No Content", 404: "Not Found", 411: "Length Required", 502: "Bad Gateway",
    503: "Service Unavailable",
}


class LengthRequired(Exception):
    pass


async def _write(writer: asyncio.StreamWriter, status: int, doc: Any) -> None:
    payload = b"" if status == 204 else json.dumps(doc).encode()
    head = (
        f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    )
    writer.write(head.encode() + payload)
    await writer.drain()


async def _hangup(reader: asyncio.StreamReader) -> None:
    """Returns when the viewer closes its side (EOF). Stray bytes are not a hangup."""
    while await reader.read(4096):
        pass


async def _read_request(reader: asyncio.StreamReader) -> Tuple[str, str, bytes]:
    line = (await reader.readline()).decode("latin-1")
    method, path, _ = line.split(" ", 2)
    length = 0
    while True:
        h = await reader.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        k, _, v = h.decode("latin-1").partition(":")
        if k.strip().lower() == "transfer-encoding":
            raise LengthRequired()
        if k.strip().lower() == "content-length":
            length = int(v.strip())
    if length > 65536:
        raise ValueError("body too large")
    body = await reader.readexactly(length) if length else b""
    return method, path, body


# --- main --------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> Tuple[Config, argparse.Namespace]:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--contestant", action="append", required=True, help="sandbox id viewers may fork (repeatable)")
    p.add_argument("--listen", default="127.0.0.1:0", help="host:port (port 0 = any free port)")
    p.add_argument("--window", type=float, default=1.0, help="seconds to collect viewers before one fork")
    p.add_argument("--cap", type=int, default=36, help="most live copies per contestant")
    p.add_argument("--ttl", type=float, default=300.0, help="seconds each copy lives")
    p.add_argument("--queue-max", type=float, default=10.0, help="seconds a viewer waits for room before `full`")
    p.add_argument("--port", type=int, default=8080, help="the contestant's port each viewer gets a URL for")
    p.add_argument("--name", default="arena-relay", help="relay label; startup deletes copies a previous run left")
    p.add_argument("--label", action="append", default=[], help="k=v on every copy (repeatable)")
    p.add_argument("--api-url", default=os.environ.get("PRIZED_BASE_URL", "https://api.prized.dev"))
    p.add_argument("--api-key-env", default="PRIZED_API_KEY", help="environment variable that holds the API key")
    p.add_argument("--events", help="append a JSON line per event to this file")
    p.add_argument("--ready-file", help="write {port, run} here once listening")
    p.add_argument("--ready", choices=["probe", "hold"], default="probe",
                   help="probe: fork, then poll each copy's URL until it answers; hold: fork with ?wait=running")
    p.add_argument("--probe-path", default="/", help="path a ready copy answers 2xx/3xx on (probe mode)")
    p.add_argument("--fault-token", type=float, default=0.0, help=argparse.SUPPRESS)
    p.add_argument("--fault-batch", type=int, default=0, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    labels = dict(kv.split("=", 1) for kv in a.label)
    cfg = Config(
        contestants=a.contestant, window=a.window, cap=a.cap, ttl=a.ttl, queue_max=a.queue_max, port=a.port,
        name=a.name, labels=labels, events=a.events, fault_token=a.fault_token, fault_batch=a.fault_batch,
        ready=a.ready, probe_path=a.probe_path,
    )
    return cfg, a


async def amain(argv: Optional[List[str]] = None) -> int:
    cfg, a = parse_args(argv)
    key = os.environ.get(a.api_key_env, "").strip()
    if not key:
        print(f"set {a.api_key_env} to a Prized API key", file=sys.stderr)
        return 2
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    # Probes get their own pool, so polling copies never starves forks and deletes.
    async with httpx.AsyncClient(limits=limits, timeout=30) as client, httpx.AsyncClient(limits=limits) as probes:
        relay = Relay(cfg, Api(a.api_url, key, client, probes))
        await relay.startup()
        host, _, port = a.listen.rpartition(":")
        server = await asyncio.start_server(Http(relay).handle, host or "127.0.0.1", int(port))
        bound = server.sockets[0].getsockname()[1]
        print(json.dumps({"listening": f"http://{host or '127.0.0.1'}:{bound}", "run": relay.run}), flush=True)
        if a.ready_file:
            with open(a.ready_file, "w") as f:
                json.dump({"port": bound, "run": relay.run}, f)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        server.close()
        out = await relay.close()
        print(json.dumps({"closed": out}), flush=True)
        return 0 if not out["left_not_deleting"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
