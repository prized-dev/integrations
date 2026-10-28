"""Create, resume, pause, and delete, against every way each can fail.

One test per row of the failure table in FAILURES.md: what
an operation made is undone, a cancellation does not stop the undoing, and
an answer that never came is settled by reading the sandbox, never guessed.
"""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING

import httpx
import pytest
from agents.sandbox.errors import ExecTransportError

from openai_agents_prized import (
    PrizedAPIError,
    PrizedError,
    PrizedSandboxClient,
    PrizedSandboxClientOptions,
    PrizedSandboxSession,
)
from openai_agents_prized._core import api_retryable
from tests.conftest import Gate, settle
from tests.fake_prized import API_KEY, API_ORIGIN

if TYPE_CHECKING:
    from collections.abc import Callable

    from agents.sandbox.session import SandboxSession

    from tests.fake_prized import FakeClock, FakePrized

CREATE = r"^/api/v1/sandboxes$"
EXEC = r"/process/execute$"
GONE = {"deleting", "deleted"}
PAUSE_ON_EXIT = PrizedSandboxClientOptions(pause_on_exit=True)


def _inner(session: SandboxSession) -> PrizedSandboxSession:
    inner = session._inner
    assert isinstance(inner, PrizedSandboxSession)
    return inner


def _id(session: SandboxSession) -> str:
    return _inner(session).sandbox_id


def _creates(fake: FakePrized) -> int:
    return len(fake.calls("POST", CREATE))


async def _paused(client: PrizedSandboxClient) -> SandboxSession:
    session = await client.create(options=PAUSE_ON_EXIT)
    async with session:
        pass
    return session


# --- create -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "lost", "posts"),
    [
        # Four tries (max_retries=3), every answer lost, then the recovery.
        ("timeout", httpx.ReadTimeout("lost"), 5),
        # A 201 that cannot be read is not retried; the recovery asks once.
        ("garbled", httpx.Response(201, content=b"<html>proxy</html>"), 2),
    ],
)
async def test_create_whose_answer_is_lost_deletes_what_it_made(
    make_client: Callable[..., PrizedSandboxClient],
    fake: FakePrized,
    answer: str,
    lost: httpx.Response | Exception,
    posts: int,
) -> None:
    fake.lose("POST", CREATE, times=posts - 1, answer=lost)
    with pytest.raises(PrizedAPIError):
        await make_client().create()
    calls = fake.calls("POST", CREATE)
    assert len(calls) == posts, answer
    # One key for the whole logical create, so the API made one sandbox...
    assert len({c.headers["idempotency-key"] for c in calls}) == 1
    assert len(fake.sandboxes) == 1
    # ...which the recovery found by asking again with that key, and deleted.
    assert fake.live() == []


