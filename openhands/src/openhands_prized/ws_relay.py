"""WebSocket relay that runs inside the sandbox (exec transport).

The proxy on the user's machine cannot open a socket into the sandbox, so for each
WebSocket a client opens it starts this script as a background command in the
sandbox, with the Agent Server's own Python (which has ``websockets``):

    python ws_relay.py PORT TARGET_B64 HEADERS_B64 INBOX [MAX_BYTES]

It connects to ``ws://127.0.0.1:PORT`` + TARGET with HEADERS, then:

- prints one line per event on stdout, which the proxy streams back:
  ``O`` (open), ``T <b64>`` (text message), ``B <b64>`` (binary message),
  ``C <code> <reason b64>`` (closed; always the last line);
- sends every line appended to INBOX (``T <b64>`` / ``B <b64>``) to the server, in
  order. The proxy appends a client's messages there.

Past MAX_BYTES of output (default 32 MiB) it closes with 1012, and the client
reconnects: the proxy holds a relay's output in memory, so one never grows without end.

Standard library plus ``websockets`` (12 or later) only: it is uploaded as is.
"""

import asyncio
import base64
import json
import os
import sys


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def close_code_for_status(status) -> int:
    """The close code a client sees for a refused handshake: the Agent Server's own
    codes for a bad key (4001) and an unknown conversation (4004), so the SDK stops
    retrying as it would on a direct connection; 4000 + status otherwise."""
    if not isinstance(status, int) or not 400 <= status < 600:
        return 1011
    return {401: 4001, 403: 4001, 404: 4004}.get(status, 4000 + status)


async def _connect(url: str, headers: list):
    try:  # websockets >= 13
        from websockets.asyncio.client import connect

        return await connect(url, additional_headers=headers, max_size=None, open_timeout=30)
    except ImportError:  # websockets 12
        import websockets

        return await websockets.connect(url, extra_headers=headers, max_size=None, open_timeout=30)


def _read_from(path: str, pos: int) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            return f.read()
    except FileNotFoundError:
        return b""


async def _pump_inbox(ws, inbox: str) -> None:
    pos, pending = 0, b""
    while True:
        chunk = await asyncio.to_thread(_read_from, inbox, pos)
        if chunk:
            pos += len(chunk)
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                kind, _, payload = line.decode("ascii").partition(" ")
                data = base64.b64decode(payload)
                await ws.send(data.decode("utf-8") if kind == "T" else data)
        else:
            await asyncio.sleep(0.05)


async def main() -> int:
    port, target_b64, headers_b64, inbox = sys.argv[1:5]
    max_bytes = int(sys.argv[5]) if len(sys.argv) > 5 else 32 * 1024 * 1024
    target = base64.b64decode(target_b64).decode("utf-8")
    headers = [tuple(h) for h in json.loads(base64.b64decode(headers_b64))]
    url = f"ws://127.0.0.1:{int(port)}{target}"
    try:
        ws = await _connect(url, headers)
    except Exception as exc:  # refused handshake (401, 404) or nothing listening
        status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status_code", None)
        code = close_code_for_status(status)
        _out(f"C {code} {_b64(str(exc)[:200].encode())}")
        return 0
    _out("O")
    pump = asyncio.ensure_future(_pump_inbox(ws, inbox))
    code, reason, sent, rotated = 1000, "", 0, False
    try:
        async for message in ws:
            if isinstance(message, str):
                line = "T " + _b64(message.encode("utf-8"))
            else:
                line = "B " + _b64(bytes(message))
            _out(line)
            sent += len(line) + 1
            if sent > max_bytes:
                rotated = True
                break
    except Exception:  # connection dropped without a close frame
        code = 1011
    finally:
        pump.cancel()
    rcvd = getattr(ws, "close_code", None)
    if rotated:
        code, reason = 1012, "relay rotation: reconnect"
    elif rcvd is not None:
        code, reason = rcvd, getattr(ws, "close_reason", "") or ""
    _out(f"C {code} {_b64(reason.encode('utf-8'))}")
    try:
        await ws.close()
    except Exception:
        pass
    try:
        os.unlink(inbox)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
