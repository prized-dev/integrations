"""The public `Runner` path, as the Agents SDK's provider guide asks a provider to test it.

openai-agents-python's docs/sandbox/custom-providers.md ("Package and test the
integration") lists the runs to make with `ScriptedModel`: fresh
client-owned sessions, application-owned live sessions, and a persisted
`Runner` resume (tests/lifecycle.py has those two), and cleanup after success,
a tool error, a timeout, a cancellation, and a partial startup. Each test here
runs one of them against the fake API and ends with no sandbox left running.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from agents import RunConfig, Runner, function_tool
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.errors import WorkspaceStartError
from agents.testing import ScriptedModel, assistant_message, function_call

from openai_agents_prized import PrizedError, PrizedSandboxClientOptions, PrizedSandboxSession
from tests.conftest import settle

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agents.tool import Tool

    from openai_agents_prized import PrizedSandboxClient
    from tests.fake_prized import FakePrized


def _agent(turns: Sequence[list[object]], *, tools: Sequence[Tool] = ()) -> SandboxAgent:
    return SandboxAgent(
        name="coder",
        model=ScriptedModel([list(turn) for turn in turns]),
        instructions="Work in the sandbox.",
        tools=list(tools),
    )


def _shell(cmd: str, call_id: str) -> list[object]:
    return [function_call("exec_command", {"cmd": cmd}, call_id=call_id)]


def _run_config(client: PrizedSandboxClient, **options: object) -> RunConfig:
    return RunConfig(
        sandbox=SandboxRunConfig(client=client, options=PrizedSandboxClientOptions(**options))
    )


async def test_a_client_owned_run_deletes_its_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    agent = _agent([_shell("echo hi > hi.txt && cat hi.txt", "c1"), [assistant_message("done")]])
    result = await Runner.run(agent, "say hi", run_config=_run_config(client))
    assert result.final_output == "done"
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


async def test_an_application_owned_session_outlives_its_runs(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    """The README's pattern: the application creates, enters, and deletes the session."""
    client = make_client()
    session = await client.create(options=PrizedSandboxClientOptions(vcpu=2))
    try:
        async with session:
            config = RunConfig(sandbox=SandboxRunConfig(session=session))
            first = _agent([_shell("echo kept > kept.txt", "c1"), [assistant_message("one")]])
            assert (await Runner.run(first, "write", run_config=config)).final_output == "one"
            # The runner never released the application's sandbox.
            inner = session._inner
            assert isinstance(inner, PrizedSandboxSession)
            sandbox_id = inner.sandbox_id
            assert fake.live() == [sandbox_id]
            second = _agent([_shell("cat kept.txt", "c2"), [assistant_message("two")]])
            result = await Runner.run(second, "read", run_config=config)
            assert result.final_output == "two"
            assert "kept" in str(result.new_items)
            assert fake.live() == [sandbox_id]
    finally:
        await client.delete(session)
    assert fake.live() == []
    assert len(fake.calls("DELETE", sandbox_id)) == 1


async def test_a_tool_error_deletes_the_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    @function_tool(failure_error_function=None)
    def broken() -> str:
        msg = "the tool broke"
        raise RuntimeError(msg)

    client = make_client()
    agent = _agent([[function_call("broken", {}, call_id="c1")]], tools=[broken])
    with pytest.raises(Exception, match="the tool broke"):
        await Runner.run(agent, "use the tool", run_config=_run_config(client))
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


async def test_a_run_that_times_out_deletes_the_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    """A command still running in the sandbox, then a tool that never returns, under a deadline."""

    @function_tool
    async def wait_forever() -> str:
        await asyncio.Event().wait()
        return "never"

    client = make_client()
    agent = _agent(
        [
            [
                function_call(
                    "exec_command", {"cmd": "sleep 600", "yield_time_ms": 200}, call_id="c1"
                )
            ],
            [function_call("wait_forever", {}, call_id="c2")],
        ],
        tools=[wait_forever],
    )
    run = Runner.run(agent, "wait", run_config=_run_config(client))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(run, timeout=2)
    await settle(client)
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


async def test_a_cancelled_run_deletes_the_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    started = asyncio.Event()

    @function_tool
    async def wait_forever() -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    client = make_client()
    agent = _agent([[function_call("wait_forever", {}, call_id="c1")]], tools=[wait_forever])
    task = asyncio.create_task(Runner.run(agent, "wait", run_config=_run_config(client)))
    await started.wait()
    assert len(fake.live()) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.close()  # waits for cleanup the cancellation left running
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


async def test_a_sandbox_that_never_boots_is_deleted(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.boot_fails = True
    fake.start_polls = 12
    agent = _agent([[assistant_message("never")]])
    with pytest.raises(PrizedError, match="state error"):
        await Runner.run(agent, "start", run_config=_run_config(make_client()))
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


async def test_a_session_that_fails_to_start_is_deleted(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    """The sandbox runs, then the session's start fails: the runner still releases it."""
    client = make_client()
    agent = _agent([[assistant_message("never")]])
    config = RunConfig(
        sandbox=SandboxRunConfig(
            client=client,
            manifest=Manifest(root="/proc/nope"),
            options=PrizedSandboxClientOptions(),
        )
    )
    with pytest.raises(WorkspaceStartError, match="cannot create the workspace root"):
        await Runner.run(agent, "start", run_config=config)
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


async def test_pause_on_exit_keeps_the_sandbox_paused_after_a_run(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    agent = _agent([_shell("echo kept > kept.txt", "c1"), [assistant_message("done")]])
    result = await Runner.run(agent, "write", run_config=_run_config(client, pause_on_exit=True))
    assert result.final_output == "done"
    (sandbox,) = fake.sandboxes.values()
    assert sandbox.state == "paused"
    assert fake.calls("DELETE", sandbox.id) == []
