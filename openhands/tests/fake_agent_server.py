"""A stand-in for the OpenHands Agent Server, for the offline tests.

Raw ASGI on uvicorn. With OH_SESSION_API_KEYS_0 set, /api routes want the
X-Session-API-Key header and WebSockets want it as their first message
({"type": "auth", "session_api_key": ...}), as the real server does.

    python fake_agent_server.py --host 127.0.0.1 --port 8123
"""

import argparse
import base64
import json
import os
import sys

KEY = os.environ.get("OH_SESSION_API_KEYS_0", "")
BIG = bytes(range(256)) * (12 * 1024)  # 3 MiB, past the proxy's inline limit


def _headers(scope):
    return {k.decode().lower(): v.decode() for k, v in scope["headers"]}


async def _send_json(send, status, obj, extra=()):
    body = json.dumps(obj).encode()
    headers = [(b"content-type", b"application/json"), *extra]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] == "http":
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        path, headers = scope["path"], _headers(scope)
        if path == "/health":
            return await _send_json(send, 200, "OK")
        if path.startswith("/api/") and KEY and headers.get("x-session-api-key") != KEY:
            return await _send_json(send, 401, {"detail": "Unauthorized"})
        if path == "/api/echo":
            return await _send_json(
                send,
                201,
                {
                    "method": scope["method"],
                    "query": scope["query_string"].decode(),
                    "headers": headers,
                    "body_b64": base64.b64encode(body).decode(),
                },
                extra=[(b"x-fake", b"yes"), (b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")],
            )
        if path == "/api/big":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/octet-stream")],
                }
            )
            return await send({"type": "http.response.body", "body": BIG})
        if path == "/api/git/changes":
            return await _send_json(send, 200, [])
        return await _send_json(send, 404, {"detail": "Not Found"})
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        path = scope["path"]
        if not path.startswith("/sockets/"):
            await send({"type": "websocket.close", "code": 1008})  # refused handshake: 403
            return
        await send({"type": "websocket.accept"})
        if KEY:
            first = await receive()
            try:
                auth = json.loads(first.get("text") or "{}")
            except ValueError:
                auth = {}
            if auth.get("type") != "auth" or auth.get("session_api_key") != KEY:
                await send({"type": "websocket.close", "code": 4001, "reason": "Authentication failed"})
                return
        hello = os.environ.get("FAKE_WS_HELLO") or json.dumps({"hello": path})
        await send({"type": "websocket.send", "text": hello})
        await send({"type": "websocket.send", "bytes": b"\x00\x01binary"})
        while True:
            msg = await receive()
            if msg["type"] == "websocket.disconnect":
                return
            text = msg.get("text")
            if text == "close-me":
                await send({"type": "websocket.close", "code": 4321, "reason": "asked to"})
                return
            if text is not None:
                await send({"type": "websocket.send", "text": "echo:" + text})
            else:
                await send({"type": "websocket.send", "bytes": b"echo:" + msg["bytes"]})


def main():
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args, _ = p.parse_known_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", ws="auto")


if __name__ == "__main__":
    sys.exit(main())