async def test_create_refused_after_a_lost_try_deletes_what_that_try_made(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.lose("POST", CREATE)  # the first try makes the sandbox; its answer is lost
    # The retry is refused: the first try took the workspace's last slot.
    fake.script("POST", CREATE, httpx.Response(429, json={"error": "sandbox_quota_exceeded"}))
    with pytest.raises(PrizedAPIError) as caught:
        await make_client().create()
    assert caught.value.code == "sandbox_quota_exceeded"
    assert caught.value.uncertain  # the refusal does not undo the lost try
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


async def test_create_that_never_arrived_leaves_nothing(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.script("POST", CREATE, httpx.ConnectError("refused"), times=4)
    with pytest.raises(PrizedAPIError) as caught:
        await make_client().create()
    assert caught.value.code == "network_error"
    # The recovery's own request made one (an unknown outcome is asked about,
    # never assumed), and it was deleted.
    assert fake.live() == []


async def test_create_whose_sandbox_never_runs_deletes_it(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.start_polls = 10_000  # it stays `starting` past the wait
    with pytest.raises(PrizedError, match="still starting"):
        await make_client().create()
    assert len(fake.sandboxes) == 1
    assert fake.live() == []
    assert fake.commands() == []  # nothing was sent to its edge


async def test_create_cancelled_after_the_api_accepted_it_deletes_on_close(
    fake: FakePrized, gated: Callable[..., PrizedSandboxClient]
) -> None:
    gate = Gate(
        fake, lambda r: r.method == "POST" and r.url.path.endswith("/sandboxes"), after=True
    )
    client = gated(gate)
    task = asyncio.create_task(client.create())
    await gate.reached.wait()
    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # close() waits for the delete the cancellation left running.
    await client.close()
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


@pytest.mark.parametrize("held", ["create answer", "boot"])
def test_a_create_cancelled_under_asyncio_run_deletes_its_sandbox_without_close(
    fake: FakePrized, clock: FakeClock, held: str
) -> None:
    """The README's program, interrupted: `asyncio.run` ends right after the cancellation.

    A cancelled create waits for its cleanup, since `asyncio.run` cancels any
    task still running when `main` returns, and nobody calls `close()`. The
    transport takes a real network's few milliseconds, so the cleanup needs
    loop turns of its own.
    """
    if held == "boot":
        fake.start_polls = 12
        create = lambda r: r.method == "GET" and "/sandboxes/sbx_" in r.url.path  # noqa: E731
    else:
        create = lambda r: r.method == "POST" and r.url.path.endswith("/sandboxes")  # noqa: E731

    async def main() -> None:
        gate = Gate(fake, create, after=held != "boot")

        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.01)
            return await gate.handler(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        client = PrizedSandboxClient(api_key=API_KEY, api_url=API_ORIGIN, http_client=http)
        client._client.sleep = clock.sleep
        client._client.monotonic = clock.monotonic
        me = asyncio.current_task()
        assert me is not None

        async def interrupt() -> None:
            await gate.reached.wait()
            me.cancel()
            gate.release.set()

        watcher = asyncio.get_running_loop().create_task(interrupt())
        try:
            await client.create()
        finally:
            watcher.cancel()  # done already, unless create() failed before the gate

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main())
    assert len(fake.sandboxes) == 1
    assert fake.live() == []


async def test_create_cancelled_while_booting_deletes_it(
    fake: FakePrized, gated: Callable[..., PrizedSandboxClient]
) -> None:
    fake.start_polls = 12  # past the create's hold: the client reads it until it runs
    gate = Gate(fake, lambda r: r.method == "GET" and "/sandboxes/sbx_" in r.url.path)
    client = gated(gate)
    task = asyncio.create_task(client.create())
    await gate.reached.wait()
    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(client)
    assert fake.live() == []


# --- resume -------------------------------------------------------------------------


async def test_resume_that_wakes_a_sandbox_which_never_runs_pauses_it_again(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await _paused(client)
    sandbox_id = _id(session)
    fake.start_polls = 10_000  # the woken sandbox stays `starting` past the wait
    before = fake.commands()
    with pytest.raises(PrizedError, match="still starting"):
        await client.resume(session.state)
    assert fake.commands() == before  # nothing was sent to its edge
    assert fake.sandbox(sandbox_id).state == "paused"
    assert len(fake.calls("POST", rf"{sandbox_id}/pause$")) == 2  # the run's, and the undo
    assert _creates(fake) == 1  # a sandbox that is there is never replaced


async def test_resume_cancelled_after_the_wake_pauses_it_again(
    fake: FakePrized, gated: Callable[..., PrizedSandboxClient]
) -> None:
    # The first read after the resume landed: the sandbox is woken, still starting.
    gate = Gate(fake, lambda r: r.method == "GET" and bool(fake.calls("POST", r"/resume$")))
    client = gated(gate)
    session = await _paused(client)
    fake.start_polls = 10_000
    task = asyncio.create_task(client.resume(session.state))
    await gate.reached.wait()
    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(client)
    assert fake.sandbox(_id(session)).state == "paused"


async def test_resume_cancelled_in_flight_is_paused_after_it_lands(
    fake: FakePrized, gated: Callable[..., PrizedSandboxClient]
) -> None:
    gate = Gate(
        fake, lambda r: r.method == "POST" and r.url.path.endswith("/resume"), in_flight=True
    )
    gate.armed = False
    client = gated(gate)
    session = await _paused(client)
    gate.armed = True
    task = asyncio.create_task(client.resume(session.state))
    await gate.reached.wait()
    task.cancel()
    await asyncio.sleep(0.2)  # a pause sent now would find it still paused
    gate.release.set()  # the resume reaches the API only now, and wakes it
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(client)
    assert fake.sandbox(_id(session)).state == "paused"
    # The undo pause went out after the resume landed, not before.
    order = [r.path.rsplit("/", 1)[-1] for r in fake.requests if r.method == "POST"]
    assert order[-2:] == ["resume", "pause"]


async def test_resume_whose_answer_is_lost_reads_the_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await _paused(client)
    sandbox_id = _id(session)
    fake.lose("POST", rf"{sandbox_id}/resume$", times=4)  # every try woke it; no answer came
    resumed = await client.resume(session.state)
    assert _id(resumed) == sandbox_id
    async with resumed:
        assert (await resumed.exec("echo ok")).stdout == b"ok\n"
    assert _creates(fake) == 1


async def test_resume_refused_is_raised_and_nothing_is_replaced(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await _paused(client)
    sandbox_id = _id(session)
    fake.script(
        "POST",
        rf"{sandbox_id}/resume$",
        httpx.Response(409, json={"error": "insufficient_balance"}),
    )
    with pytest.raises(PrizedAPIError) as caught:
        await client.resume(session.state)
    assert caught.value.code == "insufficient_balance"
    assert fake.sandbox(sandbox_id).state == "paused"
    assert _creates(fake) == 1


# --- pause (pause_on_exit) ------------------------------------------------------------


async def test_pause_whose_answer_is_lost_never_deletes(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    fake.honor_wait = False  # a pause answers `pausing` at once
    client = make_client()
    session = await client.create(options=PAUSE_ON_EXIT)
    sandbox_id = _id(session)
    # Accepted (now `pausing`), answer lost; the resend is turned down with
    # 409 sandbox_not_running, which used to send the sandbox to a delete.
    fake.lose("POST", rf"{sandbox_id}/pause$")
    async with session:
        pass
    assert fake.calls("DELETE", sandbox_id) == []
    assert fake.sandbox(sandbox_id).state == "paused"
    await client.delete(session)  # released already: nothing more is sent
    assert fake.calls("DELETE", sandbox_id) == []


async def test_pause_with_an_unknown_outcome_deletes_nothing(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client(max_retries=0)
    session = await client.create(options=PAUSE_ON_EXIT)
    sandbox_id = _id(session)
    down = httpx.Response(503, json={"error": "control_plane_unavailable"})
    fake.script("POST", rf"{sandbox_id}/pause$", down, times=2)
    fake.script("GET", rf"/api/v1/sandboxes/{sandbox_id}$", down, times=2)
    async with session:
        pass  # the shutdown's pause fails and the sandbox cannot be read: logged
    with pytest.raises(PrizedAPIError):
        await client.delete(session)  # still unknown: raised, so the caller knows
    assert fake.calls("DELETE", sandbox_id) == []
    await client.delete(session)  # the API answers again
    assert fake.sandbox(sandbox_id).state == "paused"
    assert fake.calls("DELETE", sandbox_id) == []


async def test_pause_that_did_not_land_keeps_the_sandbox_and_raises(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PAUSE_ON_EXIT)
    sandbox_id = _id(session)
    await session.start()
    await session.write("notes.txt", io.BytesIO(b"saved nowhere else"))
    busy = httpx.Response(409, json={"error": "busy"})
    fake.script("POST", rf"{sandbox_id}/pause$", busy)
    # Read after the refusal: still running. Its workspace is in no snapshot
    # (the default snapshot saves nothing), so it is kept, not deleted.
    with pytest.raises(PrizedError, match="left running, not deleted"):
        await client.delete(session)
    assert fake.calls("DELETE", sandbox_id) == []
    assert fake.sandbox(sandbox_id).state == "running"
    assert not _inner(session)._backend_closed
    await client.delete(session)  # the next release tries again, and pauses
    assert fake.sandbox(sandbox_id).state == "paused"
    assert (fake.sandbox(sandbox_id).home / "work" / "notes.txt").read_bytes() == (
        b"saved nowhere else"
    )


async def test_pause_of_a_sandbox_in_error_deletes_it(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PAUSE_ON_EXIT)
    sandbox_id = _id(session)
    fake.sandbox(sandbox_id).state = "error"  # it can never run again
    await client.delete(session)
    assert fake.sandbox(sandbox_id).state in GONE


# --- waking (only a tracked resume wakes a sandbox) ----------------------------------------


def _no_wake(fake: FakePrized) -> bool:
    """Whether every request that reached the edge said not to wake the sandbox."""
    edge = [r for r in fake.requests if r.path.startswith("/v1/sandbox/")]
    return bool(edge) and all(r.headers.get("prized-auto-resume") == "false" for r in edge)


async def test_a_command_that_lands_after_the_pause_leaves_the_sandbox_paused(
    fake: FakePrized, gated: Callable[..., PrizedSandboxClient]
) -> None:
    def late(request: httpx.Request) -> bool:
        return request.url.path.endswith("/process/execute") and b"echo late" in request.content

    gate = Gate(fake, late, in_flight=True)
    client = gated(gate)
    session = await client.create(options=PAUSE_ON_EXIT)
    sandbox_id = _id(session)
    command = asyncio.create_task(session.exec("echo late"))
    await gate.reached.wait()  # on the wire
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    await client.delete(session)  # pause_on_exit: paused
    assert fake.sandbox(sandbox_id).state == "paused"
    gate.release.set()  # the command reaches the edge only now
    await asyncio.gather(*gate.arrivals)
    await settle(client)
    assert fake.sandbox(sandbox_id).state == "paused"
    assert _no_wake(fake)


async def test_a_command_resumes_a_sandbox_paused_under_the_session(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create()
    sandbox_id = _id(session)
    fake.sandbox(sandbox_id).state = "paused"  # auto_pause_min passed
    result = await session.exec("echo back")
    assert result.stdout == b"back\n"
    assert len(fake.calls("POST", rf"{sandbox_id}/resume$")) == 1
    assert _no_wake(fake)


async def test_a_released_session_never_resumes_its_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create(options=PAUSE_ON_EXIT)
    sandbox_id = _id(session)
    await client.delete(session)
    with pytest.raises(ExecTransportError):
        await session.exec("echo after")
    assert fake.calls("POST", rf"{sandbox_id}/resume$") == []
    assert fake.sandbox(sandbox_id).state == "paused"


async def test_a_release_waits_for_a_resume_already_on_its_way(
    fake: FakePrized, gated: Callable[..., PrizedSandboxClient]
) -> None:
    gate = Gate(
        fake, lambda r: r.method == "POST" and r.url.path.endswith("/resume"), in_flight=True
    )
    client = gated(gate)
    session = await client.create(options=PAUSE_ON_EXIT)
    sandbox_id = _id(session)
    fake.sandbox(sandbox_id).state = "paused"
    command = asyncio.create_task(session.exec("echo back"))
    await gate.reached.wait()  # the session's resume is on its way
    release = asyncio.create_task(client.delete(session))
    await asyncio.sleep(0.2)  # a pause sent now would find it still paused
    gate.release.set()
    await release
    await asyncio.gather(command, return_exceptions=True)
    await settle(client)
    assert fake.sandbox(sandbox_id).state == "paused"
    order = [r.path.rsplit("/", 1)[-1] for r in fake.requests if r.method == "POST"]
    assert order.index("resume") < order.index("pause")


# --- delete --------------------------------------------------------------------------


async def test_delete_whose_answer_is_lost_reads_the_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.create()
    sandbox_id = _id(session)
    fake.lose("DELETE", rf"/sandboxes/{sandbox_id}$", times=4)
    await client.delete(session)  # it landed: no error
    assert fake.sandbox(sandbox_id).state in GONE
    assert _inner(session)._backend_closed


async def test_delete_cancelled_midway_still_deletes(
    fake: FakePrized, gated: Callable[..., PrizedSandboxClient]
) -> None:
    gate = Gate(fake, lambda r: r.method == "DELETE", after=True)
    client = gated(gate)
    session = await client.create()
    task = asyncio.create_task(client.delete(session))
    await gate.reached.wait()
    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(client)
    assert fake.live() == []
    assert _inner(session)._backend_closed


# --- exec ------------------------------------------------------------------------------


def test_retry_hints_never_invite_a_second_run() -> None:
    lost = PrizedAPIError("timeout", "no answer")
    proxy = PrizedAPIError("http_error", "HTTP 503", status=503)
    not_ready = PrizedAPIError("sandbox_not_ready", "not ready", status=503)
    broke = PrizedAPIError("insufficient_balance", "no balance", status=409)
    # A command whose answer is lost may have run: only the caller can judge.
    assert api_retryable(lost, idempotent=False) is None
    assert api_retryable(proxy, idempotent=False) is None
    # A read or a write of the same bytes can simply be sent again.
    assert api_retryable(proxy, idempotent=True) is True
    # Answers that prove nothing ran, and refusals that will not pass.
    assert api_retryable(not_ready, idempotent=False) is True
    assert api_retryable(broke, idempotent=True) is False


async def test_exec_answered_by_a_proxy_503_is_not_marked_retryable(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    from agents.sandbox.errors import ExecTransportError  # noqa: PLC0415

    session = await make_client().create()
    async with session:
        fake.script("POST", EXEC, httpx.Response(503, text="<html>bad gateway</html>"))
        with pytest.raises(ExecTransportError) as caught:
            await session.exec("echo once")
        assert caught.value.retryable is None
