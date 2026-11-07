"""``prized-managed-agents serve`` as a user runs it: settings from the environment, the receiver
on a real port, a session served end to end, and a clean stop on SIGTERM."""

from __future__ import annotations

import asyncio
import os
import signal

import httpx
from conftest import free_port, run
from mock_cma import ENV_ID, ENV_KEY, WEBHOOK_KEY

from prized_managed_agents import cli
from prized_managed_agents.config import Settings
from prized_managed_agents.sandboxes import SessionSandboxes


def test_serve_takes_a_webhook_serves_the_session_and_stops_on_sigterm(mock, world, ant_dist, monkeypatch):
    settings = Settings.from_env(
        {
            "ANTHROPIC_ENVIRONMENT_ID": ENV_ID,
            "ANTHROPIC_ENVIRONMENT_KEY": ENV_KEY,
            "ANTHROPIC_WEBHOOK_SIGNING_KEY": WEBHOOK_KEY,
            "ANTHROPIC_BASE_URL": mock.url,
            "ANT_URL": ant_dist["url"],
            "ANT_SHA256_AMD64": ant_dist["sha"]["amd64"],
            "ANT_SHA256_ARM64": ant_dist["sha"]["arm64"],
            "MAX_IDLE": "1s",
            "PROBE_EVERY": "1s",
        }
    )
    monkeypatch.setattr(cli, "SessionSandboxes", lambda s, **kw: SessionSandboxes(s, api=world.api(), **kw))
    port = free_port()

    async def go():
        serving = asyncio.create_task(cli._serve(settings, "127.0.0.1", port))
        async with httpx.AsyncClient() as http:
            for _ in range(200):
                try:
                    if (await http.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)
        mock.endpoint = f"http://127.0.0.1:{port}/webhook"
        s = mock.new_session()
        mock.run(s.id, "echo from-cli > cli.txt")
        for _ in range(400):
            boxes = world.live(**{"anthropic-session": s.id})
            if boxes and boxes[0].state == "paused":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the session's sandbox never paused")
        assert open(os.path.join(boxes[0].home, "work", "cli.txt")).read() == "from-cli\n"
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(serving, 30) == 0

    run(go())
