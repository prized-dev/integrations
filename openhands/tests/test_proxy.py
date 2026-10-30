"""The proxy's two transports against a fake Agent Server, offline."""

import base64
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from conftest import PRIZED_KEY, RELAY, free_port
from fakes import FakeAsyncSandbox
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

from openhands_prized._proxy import (
    INLINE_BODY,
    ExecUpstream,
    IngressUpstream,
    ProxyServer,
    _ws_connect,
    ingress_base,
    parse_curl_headers,
)


@pytest.fixture
def exec_proxy(tmp_path, agent_server):
    port, key = agent_server
    fake = FakeAsyncSandbox(home=str(tmp_path))

    async def connect():
        return fake

    upstream = ExecUpstream(connect, port, sys.executable, str(RELAY), tmp=str(tmp_path))
    proxy = ProxyServer(upstream)
    proxy.start()
    try:
        yield proxy.url, key, fake, tmp_path
    finally:
        proxy.stop()


def _auth(key):
    return json.dumps({"type": "auth", "session_api_key": key})


def test_http_roundtrip_keeps_method_query_headers_and_body(exec_proxy):
    url, key, fake, _ = exec_proxy
    assert httpx.get(url + "/health").json() == "OK"
    body = json.dumps({"a": 'it\'s "quoted" $(not run)'}).encode()
    r = httpx.post(
        url + "/api/echo?x=1&y=two%20words",
        content=body,
        headers={"X-Session-API-Key": key, "X-Quote": 'it\'s "q" `x`', "Content-Type": "application/json"},
    )
    assert r.status_code == 201
    seen = r.json()
    assert seen["method"] == "POST"
    assert seen["query"] == "x=1&y=two%20words"
    assert seen["headers"]["x-quote"] == 'it\'s "q" `x`'
    assert seen["headers"]["content-type"] == "application/json"
    assert base64.b64decode(seen["body_b64"]) == body
    assert r.headers["x-fake"] == "yes"
    assert r.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert not list(Path(fake.home).glob("ohp-*")), "temp dirs are removed"


def test_session_key_is_not_added_by_the_proxy(exec_proxy):
    url, _, _, _ = exec_proxy
    assert httpx.get(url + "/api/echo").status_code == 401


