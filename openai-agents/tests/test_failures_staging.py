"""A staged write publishes the bytes it uploaded, or nothing (FAILURES.md)."""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agents.sandbox.errors import WorkspaceArchiveWriteError

from openai_agents_prized import PrizedSandboxSession
from openai_agents_prized._core import STAGE_ROOT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from agents.sandbox.session import SandboxSession

    from openai_agents_prized import PrizedSandboxClient
    from tests.fake_prized import FakePrized, FakeSandbox

WRITE_JOIN = "openai-agents-prized-$$"
"""Text only the staged write's join script has."""


@pytest.fixture
async def session(make_client: Callable[..., PrizedSandboxClient]) -> AsyncIterator[SandboxSession]:
    created = await make_client().create()
    async with created:
        yield created


def _local(fake: FakePrized, session: SandboxSession, path: str) -> Path:
    inner = session._inner
    assert isinstance(inner, PrizedSandboxSession)
    return Path(fake.sandbox(inner.sandbox_id).local(path))


def _parts(fake: FakePrized, session: SandboxSession) -> list[Path]:
    stage = _local(fake, session, STAGE_ROOT)
    return sorted(stage.glob("*/part-*")) if stage.exists() else []


async def test_write_never_publishes_a_staged_part_swapped_for_a_link(
    session: SandboxSession, fake: FakePrized
) -> None:
    private = _local(fake, session, "/home/prized/private.txt")
    private.write_bytes(b"not for the agent\n")
    target = _local(fake, session, "/home/prized/work/target.txt")

    def swap(_: FakeSandbox) -> None:  # after the upload, before the join
        (part,) = _parts(fake, session)
        part.unlink()
        part.symlink_to(private)

    fake.before_exec(WRITE_JOIN, swap)
    with pytest.raises(WorkspaceArchiveWriteError) as caught:
        await session.write(Path("target.txt"), io.BytesIO(b"the agent's bytes\n"))
    assert caught.value.context["reason"] == "staged_upload_changed"
    assert not target.exists()
    assert list(target.parent.glob(".openai-agents-prized-*")) == []
    assert _parts(fake, session) == []


async def test_write_never_publishes_a_staged_part_rewritten_after_its_upload(
    session: SandboxSession, fake: FakePrized
) -> None:
    await session.write(Path("target.txt"), io.BytesIO(b"v1\n"))
    target = _local(fake, session, "/home/prized/work/target.txt")

    def rewrite(_: FakeSandbox) -> None:
        (part,) = _parts(fake, session)
        part.write_bytes(b"other bytes\n")

    fake.before_exec(WRITE_JOIN, rewrite)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.write(Path("target.txt"), io.BytesIO(b"v2\n"))
    assert target.read_bytes() == b"v1\n"


async def test_an_empty_write_still_lands(session: SandboxSession, fake: FakePrized) -> None:
    await session.write(Path("empty.txt"), io.BytesIO(b""))
    assert _local(fake, session, "/home/prized/work/empty.txt").read_bytes() == b""


async def test_writes_racing_to_make_the_same_directory_both_land(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized, tmp_path: Path
) -> None:
    # A `mkdir` that loses the race: another write makes the directory just
    # before this one does (seen live when a manifest has two files in a new
    # directory, which the SDK writes at once).
    shims = tmp_path / "shims"
    shims.mkdir()
    real = shutil.which("mkdir")
    assert real is not None
    shim = shims / "mkdir"
    shim.write_text(f'#!/bin/sh\n"{real}" "$@" 2>/dev/null\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    fake.base_path = f"{shims}:{fake.base_path}"
    session = await make_client().create()
    async with session:
        await session.write(Path("tests/__init__.py"), io.BytesIO(b""))
        await session.write(Path("tests/deep/er/test_a.py"), io.BytesIO(b"a = 1\n"))
        assert _local(fake, session, "/home/prized/work/tests/deep/er/test_a.py").read_bytes() == (
            b"a = 1\n"
        )
