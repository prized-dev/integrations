"""Shared fixtures for PrizedSandbox tests."""

from __future__ import annotations

import importlib.util
import os
import types
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typing_extensions import TypedDict

_HAS_PRIZED = importlib.util.find_spec('prized') is not None
collect_ignore = [] if _HAS_PRIZED else ['test_prized_live.py', 'test_prized_sandbox.py', 'test_conformance.py']

# Live sandboxes are deleted by the tests that create them; CI stops a live job after 15 minutes, before a
# `finally` can run, so each one also pauses after a short idle time and has a short run time: a sandbox
# left behind that way stops using its vCPUs, and is deleted when its run time ends.
LIVE_AUTO_PAUSE_MIN = 5
LIVE_MAX_RUNTIME_MIN = 30
LIVE_LABELS = {
    'pydantic-ai-test': 'live',
    **dict(item.split('=', 1) for item in os.environ.get('PRIZED_TEST_LABELS', '').split(',') if '=' in item),
}

if TYPE_CHECKING or _HAS_PRIZED:  # pragma: no branch - the SDK-installed and slim jobs take opposite branches
    import prized

    from .fake_prized import FakePrized


class Connection(TypedDict):
    base_url: str | None
    edge_url: str | None


def live_connection() -> Connection:
    """Connection options for the live tier: the SDK's defaults unless the environment points elsewhere."""
    return Connection(
        base_url=os.environ.get('PRIZED_BASE_URL') or None, edge_url=os.environ.get('PRIZED_EDGE_URL') or None
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Gate the live tier, before any live fixture can create a sandbox.

    It runs only with `PYDANTIC_AI_HARNESS_PRIZED_LIVE=1` and a non-empty `PRIZED_API_KEY`. Otherwise it
    skips, unless `PRIZED_REQUIRE_LIVE=1` (CI's live job sets it), where a missing key fails instead: a
    live job that tested nothing must not pass as a silent skip.
    """
    if item.get_closest_marker('prized_live') is None:
        return
    opted_in = os.getenv('PYDANTIC_AI_HARNESS_PRIZED_LIVE') == '1'
    if opted_in and os.getenv('PRIZED_API_KEY'):  # pragma: no cover - live job
        return
    reason = 'requires PYDANTIC_AI_HARNESS_PRIZED_LIVE=1 and a non-empty PRIZED_API_KEY'
    if os.getenv('PRIZED_REQUIRE_LIVE') == '1':  # pragma: no cover - live job
        pytest.fail(f'PRIZED_REQUIRE_LIVE=1 but the live tier cannot run: it {reason}.')
    pytest.skip(reason)


class _PoisonedPrized(types.ModuleType):
    """A `prized` stand-in that fails loudly on any attribute access.

    The real SDK is installed in the dev venv for the live tier, so a unit test that forgets the
    `fake_prized` fixture would otherwise reach production and, with a developer API key configured,
    create real billed sandboxes.
    """

    def __getattr__(self, name: str) -> object:  # pragma: no cover - tripwire, hit only by a misbehaving test
        raise AssertionError(
            'A prized_sandbox unit test touched the real `prized` package. '
            'Use the `fake_prized` fixture, or mark the test `prized_live`.'
        )


@pytest.fixture(autouse=True)
def _no_real_prized(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Poison `prized` for every test here except the opt-in live tier."""
    if 'prized_live' in request.keywords:  # pragma: no cover - live tier runs without coverage
        yield
        return
    monkeypatch.setattr('pydantic_ai_harness.prized_sandbox._backend.prized', _PoisonedPrized('prized'))
    yield


if _HAS_PRIZED:  # pragma: no branch - the fixture cannot be defined without its SDK-backed fake

    @pytest.fixture
    async def fake_prized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncGenerator[FakePrized]:
        """Swap the SDK's `AsyncSandbox` for the fake and return its control surface.

        The real module stays in place for its error types, which the backend matches on.
        """
        fake = FakePrized()
        # Resolved so the working directory `pwd -P` reports matches it on hosts whose temp
        # directory sits behind a symlink.
        fake.host_root = tmp_path.resolve()
        monkeypatch.setattr('pydantic_ai_harness.prized_sandbox._backend.prized', prized)
        monkeypatch.setattr(prized, 'AsyncSandbox', fake.sandbox_class)
        monkeypatch.setenv('PRIZED_API_KEY', 'dcp_fake')
        # Long command lines are staged in this directory with the file API, which the fake keeps on the host.
        monkeypatch.setattr('pydantic_ai_harness.prized_sandbox._backend._SCRIPT_DIR', str(fake.host_root / 'scripts'))
        # The fake's sandboxes live under `host_root`, which stands in for `/home/prized` for the file API.
        monkeypatch.setattr('pydantic_ai_harness.prized_sandbox._backend._HOME', str(fake.host_root))
        yield fake
        await fake.reap()
