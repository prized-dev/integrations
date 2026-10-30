"""A loopback proxy in front of the Agent Server in a Prized sandbox.

The OpenHands SDK (and Agent Canvas in a browser) talks to
``http://127.0.0.1:<port>`` as if it were the Agent Server. Each request and
WebSocket is carried to the sandbox by one of two transports:

- ``exec``: through Prized's command API. An HTTP request runs ``curl`` against the
  server on the sandbox's loopback; a WebSocket is relayed by a background command
  (``ws_relay.py``) whose output is streamed back. It needs nothing but an API key
  with Sandboxes: Write, so it works on every plan.
- ``ingress``: straight to the sandbox's port URL (``https://<port>-<id>.<domain>``),
  adding the ``Prized-Key`` header that URL needs. Neither the SDK's WebSocket
  client nor a browser can add that header itself; the proxy does, and the key
  never leaves this process.

Authentication with the Agent Server itself (its session key) passes through
untouched in both: the proxy is a pipe, not a gate.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import shlex
import socket
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("openhands_prized")

Header = tuple[str, str]

# Never forwarded either way: hop-by-hop, framing the proxy redoes, and the Prized key.
_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "expect",
        "prized-key",
    }
)
# Also dropped on requests: the proxy asks for plain bodies (exec has no gzip step).
_REQ_DROP = _HOP | {"accept-encoding"}
# Also dropped on a WebSocket's handshake: the client library writes its own.
_WS_DROP = _REQ_DROP | {
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-accept",
    "sec-websocket-protocol",
}
_METHOD = re.compile(r"[A-Z]{1,16}")
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

INLINE_BODY = 48 * 1024  # request bodies up to this go inside the command; larger ones as a file
INLINE_RESPONSE = 2 * 1024 * 1024  # response bodies up to this come back in the command's output
EXEC_TIMEOUT = 230  # under the SDK's 240 s plain-exec limit, so a request is one call
CURL_TIMEOUT = 220
MAX_CONCURRENT = 6  # Prized runs up to 8 commands at once per sandbox; leave room


def _filter(headers: list[Header], drop: frozenset[str]) -> list[Header]:
    return [(k, v) for k, v in headers if k.lower() not in drop and "\n" not in v and "\r" not in v]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _json_error(status: int, message: str) -> tuple[int, list[Header], bytes]:
    body = json.dumps({"detail": message, "source": "openhands-prized proxy"}).encode()
    return status, [("content-type", "application/json")], body


def parse_curl_headers(raw: bytes) -> tuple[int, list[Header]]:
    """The status and headers of the last response in curl's ``-D`` dump (a 100
    Continue or a redirect would come first)."""
    text = raw.decode("latin-1")
    blocks = [b for b in re.split(r"\r?\n\r?\n", text) if b.strip()]
    block = next((b for b in reversed(blocks) if b.startswith("HTTP/")), "")
    lines = block.splitlines()
    if not lines:
        raise ValueError("no HTTP status line in the response")
    status = int(lines[0].split()[1])
    headers = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep and _TOKEN.fullmatch(name.strip()):
            headers.append((name.strip(), value.strip()))
    return status, headers


class ClientSocket:
    """The client's side of a proxied WebSocket, over ASGI."""

    def __init__(self, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]):
        self._receive, self._send = receive, send
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        if not self.accepted:
            self.accepted = True
            await self._send({"type": "websocket.accept"})

    async def receive(self) -> str | bytes | None:
        """The client's next message, or None once it has gone."""
        while not self.closed:
            msg = await self._receive()
            if msg["type"] == "websocket.receive":
                if msg.get("text") is not None:
                    return msg["text"]
                return msg.get("bytes") or b""
            if msg["type"] == "websocket.disconnect":
                self.closed = True
        return None

    async def send(self, data: str | bytes) -> None:
        if self.closed:
            return
        key = "text" if isinstance(data, str) else "bytes"
        await self._send({"type": "websocket.send", key: data})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        # Before accept, ASGI turns a close into a refused handshake (403).
        await self._send({"type": "websocket.close", "code": code, "reason": reason[:120]})


class Upstream(Protocol):
    name: str

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def http(
        self, method: str, target: str, headers: list[Header], body: bytes
    ) -> tuple[int, list[Header], bytes]: ...

    async def websocket(self, target: str, headers: list[Header], client: ClientSocket) -> None: ...


