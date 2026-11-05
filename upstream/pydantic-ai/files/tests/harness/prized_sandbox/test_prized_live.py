"""Integration tests that require a real Prized sandbox.

The fake-backed suites cover the harness-owned logic: deadline handling, protocol translation, and
exception mapping. This live tier admits only what a correctly written fake could not catch: real
process execution on Prized's edge, process-group stops over a real command, and real lifecycle state
(pause, resume and fork) in Prized's control plane.

Gating: the `prized_live` marker; skipped unless `PYDANTIC_AI_HARNESS_PRIZED_LIVE=1` and a non-empty
`PRIZED_API_KEY` (fails instead when `PRIZED_REQUIRE_LIVE=1`). See `conftest.py`.

Run locally:
`PYDANTIC_AI_HARNESS_PRIZED_LIVE=1 uv run pytest -m prized_live tests/harness/prized_sandbox/`
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
import pytest

from pydantic_ai.workspaces import WorkspaceTimeoutError, WorkspaceUnavailableError
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.prized_sandbox import PrizedSandbox, PrizedSandboxBackend

from .._tool_calls import call_tools
from .conftest import LIVE_AUTO_PAUSE_MIN, LIVE_LABELS, LIVE_MAX_RUNTIME_MIN, live_connection

pytestmark = [pytest.mark.prized_live, pytest.mark.anyio]

# A process that keeps counting into a file; a copy or a resumed sandbox that still has it keeps counting.
# Single-quoted inside, so the loop's `$i` is the inner shell's.
_TICKER = "nohup sh -c 'i=0; while true; do i=$((i+1)); echo $i > ticks; sleep 0.2; done' >/dev/null 2>&1 &"
_TICKING = 'a=$(cat ticks); sleep 1; b=$(cat ticks); [ "$b" -gt "$a" ] && echo ticking || echo stopped'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _capability() -> PrizedSandbox[None]:
    return PrizedSandbox(
        vcpu=2,
        memory_mb=4096,
        labels=LIVE_LABELS,
        auto_pause_min=LIVE_AUTO_PAUSE_MIN,
        max_runtime_min=LIVE_MAX_RUNTIME_MIN,
        **live_connection(),
    )


@asynccontextmanager
async def _owned() -> AsyncGenerator[PrizedSandboxBackend]:
    backend = PrizedSandboxBackend(
        vcpu=2,
        memory_mb=4096,
        labels=LIVE_LABELS,
        auto_pause_min=LIVE_AUTO_PAUSE_MIN,
        max_runtime_min=LIVE_MAX_RUNTIME_MIN,
        **live_connection(),
    )
    try:
        yield backend
    finally:
        try:
            if backend.ref is not None:
                await _capability().destroy(backend.ref)
        finally:
            await backend.aclose()


async def test_commands_get_a_usable_environment() -> None:
    """Validates the fake-encoded assumptions that commands run as `prized` in `/home/prized/work` with the
    image's `PATH`, and that a per-call `env` adds to it."""
    async with _owned() as backend:
        result = await backend.run(
            'whoami; pwd -P; echo "$PATH"; python3 --version; git --version', shell=True, timeout=60
        )
        user, cwd, path, python, git = result.stdout.splitlines()
        assert (user, cwd) == ('prized', '/home/prized/work')
        assert path and python.startswith('Python 3') and git.startswith('git version')
        assert await backend.working_dir() == '/home/prized/work'
        result = await backend.run('echo "$FOO"; echo "$PATH"', shell=True, env={'FOO': '1'}, timeout=60)
        assert result.stdout.splitlines() == ['1', path]


async def test_timeout_raises_with_the_partial_output_and_stops_the_command() -> None:
    """Validates that a timeout keeps what the command printed and that the command does not live on."""
    async with _owned() as backend:
        with pytest.raises(WorkspaceTimeoutError) as caught:
            await backend.run('echo started; sleep 8; touch finished', shell=True, timeout=2)
        assert 'started' in caught.value.stdout
        await anyio.sleep(8)
        assert (
            await backend.run('ls finished 2>/dev/null || echo absent', shell=True, timeout=60)
        ).stdout == 'absent\n'


async def test_cancelling_a_command_stops_its_process_group() -> None:
    """Validates the fake-encoded assumption that the edge's `bash -c` leads its own process group, so the
    stop script that follows a cancelled call reaches every process the command started."""
    async with _owned() as backend:
        await backend.run(['true'], timeout=60)
        with anyio.move_on_after(2):
            await backend.run('sleep 6 & sleep 6; touch finished', shell=True, timeout=60)
        await anyio.sleep(7)
        assert (
            await backend.run('ls finished 2>/dev/null || echo absent', shell=True, timeout=60)
        ).stdout == 'absent\n'


async def test_pause_keeps_memory_and_the_next_command_resumes_it() -> None:
    """What the fake can't show: a paused sandbox keeps its running processes, and a command resumes it."""
    capability = _capability()
    async with _owned() as backend:
        await backend.run(['sh', '-c', _TICKER], timeout=60)
        assert backend.ref is not None
        await capability.pause(backend.ref)
        await backend.aclose()  # its handle still says running; a fresh one reads the state
        probe = capability.backend(backend.ref)
        handle = await probe.get_sandbox()
        try:
            assert handle.state == 'paused'
        finally:
            await handle.close()
            await probe.aclose()
        resumed = capability.backend(backend.ref)
        try:
            assert (await resumed.run(['sh', '-c', _TICKING], timeout=60)).stdout == 'ticking\n'
        finally:
            await resumed.aclose()


async def test_fork_copies_files_and_running_processes_and_the_copies_diverge() -> None:
    """What the fake can't show: the copy starts with the sandbox's files and running processes, and the two
    change independently after."""
    capability = _capability()
    async with _owned() as source:
        await source.run(['sh', '-c', f'echo before > state.txt; {_TICKER}'], timeout=60)
        assert source.ref is not None
        ref = await capability.fork(source.ref)
        try:
            assert ref != source.ref
            copy = capability.backend(ref)
            assert (
                await copy.run(['sh', '-c', f'cat state.txt; {_TICKING}'], timeout=60)
            ).stdout == 'before\nticking\n'
            await copy.run(['sh', '-c', 'echo after > state.txt'], timeout=60)
            await copy.aclose()
            assert (await source.run(['cat', 'state.txt'], timeout=60)).stdout == 'before\n'
        finally:
            await capability.destroy(ref)


async def test_a_sandbox_deleted_during_a_command_is_unavailable() -> None:
    """Validates that a command in flight when its sandbox is deleted ends as unavailable, not as a slow command."""
    capability = _capability()
    async with _owned() as backend:
        await backend.run(['true'], timeout=60)
        assert backend.ref is not None
        ref = backend.ref

        async def delete_soon() -> None:
            await anyio.sleep(2)
            await capability.destroy(ref)

        async with anyio.create_task_group() as group:
            group.start_soon(delete_soon)
            with pytest.raises(WorkspaceUnavailableError):
                await backend.run(['sleep', '60'], timeout=120)


async def test_coder_tools_run_in_the_sandbox() -> None:
    """A file `Coder` writes through the shell is where a command started in the working directory finds it."""
    async with _owned() as backend:
        results = await call_tools(
            [Coder()],
            [
                ('write_file', {'path': 'hello.py', 'content': "print('hi from prized')\n"}),
                ('shell', {'command': 'python3 hello.py'}),
            ],
            workspace=backend,
        )
        assert 'hi from prized' in results[1]
