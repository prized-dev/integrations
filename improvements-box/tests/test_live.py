"""Live: one suggestion on the demo app, through real Prized sandboxes and the real Claude Code.

Opt-in: ``PRIZED_LIVE=1``, ``PRIZED_API_KEY`` (Sandboxes: Write) and ``ANTHROPIC_API_KEY`` (or
``CLAUDE_CODE_OAUTH_TOKEN``) in this process's environment. The model key goes into the agent's
command only; the Prized key never enters a sandbox. The sandbox carries a run label and is gone
at the end. ``IMPROVEMENTS_MODEL`` picks the model (default ``sonnet``).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest
from conftest import TEST

from improvements_box import Box, Config

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("PRIZED_LIVE") != "1"
        or not os.environ.get("PRIZED_API_KEY")
        or not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
        reason="live tests need PRIZED_LIVE=1, PRIZED_API_KEY and a model key",
    ),
]


def test_a_suggestion_becomes_a_patch_on_a_real_sandbox(demo: Path, tmp_path: Path) -> None:
    from prized import Sandbox

    run = f"live-{secrets.token_hex(3)}"
    cfg = Config(
        repo=str(demo),
        test=TEST,
        out=str(tmp_path / "out"),
        labels={"task": "improvements-box", "run": run},
        agent_minutes=8,
        max_budget_usd=1.0,
        model=os.environ.get("IMPROVEMENTS_MODEL", "sonnet"),
    )
    box = Box(cfg, log=print)
    try:
        s = box.submit("Support Kelvin input too, like 300K, with a test.")
        box.wait(s, timeout=900)
        print(s.view()["steps"], s.agent_cost_usd)
        assert s.outcome == "patch", s.detail
        assert s.sandbox_state == "deleted"
        assert "test_temps.py" in s.files
        assert Sandbox.list(labels={"run": run}) == []
    finally:
        box.close()
        Sandbox.delete_by_labels({"run": run})