def test_large_request_body_goes_up_as_a_file(exec_proxy):
    url, key, fake, _ = exec_proxy
    body = bytes(range(256)) * (INLINE_BODY // 256 + 10)
    r = httpx.put(url + "/api/echo", content=body, headers={"X-Session-API-Key": key})
    assert base64.b64decode(r.json()["body_b64"]) == body
    assert r.json()["method"] == "PUT"
    assert len(fake.files.writes) == 1


def test_large_response_comes_back_as_a_file(exec_proxy):
    url, key, fake, _ = exec_proxy
    r = httpx.get(url + "/api/big", headers={"X-Session-API-Key": key}, timeout=60)
    assert r.status_code == 200
    assert r.content == bytes(range(256)) * (12 * 1024)
    assert len(fake.files.reads) == 1
    assert not list(Path(fake.home).glob("ohp-*"))


def test_head_and_options(exec_proxy):
    url, _, _, _ = exec_proxy
    r = httpx.head(url + "/health")
    assert r.status_code == 200 and r.content == b""
    assert httpx.request("OPTIONS", url + "/health").status_code in (200, 405)


def test_server_down_is_a_502_with_a_reason(tmp_path):
    fake = FakeAsyncSandbox(home=str(tmp_path))

    async def connect():
        return fake

    proxy = ProxyServer(ExecUpstream(connect, free_port(), sys.executable, str(RELAY), tmp=str(tmp_path)))
    proxy.start()
    try:
        r = httpx.get(proxy.url + "/health")
        assert r.status_code == 502
        assert "not answering" in r.json()["detail"]
    finally:
        proxy.stop()


def test_websocket_relay_both_ways_and_close_code(exec_proxy):
    url, key, fake, tmp = exec_proxy
    with ws_connect(url.replace("http", "ws") + "/sockets/events/abc?resend=all") as ws:
        ws.send(_auth(key))
        assert json.loads(ws.recv(timeout=20)) == {"hello": "/sockets/events/abc"}
        assert ws.recv(timeout=20) == b"\x00\x01binary"
        ws.send("hi")
        assert ws.recv(timeout=20) == "echo:hi"
        ws.send(b"\xffraw")
        assert ws.recv(timeout=20) == b"echo:\xffraw"
        ws.send("close-me")
        with pytest.raises(ConnectionClosed) as info:
            ws.recv(timeout=20)
    assert info.value.rcvd.code == 4321
    assert info.value.rcvd.reason == "asked to"
    _wait_relays_gone(fake, tmp)


def test_websocket_bad_key_closes_4001(exec_proxy):
    url, _, _, _ = exec_proxy
    with ws_connect(url.replace("http", "ws") + "/sockets/events/abc") as ws:
        ws.send(_auth("wrong"))
        with pytest.raises(ConnectionClosed) as info:
            ws.recv(timeout=20)
    assert info.value.rcvd.code == 4001


def test_websocket_refused_handshake_maps_to_a_close_code(exec_proxy):
    url, key, _, _ = exec_proxy
    with ws_connect(url.replace("http", "ws") + "/nope") as ws:
        ws.send(_auth(key))
        with pytest.raises(ConnectionClosed) as info:
            ws.recv(timeout=20)
    assert info.value.rcvd.code == 4001  # the server's 403


def test_client_leaving_stops_the_relay(exec_proxy):
    url, key, fake, tmp = exec_proxy
    with ws_connect(url.replace("http", "ws") + "/sockets/events/x") as ws:
        ws.send(_auth(key))
        ws.recv(timeout=20)
    _wait_relays_gone(fake, tmp)


def _wait_relays_gone(fake, tmp, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done = all(h.proc.returncode is not None for h in fake.handles)
        if done and not list(Path(tmp).glob("ohp-ws-*")):
            assert all(h.cleaned for h in fake.handles)
            return
        time.sleep(0.1)
    raise AssertionError("relay still running or inbox left behind")


def test_parse_curl_headers_takes_the_last_response():
    raw = b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 207 Multi\r\nA: 1\r\nB: two: parts\r\nbad line\r\n\r\n"
    status, headers = parse_curl_headers(raw)
    assert status == 207
    assert headers == [("A", "1"), ("B", "two: parts")]


def test_ingress_base_only_trusts_prized_domains():
    t = "https://{port}-sbx-00000000000000aa.p.prized.dev/"
    assert ingress_base(t, 8000, "https://api.prized.dev") == "https://8000-sbx-00000000000000aa.p.prized.dev"
    for bad in ("http://{port}-x.p.prized.dev/", "https://{port}-x.evil.com/", "https://{port}.prized.dev.evil.com/"):
        with pytest.raises(ValueError):
            ingress_base(bad, 8000, "https://api.prized.dev")
    assert ingress_base("https://{port}-x.sbx.example.org/", 1, "https://api.example.org").startswith("https://1-x")


# ---- ingress transport ------------------------------------------------------


@pytest.fixture
def fake_ingress(agent_server):
    """A local stand-in for the sandbox port edge: wants Prized-Key, forwards to the server."""
    import threading

    import uvicorn

    port, key = agent_server
    seen = []

    async def edge(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                await send({"type": msg["type"] + ".complete"})
                if msg["type"] == "lifespan.shutdown":
                    return
        keys = [v.decode() for k, v in scope["headers"] if k.lower() == b"prized-key"]
        seen.append(keys)
        if keys != [PRIZED_KEY]:
            if scope["type"] == "websocket":
                await receive()
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b'{"error":"sign_in_required"}'})
            return
        upstream = ExecLikeForward(port)
        await upstream(scope, receive, send)

    edge_port = free_port()
    server = uvicorn.Server(uvicorn.Config(edge, host="127.0.0.1", port=edge_port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{edge_port}", key, seen
    finally:
        server.should_exit = True
        t.join(10)


class ExecLikeForward:
    """Forwards an ASGI request to the fake server over plain HTTP / WS."""

    def __init__(self, port):
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            headers = [(k.decode(), v.decode()) for k, v in scope["headers"] if k.lower() not in (b"host",)]
            target = scope["path"] + ("?" + scope["query_string"].decode() if scope["query_string"] else "")
            async with httpx.AsyncClient() as c:
                r = await c.request(
                    scope["method"], f"http://127.0.0.1:{self.port}{target}", headers=headers, content=body
                )
            await send(
                {
                    "type": "http.response.start",
                    "status": r.status_code,
                    "headers": [(k.encode(), v.encode()) for k, v in r.headers.multi_items() if k != "content-length"],
                }
            )
            await send({"type": "http.response.body", "body": r.content})
        else:
            await receive()
            await send({"type": "websocket.accept"})
            up = await _ws_connect(f"ws://127.0.0.1:{self.port}{scope['path']}", [])
            if True:
                import asyncio

                async def a():
                    async for m in up:
                        key = "text" if isinstance(m, str) else "bytes"
                        await send({"type": "websocket.send", key: m})
                    await send({"type": "websocket.close", "code": up.close_code or 1000})

                async def b():
                    while True:
                        m = await receive()
                        if m["type"] == "websocket.disconnect":
                            return
                        await up.send(m.get("text") if m.get("text") is not None else m["bytes"])

                tasks = {asyncio.ensure_future(a()), asyncio.ensure_future(b())}
                await asyncio.wait(tasks, return_when="FIRST_COMPLETED")
                for t in tasks:
                    t.cancel()
                await up.close()


def test_ingress_adds_the_prized_key_and_replaces_any_sent(fake_ingress):
    base, key, seen = fake_ingress
    proxy = ProxyServer(IngressUpstream(base, PRIZED_KEY))
    proxy.start()
    try:
        r = httpx.get(proxy.url + "/api/echo?q=1", headers={"X-Session-API-Key": key, "Prized-Key": "evil"})
        assert r.status_code == 201
        assert r.json()["query"] == "q=1"
        assert seen[-1] == [PRIZED_KEY], "exactly one Prized-Key, the proxy's"
        with ws_connect(proxy.url.replace("http", "ws") + "/sockets/events/z") as ws:
            ws.send(_auth(key))
            assert json.loads(ws.recv(timeout=20)) == {"hello": "/sockets/events/z"}
            ws.send("hi")
            ws.recv(timeout=20)
            assert ws.recv(timeout=20) == "echo:hi"
        assert seen[-1] == [PRIZED_KEY]
    finally:
        proxy.stop()


def test_ingress_wrong_key_is_passed_back(fake_ingress):
    base, _, _ = fake_ingress
    proxy = ProxyServer(IngressUpstream(base, "dcp_wrong"))
    proxy.start()
    try:
        assert httpx.get(proxy.url + "/health").status_code == 401
    finally:
        proxy.stop()


def test_quiesce_closes_sockets_refuses_requests_and_reopens(tmp_path, agent_server):
    port, key = agent_server
    fake = FakeAsyncSandbox(home=str(tmp_path))

    async def connect():
        return fake

    proxy = ProxyServer(ExecUpstream(connect, port, sys.executable, str(RELAY), tmp=str(tmp_path)))
    proxy.start()
    try:
        with ws_connect(proxy.url.replace("http", "ws") + "/sockets/events/q") as ws:
            ws.send(_auth(key))
            ws.recv(timeout=20)
            assert proxy.quiesce(20) is True
            with pytest.raises(ConnectionClosed) as info:
                while True:
                    ws.recv(timeout=20)
        assert info.value.rcvd.code == 1012
        _wait_relays_gone(fake, tmp_path)
        execs = len(fake.execs)
        r = httpx.get(proxy.url + "/health")
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        with (
            ws_connect(proxy.url.replace("http", "ws") + "/sockets/events/q") as ws,
            pytest.raises(ConnectionClosed) as info,
        ):
            ws.recv(timeout=20)
        assert info.value.rcvd.code == 1013
        assert len(fake.execs) == execs, "nothing reached the sandbox while paused"
        proxy.unquiesce()
        assert httpx.get(proxy.url + "/health").status_code == 200
    finally:
        proxy.stop()
