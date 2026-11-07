"""One Prized sandbox per Claude Managed Agents session, found again by its labels.

- ``managed-by=prized-managed-agents`` and ``anthropic-environment=env_…`` on every sandbox this
  serves, and ``anthropic-session=sesn_…`` (the label ``on-work.sh`` uses too) for its session.
- ``prized-managed-agents-role=warm-parent`` on a prepared sandbox that sessions fork from.

Labels are the only state that matters across restarts: a restarted orchestrator, or a second one
on the same environment, finds the same sandboxes. Nothing here runs a command in a paused sandbox,
since a command would resume it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prized import AsyncSandbox, PrizedError

from .config import Settings, minutes

log = logging.getLogger("prized_managed_agents.sandboxes")

MANAGED_BY = "managed-by"
OWNER = "prized-managed-agents"
ENVIRONMENT = "anthropic-environment"
SESSION = "anthropic-session"
ROLE = "prized-managed-agents-role"
WARM_PARENT = "warm-parent"
LIVE = ("starting", "running", "pausing", "paused", "resizing")
# Codes that mean the sandbox will not come back; anything else may pass.
GONE = ("not_found", "sandbox_deleted")


def gone(err: BaseException) -> bool:
    return isinstance(err, PrizedError) and (err.code in GONE or err.status == 404)


def resumable(err: PrizedError) -> bool:
    """False when a resume can never work: the sandbox is gone or used up its run time. A quota,
    the balance, or no host free right now may pass, and a new sandbox would lose the files."""
    return not (gone(err) or err.reason == "max_runtime" or err.code == "sandbox_error")


@dataclass
class Ensured:
    sandbox: Any
    how: str  # running, resumed, forked or created
    fresh: bool  # made by this call, so it holds nothing of the session's yet


class SessionSandboxes:
    def __init__(
        self,
        settings: Settings,
        *,
        api: Any = AsyncSandbox,
        sleep: Callable[[float], Any] = asyncio.sleep,
        **connection: Any,
    ) -> None:
        self.s = settings
        self.api = api
        self.sleep = sleep
        self.connection = connection
        self.base = {**dict(settings.labels), MANAGED_BY: OWNER, ENVIRONMENT: settings.environment_id}

    def labels(self, session_id: str) -> dict[str, str]:
        return {**self.base, SESSION: session_id}

    async def find(self, session_id: str) -> Any | None:
        """The session's newest live sandbox, or None. Found by the session label alone, so a
        sandbox ``on-work.sh`` made for the session is found too."""
        found = [
            sb
            for sb in await self.api.list(labels={SESSION: session_id}, **self.connection)
            if sb.state in LIVE and (sb.labels or {}).get(ROLE) != WARM_PARENT
        ]
        if len(found) > 1:
            log.warning("%s: %d sandboxes carry its label; using the newest, %s", session_id, len(found), found[0].id)
        return found[0] if found else None

    async def managed(self) -> list[Any]:
        """Every live session sandbox this environment's orchestrator made (warm parents aside)."""
        return [
            sb
            for sb in await self.api.list(labels=self.base, **self.connection)
            if sb.state in LIVE and (sb.labels or {}).get(ROLE) != WARM_PARENT and (sb.labels or {}).get(SESSION)
        ]

    async def ensure(self, session_id: str, *, template: str | None = None, fork_from: str | None = None) -> Ensured:
        """A running sandbox for the session: its own, resumed with its files and processes; else a
        fork of a warm parent; else a new one."""
        sb = await self.find(session_id)
        if sb is not None:
            try:
                if sb.state in ("paused", "pausing"):
                    await sb.resume()
                    return Ensured(sb, "resumed", False)
                await self._wait_running(sb)
                return Ensured(sb, "running", False)
            except PrizedError as err:
                if resumable(err):
                    raise
                log.warning(
                    "%s: %s can't come back (%s%s); starting a new sandbox",
                    session_id,
                    sb.id,
                    err.code,
                    f", {err.reason}" if err.reason else "",
                )
        labels = self.labels(session_id)
        parent_id = fork_from or self.s.warm_parent
        if parent_id:
            child = await self._fork(parent_id, labels, required=bool(fork_from))
            if child is not None:
                return Ensured(child, "forked", True)
        sb = await self.api.create(
            template or self.s.template,
            vcpu=self.s.vcpu,
            memory_gb=self.s.memory_gb,
            disk_gb=self.s.disk_gb,
            labels=labels,
            max_runtime_min=minutes(self.s.max_runtime_s) if self.s.max_runtime_s else None,
            auto_pause_min=minutes(self.s.auto_pause_s),
            auto_delete_min=minutes(self.s.keep_paused_s) if self.s.keep_paused_s else 0,
            **self.connection,
        )
        return Ensured(sb, "created", True)

    async def _fork(self, parent_id: str, labels: dict[str, str], *, required: bool) -> Any | None:
        try:
            parent = await self.api.connect(parent_id, **self.connection)
            have = parent.labels or {}
            missing = [f"{k}={v}" for k, v in {**self.base, ROLE: WARM_PARENT}.items() if have.get(k) != v]
            if missing:
                # Only this environment's own warm parents (same labels as its sessions' sandboxes):
                # a session's metadata must not fork another environment's sandbox, or anything else.
                raise PermissionError(
                    f"{parent_id} is not a warm parent of this environment (it lacks {', '.join(missing)}); "
                    "make one with `prized-managed-agents prepare`"
                )
            return (await parent.fork(1, labels=labels))[0]
        except (PrizedError, PermissionError) as err:
            if required:
                raise
            log.warning("forking warm parent %s failed (%s); creating a sandbox instead", parent_id, err)
            return None

    async def _wait_running(self, sb: Any, timeout: float = 300.0) -> None:
        waited = 0.0
        while sb.state != "running":
            if sb.state not in LIVE or waited > timeout:
                raise PrizedError("sandbox_error", f"{sb.id} is {sb.state}, not running", sandbox_id=sb.id)
            await self.sleep(1.0)
            waited += 1.0
            await sb.refresh()

    async def pause(self, sb: Any) -> None:
        if sb.state not in ("paused", "pausing"):
            await sb.pause()

    async def delete_session(self, session_id: str) -> list[str]:
        """Delete every sandbox of the session (ours or on-work.sh's), never a warm parent."""
        deleted = []
        for sb in await self.api.list(labels={SESSION: session_id}, **self.connection):
            if (sb.labels or {}).get(ROLE) == WARM_PARENT or sb.state in ("deleting", "deleted"):
                continue
            try:
                await sb.delete()
                deleted.append(sb.id)
            except PrizedError as err:
                if not gone(err):
                    raise
        return deleted

    async def make_parent(self, prepare: Callable[[Any], Any], *, template: str | None = None) -> Any:
        """Create a sandbox, let ``prepare`` set it up, and pause it as a warm parent. Its copies
        inherit its auto-pause and auto-delete, so those are the sessions' settings."""
        sb = await self.api.create(
            template or self.s.template,
            vcpu=self.s.vcpu,
            memory_gb=self.s.memory_gb,
            disk_gb=self.s.disk_gb,
            labels={**self.base, ROLE: WARM_PARENT},
            max_runtime_min=minutes(self.s.max_runtime_s) if self.s.max_runtime_s else None,
            auto_pause_min=minutes(self.s.auto_pause_s),
            auto_delete_min=minutes(self.s.keep_paused_s) if self.s.keep_paused_s else 0,
            **self.connection,
        )
        try:
            await prepare(sb)
            await sb.pause()
        except BaseException:
            await asyncio.shield(sb.delete())
            raise
        return sb
