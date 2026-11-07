"""Settings from the environment, the receiver's other routes, and the in-sandbox status parser."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import standardwebhooks
from conftest import run, settings_for
from mock_cma import ENV_ID, ENV_KEY

from prized_managed_agents import worker as w
from prized_managed_agents.cma import CMA
from prized_managed_agents.config import ANT_SHA256, ConfigError, Settings, seconds
from prized_managed_agents.orchestrator import Orchestrator
from prized_managed_agents.sandboxes import SessionSandboxes
from prized_managed_agents.server import MAX_BODY, WebhookApp
from prized_managed_agents.webhooks import Verifier

BASE = {"ANTHROPIC_ENVIRONMENT_ID": ENV_ID, "ANTHROPIC_ENVIRONMENT_KEY": ENV_KEY}


def test_settings_from_the_environment():
    s = Settings.from_env(
        {
            **BASE,
            "ANTHROPIC_WEBHOOK_SIGNING_KEY": "whsec_abc",
            "KEEP_PAUSED": "2d",
            "AUTO_PAUSE": "20m",
            "RUN_TIMEOUT": "90m",
            "PRIZED_LABELS": "team=infra, cost=cma",
            "MAX_SESSIONS": "7",
            "ORCH_ANTHROPIC_BASE_URL": "https://example.test/",
        }
    )
    assert s.webhooks and s.keep_paused_s == 2 * 86400 and s.auto_pause_s == 1200 and s.run_timeout_s == 5400
    assert s.labels == {"team": "infra", "cost": "cma"} and s.max_sessions == 7
    assert s.base_url == "https://example.test" and s.ant_sha256 == ANT_SHA256
    assert s.reclaim_ms == 240_000
    assert "abc" not in repr(s) and ENV_KEY not in repr(s)


@pytest.mark.parametrize(
    "env, match",
    [
        ({}, "ANTHROPIC_ENVIRONMENT_ID and ANTHROPIC_ENVIRONMENT_KEY not set"),
        ({**BASE, "ANTHROPIC_ENVIRONMENT_ID": "nope"}, "env_"),
        ({**BASE, "KEEP_PAUSED": "soon"}, "KEEP_PAUSED"),
        ({**BASE, "ANT_VERSION": "2.8.0"}, "ANT_SHA256_AMD64"),
        ({**BASE, "PRIZED_LABELS": "novalue"}, "key=value"),
        ({**BASE, "MAX_HANDOFFS": "x"}, "not a number"),
        ({**BASE, "ANTHROPIC_WEBHOOK_SIGNING_KEY": "whsec_not*base64"}, "not a whsec_ key"),
    ],
)
def test_bad_settings_say_what_to_fix(env, match):
    with pytest.raises(ConfigError, match=match):
        Settings.from_env(env)


def test_another_ant_release_takes_its_own_checksums():
    s = Settings.from_env({**BASE, "ANT_VERSION": "2.8.0", "ANT_SHA256_AMD64": "a" * 64})
    assert s.ant_version == "2.8.0" and s.ant_sha256 == {"amd64": "a" * 64}


def test_durations():
    assert seconds("90", name="X") == 90 and seconds("15m", name="X") == 900 and seconds("1d", name="X") == 86400


async def call(app, method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None):
    sent = []
    chunks = [body[i : i + 65536] for i in range(0, len(body), 65536)] or [b""]

    async def receive():
        chunk = chunks.pop(0) if chunks else b""
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    await app(scope, receive, send)
    return sent[0]["status"], json.loads(sent[1]["body"]), dict(sent[0]["headers"])


def test_the_receivers_other_answers_and_never_a_redirect(mock, world, ant_dist):
    async def go():
        s = settings_for(mock, ant_dist)
        orch = Orchestrator(
            s, CMA(s.base_url, s.environment_id, s.environment_key), SessionSandboxes(s, api=world.api())
        )
        app = WebhookApp(orch, Verifier(s.webhook_key))
        assert (await call(app, "GET", "/healthz"))[:2] == (
            200,
            {"ok": True, "environment": ENV_ID, "webhooks": True, "runs": 0},
        )
        assert (await call(app, "GET", "/webhook"))[0] == 405
        assert (await call(app, "POST", "/elsewhere"))[0] == 404
        assert (await call(app, "POST", "/webhook", b"{}"))[0] == 401
        assert (await call(app, "POST", "/webhook", b"x" * (MAX_BODY + 1)))[0] == 413
        off = WebhookApp(orch, None)
        assert (await call(off, "POST", "/webhook", b"{}"))[0] == 404
        # A verified delivery that isn't an event we read: 200, so the endpoint stays healthy.
        body = '{"type": "something_new", "id": "whe_new"}'
        at = datetime.now(timezone.utc)
        headers = {
            "webhook-id": "whe_new",
            "webhook-timestamp": str(int(at.timestamp())),
            "webhook-signature": standardwebhooks.Webhook(s.webhook_key).sign("whe_new", at, body),
        }
        assert (await call(app, "POST", "/webhook", body.encode(), headers))[:2] == (200, {"status": "ignored"})
        assert (await call(app, "POST", "/webhook", body.encode(), headers))[:2] == (200, {"status": "duplicate"})
        await orch.cma.close()

    run(go())


def test_status_lines_parse_and_junk_is_skipped():
    text = "w-1 sesn_1 work_1 alive 4242 1\nw-2 sesn_1 work_2 exited 3 0\nw-3 sesn_2 work_3 lost - 0\nnoise\n"
    got = w.parse_status(text)
    assert [(r.run_id, r.state, r.number, r.acked) for r in got] == [
        ("w-1", "alive", 4242, True),
        ("w-2", "exited", 3, False),
        ("w-3", "lost", None, False),
    ]
    with pytest.raises(ValueError):
        w.env("../etc")