class ExecUpstream:
    """HTTP and WebSockets carried over Prized's command API."""

    name = "exec"

    def __init__(
        self,
        connect: Callable[[], Awaitable[Any]],
        port: int,
        python: str,
        relay: str,
        tmp: str = "/tmp",
    ):
        """``connect`` returns an AsyncSandbox (or a stand-in) for this proxy's loop;
        ``python`` and ``relay`` are absolute paths in the sandbox."""
        self._connect = connect
        self.port, self.python, self.relay, self.tmp = port, python, relay, tmp
        self.sb: Any = None
        self._slots = asyncio.Semaphore(MAX_CONCURRENT)

    async def start(self) -> None:
        self.sb = await self._connect()

    async def close(self) -> None:
        close = getattr(self.sb, "close", None)
        if close is not None:
            await close()

    async def _exec(self, cmd: str, timeout: float) -> Any:
        async with self._slots:
            return await self.sb.exec(cmd, timeout=timeout)

    async def http(self, method: str, target: str, headers: list[Header], body: bytes):
        if not _METHOD.fullmatch(method):
            return _json_error(400, "unsupported method")
        if not target.startswith("/") or any(c in target for c in " \r\n\t\x00"):
            return _json_error(400, "bad request target")
        d = f"{self.tmp}/ohp-{secrets.token_hex(8)}"
        steps = [f"d={shlex.quote(d)}", 'mkdir -m 700 -p "$d" || exit 90']
        if body and len(body) > INLINE_BODY:
            await self.sb.files.write(f"{d}.body", body)
            steps.append(f'mv {shlex.quote(d + ".body")} "$d/b"')
        elif body:
            steps.append(f'printf %s {shlex.quote(_b64(body))} | base64 -d > "$d/b"')
        curl = ["curl", "-sS", "--path-as-is", "-H", "Expect:"]
        curl += ["--head"] if method == "HEAD" else ["-X", method]
        sent = _filter(headers, _REQ_DROP)
        for k, v in sent:
            curl += ["-H", f"{k}: {v}"]
        names = {k.lower() for k, _ in sent}
        for default in ("content-type", "accept", "user-agent"):
            if default not in names:
                curl += ["-H", f"{default}:"]  # no curl defaults the client did not send
        line = " ".join(shlex.quote(a) for a in curl)
        if body:
            line += ' --data-binary @"$d/b"'
        line += f' -o "$d/o" -D "$d/h" --max-time {CURL_TIMEOUT} '
        line += shlex.quote(f"http://127.0.0.1:{self.port}{target}")
        steps += [
            line,
            'echo "RC $?"',
            "printf 'H '; base64 -w0 < \"$d/h\" 2>/dev/null; echo",
            's=$(stat -c %s "$d/o" 2>/dev/null || echo 0)',
            (
                f'if [ "$s" -le {INLINE_RESPONSE} ]; then printf \'B \'; base64 -w0 < "$d/o" 2>/dev/null; echo; '
                'rm -rf "$d"; else echo "F $d/o"; fi'
            ),
        ]
        try:
            result = await self._exec("\n".join(steps), EXEC_TIMEOUT)
        except Exception as exc:  # the command API itself failed
            return _json_error(502, f"Prized exec failed: {exc}")
        fields = {}
        for out in result.stdout.splitlines():
            kind, _, rest = out.partition(" ")
            fields.setdefault(kind, rest)
        rc = fields.get("RC")
        if rc is None:
            return _json_error(502, f"proxy command failed (exit {result.exit_code}): {result.stderr[-300:]}")
        if rc != "0":
            if rc == "7":
                return _json_error(502, "the Agent Server in the sandbox is not answering")
            if rc == "28":
                return _json_error(504, "the Agent Server took too long to answer")
            return _json_error(502, f"curl exit {rc}: {result.stderr[-300:]}")
        status, resp_headers = parse_curl_headers(base64.b64decode(fields.get("H", "")))
        if "F" in fields:
            path = fields["F"]
            try:
                data = await self.sb.files.read_bytes(path)
            finally:
                await self._exec(f"rm -rf {shlex.quote(path.rsplit('/', 1)[0])}", 30)
        else:
            data = base64.b64decode(fields.get("B", ""))
        return status, _filter(resp_headers, _HOP | {"content-encoding"}), data

    async def websocket(self, target: str, headers: list[Header], client: ClientSocket) -> None:
        await client.accept()
        # The SDK sends its session key as the first message; take it now so the
        # relay hands it over the moment it connects.
        try:
            first = await asyncio.wait_for(client.receive(), 1.0)
        except TimeoutError:
            first = None
        if client.closed:
            return
        inbox = f"{self.tmp}/ohp-ws-{secrets.token_hex(8)}.in"
        seed = f"printf '%s\\n' {shlex.quote(_frame(first))}" if first is not None else ":"
        argv = [
            self.python,
            self.relay,
            str(self.port),
            _b64(target.encode()),
            _b64(json.dumps(_filter(headers, _WS_DROP)).encode()),
            inbox,
        ]
        cmd = f"umask 077; {seed} > {shlex.quote(inbox)}; exec " + " ".join(shlex.quote(a) for a in argv)
        async with self._slots:
            handle = await self.sb.exec(cmd, background=True)
        lines: asyncio.Queue[str | None] = asyncio.Queue()
        pending = [""]

        def on_stdout(chunk: str) -> None:
            pending[0] += chunk
            *done, pending[0] = pending[0].split("\n")
            for line in done:
                if line:
                    lines.put_nowait(line)

        async def follow() -> None:
            try:
                await handle.wait(on_stdout=on_stdout)
            finally:
                lines.put_nowait(None)

        async def downstream() -> None:
            while True:
                line = await lines.get()
                if line is None:
                    await client.close(1011, "relay ended")
                    return
                kind, _, rest = line.partition(" ")
                if kind == "T":
                    await client.send(base64.b64decode(rest).decode("utf-8"))
                elif kind == "B":
                    await client.send(base64.b64decode(rest))
                elif kind == "C":
                    code, _, reason = rest.partition(" ")
                    await client.close(int(code), base64.b64decode(reason).decode("utf-8", "replace"))
                    return

        async def upstream() -> None:
            while True:
                msg = await client.receive()
                if msg is None:
                    return
                await self._exec(f"printf '%s\\n' {shlex.quote(_frame(msg))} >> {shlex.quote(inbox)}", 30)

        follower = asyncio.ensure_future(follow())
        tasks = {asyncio.ensure_future(downstream()), asyncio.ensure_future(upstream())}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            try:
                await handle.kill()
            except Exception:
                pass
            follower.cancel()
            try:
                await handle.cleanup()
            except Exception:
                pass
            try:
                await self._exec(f"rm -f {shlex.quote(inbox)}", 30)
            except Exception:
                pass
            await client.close(1000)


