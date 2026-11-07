"""Claude Managed Agents webhooks: verify a delivery, read its event, and drop retries.

Deliveries follow Standard Webhooks (https://www.standardwebhooks.com/): the ``webhook-id``,
``webhook-timestamp`` and ``webhook-signature`` headers, an HMAC-SHA256 over
``{id}.{timestamp}.{raw body}`` keyed with the base64 secret after ``whsec_``, sent as ``v1,<base64>``
(several, space-separated, while a secret rotates). Anthropic's SDKs refuse a delivery more than
5 minutes from now either way, and so does this (https://platform.claude.com/docs/en/managed-agents/webhooks).

The payload names the event and the resource, never its state: ``{"type": "event", "id": "whe_…",
"created_at": …, "data": {"type": "session.status_run_started", "id": "sesn_…", …}}``. Retries
carry the same ``id`` (also the ``webhook-id`` header), so that is what ``Seen`` deduplicates on.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

TOLERANCE_S = 300


class WebhookError(Exception):
    """A delivery that is not from Anthropic, or not one we can read. Answered with 401 or 400."""


def decode_key(key: str) -> bytes:
    raw = key[len("whsec_") :] if key.startswith("whsec_") else key
    try:
        secret = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("ANTHROPIC_WEBHOOK_SIGNING_KEY is not a whsec_ key (its base64 doesn't decode)") from err
    if not secret:
        raise ValueError("ANTHROPIC_WEBHOOK_SIGNING_KEY is empty")
    return secret


class Verifier:
    def __init__(self, key: str, *, tolerance_s: int = TOLERANCE_S, clock: Callable[[], float] = time.time) -> None:
        self._secret = decode_key(key)
        self.tolerance_s = tolerance_s
        self.clock = clock

    def verify(self, body: bytes, headers: Mapping[str, str]) -> tuple[str, Any]:
        """The delivery's ``webhook-id`` and its parsed JSON body, or ``WebhookError``."""
        h = {k.lower(): v for k, v in headers.items()}
        msg_id, stamp, signatures = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature")
        if not (msg_id and stamp and signatures):
            raise WebhookError("missing webhook-id, webhook-timestamp or webhook-signature")
        try:
            sent = float(stamp)
        except ValueError as err:
            raise WebhookError("webhook-timestamp is not a number") from err
        if not math.isfinite(sent):
            raise WebhookError("webhook-timestamp is not a number")
        now = self.clock()
        if sent < now - self.tolerance_s:
            raise WebhookError("the delivery is more than 5 minutes old")
        if sent > now + self.tolerance_s:
            raise WebhookError("the delivery is dated more than 5 minutes ahead")
        signed = f"{msg_id}.{math.floor(sent)}.".encode() + body
        expected = hmac.new(self._secret, signed, hashlib.sha256).digest()
        for entry in signatures.split(" "):
            version, sep, value = entry.partition(",")
            if version != "v1" or not sep:
                continue
            try:
                given = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                continue
            if hmac.compare_digest(expected, given):
                try:
                    return msg_id, json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError) as err:
                    raise WebhookError("the body is not JSON") from err
        raise WebhookError("no signature matches")


@dataclass(frozen=True)
class Event:
    id: str
    type: str  # e.g. session.status_run_started
    resource_id: str  # e.g. sesn_…
    created_at: str

    @classmethod
    def parse(cls, doc: Any) -> Event:
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(doc, dict) or doc.get("type") != "event" or not isinstance(data, dict):
            raise WebhookError("not a webhook event")
        if not isinstance(data.get("type"), str) or not isinstance(data.get("id"), str):
            raise WebhookError("the event has no data.type or data.id")
        return cls(
            id=str(doc.get("id") or ""),
            type=data["type"],
            resource_id=data["id"],
            created_at=str(doc.get("created_at") or ""),
        )


class Seen:
    """Delivery ids handled in the last ``ttl_s`` (retries come within minutes), up to ``cap``."""

    def __init__(self, ttl_s: float = 3600.0, cap: int = 50_000, clock: Callable[[], float] = time.monotonic):
        self.ttl_s, self.cap, self.clock = ttl_s, cap, clock
        self._ids: OrderedDict[str, float] = OrderedDict()

    def __contains__(self, msg_id: str) -> bool:
        self._expire()
        return msg_id in self._ids

    def add(self, msg_id: str) -> None:
        self._ids[msg_id] = self.clock()
        self._ids.move_to_end(msg_id)
        self._expire()

    def _expire(self) -> None:
        cutoff = self.clock() - self.ttl_s
        while self._ids and (len(self._ids) > self.cap or next(iter(self._ids.values())) < cutoff):
            self._ids.popitem(last=False)
