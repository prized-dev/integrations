"""Capability that supplies a Prized sandbox to an agent run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic_ai.capabilities import AbstractCapability, WrapRunHandler
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai_harness._workspace import innermost_backend
from pydantic_ai_harness._workspace_provider import (
    check_env,
    check_integer,
    check_provider,
    check_working_dir,
    reject_defer_loading,
)
from pydantic_ai_harness.prized_sandbox._backend import (
    DEFAULT_TEMPLATE,
    PROVIDER,
    PrizedSandboxBackend,
    destroy_sandbox,
)

if TYPE_CHECKING:
    from pydantic_ai.agent import AgentRunResult


class _SuppliedBackend(PrizedSandboxBackend):
    """The backend `PrizedSandbox` built for one run; the capability closes its handle when that run ends."""

    def __init__(self, *, run_id: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.run_id = run_id
        # Until a run claims it, nothing closes this backend: Temporal builds one per activity and
        # drops it, so its own handle closes after each operation instead.
        self._close_after_operation = True

    def claim(self, owned: bool) -> None:
        """Keep the handle open across operations while the run that closes it is in progress."""
        self._close_after_operation = not owned


@dataclass(kw_only=True)
class PrizedSandbox(AbstractCapability[AgentDepsT]):
    """Supply a [Prized](https://prized.dev) sandbox, a Firecracker microVM, as the run's workspace.

    A run with no reference creates a fresh sandbox on first use; pass a `WorkspaceRef` to attach to an
    existing one. Ending a run does not delete the sandbox: it pauses itself after `auto_pause_min` idle
    minutes with its memory and running processes, and the next command resumes it where it stopped.
    `pause(ref)` pauses it at once, `fork(ref)` copies it live, and `destroy(ref)` deletes it.
    Commands run under `bash` as the sandbox's non-root `prized` user.

    This capability supplies the workspace only. Compose it with capabilities that use it, such as
    `Coder`, `Shell`, and `FileSystem`. See
    [Workspaces](https://pydantic.dev/docs/ai/core-concepts/workspace/) for more.
    """

    template: str = DEFAULT_TEMPLATE
    """Template a newly created sandbox starts from: `prized-base-v1` (Ubuntu 24.04 with Python, Node and
    git) or `prized-agent-v1` (adds Claude Code and Codex). An unknown template raises
    `WorkspaceUnavailableError` on first use."""

    vcpu: int | None = None
    """vCPUs of a newly created sandbox; the template's default when `None` (2 on Prized's free tier)."""

    memory_mb: int | None = None
    """Memory of a newly created sandbox in MiB; the template's default when `None` (4096 on the free tier)."""

    disk_gb: int | None = None
    """Disk of a newly created sandbox in GiB; the template's default when `None`."""

    auto_pause_min: int | None = 10
    """Idle minutes before a newly created sandbox pauses itself, memory and processes kept; the next
    command resumes it. `0` turns it off, and `None` keeps Prized's default (off)."""

    auto_delete_min: int | None = None
    """Minutes paused before a newly created sandbox deletes itself; `None` keeps Prized's default (never)."""

    max_runtime_min: int | None = None
    """Minutes of running before a newly created sandbox is deleted; Prized's default when `None`
    (24 hours; 60 minutes on the free tier)."""

    working_dir: str | None = None
    """Absolute directory commands start in and relative paths resolve against; `None` uses the
    sandbox's own (`/home/prized/work`). Created on a new sandbox; an attached sandbox must already have it."""

    env: Mapping[str, str] | None = field(default=None, repr=False)
    """Environment variables every command gets, also on an attached sandbox; a command's `env` is
    layered on top. Nothing is read from the host."""

    labels: Mapping[str, str] | None = None
    """Labels for a newly created sandbox, to find it later; `created-by=pydantic-ai` is always added."""

    api_key: str | None = field(default=None, repr=False)
    """A Prized API key; when `None`, `PRIZED_API_KEY` from the environment."""

    base_url: str | None = None
    """The Prized API; Prized's own when `None`."""

    edge_url: str | None = None
    """The command endpoint; the one the API names for each sandbox when `None`."""

    def __post_init__(self) -> None:
        reject_defer_loading('PrizedSandbox', self.defer_loading)
        # Checked here rather than at the first workspace operation, so a bad value fails where it is written.
        check_working_dir(self.working_dir)
        check_integer('vcpu', self.vcpu, optional=True)
        check_integer('memory_mb', self.memory_mb, optional=True)
        check_integer('disk_gb', self.disk_gb, optional=True)
        check_integer('auto_pause_min', self.auto_pause_min, minimum=0, optional=True)
        check_integer('auto_delete_min', self.auto_delete_min, minimum=0, optional=True)
        check_integer('max_runtime_min', self.max_runtime_min, optional=True)
        check_env(self.env)

    @classmethod
    def from_spec(
        cls,
        *,
        id: str | None = None,
        description: str | None = None,
        defer_loading: bool = False,
        template: str = DEFAULT_TEMPLATE,
        vcpu: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        auto_pause_min: int | None = 10,
        auto_delete_min: int | None = None,
        max_runtime_min: int | None = None,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> PrizedSandbox[Any]:
        """Build from an agent spec, naming the fields a spec can express (credentials stay out of specs)."""
        return cls(
            id=id,
            description=description,
            defer_loading=defer_loading,
            template=template,
            vcpu=vcpu,
            memory_mb=memory_mb,
            disk_gb=disk_gb,
            auto_pause_min=auto_pause_min,
            auto_delete_min=auto_delete_min,
            max_runtime_min=max_runtime_min,
            working_dir=working_dir,
            env=env,
            labels=labels,
        )

    def backend(self, ref: WorkspaceRef) -> PrizedSandboxBackend:
        """Construct a lazy backend for an existing sandbox."""
        return PrizedSandboxBackend(
            ref=ref,
            working_dir=self.working_dir,
            env=self.env,
            api_key=self.api_key,
            base_url=self.base_url,
            edge_url=self.edge_url,
        )

    async def destroy(self, ref: WorkspaceRef) -> None:
        """Delete a sandbox by id, whatever state it is in, without resuming it.

        A sandbox that no longer exists is already gone, so destroying it returns quietly.
        """
        check_provider(ref, PROVIDER)
        await destroy_sandbox(ref.id, api_key=self.api_key, base_url=self.base_url, edge_url=self.edge_url)

    async def pause(self, ref: WorkspaceRef) -> None:
        """Pause a sandbox now, with its memory and running processes; no compute is charged while it is paused.

        The next run that attaches to it, or any command, resumes it where it stopped.
        """
        backend = self.backend(ref)
        try:
            await backend.pause()
        finally:
            await backend.aclose()

    async def fork(self, ref: WorkspaceRef) -> WorkspaceRef:
        """Copy a sandbox live into a new sandbox and return the copy's ref.

        The copy has the sandbox's files, memory and running processes as they are now, and the two change
        independently after: pass its ref as `workspace=` to run an agent in the copy, and `destroy` it when
        you are done. A paused sandbox is copied as it was paused and stays paused.
        """
        backend = self.backend(ref)
        try:
            return await backend.fork()
        finally:
            await backend.aclose()

    def get_workspace(self, ctx: RunContext[AgentDepsT], *, ref: WorkspaceRef | None) -> WorkspaceBackend | None:
        """Build the backend for this run. No I/O here: it attaches or creates on first use."""
        if ref is not None and ref.provider != PROVIDER:
            return None
        return _SuppliedBackend(
            run_id=ctx.run_id,
            ref=ref,
            template=self.template,
            vcpu=self.vcpu,
            memory_mb=self.memory_mb,
            disk_gb=self.disk_gb,
            working_dir=self.working_dir,
            env=self.env,
            labels=self.labels,
            auto_pause_min=self.auto_pause_min,
            auto_delete_min=self.auto_delete_min,
            max_runtime_min=self.max_runtime_min,
            api_key=self.api_key,
            base_url=self.base_url,
            edge_url=self.edge_url,
        )

    async def wrap_run(self, ctx: RunContext[AgentDepsT], *, handler: WrapRunHandler) -> AgentRunResult[Any]:
        # Only the backend this run built: one passed in as `workspace=`, such as a parent run's
        # handed to a subagent, belongs to the run that built it and may still be in use.
        backend = innermost_backend(ctx.workspace)
        owned = backend if isinstance(backend, _SuppliedBackend) and backend.run_id == ctx.run_id else None
        if owned is not None:
            owned.claim(True)
        try:
            return await handler()
        finally:
            if owned is not None:
                # `result.workspace` outlives the run, with no one left to close it.
                owned.claim(False)
                await owned.aclose()
