"""Prized Sandboxes as a sandbox client for the OpenAI Agents SDK.

`PrizedSandboxClient` gives each sandbox session its own Prized sandbox, a
Linux microVM, created through the Sandbox API (https://prized.dev/docs/sandboxes-api).
The agent's commands run there as the user `prized` (no root) under
`bash -c`, its files move through the sandbox's file API, and the workspace
root defaults to `/home/prized/work`, where Prized commands run by default.

It follows the built-in hosted clients in `agents.extensions.sandbox`: the
client creates, resumes, and deletes sandboxes; the session implements
`exec`, `read`, `write`, `running`, `persist_workspace`, `hydrate_workspace`,
and the PTY calls behind `exec_command` and `write_stdin` (a process that
outlives one call runs in the background, with a TTY on request), and
validates every path against the workspace inside the sandbox. A paused
Prized sandbox keeps its memory and disk, so `pause_on_exit=True` lets a later
`resume()` continue in the same sandbox.

Not supported, because a Prized sandbox has no root and no inbound network:
manifest users other than `prized`, any manifest group, mounts, and exposed
ports.

The parts: `_options` (options and state), `_lifecycle` (create, resume,
pause, delete, each undone on failure), `_core` (commands), `_files`,
`_pty`, and `_scripts` (what runs in the sandbox).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import TYPE_CHECKING

from agents.sandbox.session import BaseSandboxClient, Instrumentation, SandboxSessionState
from agents.sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from typing_extensions import Self

from openai_agents_prized._api import (
    API_KEYS_URL,
    DEFAULT_API_URL,
    SIGN_UP_URL,
    AsyncPrizedClient,
    PrizedError,
    normalize_api_url,
)
from openai_agents_prized._files import PrizedFilesMixin
from openai_agents_prized._lifecycle import create_running, reattach, release
from openai_agents_prized._options import (
    DEFAULT_PRIZED_TEMPLATE,
    DEFAULT_PRIZED_WORKSPACE_ROOT,
    PrizedSandboxClientOptions,
    PrizedSandboxSessionState,
    PrizedSandboxTimeouts,
    check_manifest,
    coerce_timeouts,
    create_body,
    resolve_manifest_root,
)
from openai_agents_prized._pty import PrizedPtyMixin

if TYPE_CHECKING:
    import httpx
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.session import SandboxSession
    from agents.sandbox.session.dependencies import Dependencies

logger = logging.getLogger("openai_agents_prized")


class PrizedSandboxSession(PrizedPtyMixin, PrizedFilesMixin):
    """A sandbox session backed by one Prized sandbox."""

    @classmethod
    def from_state(
        cls,
        state: PrizedSandboxSessionState,
        *,
        client: AsyncPrizedClient,
        toolbox: str,
    ) -> PrizedSandboxSession:
        """Create a session from its state.

        Args:
            state: The session state.
            client: The API client of the `PrizedSandboxClient` that made it.
            toolbox: The sandbox's edge base URL.

        Returns:
            The session.
        """
        return cls(state=state, client=client, toolbox=toolbox)

    async def running(self) -> bool:
        """Return whether the sandbox is `running`; `False` when it cannot be read."""
        try:
            sandbox = await self._client.get_sandbox(self.state.sandbox_id)
        except PrizedError:
            return False
        return sandbox.get("state") == "running"

    async def _shutdown_backend(self) -> None:
        await self._release_backend(raise_errors=False)

    async def _release_backend(self, *, raise_errors: bool) -> None:
        """Pause the sandbox with `pause_on_exit`, else delete it; once.

        From here on nothing the session sends wakes the sandbox, and a resume
        already on its way lands before the pause or delete is sent. A
        cancelled caller leaves the release running to its end.
        """
        async with self._release_lock:
            if self._backend_closed:
                return
            waking = await self._stop_waking()
            work = self._client.background(self._release_after(waking))
            work.add_done_callback(self._note_released)
            try:
                await asyncio.shield(work)
            except PrizedError as exc:
                if raise_errors:
                    raise
                logger.warning(
                    "Could not release Prized sandbox %s: %s", self.state.sandbox_id, exc
                )

    async def _release_after(self, waking: asyncio.Future[str] | None) -> None:
        if waking is not None:
            await asyncio.wait({waking})  # landed or failed, either way it is settled
        await release(self._client, self.state.sandbox_id, pause=self.state.pause_on_exit)

    def _note_released(self, work: asyncio.Future[None]) -> None:
        if not work.cancelled() and work.exception() is None:
            self._backend_closed = True


class PrizedSandboxClient(BaseSandboxClient[PrizedSandboxClientOptions | None]):
    """Creates, resumes, and deletes Prized sandboxes for sandbox sessions."""

    backend_id = "prized"
    supports_default_options = True
    _instrumentation: Instrumentation

    def __init__(  # noqa: PLR0913  # keyword-only configuration
        self,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        """Create a client.

        Args:
            api_key: A Prized API key (`dcp_...`) with Sandboxes access;
                `PRIZED_API_KEY` when `None`.
            api_url: The API origin; `PRIZED_API_URL`, else
                `https://api.prized.dev`.
            http_client: An `httpx.AsyncClient` to send requests with; the
                client never closes one it was given.
            max_retries: Retries for requests that may safely be repeated.
            instrumentation: Session event sinks.
            dependencies: Session dependencies.

        Raises:
            ValueError: If there is no API key, or the API URL is not https.
        """
        key = api_key or os.environ.get("PRIZED_API_KEY")
        if not key:
            msg = (
                "Set PRIZED_API_KEY, or pass api_key. New to Prized? "
                f"{SIGN_UP_URL} signs you up and makes one; otherwise make one at {API_KEYS_URL}"
            )
            raise ValueError(msg)
        url = normalize_api_url(api_url or os.environ.get("PRIZED_API_URL") or DEFAULT_API_URL)
        self._client = AsyncPrizedClient(
            api_key=key, api_url=url, http_client=http_client, max_retries=max_retries
        )
        self._instrumentation = (
            instrumentation if instrumentation is not None else Instrumentation()
        )
        self._dependencies = dependencies

    async def close(self) -> None:
        """Wait for cleanup still running in the background, then close the HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        """Return the client."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the client."""
        await self.close()

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: PrizedSandboxClientOptions | dict[str, object] | None = None,
    ) -> SandboxSession:
        """Create a sandbox and a session for it.

        A sandbox that fails to come up is deleted, as is one whose create
        answer was lost or that was cancelled midway.

        Args:
            snapshot: The snapshot (or its spec) the session persists to.
            manifest: The workspace to materialize. A manifest on the SDK's
                default root (`/workspace`) uses `/home/prized/work` instead.
            options: The new sandbox's options; the defaults when `None`.

        Returns:
            The session; start it with `async with` or `start()`.
        """
        if options is None:
            options = PrizedSandboxClientOptions()
        elif not isinstance(options, PrizedSandboxClientOptions):
            options = PrizedSandboxClientOptions.model_validate(options)
        manifest = resolve_manifest_root(manifest)
        self._validate_manifest_for_create(manifest)
        check_manifest(manifest)
        session_id = uuid.uuid4()
        state = PrizedSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=resolve_snapshot(snapshot, str(session_id)),
            sandbox_id="",
            template=options.template,
            vcpu=options.vcpu,
            memory_mb=options.memory_mb,
            disk_gb=options.disk_gb,
            base_env_vars=dict(options.env_vars or {}),
            labels=dict(options.labels or {}),
            auto_pause_min=options.auto_pause_min,
            auto_delete_min=options.auto_delete_min,
            max_runtime_min=options.max_runtime_min,
            pause_on_exit=options.pause_on_exit,
            timeouts=coerce_timeouts(options.timeouts),
        )
        sandbox, toolbox = await create_running(
            self._client, create_body(state), start_s=state.timeouts.start_s
        )
        state.sandbox_id = sandbox["id"]
        inner = PrizedSandboxSession.from_state(state, client=self._client, toolbox=toolbox)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        """Release the session's sandbox: pause it with `pause_on_exit`, else delete it.

        Nothing is sent when the session's shutdown released it already.

        Args:
            session: A session this client made or resumed.

        Returns:
            The session.

        Raises:
            PrizedError: If the sandbox may still be running and billing: it
                could be neither paused nor deleted, or the answer was lost
                and the sandbox could not be read to tell.
        """
        inner = session._inner  # as the built-in clients do
        if not isinstance(inner, PrizedSandboxSession):
            msg = "PrizedSandboxClient.delete expects a PrizedSandboxSession"
            raise TypeError(msg)
        await inner._release_backend(raise_errors=True)
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        """Resume a session from its state.

        The sandbox the state names is used when it still exists (a paused one
        is resumed, with its files, and paused again if it then
        does not reach `running`); otherwise a new sandbox with the same options is
        created and the session restores the workspace from its snapshot when
        it starts. A sandbox that cannot be read is never replaced.

        Args:
            state: A `PrizedSandboxSessionState`.

        Returns:
            The session.
        """
        if not isinstance(state, PrizedSandboxSessionState):
            msg = "PrizedSandboxClient.resume expects a PrizedSandboxSessionState"
            raise TypeError(msg)
        state.assert_path_grants_rebound()
        check_manifest(state.manifest)
        toolbox = None
        if state.sandbox_id:
            toolbox = await reattach(self._client, state.sandbox_id, start_s=state.timeouts.start_s)
        reconnected = toolbox is not None
        if toolbox is None:
            sandbox, toolbox = await create_running(
                self._client, create_body(state), start_s=state.timeouts.start_s
            )
            state.sandbox_id = sandbox["id"]
            state.workspace_root_ready = False
        inner = PrizedSandboxSession.from_state(state, client=self._client, toolbox=toolbox)
        inner._set_start_state_preserved(reconnected, system=reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        """Read a session state serialized by `serialize_session_state`.

        Args:
            payload: The serialized state.

        Returns:
            The `PrizedSandboxSessionState`.
        """
        return self._deserialize_session_state_payload(payload, PrizedSandboxSessionState)


__all__ = [
    "DEFAULT_PRIZED_TEMPLATE",
    "DEFAULT_PRIZED_WORKSPACE_ROOT",
    "PrizedSandboxClient",
    "PrizedSandboxClientOptions",
    "PrizedSandboxSession",
    "PrizedSandboxSessionState",
    "PrizedSandboxTimeouts",
]
