"""Creating, waking, pausing, and deleting sandboxes, with every failure undone.

Each operation either finishes or leaves nothing behind that its caller does
not know about, and an answer that never came is never read as a yes or a no:

- A create whose answer is lost, or that is cancelled, is asked again with
  its own `Idempotency-Key` (the API answers with the sandbox the first
  request made, and makes no second one), and that sandbox is deleted.
- A resume that woke a sandbox and then failed pauses it again. Only a
  resume request this module sends and tracks wakes a sandbox. A sandbox that
  reads `running` takes commands at once, so nothing is sent to its edge
  before the caller's own request.
- A pause or a delete that failed reads the sandbox before anything else is
  done: a pause that landed is never followed by a delete, and a delete
  counts as done only once the sandbox is `deleting`, `deleted`, or gone. A
  pause that did not land leaves the sandbox running and raises: it is never
  deleted in its place, since its workspace may be saved nowhere else.

Cleanup that must outlive a cancellation runs through
`AsyncPrizedClient.finish`, which `AsyncPrizedClient.aclose` waits for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from openai_agents_prized._api import (
    HOLD_MAX,
    AsyncPrizedClient,
    PrizedAPIError,
    PrizedError,
    PrizedSandboxGoneError,
    new_idempotency_key,
    outcome_unknown,
    toolbox_base_for,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger("openai_agents_prized")

GONE = frozenset({"deleting", "deleted"})
"""States of a sandbox that is on its way out, or out."""

PAUSED = frozenset({"paused", "pausing", "resizing"})
"""States of a sandbox that is paused or will be (a resize runs on a paused one)."""

UP = frozenset({"starting", "running"})
"""States of a sandbox that is awake or waking."""

RUNNING = frozenset({"running"})

_CREATE_REPLAYS = 5
"""How often a create's recovery asks again while the first request is still in flight."""


async def read_sandbox(client: AsyncPrizedClient, sandbox_id: str) -> dict[str, Any] | None:
    """Read a sandbox; `None` once it is deleted.

    Args:
        client: The API client.
        sandbox_id: The sandbox's id.

    Returns:
        The sandbox object, or `None` when the API answers `404 not_found`.

    Raises:
        PrizedAPIError: If the read failed, so the sandbox's state is unknown.
    """
    try:
        return await client.get_sandbox(sandbox_id)
    except PrizedAPIError as exc:
        if exc.code == "not_found":
            return None
        raise


