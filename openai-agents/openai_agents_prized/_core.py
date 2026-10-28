"""What every part of a Prized session shares: its sandbox, and running commands there.

No request the session sends to the edge wakes the sandbox by itself: each
goes with `Prized-Auto-Resume: false`. One that lands late (a cancelled
command still on the wire, or a retry) after the session paused the sandbox
answers `409 sandbox_not_running`, and the sandbox stays paused. When a
request finds the sandbox paused while the session still holds it (it paused
after `auto_pause_min`), the session resumes it through the control plane,
one tracked resume shared by every request waiting on it, and sends the
request again. Once the session starts to release the sandbox, nothing
resumes it, and the release waits for a resume already on its way.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shlex
import uuid
from typing import TYPE_CHECKING, Any, TypeVar

from agents.sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecTimeoutError,
    ExecTransportError,
    SandboxError,
    WorkspaceStartError,
)
from agents.sandbox.session import BaseSandboxSession
from agents.sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER
from agents.sandbox.types import ExecResult
from agents.sandbox.workspace_paths import sandbox_path_str

from openai_agents_prized._api import (
    EDGE_MAX_TIMEOUT,
    EXEC_UNDELIVERED,
    NEVER_RETRY,
    AsyncPrizedClient,
    PrizedAPIError,
    PrizedError,
    outcome_unknown,
)
from openai_agents_prized._lifecycle import wake as wake_sandbox
from openai_agents_prized._options import (
    PRIZED_HOME,
    PRIZED_USER,
    PrizedSandboxSessionState,
    check_manifest,
)
from openai_agents_prized._wire import helper_effect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
    from pathlib import Path

    from agents.sandbox.materialization import MaterializationResult
    from agents.sandbox.session.runtime_helpers import RuntimeHelperScript

    from openai_agents_prized._pty import PtyProcess

logger = logging.getLogger("openai_agents_prized")

STAGE_ROOT = f"{PRIZED_HOME}/.cache/openai-agents-prized"
"""Where transfers are staged: under the home, so the file API reaches them."""

_WAKES = 2
"""How often one request may have the session resume its sandbox."""

_T = TypeVar("_T")


def under_home(path: str) -> bool:
    """Return whether the file API can reach `path` (it serves `/home/prized` only).

    Args:
        path: An absolute sandbox path.

    Returns:
        `True` for paths under `/home/prized`.
    """
    return path.startswith(f"{PRIZED_HOME}/")


def api_retryable(error: PrizedAPIError, *, idempotent: bool) -> bool | None:
    """The SDK's `retryable` hint for a failed request.

    Args:
        error: The failure.
        idempotent: Whether doing the operation twice is harmless (a read,
            or a write of the same bytes). A command is not.

    Returns:
        `True` when a retry is safe, `False` when it would fail the same way,
        `None` when the command may have run and only the caller can judge.
    """
    if error.code in NEVER_RETRY:
        return False
    if error.code in EXEC_UNDELIVERED or error.status == 429:  # noqa: PLR2004
        return True  # turned away before anything ran
    if outcome_unknown(error):
        return True if idempotent else None
    return False


def api_context(error: PrizedAPIError, **extra: object) -> dict[str, object]:
    """The error context the SDK's errors carry for a failed request.

    Args:
        error: The failure.
        **extra: More context.

    Returns:
        The context.
    """
    context: dict[str, object] = {"backend": "prized", "provider_error_code": error.code}
    if error.status is not None:
        context["http_status"] = error.status
    context["provider_error"] = str(error)
    context.update(extra)
    return context


def _edge_timeout(timeout: float | None, unbounded: int) -> int:
    if timeout is None:
        return unbounded
    return max(1, min(EDGE_MAX_TIMEOUT, math.ceil(timeout)))


def _text(value: object) -> bytes:
    return value.encode("utf-8", errors="replace") if isinstance(value, str) else b""


class PrizedSessionBase(BaseSandboxSession):
    """One Prized sandbox, and the commands that run in it."""

    state: PrizedSandboxSessionState  # ty: ignore[invalid-mutable-override]  # as built-ins do

    def __init__(
        self,
        *,
        state: PrizedSandboxSessionState,
        client: AsyncPrizedClient,
        toolbox: str,
    ) -> None:
        """Create a session for a sandbox that is already running.

        Args:
            state: The session state.
            client: The API client of the `PrizedSandboxClient` that made it.
            toolbox: The sandbox's edge base URL.
        """
        self.state = state
        self._client = client
        self._toolbox = toolbox
        self._workspace_root_ready = state.workspace_root_ready
        self._backend_closed = False
        self._release_lock = asyncio.Lock()
        self._pty_lock = asyncio.Lock()
        self._pty_processes: dict[int, PtyProcess] = {}
        self._reserved_pty_process_ids: set[int] = set()
        # Processes whose start or kill has no confirmed outcome, by
        # directory: `pty_terminate_all` ends them.
        self._pty_unconfirmed: dict[str, PtyProcess] = {}
        # Set once the session starts to release its sandbox: from then on
        # nothing resumes it.
        self._releasing = False
        # The resume a request that found the sandbox paused started, shared
        # by every request that finds it paused meanwhile.
        self._wake_lock = asyncio.Lock()
        self._wake_flight: asyncio.Future[str] | None = None

    @property
    def sandbox_id(self) -> str:
        """The Prized sandbox's id (`sbx_...`)."""
        return self.state.sandbox_id

    # --- workspace setup -----------------------------------------------------

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        return self.state.sandbox_id

    def _mark_workspace_root_ready_from_probe(self) -> None:
        super()._mark_workspace_root_ready_from_probe()
        self._workspace_root_ready = True

    async def _prepare_backend_workspace(self) -> None:
        if self._workspace_state_preserved_on_start() and self._workspace_root_ready:
            return
        root = self._workspace_root_path()
        try:
            result = await self._run(
                ["mkdir", "-p", "--", sandbox_path_str(root)],
                timeout=self.state.timeouts.fast_op_s,
                cwd=PRIZED_HOME,
            )
        except SandboxError as exc:
            raise WorkspaceStartError(
                path=root,
                context={"backend": "prized", "reason": "workspace_root_setup_failed"},
                cause=exc,
            ) from exc
        if not result.ok():
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise WorkspaceStartError(
                path=root,
                context={
                    "backend": "prized",
                    "reason": "workspace_root_nonzero_exit",
                    "exit_code": result.exit_code,
                    "stderr": stderr,
                },
                message=(
                    f"failed to start session: cannot create the workspace root {root} "
                    f"({stderr or result.exit_code}); the user prized can write under "
                    f"{PRIZED_HOME} and /tmp"
                ),
            )
        self._workspace_root_ready = True

    async def _apply_manifest(
        self,
        *,
        only_ephemeral: bool = False,
        provision_accounts: bool = True,
    ) -> MaterializationResult:
        # The user `prized` exists already, and no other account can be made.
        if provision_accounts and not only_ephemeral:
            check_manifest(self.state.manifest)
        return await super()._apply_manifest(
            only_ephemeral=only_ephemeral, provision_accounts=False
        )

    async def provision_manifest_accounts(self) -> None:
        """Check the manifest's accounts; `prized` is the only one, and it exists."""
        check_manifest(self.state.manifest)

    def _stage_path(self, kind: str) -> str:
        return f"{STAGE_ROOT}/{self.state.session_id.hex}-{uuid.uuid4().hex}-{kind}"

    async def _path_rules(self) -> tuple[str, str, list[str]]:
        """The SDK's path resolver in the sandbox, the workspace root, and the grants.

        In the order the resolver takes them: `helper ROOT PATH MODE [GRANT
        READ_ONLY]...`.
        """
        helper = await self._ensure_runtime_helper_installed(RESOLVE_WORKSPACE_PATH_HELPER)
        policy = self._workspace_path_policy()
        grants = [
            arg
            for grant_root, read_only in policy.extra_path_grant_rules()
            for arg in (grant_root.as_posix(), "1" if read_only else "0")
        ]
        return str(helper), policy.sandbox_root().as_posix(), grants

    # --- commands --------------------------------------------------------------

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        argv = self._without_run_as_prefix([str(part) for part in command])
        return await self._run(argv, timeout=timeout, error_command=command)

    def _without_run_as_prefix(self, argv: list[str]) -> list[str]:
        """Drop the `sudo -u <user> --` that `exec(user=...)` adds.

        There is no sudo in a Prized sandbox. `prized` is who every command runs
        as already; any other user cannot be switched to.
        """
        if len(argv) >= 4 and argv[0] == "sudo" and argv[1] == "-u" and argv[3] == "--":  # noqa: PLR2004
            if argv[2] == PRIZED_USER:
                return argv[4:]
            raise ConfigurationError(
                message=(
                    f"Prized sandboxes run every command as `{PRIZED_USER}`; "
                    f"running as `{argv[2]}` is not supported"
                ),
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="exec",
                context={"backend": "prized", "user": argv[2]},
            )
        return argv

    async def _resolved_envs(self) -> dict[str, str]:
        # The create's envVars apply to every command already; the manifest's
        # environment goes on top of them per request.
        return dict(await self.state.manifest.environment.resolve())

    async def _run(  # noqa: PLR0913  # one keyword per part of the request
        self,
        argv: Sequence[str],
        *,
        timeout: float | None,
        error_command: Sequence[str | Path] | None = None,
        cwd: str | None = None,
        extra_env: Mapping[str, str] | None = None,
        wake: bool = True,
    ) -> ExecResult:
        command = tuple(error_command) if error_command is not None else tuple(argv)
        edge_timeout = _edge_timeout(timeout, self.state.timeouts.exec_timeout_unbounded_s)
        if cwd is None:
            root_ready = self._workspace_root_ready
            cwd = sandbox_path_str(self._workspace_root_path()) if root_ready else PRIZED_HOME
        body: dict[str, Any] = {"command": shlex.join(argv), "cwd": cwd, "timeout": edge_timeout}
        env = await self._resolved_envs()
        env.update(extra_env or {})
        if env:
            body["env"] = env
        # A command of this package's own (no caller's command to name) gets
        # words that never speak of "the command" if its answer is lost.
        effect = helper_effect() if error_command is None else None
        call = self.on_edge(
            lambda: self._client.execute(
                self._toolbox, body, timeout=edge_timeout, wake=False, effect=effect
            ),
            wake=wake,
        )
        try:
            if timeout is None:
                answer = await call
            else:
                answer = await asyncio.wait_for(call, timeout=max(0.0, timeout))
        except asyncio.TimeoutError as exc:
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=exc) from exc
        except PrizedAPIError as exc:
            raise ExecTransportError(
                command=command,
                context=api_context(exc),
                cause=exc,
                message=f"Prized exec failed: {exc}",
                retryable=api_retryable(exc, idempotent=False),
            ) from exc
        if answer.get("timedOut"):
            raise ExecTimeoutError(
                command=command,
                timeout_s=timeout,
                context={"backend": "prized", "edge_timeout_s": edge_timeout},
            )
        return ExecResult(
            stdout=_text(answer.get("stdout")),
            stderr=_text(answer.get("stderr")),
            exit_code=int(answer["exitCode"]),
        )

    async def on_edge(self, send: Callable[[], Awaitable[_T]], *, wake: bool) -> _T:
        """Send an edge request that never wakes the sandbox by itself.

        `send` makes the request with `Prized-Auto-Resume: false`. With
        `wake`, a `409 sandbox_not_running` (nothing ran) has the session
        resume the sandbox it holds, then `send` goes again; without it, or
        once the session is releasing the sandbox, that answer is raised.

        Args:
            send: Makes the request, reading the edge URL when called.
            wake: Whether the session may resume its sandbox for it.

        Returns:
            What `send` returns.

        Raises:
            PrizedAPIError: If the request fails, or the resume is refused.
        """
        attempt = 0
        while True:
            try:
                return await send()
            except PrizedAPIError as exc:  # noqa: PERF203  # the wake-and-resend loop
                attempt += 1
                if not wake or exc.code != "sandbox_not_running" or attempt > _WAKES:
                    raise
                if not await self._wake():
                    raise

    async def _wake(self) -> bool:
        """Resume the session's sandbox for a request that found it paused.

        Returns:
            `False` when nothing was woken: the session is releasing the
            sandbox, or it is gone.
        """
        async with self._wake_lock:
            if self._releasing:
                return False
            flight = self._wake_flight
            if flight is None or flight.done():
                flight = self._client.background(
                    wake_sandbox(
                        self._client, self.state.sandbox_id, start_s=self.state.timeouts.start_s
                    )
                )
                self._wake_flight = flight
        try:
            self._toolbox = await asyncio.shield(flight)
        except PrizedAPIError:
            raise  # the resume's own refusal (balance, quota) is the answer
        except PrizedError as exc:
            logger.warning("Could not resume Prized sandbox %s: %s", self.state.sandbox_id, exc)
            return False
        return True

    async def _stop_waking(self) -> asyncio.Future[str] | None:
        """Let nothing resume the sandbox from now on; return a resume still on its way."""
        async with self._wake_lock:
            self._releasing = True
            flight = self._wake_flight
        return None if flight is None or flight.done() else flight

    async def _run_quietly(self, argv: Sequence[str]) -> bool:
        """Run a cleanup command; return whether it ran and exited 0.

        It never wakes the sandbox: cleanup that a cancellation left running
        can arrive after the session paused it, and must leave it paused.
        """
        try:
            result = await self._run(
                argv, timeout=self.state.timeouts.fast_op_s, cwd=PRIZED_HOME, wake=False
            )
        except Exception as exc:  # noqa: BLE001  # cleanup is best effort
            logger.debug("Prized cleanup command failed: %s", exc)
            return False
        return result.ok()

    async def _remove_stage(self, stage: str) -> None:
        await self._run_quietly(["rm", "-rf", "--", stage])

    async def _finish(self, cleanup: Coroutine[Any, Any, None]) -> None:
        """Run cleanup to the end, even if this task is cancelled meanwhile."""
        await self._client.finish(cleanup)


def ran_for_sure_not(error: BaseException) -> bool:
    """Whether a failed command is proven never to have started.

    Args:
        error: What the command raised.

    Returns:
        `True` only for an API answer that turned the command away; a lost
        answer, a timeout, or a cancellation may have let it run.
    """
    cause = error.__cause__
    if not isinstance(error, ExecTransportError) or not isinstance(cause, PrizedAPIError):
        return False
    return not outcome_unknown(cause)


__all__ = [
    "STAGE_ROOT",
    "PrizedSessionBase",
    "api_context",
    "api_retryable",
    "ran_for_sure_not",
    "under_home",
]
