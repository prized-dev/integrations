"""The webhook receiver: a small ASGI app (``POST /webhook``, ``GET /healthz``).

Anthropic delivers to an HTTPS URL on port 443 with a public hostname, and treats any 2xx as
received. It retries other answers (three attempts in all, 5 to 120 seconds apart) and disables the
endpoint on any 3xx, so this never redirects. The answers:

- 200 once the event is acted on (the work queue, not the webhook, is the record: a run-started
  event only wakes the intake, which claims whatever is queued), or for a retry already handled;
- 401 for a delivery whose signature or timestamp doesn't check out;
- 400 for a verified body that isn't an event; 413 past 1 MiB;
- 503 while shutting down, so the delivery is retried, to this process's successor.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .orchestrator import Orchestrator
from .webhooks import Event, Seen, Verifier, WebhookError

log = logging.getLogger("prized_managed_agents.server")

MAX_BODY = 1 << 20
Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class WebhookApp:
    def __init__(self, orchestrator: Orchestrator, verifier: Verifier | None, seen: Seen | None = None) -> None:
        self.orch = orchestrator
        self.verifier = verifier
        self.seen = seen or Seen()
        self.counts = {"delivered": 0, "duplicate": 0, "refused": 0}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        method, path = scope["method"], scope["path"]
        if path == "/healthz" and method in ("GET", "HEAD"):
            await _reply(
                send,
                200,
                {
                    "ok": not self.orch.stopping,
                    "environment": self.orch.s.environment_id,
                    "webhooks": self.verifier is not None,
                    "runs": len(self.orch.runs),
                },
            )
            return
        if path != "/webhook":
            await _reply(send, 404, {"error": "not found"})
            return
        if method != "POST":
            await _reply(send, 405, {"error": "POST only"}, {"allow": "POST"})
            return
        if self.verifier is None:
            await _reply(send, 404, {"error": "webhooks are off: set ANTHROPIC_WEBHOOK_SIGNING_KEY"})
            return
        if self.orch.stopping:
            await _reply(send, 503, {"status": "shutting down"}, {"retry-after": "5"})
            return
        body = await _read_body(receive)
        if body is None:
            await _reply(send, 413, {"error": "body over 1 MiB"})
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        try:
            msg_id, doc = self.verifier.verify(body, headers)
        except WebhookError as err:
            self.counts["refused"] += 1
            log.warning("refused a webhook delivery: %s", err)
            await _reply(send, 401, {"error": "signature verification failed"})
            return
        if msg_id in self.seen:
            self.counts["duplicate"] += 1
            await _reply(send, 200, {"status": "duplicate"})
            return
        try:
            event = Event.parse(doc)
        except WebhookError as err:
            await _reply(send, 400, {"error": str(err)})
            return
        did = await self.orch.on_event(event)
        self.seen.add(msg_id)
        self.counts["delivered"] += 1
        log.info("webhook %s: %s %s: %s", msg_id, event.type, event.resource_id, did)
        await _reply(send, 200, {"status": "ok", "did": did})


async def _read_body(receive: Receive) -> bytes | None:
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b"".join(chunks)
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


async def _reply(send: Send, status: int, doc: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    body = json.dumps(doc).encode()
    raw = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    raw += [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})
