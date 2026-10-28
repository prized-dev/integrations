"""Processes that outlive one call (`supports_pty`), against the fake API.

The fake runs them with a real bash and util-linux `script` on this machine.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agents.sandbox.errors import PtySessionNotFoundError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from agents.sandbox.session import SandboxSession

    from openai_agents_prized import PrizedSandboxClient


@pytest.fixture
async def session(make_client: Callable[..., PrizedSandboxClient]) -> AsyncIterator[SandboxSession]:
    created = await make_client().create()
    async with created:
        yield created


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return not stat.rsplit(") ", 1)[1].startswith(("Z", "X"))


async def _gone(pids: list[int]) -> bool:
    """Whether every pid has exited; SIGKILL lands, and init reaps, a moment later."""
    for _ in range(100):
        if not any(_alive(pid) for pid in pids):
            return True
        await asyncio.sleep(0.02)
    return False


def _leader(session: SandboxSession, process_id: int) -> int:
    entry = session._inner._pty_processes[process_id]
    return int(Path(entry.dir, "pid").read_text())


async def test_a_short_command_returns_its_exit_code(session: SandboxSession) -> None:
    assert session.supports_pty()
    update = await session.pty_exec_start("echo out; echo err >&2; exit 3", yield_time_s=5)
    assert update.process_id is None
    assert update.exit_code == 3
    assert update.output == b"out\nerr\n"


async def test_a_long_command_keeps_running_and_is_polled(session: SandboxSession) -> None:
    # The command waits for a file the test makes, so a slow runner cannot
    # see it finish inside the first yield.
    first = await session.pty_exec_start(
        "echo start; until [ -e release ]; do sleep 0.05; done; echo end", yield_time_s=0.5
    )
    assert first.process_id is not None
    assert first.exit_code is None
    assert first.output == b"start\n"
    await session.exec("touch release")
    rest = await session.pty_write_stdin(session_id=first.process_id, chars="", yield_time_s=10)
    assert rest.process_id is None
    assert rest.exit_code == 0
    assert rest.output == b"end\n"
    with pytest.raises(PtySessionNotFoundError):
        await session.pty_write_stdin(session_id=first.process_id, chars="")


async def test_a_tty_takes_input_and_ctrl_c(session: SandboxSession) -> None:
    started = await session.pty_exec_start(
        "printf 'ready\\n'; while IFS= read -r line; do printf 'got:%s\\n' \"$line\"; done",
        tty=True,
        yield_time_s=0.5,
    )
    assert started.process_id is not None
    assert started.output.replace(b"\r\n", b"\n") == b"ready\n"
    echoed = await session.pty_write_stdin(
        session_id=started.process_id, chars="hi\n", yield_time_s=0.5
    )
    assert echoed.output.replace(b"\r\n", b"\n") == b"hi\ngot:hi\n"
    stopped = await session.pty_write_stdin(
        session_id=started.process_id, chars="\u0003", yield_time_s=5
    )
    assert stopped.process_id is None
    assert stopped.exit_code == 130  # SIGINT


async def test_stdin_needs_a_tty(session: SandboxSession) -> None:
    started = await session.pty_exec_start("sleep 5", yield_time_s=0.3)
    assert started.process_id is not None
    with pytest.raises(RuntimeError, match="stdin is not available"):
        await session.pty_write_stdin(session_id=started.process_id, chars="x")


async def test_output_is_truncated_by_tokens(session: SandboxSession) -> None:
    update = await session.pty_exec_start("seq 1 20000", yield_time_s=5, max_output_tokens=50)
    assert update.exit_code == 0
    assert update.original_token_count is not None
    assert update.original_token_count > 50
    assert len(update.output) < 2000


async def test_terminate_all_kills_every_process_tree(session: SandboxSession) -> None:
    plain = await session.pty_exec_start("sleep 100", yield_time_s=0.3)
    tty = await session.pty_exec_start("sleep 100", tty=True, yield_time_s=0.3)
    assert plain.process_id is not None
    assert tty.process_id is not None
    leaders = [_leader(session, plain.process_id), _leader(session, tty.process_id)]
    dirs = [Path(e.dir) for e in session._inner._pty_processes.values()]
    assert all(_alive(pid) for pid in leaders)
    await session.pty_terminate_all()
    assert await _gone(leaders)
    assert not any(d.exists() for d in dirs)


async def test_stop_ends_running_processes(
    make_client: Callable[..., PrizedSandboxClient],
) -> None:
    session = await make_client().create()
    async with session:
        update = await session.pty_exec_start("sleep 100", yield_time_s=0.3)
        assert update.process_id is not None
        leader = _leader(session, update.process_id)
    assert await _gone([leader])


async def test_a_timeout_kills_the_process(session: SandboxSession) -> None:
    update = await session.pty_exec_start("sleep 30", timeout=1, yield_time_s=5)
    assert update.process_id is None
    assert update.exit_code == 137  # SIGKILL from `timeout -s KILL`


@pytest.mark.live
async def test_live_processes() -> None:
    from openai_agents_prized import PrizedSandboxClient, PrizedSandboxClientOptions  # noqa: PLC0415

    client = PrizedSandboxClient()
    options = PrizedSandboxClientOptions(
        vcpu=2,
        memory_mb=4096,
        max_runtime_min=15,
        labels={"test": "openai-agents-prized"},
    )
    session = await client.create(options=options)
    try:
        async with session:
            first = await session.pty_exec_start("echo a; sleep 2; echo b", yield_time_s=0.5)
            assert first.process_id is not None
            assert first.output == b"a\n"
            rest = await session.pty_write_stdin(session_id=first.process_id, chars="")
            assert (rest.exit_code, rest.output) == (0, b"b\n")

            tty = await session.pty_exec_start(
                "printf 'ready\\n'; while IFS= read -r l; do printf 'got:%s\\n' \"$l\"; done",
                tty=True,
                yield_time_s=1,
            )
            assert tty.process_id is not None
            assert tty.output.replace(b"\r\n", b"\n") == b"ready\n"
            echo = await session.pty_write_stdin(session_id=tty.process_id, chars="hi\n")
            assert echo.output.replace(b"\r\n", b"\n") == b"hi\ngot:hi\n"
            stop = await session.pty_write_stdin(
                session_id=tty.process_id, chars="\u0003", yield_time_s=5
            )
            assert stop.exit_code == 130

            left = await session.pty_exec_start("sleep 300", tty=True, yield_time_s=0.3)
            assert left.process_id is not None
            await session.pty_terminate_all()
            gone = await session.exec("pgrep -f '[s]leep 300' || echo none")
            assert gone.stdout == b"none\n"
    finally:
        await client.delete(session)
        await client.close()
