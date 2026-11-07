"""Signature checks, held against the Standard Webhooks reference library (the one Anthropic's SDK
verifies with) rather than against ourselves."""

from __future__ import annotations

import base64
import json
import random
from datetime import datetime, timezone

import pytest
import standardwebhooks
import standardwebhooks.webhooks

from prized_managed_agents.webhooks import Event, Seen, Verifier, WebhookError, decode_key

KEY = "whsec_" + base64.b64encode(bytes(range(32))).decode()
NOW = 1_800_000_000


def signed(body: str, *, key: str = KEY, msg_id: str = "whe_1", at: int = NOW) -> dict[str, str]:
    stamp = datetime.fromtimestamp(at, timezone.utc)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(at),
        "webhook-signature": standardwebhooks.Webhook(key).sign(msg_id, stamp, body),
    }


def verifier(at: float = NOW) -> Verifier:
    return Verifier(KEY, clock=lambda: at)


BODY = json.dumps(
    {
        "type": "event",
        "id": "whe_1",
        "created_at": "2026-11-06T00:00:00Z",
        "data": {"type": "session.status_run_started", "id": "sesn_1"},
    }
)


def test_a_delivery_the_reference_library_signs_verifies():
    msg_id, doc = verifier().verify(BODY.encode(), signed(BODY))
    assert msg_id == "whe_1"
    assert Event.parse(doc) == Event("whe_1", "session.status_run_started", "sesn_1", "2026-11-06T00:00:00Z")


def test_header_names_are_case_insensitive():
    headers = {k.upper(): v for k, v in signed(BODY).items()}
    assert verifier().verify(BODY.encode(), headers)[0] == "whe_1"


def test_the_published_standard_webhooks_example():
    # The example in Standard Webhooks' libraries' test suites.
    key = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    msg_id, at, body = "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, '{"test": 2432232314}'
    expected = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
    assert standardwebhooks.Webhook(key).sign(msg_id, datetime.fromtimestamp(at, timezone.utc), body) == expected
    headers = {"webhook-id": msg_id, "webhook-timestamp": str(at), "webhook-signature": expected}
    assert Verifier(key, clock=lambda: at).verify(body.encode(), headers)[1] == {"test": 2432232314}


@pytest.mark.parametrize(
    "change",
    ["body", "id", "timestamp", "key", "no-signature", "no-id", "v1a-only", "garbage"],
)
def test_anything_tampered_or_missing_is_refused(change):
    body, headers = BODY, signed(BODY)
    if change == "body":
        body = BODY.replace("sesn_1", "sesn_2")
    elif change == "id":
        headers["webhook-id"] = "whe_2"
    elif change == "timestamp":
        headers["webhook-timestamp"] = str(NOW + 1)
    elif change == "key":
        headers = signed(BODY, key="whsec_" + base64.b64encode(b"x" * 32).decode())
    elif change == "no-signature":
        del headers["webhook-signature"]
    elif change == "no-id":
        del headers["webhook-id"]
    elif change == "v1a-only":
        headers["webhook-signature"] = headers["webhook-signature"].replace("v1,", "v1a,")
    elif change == "garbage":
        headers["webhook-signature"] = "v1 ,,, v1,%%%"
    with pytest.raises(WebhookError):
        verifier().verify(body.encode(), headers)


def test_during_a_key_rotation_any_listed_signature_may_match():
    good = signed(BODY)["webhook-signature"]
    old = signed(BODY, key="whsec_" + base64.b64encode(b"o" * 32).decode())["webhook-signature"]
    headers = {**signed(BODY), "webhook-signature": f"{old} v1a,abc nonsense {good}"}
    assert verifier().verify(BODY.encode(), headers)[0] == "whe_1"


@pytest.mark.parametrize("skew", [-301, 301])
def test_more_than_five_minutes_off_is_refused(skew):
    with pytest.raises(WebhookError, match="5 minutes"):
        verifier(NOW + -skew).verify(BODY.encode(), signed(BODY))


@pytest.mark.parametrize("skew", [-299, 0, 299])
def test_within_five_minutes_is_fine(skew):
    assert verifier(NOW + skew).verify(BODY.encode(), signed(BODY))


def test_we_agree_with_the_reference_library_on_random_deliveries(monkeypatch):
    # The reference reads its clock itself; pin it to NOW.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime.fromtimestamp(NOW, tz)

    monkeypatch.setattr(standardwebhooks.webhooks, "datetime", Clock)
    rng = random.Random(7)
    for _ in range(300):
        body = json.dumps({"type": "event", "n": rng.random(), "s": "é" * rng.randint(0, 3)})
        msg_id = f"whe_{rng.getrandbits(40):x}"
        at = NOW + rng.randint(-400, 400)
        headers = signed(body, msg_id=msg_id, at=at)
        mutate = rng.choice(["none", "body", "sig", "stamp"])
        if mutate == "body":
            body = body + " "
        elif mutate == "sig":
            sig = headers["webhook-signature"]
            headers["webhook-signature"] = sig[:-3] + ("A" if sig[-3] != "A" else "B") + sig[-2:]
        elif mutate == "stamp":
            headers["webhook-timestamp"] = str(at + 1)
        try:
            standardwebhooks.Webhook(KEY).verify(body, dict(headers))
            theirs = True
        except standardwebhooks.WebhookVerificationError:
            theirs = False
        try:
            verifier().verify(body.encode(), headers)
            ours = True
        except WebhookError:
            ours = False
        assert ours == theirs, (mutate, at - NOW)


def test_keys_decode_with_or_without_prefix_and_padding():
    raw = base64.b64encode(b"k" * 32).decode()
    assert decode_key("whsec_" + raw) == decode_key(raw.rstrip("=")) == b"k" * 32
    with pytest.raises(ValueError):
        decode_key("whsec_")
    with pytest.raises(ValueError):
        decode_key("whsec_not*base64")


@pytest.mark.parametrize(
    "doc",
    [[], {"type": "nope", "data": {"type": "x", "id": "y"}}, {"type": "event"}, {"type": "event", "data": {"id": "y"}}],
)
def test_an_event_needs_its_shape(doc):
    with pytest.raises(WebhookError):
        Event.parse(doc)


def test_seen_forgets_after_its_ttl_and_past_its_cap():
    t = [0.0]
    seen = Seen(ttl_s=10, cap=3, clock=lambda: t[0])
    seen.add("a")
    assert "a" in seen
    t[0] = 11
    assert "a" not in seen
    for x in "bcde":
        seen.add(x)
    assert "b" not in seen and all(x in seen for x in "cde")