def _frame(msg: str | bytes) -> str:
    if isinstance(msg, str):
        return "T " + _b64(msg.encode("utf-8"))
    return "B " + _b64(bytes(msg))


class IngressUpstream:
    """Straight to the sandbox's port URL, with the Prized key added."""

    name = "ingress"

    def __init__(self, base_url: str, prized_key: str):
        self.base = base_url.rstrip("/")
        self._key = prized_key
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0), follow_redirects=False)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def http(self, method: str, target: str, headers: list[Header], body: bytes):
        assert self._client is not None
        sent = _filter(headers, _REQ_DROP) + [("Prized-Key", self._key)]
        try:
            r = await self._client.request(method, self.base + target, headers=sent, content=body or None)
        except httpx.HTTPError as exc:
            return _json_error(502, f"sandbox port URL failed: {type(exc).__name__}")
        # httpx has already undone any content-encoding.
        out = [(k, v) for k, v in r.headers.multi_items() if k.lower() not in _HOP | {"content-encoding"}]
        return r.status_code, out, r.content

    async def websocket(self, target: str, headers: list[Header], client: ClientSocket) -> None:
        from websockets.exceptions import ConnectionClosed

        scheme = "wss://" if self.base.startswith("https://") else "ws://"
        url = scheme + self.base.split("://", 1)[1] + target
        sent = _filter(headers, _WS_DROP) + [("Prized-Key", self._key)]
        try:
            server = await _ws_connect(url, sent)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            from .ws_relay import close_code_for_status

            await client.accept()
            await client.close(close_code_for_status(status), str(exc)[:120])
            return
        await client.accept()

        async def downstream() -> None:
            try:
                async for message in server:
                    await client.send(message)
            except ConnectionClosed:
                pass
            await client.close(getattr(server, "close_code", None) or 1000, getattr(server, "close_reason", "") or "")

        async def upstream() -> None:
            while True:
                msg = await client.receive()
                if msg is None:
                    return
                await server.send(msg)

        tasks = {asyncio.ensure_future(downstream()), asyncio.ensure_future(upstream())}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await server.close()
            await client.close(1000)


async def _ws_connect(url: str, headers: list[Header]) -> Any:
    try:  # websockets >= 13
        from websockets.asyncio.client import connect

        return await connect(url, additional_headers=headers, max_size=None, open_timeout=30)
    except ImportError:  # websockets 12
        import websockets

        return await websockets.connect(url, extra_headers=headers, max_size=None, open_timeout=30)


