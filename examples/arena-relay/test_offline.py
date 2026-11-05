"""Offline tests: the relay against a fake API (httpx.MockTransport). No key, no network.

    python test_offline.py      (or pytest, if you have it)
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay as R  # noqa: E402

CID = "sbx_contestant0001"


class FakeApi:
    """Enough of the sandboxes API for the relay: fork (with a host room and a
    quota), get, port token, delete, list and delete by label."""

    def __init__(self, room: int = 100, fork_delay: float = 0.05, fail_fork_after_create: int = 0,
                 answer_delay: float = 0.0, fork_status: int = 0) -> None:
        self.room = room
        self.fork_delay = fork_delay
        self.answer_delay = answer_delay  # copies exist, the answer is slow: a client gone meanwhile never sees them
        self.fork_status = fork_status  # answer every fork with this error status
        self.fail_fork_after_create = fail_fork_after_create  # the Nth fork makes copies, then drops the answer
        self.sandboxes: Dict[str, Dict[str, Any]] = {}
        self.forks: List[Dict[str, Any]] = []
        self.seq = 0
        self.probes = 0
        self.probed: Dict[str, int] = {}
        self.slow: Dict[str, int] = {}  # copy id -> probes before its URL answers
        self.holds: List[bool] = []

    def live(self) -> List[str]:
        return [sid for sid, s in self.sandboxes.items() if s["state"] not in ("deleting", "deleted")]

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        q = parse_qsl(req.url.query.decode())
        if req.url.host.endswith(".example") and req.url.host != "api.example":  # a copy's live URL
            sid = req.url.host.split("-", 1)[1].split(".")[0]
            self.probes += 1
            self.probed[sid] = self.probed.get(sid, 0) + 1
            need = self.slow.get(sid, 1)
            up = sid in self.live() and self.probes > 3 and self.probed[sid] >= need  # starting at first
            return httpx.Response(200 if up else 502)
        if req.method == "POST" and (m := re.fullmatch(r"/api/v1/sandboxes/([^/]+)/fork", path)):
            body = json.loads(req.content)
            assert dict(q) in ({"wait": "running", "waitTimeoutMs": "8000"}, {})
            self.holds.append(bool(q))
            assert set(body) == {"count", "labels", "maxRuntimeMin"}
            await asyncio.sleep(self.fork_delay)
            if self.fork_status:
                self.forks.append({"count": body["count"], "status": self.fork_status})
                return httpx.Response(self.fork_status, json={"error": "not_found" if self.fork_status == 404 else "internal"})
            n = body["count"]
            fits = self.room - len(self.live())
            if n > fits:
                self.forks.append({"count": n, "status": 409})
                return httpx.Response(409, json={"error": "fork_capacity", "fits": max(0, fits)})
            made = []
            for _ in range(n):
                self.seq += 1
                sid = f"sbx_copy{self.seq:012d}"
                self.sandboxes[sid] = {"id": sid, "state": "running", "labels": dict(body["labels"])}
                made.append(dict(self.sandboxes[sid]))
            self.forks.append({"count": n, "status": 202, "labels": body["labels"]})
            if self.fail_fork_after_create and len(self.forks) == self.fail_fork_after_create:
                raise httpx.ReadTimeout("answer lost", request=req)
            await asyncio.sleep(self.answer_delay)
            return httpx.Response(202, json={"fork": {"id": f"fork_{self.seq}"}, "sandboxes": made})
        if req.method == "POST" and (m := re.fullmatch(r"/api/v1/sandboxes/([^/]+)/ports/(\d+)/token", path)):
            sid = m.group(1)
            if sid not in self.live():
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json={"url": f"https://{m.group(2)}-{sid}.example/?prized_token=t", "token": "t"})
        if req.method == "GET" and (m := re.fullmatch(r"/api/v1/sandboxes/([^/]+)", path)):
            s = self.sandboxes.get(m.group(1))
            return httpx.Response(200, json={"sandbox": s}) if s else httpx.Response(404, json={"error": "not_found"})
        if req.method == "DELETE" and (m := re.fullmatch(r"/api/v1/sandboxes/([^/]+)", path)):
            s = self.sandboxes.get(m.group(1))
            if not s or s["state"] == "deleted":
                return httpx.Response(404, json={"error": "not_found"})
            s["state"] = "deleted"
            return httpx.Response(200, json={"sandbox": s})
        if path == "/api/v1/sandboxes":
            want = dict(v.split("=", 1) for k, v in q if k == "label")
            hits = [s for s in self.sandboxes.values()
                    if s["state"] != "deleted" and all(s["labels"].get(k) == v for k, v in want.items())]
            if req.method == "GET":
                return httpx.Response(200, json={"sandboxes": hits})
            if req.method == "DELETE":
                for s in hits:
                    s["state"] = "deleted"
                return httpx.Response(200, json={"count": len(hits), "ids": [s["id"] for s in hits], "more": False})
        return httpx.Response(404, json={"error": "not_found", "path": path})


def make(fake: FakeApi, **kw: Any) -> R.Relay:
    kw.setdefault("ready", "hold")
    cfg = R.Config(contestants=[CID], window=0.1, ttl=kw.pop("ttl", 0.6), queue_max=kw.pop("queue_max", 1.0),
                   reconcile_every=kw.pop("reconcile_every", 0.3), no_room_backoff=0.2, **kw)
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return R.Relay(cfg, R.Api("https://api.example", "dcp_000000000000", client))


async def viewers(relay: R.Relay, n: int, spread: float = 0.2) -> List[Any]:
    async def one(i: int) -> Any:
        await asyncio.sleep(spread * i / max(1, n))
        try:
            return await relay.batchers[CID].request(f"v{i}")
        except R.Full as exc:
            return exc
    return await asyncio.gather(*(one(i) for i in range(n)))


async def settle(relay: R.Relay, fake: FakeApi, wait: float) -> Dict[str, Any]:
    await asyncio.sleep(wait)
    out = await relay.close()
    return out


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_one_fork_for_a_burst_and_ttl_deletes_every_copy() -> None:
    async def go() -> None:
        fake = FakeApi()
        relay = make(fake)
        await relay.startup()
        got = await viewers(relay, 25, spread=0.05)
        assert all(isinstance(c, R.Copy) for c in got)
        assert len({c.id for c in got}) == 25
        assert [f["count"] for f in fake.forks] == [25]
        assert fake.forks[0]["labels"]["relay_batch"] == got[0].batch
        await asyncio.sleep(1.5)  # past the TTL
        assert fake.live() == []
        out = await relay.close()
        assert out["left_not_deleting"] == [] and out["copies_deleted"] == 25
    run(go())


def test_cap_answers_full_fast_and_leaks_nothing() -> None:
    async def go() -> None:
        fake = FakeApi()
        relay = make(fake, cap=36, ttl=5.0, queue_max=0.5)
        await relay.startup()
        got = await viewers(relay, 50, spread=0.3)
        served = [c for c in got if isinstance(c, R.Copy)]
        full = [c for c in got if isinstance(c, R.Full)]
        assert len(served) == 36 and len(full) == 14
        assert sum(f["count"] for f in fake.forks if f["status"] == 202) == 36
        out = await relay.close()
        assert fake.live() == [] and out["left_not_deleting"] == []
    run(go())


def test_api_room_is_taken_and_the_rest_queue_or_get_full() -> None:
    async def go() -> None:
        fake = FakeApi(room=10)
        relay = make(fake, ttl=0.3, queue_max=2.0)
        await relay.startup()
        got = await viewers(relay, 15, spread=0.05)
        # 409 fits=10 -> 10 now; the other 5 get copies once the first 10 expire
        assert [f["count"] for f in fake.forks][:2] == [15, 10]
        assert all(isinstance(c, R.Copy) for c in got)
        await asyncio.sleep(1.0)
        out = await relay.close()
        assert fake.live() == [] and out["left_not_deleting"] == []
    run(go())


def test_error_after_the_fork_deletes_the_batch_and_requeues() -> None:
    async def go() -> None:
        fake = FakeApi()
        relay = make(fake, fault_batch=1, queue_max=2.0)
        await relay.startup()
        got = await viewers(relay, 8, spread=0.02)
        assert all(isinstance(c, R.Copy) for c in got)
        first = [f for f in fake.forks if f["status"] == 202][0]["labels"]["relay_batch"]
        assert all(c.batch != first for c in got)
        assert not [s for s in fake.live() if fake.sandboxes[s]["labels"]["relay_batch"] == first]
        out = await relay.close()
        assert fake.live() == [] and out["left_not_deleting"] == []
    run(go())


def test_lost_fork_answer_is_swept_by_label() -> None:
    async def go() -> None:
        fake = FakeApi(fail_fork_after_create=1)
        relay = make(fake, queue_max=6.0)
        await relay.startup()
        got = await viewers(relay, 5, spread=0.02)
        assert all(isinstance(c, R.Copy) for c in got)  # served by the next window
        assert len(fake.sandboxes) == 10 and len(fake.live()) == 5
        out = await relay.close()
        assert fake.live() == [] and out["left_not_deleting"] == []
    run(go())


def test_token_failures_delete_that_copy_and_retry_the_viewer() -> None:
    async def go() -> None:
        import random
        random.seed(3)
        fake = FakeApi()
        relay = make(fake, fault_token=0.3, queue_max=3.0)
        await relay.startup()
        got = await viewers(relay, 20, spread=0.05)
        assert sum(isinstance(c, R.Copy) for c in got) == 20
        assert len(fake.sandboxes) > 20  # some copies were made again
        out = await relay.close()
        assert fake.live() == [] and out["left_not_deleting"] == []
    run(go())


def test_viewer_that_leaves_gets_its_copy_deleted() -> None:
    async def go() -> None:
        fake = FakeApi(fork_delay=0.3)
        relay = make(fake, ttl=30.0)
        await relay.startup()
        task = asyncio.ensure_future(relay.batchers[CID].request("v0"))
        await asyncio.sleep(0.2)  # inside the fork
        task.cancel()
        await asyncio.sleep(0.5)
        assert len(fake.sandboxes) == 1 and fake.live() == []
        await relay.close()
    run(go())


def test_reconcile_deletes_copies_the_relay_does_not_hold() -> None:
    async def go() -> None:
        fake = FakeApi()
        relay = make(fake, ttl=30.0)
        await relay.startup()
        fake.sandboxes["sbx_stray"] = {"id": "sbx_stray", "state": "running",
                                       "labels": {"relay": relay.cfg.name, "relay_run": relay.run, "relay_batch": "bx"}}
        await asyncio.sleep(0.5)
        assert "sbx_stray" not in fake.live() and relay.stats["orphans_deleted"] == 1
        await relay.close()
    run(go())


def test_startup_sweeps_what_a_previous_run_left() -> None:
    async def go() -> None:
        fake = FakeApi()
        fake.sandboxes["sbx_old"] = {"id": "sbx_old", "state": "running", "labels": {"relay": "arena-relay", "relay_run": "dead"}}
        relay = make(fake)
        await relay.startup()
        assert fake.live() == []
        await relay.close()
    run(go())


def test_http_route_and_hangup() -> None:
    async def go() -> None:
        fake = FakeApi(fork_delay=0.3)
        relay = make(fake, ttl=30.0)
        await relay.startup()
        server = await asyncio.start_server(R.Http(relay).handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as c:
            r = await c.post(f"http://127.0.0.1:{port}/v1/contestants/{CID}/copies", json={"viewer": "a"})
            assert r.status_code == 200 and r.json()["copyId"].startswith("sbx_copy")
            assert (await c.post(f"http://127.0.0.1:{port}/v1/contestants/nope/copies")).status_code == 404
            d = await c.delete(f"http://127.0.0.1:{port}/v1/copies/{r.json()['copyId']}")
            assert d.status_code == 204 and fake.live() == []
            try:  # hang up inside the window
                await c.post(f"http://127.0.0.1:{port}/v1/contestants/{CID}/copies", json={"viewer": "b"}, timeout=0.2)
                raise AssertionError("the relay answered inside its window")
            except httpx.ReadTimeout:
                pass
        await asyncio.sleep(0.6)
        assert fake.live() == []
        server.close()
        out = await relay.close()
        assert out["left_not_deleting"] == []
    run(go())


def test_probe_mode_forks_without_the_hold_and_waits_for_the_url() -> None:
    async def go() -> None:
        fake = FakeApi()
        relay = make(fake, ready="probe", probe_path="/api/state")
        await relay.startup()
        got = await viewers(relay, 6, spread=0.02)
        assert all(isinstance(c, R.Copy) for c in got) and fake.holds == [False]
        assert fake.probes > 6  # it kept polling until each copy answered
        out = await relay.close()
        assert fake.live() == [] and out["left_not_deleting"] == []
    run(go())


def test_probe_mode_hands_out_each_copy_when_it_answers() -> None:
    async def go() -> None:
        fake = FakeApi()
        fake.slow = {"sbx_copy000000000001": 12}  # the first copy's URL answers about 1.8 s late
        relay = make(fake, ready="probe", ttl=30.0, queue_max=5.0)
        await relay.startup()
        got = await viewers(relay, 5, spread=0.02)
        assert all(isinstance(c, R.Copy) for c in got)
        slow = [c for c in got if c.id == "sbx_copy000000000001"][0]
        fast = [c for c in got if c is not slow]
        assert max(c.ready for c in fast) + 1.0 < slow.ready
        assert got[0] is not slow  # the first viewer got the first copy that answered
        out = await relay.close()
        assert fake.live() == [] and out["left_not_deleting"] == []
    run(go())


# --- from the review (Nov 5): each of these once left a viewer unanswered or a copy behind ---


def test_a_bug_in_a_flush_still_answers_the_viewer() -> None:
    async def go() -> None:
        fake = FakeApi()
        relay = make(fake, queue_max=1.0)
        await relay.startup()

        async def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("unexpected")

        relay.fork_batch = boom  # type: ignore[method-assign]
        task = asyncio.ensure_future(relay.batchers[CID].request("v0"))
        await asyncio.sleep(2.0)
        assert task.done() and isinstance(task.exception(), R.Full)
        await relay.close()
    run(go())


def test_a_closed_stderr_does_not_stop_the_relay() -> None:
    class Broken:
        def write(self, s: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            raise BrokenPipeError(32, "Broken pipe")

    async def go() -> None:
        fake = FakeApi(room=0)  # every fork: 409 fits 0, logged to stderr
        relay = make(fake, queue_max=1.0)
        await relay.startup()
        old, sys.stderr = sys.stderr, Broken()  # type: ignore[assignment]
        try:
            task = asyncio.ensure_future(relay.batchers[CID].request("v0"))
            await asyncio.sleep(2.0)
        finally:
            sys.stderr = old
        assert task.done() and isinstance(task.exception(), R.Full)
        await relay.close()
    run(go())


def test_a_permanent_refusal_answers_502_at_once() -> None:
    async def go() -> None:
        fake = FakeApi(fork_status=404)  # the contestant is gone
        relay = make(fake, queue_max=3.0)
        await relay.startup()
        try:
            await relay.batchers[CID].request("v0")
            raise AssertionError("served")
        except R.ForkFailed as exc:
            assert exc.final and len(fake.forks) == 1
        await relay.close()
    run(go())


def test_viewers_requeued_during_shutdown_get_an_answer() -> None:
    async def go() -> None:
        fake = FakeApi(fork_delay=0.4, fork_status=500)
        relay = make(fake, queue_max=5.0)
        await relay.startup()
        task = asyncio.ensure_future(relay.batchers[CID].request("v0"))
        await asyncio.sleep(0.25)  # inside the fork
        await relay.close()
        await asyncio.sleep(0.1)
        assert task.done() and isinstance(task.exception(), R.Full) and task.exception().reason == "shutting_down"
    run(go())


def test_shutdown_cancels_a_slow_fork_and_still_leaves_nothing() -> None:
    async def go() -> None:
        fake = FakeApi(answer_delay=5.0)  # the copies exist; the answer would come in 5 s
        relay = make(fake, ttl=30.0, queue_max=20.0)
        await relay.startup()
        task = asyncio.ensure_future(relay.batchers[CID].request("v0"))
        await asyncio.sleep(0.4)
        assert len(fake.live()) == 1
        out = await relay.close(grace=0.2)
        assert fake.live() == [] and out["left_not_deleting"] == []
        assert task.done() and isinstance(task.exception(), R.Full)
    run(go())


def test_http_ignores_stray_bytes_and_refuses_chunked_bodies() -> None:
    async def raw(port: int, data: bytes) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(data)
        await w.drain()
        try:
            return await asyncio.wait_for(r.read(), 4)
        finally:
            w.close()

    async def go() -> None:
        fake = FakeApi()
        relay = make(fake, ttl=30.0)
        await relay.startup()
        server = await asyncio.start_server(R.Http(relay).handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        body = b'{"viewer":"x"}'
        head = f"POST /v1/contestants/{CID}/copies HTTP/1.1\r\nHost: x\r\n"
        chunked = (head + f"Transfer-Encoding: chunked\r\n\r\n{len(body):x}\r\n").encode() + body + b"\r\n0\r\n\r\n"
        assert (await raw(port, chunked)).startswith(b"HTTP/1.1 411")
        stray = (head + f"Content-Length: {len(body)}\r\n\r\n").encode() + body + b"\r\n"
        assert (await raw(port, stray)).startswith(b"HTTP/1.1 200")
        server.close()
        await relay.close()
    run(go())


def test_a_hangup_as_the_copy_comes_gives_the_copy_back() -> None:
    async def go() -> None:
        import socket
        fake = FakeApi()
        relay = make(fake, ttl=30.0, reconcile_every=0.3)
        await relay.startup()
        server = await asyncio.start_server(R.Http(relay).handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sock = socket.create_connection(("127.0.0.1", port))
        sock.setblocking(False)
        assign = relay.assign

        def assign_then_hangup(v: Any, c: Any) -> None:
            assign(v, c)
            sock.close()  # the viewer goes in the same instant

        relay.assign = assign_then_hangup  # type: ignore[method-assign]
        sock.send(f"POST /v1/contestants/{CID}/copies HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{{}}".encode())
        await asyncio.sleep(1.0)
        assert fake.live() == [] and relay.live == {}
        server.close()
        await relay.close()
    run(go())


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"{len(tests)} passed")
