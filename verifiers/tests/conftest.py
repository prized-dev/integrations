"""The tests run against stand-ins: tests/fake_verifiers in place of verifiers (put first on the
path, before verifiers_prized is imported) and tests/fake_prized.py in place of the Prized API.
Nothing here talks to the network or runs verifiers itself."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

HERE = Path(__file__).parent
FAKE_VERIFIERS = HERE / "fake_verifiers"
sys.path.insert(0, str(FAKE_VERIFIERS))
sys.path.insert(0, str(HERE))
for name in [m for m in sys.modules if m == "verifiers" or m.startswith("verifiers.")]:
    del sys.modules[name]

import verifiers  # noqa: E402

assert Path(verifiers.__file__).is_relative_to(FAKE_VERIFIERS), verifiers.__file__

import fake_prized  # noqa: E402

from verifiers_prized import pool  # noqa: E402

FAKE_KEY = "dcp_0000000000000000000000000000000000000000"


@pytest.fixture
def cloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[fake_prized.Cloud]:
    fake = fake_prized.Cloud(tmp_path / "cloud")
    fake.root.mkdir()
    monkeypatch.setattr(fake_prized, "CLOUD", fake)
    monkeypatch.setattr(
        pool,
        "sdk",
        types.SimpleNamespace(AsyncSandbox=fake_prized.AsyncSandbox, Sandbox=fake_prized.Sandbox),
    )
    monkeypatch.setenv("PRIZED_API_KEY", FAKE_KEY)
    monkeypatch.setattr(pool, "LINGER", 0.05)
    # The build CLI adds a handler to the package's logger: none outlives its test.
    monkeypatch.setattr(pool.logger, "handlers", list(pool.logger.handlers))
    monkeypatch.setattr(pool.logger, "level", pool.logger.level)
    pool._pools.clear()
    pool._tracked.clear()
    pool._used.clear()
    yield fake
    # Nothing tracked or used may reach the atexit hook, which would call the real API.
    pool._pools.clear()
    pool._tracked.clear()
    pool._used.clear()