async def wait_for_state(
    client: AsyncPrizedClient,
    sandbox_id: str,
    targets: frozenset[str],
    *,
    timeout: float,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """Read a sandbox until its state is one of `targets`.

    Args:
        client: The API client.
        sandbox_id: The sandbox's id.
        targets: The states to wait for, such as `{"running"}`.
        timeout: Seconds to wait before giving up.
        poll_interval: Seconds between reads.

    Returns:
        The sandbox object in one of `targets`.

    Raises:
        PrizedSandboxGoneError: If the sandbox goes to `error`, is deleted, or
            is no longer found.
        PrizedError: If the timeout passes first.
    """
    deadline = client.monotonic() + timeout
    while True:
        sandbox = await read_sandbox(client, sandbox_id)
        if sandbox is None:
            msg = f"Sandbox {sandbox_id} is gone"
            raise PrizedSandboxGoneError(msg)
        state = sandbox.get("state")
        if state in targets:
            return sandbox
        if state == "error":
            reason = sandbox.get("errorReason") or "no reason given"
            msg = f"Sandbox {sandbox_id} is in state error: {reason}"
            raise PrizedSandboxGoneError(msg)
        if state in GONE:
            msg = f"Sandbox {sandbox_id} is {state}"
            raise PrizedSandboxGoneError(msg)
        if client.monotonic() >= deadline:
            want = " or ".join(sorted(targets))
            msg = f"Sandbox {sandbox_id} is still {state} after {timeout:g} s (waiting for {want})"
            raise PrizedError(msg)
        await client.sleep(poll_interval)


async def create_running(
    client: AsyncPrizedClient, body: Mapping[str, Any], *, start_s: float
) -> tuple[dict[str, Any], str]:
    """Create a sandbox and wait until it is `running`, when it takes commands.

    Args:
        client: The API client.
        body: The create document.
        start_s: How long it may take to reach `running`.

    Returns:
        The sandbox object and its edge base URL.

    Raises:
        PrizedError: If the API refuses, or the sandbox does not come up. A
            sandbox the API made is deleted first, including after a lost
            answer or a cancellation.
    """
    key = new_idempotency_key()
    request = asyncio.ensure_future(client.create_sandbox(body, idempotency_key=key, hold=HOLD_MAX))
    try:
        sandbox = await asyncio.shield(request)
    except BaseException as exc:
        if isinstance(exc, PrizedAPIError) and not outcome_unknown(exc):
            raise  # turned down: nothing was made
        # Cancelled, or the answer never came: a sandbox may exist.
        await client.finish(_recover_create(client, request, body, key))
        raise
    sandbox_id = sandbox["id"]
    try:
        if sandbox.get("state") != "running":
            sandbox = await wait_for_state(client, sandbox_id, RUNNING, timeout=start_s)
        toolbox = toolbox_base_for(sandbox_id, sandbox.get("toolboxBase"), api_url=client.api_url)
    except BaseException:
        # Never leave a sandbox billing that no session owns.
        await client.finish(delete_quietly(client, sandbox_id))
        raise
    return sandbox, toolbox


async def _recover_create(
    client: AsyncPrizedClient,
    request: asyncio.Future[dict[str, Any]],
    body: Mapping[str, Any],
    key: str,
) -> None:
    """Find the sandbox a create with an unknown outcome made, and delete it."""
    sandbox: dict[str, Any] | None = None
    try:
        sandbox = await request  # a cancelled caller left the request running
    except PrizedAPIError as exc:
        if not outcome_unknown(exc):
            return
    except Exception:  # an unknown outcome, asked again below
        logger.debug("Prized create %s failed without an answer", key, exc_info=True)
    attempt = 0
    while sandbox is None:
        # The same key and body: the API answers with the sandbox the first
        # request made, and makes no second one. If the first never arrived,
        # this one makes it, and it is deleted below all the same.
        try:
            sandbox = await client.create_sandbox(body, idempotency_key=key)
        except PrizedAPIError as exc:  # noqa: PERF203  # the replay loop
            attempt += 1
            if exc.code == "idempotency_in_progress" and attempt < _CREATE_REPLAYS:
                await client.sleep(1.0)
                continue
            if outcome_unknown(exc) or exc.code == "idempotency_in_progress":
                logger.warning(
                    "Could not tell whether Prized create %s made a sandbox (%s); "
                    "list your sandboxes to check",
                    key,
                    exc,
                )
            return
    await delete_quietly(client, sandbox["id"])


async def delete_confirmed(client: AsyncPrizedClient, sandbox_id: str) -> None:
    """Delete a sandbox, and return only once it is deleting or gone.

    Args:
        client: The API client.
        sandbox_id: The sandbox's id.

    Raises:
        PrizedAPIError: If the API refused, or the answer was lost and the
            sandbox is not deleting (or could not be read).
    """
    try:
        await client.delete_sandbox(sandbox_id)
    except PrizedAPIError as exc:
        if not outcome_unknown(exc):
            raise
        # The answer was lost: whether the delete landed is the sandbox's state now.
        sandbox = await read_sandbox(client, sandbox_id)
        if sandbox is not None and sandbox.get("state") not in GONE:
            raise


async def delete_quietly(client: AsyncPrizedClient, sandbox_id: str) -> None:
    """`delete_confirmed`, logging a failure instead of raising it.

    Args:
        client: The API client.
        sandbox_id: The sandbox's id.
    """
    try:
        await delete_confirmed(client, sandbox_id)
    except Exception as exc:  # noqa: BLE001  # cleanup is best effort, and said so
        logger.warning("Could not delete Prized sandbox %s: %s", sandbox_id, exc)


async def pause_confirmed(client: AsyncPrizedClient, sandbox_id: str) -> str:
    """Pause a sandbox; return the state that says what happened.

    A pause that landed before its answer was lost shows `pausing` or
    `paused`, and its resend is turned down with `409 sandbox_not_running`:
    so on any failure the sandbox is read, and its state decides.

    Args:
        client: The API client.
        sandbox_id: The sandbox's id.

    Returns:
        `paused`, `pausing`, or `resizing` when the pause landed or will;
        `gone` when the sandbox is deleting or deleted; otherwise the state
        it stayed in (`running`, `starting`, `error`), so the pause did not
        land.

    Raises:
        PrizedAPIError: If the pause failed and the sandbox could not be read:
            whether it paused is unknown.
    """
    try:
        sandbox = await client.pause_sandbox(sandbox_id, hold=HOLD_MAX)
    except PrizedAPIError as exc:
        if exc.code == "not_found":
            return "gone"
        try:
            found = await read_sandbox(client, sandbox_id)
        except PrizedAPIError:
            raise exc from None
        if found is None:
            return "gone"
        state = str(found.get("state"))
        return "gone" if state in GONE else state
    # Accepted. A held answer can still show `running` or `starting` when the
    # hold ended early (a later resume turned it around): the pause landed
    # all the same, and what happens next is that caller's.
    state = str(sandbox.get("state"))
    if state in GONE:
        return "gone"
    return state if state in PAUSED or state == "error" else "pausing"


async def release(client: AsyncPrizedClient, sandbox_id: str, *, pause: bool) -> None:
    """End a session's hold on its sandbox: pause it, or delete it.

    A pause never falls back to a delete while the sandbox can still run:
    with `pause_on_exit` its workspace may be saved nowhere else (the SDK's
    default snapshot saves nothing, and `client.delete(session)` persists
    nothing first). A pause that did not land raises and leaves the sandbox
    running, so the caller can try again; its own `auto_pause_min` and
    `max_runtime_min` still apply. Only a sandbox in `error`, which can never
    run again, is deleted instead.

    Args:
        client: The API client.
        sandbox_id: The sandbox's id.
        pause: Pause instead of delete.

    Raises:
        PrizedError: If the sandbox may still be running.
    """
    if pause:
        state = await pause_confirmed(client, sandbox_id)
        if state in PAUSED or state == "gone":
            return
        if state != "error":
            msg = (
                f"Could not pause Prized sandbox {sandbox_id}: it is still {state}. It was "
                "left running, not deleted, so its workspace is kept; release it again"
            )
            raise PrizedError(msg)
        logger.warning("Prized sandbox %s is in error; deleting it instead of pausing", sandbox_id)
    await delete_confirmed(client, sandbox_id)


async def wake(client: AsyncPrizedClient, sandbox_id: str, *, start_s: float) -> str:
    """Bring a sandbox to `running`, when it takes commands, resuming it when it is paused.

    The resume request is the only thing that wakes it, and it is tracked: a
    sandbox this call woke is paused again when anything after the wake fails
    or is cancelled. One paused again after this returns is resumed by the
    session's next request (a `409 sandbox_not_running` on the edge).

    Args:
        client: The API client.
        sandbox_id: The sandbox's id.
        start_s: How long a resume may take to reach `running`.

    Returns:
        The edge base URL.

    Raises:
        PrizedSandboxGoneError: If the sandbox is deleted, deleting, or in
            `error`.
        PrizedError: If the sandbox could not be read or woken.
    """
    sandbox = await read_sandbox(client, sandbox_id)
    current = None if sandbox is None else sandbox.get("state")
    if sandbox is None or current in GONE or current == "error":
        msg = f"Sandbox {sandbox_id} is {current or 'gone'}"
        raise PrizedSandboxGoneError(msg)
    resume: asyncio.Future[dict[str, Any]] | None = None
    try:
        if current == "resizing":
            sandbox = await wait_for_state(
                client, sandbox_id, frozenset({"paused"}), timeout=start_s
            )
            current = "paused"
        if current in {"paused", "pausing"}:
            # Its own task, so a cancellation here cannot stop it halfway: a
            # resume already on its way would land after the undo below.
            resume = asyncio.ensure_future(_resume_confirmed(client, sandbox_id))
            sandbox = await asyncio.shield(resume)
        if sandbox.get("state") != "running":
            sandbox = await wait_for_state(client, sandbox_id, RUNNING, timeout=start_s)
        toolbox = toolbox_base_for(sandbox_id, sandbox.get("toolboxBase"), api_url=client.api_url)
    except PrizedSandboxGoneError:
        raise
    except BaseException:
        if resume is not None:
            await client.finish(_pause_again(client, sandbox_id, after=resume))
        raise
    return toolbox


async def reattach(client: AsyncPrizedClient, sandbox_id: str, *, start_s: float) -> str | None:
    """Bring a session's sandbox to `running` (`wake`).

    Args:
        client: The API client.
        sandbox_id: The sandbox's id.
        start_s: How long a resume may take to reach `running`.

    Returns:
        The edge base URL, or `None` when the sandbox is gone (deleted, or
        in `error`, which is deleted here) and a new one is needed.

    Raises:
        PrizedError: If the sandbox could not be read or woken: the caller
            should not replace a sandbox that may still be there.
    """
    try:
        return await wake(client, sandbox_id, start_s=start_s)
    except PrizedSandboxGoneError as exc:
        logger.info("Prized sandbox %s is gone (%s); creating a new one", sandbox_id, exc)
        await client.finish(delete_quietly(client, sandbox_id))
        return None


async def _resume_confirmed(client: AsyncPrizedClient, sandbox_id: str) -> dict[str, Any]:
    """Resume a sandbox; a lost answer is settled by reading it."""
    try:
        return await client.resume_sandbox(sandbox_id, hold=HOLD_MAX)
    except PrizedAPIError as exc:
        if exc.code == "not_found":
            raise PrizedSandboxGoneError(str(exc)) from exc
        if not outcome_unknown(exc):
            raise  # turned down (balance, quota): nothing woke
        sandbox = await read_sandbox(client, sandbox_id)
        state = None if sandbox is None else sandbox.get("state")
        if sandbox is None or state in GONE or state == "error":
            msg = f"Sandbox {sandbox_id} is {state or 'gone'}"
            raise PrizedSandboxGoneError(msg) from exc
        if state in UP:
            return sandbox
        raise


async def _pause_again(
    client: AsyncPrizedClient, sandbox_id: str, *, after: asyncio.Future[Any]
) -> None:
    """Pause a sandbox a failed resume woke, once that resume has settled.

    Log what could not be done.
    """
    with contextlib.suppress(Exception):
        await after  # whether it failed does not matter here, only that it landed
    try:
        state = await pause_confirmed(client, sandbox_id)
    except PrizedError as exc:
        logger.warning(
            "Prized sandbox %s may still be running after a failed resume: %s", sandbox_id, exc
        )
        return
    if state not in PAUSED and state != "gone":
        logger.warning(
            "Could not pause Prized sandbox %s again after a failed resume; it is %s",
            sandbox_id,
            state,
        )
