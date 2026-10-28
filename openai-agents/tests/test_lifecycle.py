"""The Agents SDK's lifecycle scenario (tests/lifecycle.py) on Prized.

Offline against the fake API, whose commands really run in a directory on
this machine; and live against the Prized API when PRIZED_LIVE_TESTS=1 and
PRIZED_API_KEY are set.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from openai_agents_prized import PrizedSandboxClient, PrizedSandboxClientOptions
from tests.lifecycle import (
    FIRST_RUN_OUTPUTS,
    MOCK_REPO,
    PUBLIC_REPO,
    RESUMED_RUN_OUTPUTS,
    create_local_sources,
    run_lifecycle,
    successful_events,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from tests.fake_prized import FakePrized

MOCK_TOOL = """#!/bin/sh
# The SDK's mock git and cp from its own lifecycle test: clone does nothing,
# and the copy out of the clone writes the fixture repository's two files.
case "$(basename "$0")" in
  git) exit 0 ;;
  cp)
    dest=""
    for arg in "$@"; do dest="$arg"; done
    mkdir -p "$dest/pkg"
    printf "mock git repo readme v1\\n" > "$dest/README.md"
    printf "VALUE = 'mock git module v1'\\n" > "$dest/pkg/module.py"
    ;;
esac
"""


@pytest.fixture
def mock_git(fake: FakePrized, tmp_path: Path) -> None:
    bin_dir = tmp_path / "mock-bin"
    bin_dir.mkdir()
    for name in ("git", "cp"):
        tool = bin_dir / name
        tool.write_text(MOCK_TOOL)
        tool.chmod(0o755)
    fake.base_path = f"{bin_dir}{os.pathsep}{fake.base_path}"


@pytest.fixture(autouse=True)
def _local_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The runner's default snapshot is a LocalSnapshot under the user's home.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.mark.usefixtures("mock_git")
async def test_lifecycle_new_sandbox_on_resume(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized, tmp_path: Path
) -> None:
    """Deleted at the end of the first run; the resume makes a new sandbox and
    restores the workspace from the snapshot."""
    result = await run_lifecycle(
        make_client=make_client,
        source_root=create_local_sources(tmp_path),
        repo=MOCK_REPO,
        options=PrizedSandboxClientOptions(),
    )
    assert result.first_outputs == FIRST_RUN_OUTPUTS
    assert result.resumed_outputs[-3:] == RESUMED_RUN_OUTPUTS
    assert result.session_state["type"] == "prized"
    assert result.session_state["snapshot"]["type"] == "local"
    assert result.session_state["workspace_root_ready"] is True
    assert result.session_state["manifest"]["root"] == "/home/prized/work"
    assert result.resumed_sandbox_id != result.first_sandbox_id
    assert fake.sandbox(result.first_sandbox_id).state in {"deleting", "deleted"}
    assert fake.live() == []
    assert successful_events(result.events, op="stop") == 2
    assert successful_events(result.events, op="shutdown") == 2
    # The resumed sandbox got its workspace back from the snapshot.
    assert any("tar -C" in c and "-xf -" in c for c in fake.commands())


@pytest.mark.usefixtures("mock_git")
async def test_lifecycle_pause_on_exit_keeps_the_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized, tmp_path: Path
) -> None:
    """Paused at the end of the first run; the resume wakes the same sandbox,
    whose workspace matches the snapshot, so nothing is restored."""
    result = await run_lifecycle(
        make_client=make_client,
        source_root=create_local_sources(tmp_path),
        repo=MOCK_REPO,
        options=PrizedSandboxClientOptions(pause_on_exit=True),
    )
    assert result.first_outputs == FIRST_RUN_OUTPUTS
    assert result.resumed_outputs[-3:] == RESUMED_RUN_OUTPUTS
    assert result.resumed_sandbox_id == result.first_sandbox_id
    assert fake.sandbox(result.first_sandbox_id).state == "paused"
    assert len(fake.calls("POST", r"^/api/v1/sandboxes$")) == 1
    assert not any("tar -C" in c and "-xf -" in c for c in fake.commands())


@pytest.fixture
async def live_client() -> AsyncIterator[Callable[..., PrizedSandboxClient]]:
    made: list[PrizedSandboxClient] = []

    def factory(**kwargs: Any) -> PrizedSandboxClient:
        client = PrizedSandboxClient(**kwargs)
        made.append(client)
        return client

    yield factory
    for client in made:
        await client.close()


LIVE_OPTIONS: dict[str, Any] = {
    "vcpu": 2,
    "memory_mb": 4096,
    "max_runtime_min": 30,
    "labels": {"test": "openai-agents-prized"},
}


@pytest.mark.live
@pytest.mark.parametrize("pause_on_exit", [False, True], ids=["new-sandbox", "pause-on-exit"])
async def test_live_lifecycle(
    live_client: Callable[..., PrizedSandboxClient], tmp_path: Path, pause_on_exit: bool
) -> None:
    result = await run_lifecycle(
        make_client=live_client,
        source_root=create_local_sources(tmp_path),
        repo=PUBLIC_REPO,
        options=PrizedSandboxClientOptions(**LIVE_OPTIONS, pause_on_exit=pause_on_exit),
    )
    try:
        assert result.first_outputs == FIRST_RUN_OUTPUTS
        assert result.resumed_outputs[-3:] == RESUMED_RUN_OUTPUTS
        assert (result.resumed_sandbox_id == result.first_sandbox_id) is pause_on_exit
    finally:
        # Paused sandboxes still count toward the quota: delete them.
        cleanup = live_client()
        for sandbox_id in {result.first_sandbox_id, result.resumed_sandbox_id}:
            await cleanup._client.delete_sandbox(sandbox_id)