class ProxyApp:
    """The ASGI app: every HTTP request and WebSocket goes to the upstream."""

    def __init__(self, upstream: Upstream):
        self.upstream = upstream
        self.loop: asyncio.AbstractEventLoop | None = None
        # While the sandbox is paused nothing may reach it: any command would resume it.
        self.paused = False
        self._inflight = 0
        self._idle = asyncio.Event()
        self._sockets: set[ClientSocket] = set()

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self._lifespan(receive, send)
            return
        self._inflight += 1
        self._idle.clear()
        try:
            if kind == "http":
                await self._http(scope, receive, send)
            elif kind == "websocket":
                await self._websocket(scope, receive, send)
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    async def quiesce(self, timeout: float) -> bool:
        """Refuse new requests, close open WebSockets (1012: clients reconnect later) and
        wait for what is in flight to finish. True when nothing is left running."""
        self.paused = True
        for client in list(self._sockets):
            await client.close(1012, "sandbox pausing")
        if self._inflight == 0:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def unquiesce(self) -> None:
        self.paused = False

    async def _lifespan(self, receive: Callable, send: Callable) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                self.loop = asyncio.get_running_loop()
                try:
                    await self.upstream.start()
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                try:
                    await self.upstream.close()
                finally:
                    await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    def _target(scope: dict) -> str:
        path = scope.get("raw_path") or scope["path"].encode()
        target = path.decode("latin-1")
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode("latin-1")
        return target

    @staticmethod
    def _headers(scope: dict) -> list[Header]:
        return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]

    async def _http(self, scope: dict, receive: Callable, send: Callable) -> None:
        chunks = []
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        if self.paused:
            status, headers, body = _json_error(503, "the sandbox is paused; resume the workspace first")
            headers.append(("retry-after", "5"))
        else:
            status, headers, body = await self._forward(scope, b"".join(chunks))
        raw = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]
        raw.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": raw})
        await send({"type": "http.response.body", "body": body})

    async def _forward(self, scope: dict, body: bytes) -> tuple[int, list[Header], bytes]:
        try:
            return await self.upstream.http(scope["method"], self._target(scope), self._headers(scope), body)
        except Exception as exc:
            logger.exception("proxy request failed")
            return _json_error(502, f"{type(exc).__name__}: {exc}")

    async def _websocket(self, scope: dict, receive: Callable, send: Callable) -> None:
        msg = await receive()
        if msg["type"] != "websocket.connect":
            return
        client = ClientSocket(receive, send)
        if self.paused:
            await client.accept()
            await client.close(1013, "sandbox paused")  # try again later
            return
        self._sockets.add(client)
        try:
            await self.upstream.websocket(self._target(scope), self._headers(scope), client)
        except Exception:
            logger.exception("proxied websocket failed")
            await client.close(1011, "proxy error")
        finally:
            self._sockets.discard(client)


class ProxyServer:
    """Runs a ProxyApp with uvicorn on 127.0.0.1, in a daemon thread."""

    def __init__(self, upstream: Upstream, port: int | None = None, host: str = "127.0.0.1"):
        self.upstream = upstream
        self.host = host
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port or 0))
        self.port: int = self._sock.getsockname()[1]
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 30.0) -> None:
        import uvicorn

        self.app = ProxyApp(self.upstream)
        config = uvicorn.Config(
            self.app,
            log_level="warning",
            lifespan="on",
            ws="auto",
            timeout_graceful_shutdown=3,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._sock]}, name="openhands-prized-proxy", daemon=True
        )
        self._thread.start()
        deadline = threading.Event()
        waited = 0.0
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError("the openhands-prized proxy failed to start (see the log above)")
            if waited >= timeout:
                self.stop()
                raise RuntimeError(f"the openhands-prized proxy did not start in {timeout:.0f} s")
            deadline.wait(0.05)
            waited += 0.05

    def quiesce(self, timeout: float = 30.0) -> bool:
        """See ProxyApp.quiesce; called from another thread."""
        loop = self.app.loop if self._server is not None else None
        if loop is None:
            return True
        return asyncio.run_coroutine_threadsafe(self.app.quiesce(timeout), loop).result(timeout + 5)

    def unquiesce(self) -> None:
        if self._server is not None:
            self.app.unquiesce()

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        try:
            self._sock.close()
        except OSError:
            pass


def ingress_base(template: str, port: int, api_base: str) -> str:
    """The port's URL from the sandbox doc's ``previewUrlTemplate``, refused unless it
    is on Prized's own domain (or the API's, for another deployment): the Prized key
    goes to it on every request."""
    url = template.replace("{port}", str(port))
    host = (urlsplit(url).hostname or "").lower()
    api_host = (urlsplit(api_base).hostname or "").lower()
    trusted = [api_host.split(".", 1)[-1]] if api_host.count(".") >= 2 else [api_host]
    trusted.append("prized.dev")
    if urlsplit(url).scheme != "https" or not any(host == t or host.endswith("." + t) for t in trusted if t):
        raise ValueError(f"refusing to send the Prized key to {host!r}")
    return url.rstrip("/")
