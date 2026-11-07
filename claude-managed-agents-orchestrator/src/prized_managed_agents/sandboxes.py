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
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


# Free-tier refusals that deleting a paused sandbox can lift: paused sandboxes hold disk, not slots.
ROOM_REASONS = ("retained_disk", "paused_limit")
MAX_EVICTIONS = 3
EVICT_WAIT_S = 30.0


def free_tier_refusal(err: BaseException) -> bool:
    return isinstance(err, PrizedError) and (err.body or {}).get("tier") == "free"


def idle_since(sb: Any) -> datetime | None:
    """When the sandbox was last used (its last command, file call or resume), in UTC; None when
    Prized didn't say, and then nothing is deleted for its age."""
    at = getattr(sb, "last_active_at", None)  # Prized's lastActiveAt; created_at would overstate its age
    if at is None:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def resumable(err: PrizedError) -> bool:
    """False when a resume can never work: the sandbox is gone or used up its run time. A quota,
    the balance, or no host free right now may pass, and a new sandbox would lose the files."""
    return not (gone(err) or err.reason == "max_runtime" or err.code == "sandbox_error")


@dataclass
class Ensured:
    sandbox: Any
    how: str  # running, resumed, forked or created
    fresh: bool  # made by this call, so it holds nothing of the session's yet
    # Paused sandboxes deleted to make room on the free tier: (sandbox id, its session, idle since).
    evicted: list[tuple[str, str, datetime | None]] = field(default_factory=list)


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
        # Sessions with a handoff under way (the orchestrator's), whose sandboxes are never evicted.
        self.busy: Callable[[], Collection[str]] = lambda: ()

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
        evicted: list[tuple[str, str, datetime | None]] = []
        while True:
            try:
                made = await self._new(session_id, template=template, fork_from=fork_from)
            except PrizedError as err:
                if len(evicted) >= MAX_EVICTIONS or not self._room_by_evicting(err):
                    raise
                victim = await self.evict_one(session_id)
                if victim is None:
                    raise
                evicted.append(victim)
                continue
            made.evicted = evicted
            return made

    def _room_by_evicting(self, err: PrizedError) -> bool:
        # Only a refusal Prized tags as the free tier's: paid quotas have the same reasons, and a
        # workspace can top up while the orchestrator runs.
        return (
            self.s.evict_paused
            and free_tier_refusal(err)
            and err.code == "sandbox_quota_exceeded"
            and err.reason in ROOM_REASONS
        )

    async def evict_one(self, keep_session: str) -> tuple[str, str, datetime | None] | None:
        """Delete the session sandbox of this environment paused longest (never a warm parent, the
        session being served, or one with a handoff or a run under way), wait until it is gone, and
        say which."""
        paused = [sb for sb in await self.managed() if sb.state == "paused"]
        latest = datetime.max.replace(tzinfo=timezone.utc)
        for sb in sorted(paused, key=lambda sb: idle_since(sb) or latest):
            sid = (sb.labels or {}).get(SESSION, "")
            # Checked again right before the delete, with no await in between: a handoff that began
            # while the list was read has marked its session busy by now.
            if sid == keep_session or sid in self.busy():
                continue
            try:
                # A deleted sandbox counts against the quota until it is gone, so wait for that.
                await sb.delete(wait=True, timeout=EVICT_WAIT_S)
            except PrizedError as err:
                if not gone(err):
                    raise
                continue
            log.warning("%s: deleted its paused sandbox %s to make room on the free tier", sid, sb.id)
            return sb.id, sid, idle_since(sb)
        return None

    async def _new(self, session_id: str, *, template: str | None, fork_from: str | None) -> Ensured:
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
            if required or (isinstance(err, PrizedError) and self._room_by_evicting(err)):
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
        """Create a sandbox, let ``prepare`` set it up, and pause it as a warm parent. It is kept until
        it is retired, or for WARM_PARENT_KEEP. Its copies inherit its auto-pause, which is the
        sessions' setting, and its auto-delete, so the orchestrator's sweep deletes a copy once it
        has been paused for KEEP_PAUSED."""
        keep = self.s.warm_parent_keep_s
        sb = await self.api.create(
            template or self.s.template,
            vcpu=self.s.vcpu,
            memory_gb=self.s.memory_gb,
            disk_gb=self.s.disk_gb,
            labels={**self.base, ROLE: WARM_PARENT},
            max_runtime_min=minutes(self.s.max_runtime_s) if self.s.max_runtime_s else None,
            auto_pause_min=minutes(self.s.auto_pause_s),
            auto_delete_min=minutes(keep) if keep else 0,
            **self.connection,
        )
        try:
            await prepare(sb)
            await sb.pause()
        except BaseException:
            await asyncio.shield(sb.delete())
            raise
        return sb

    async def parents(self) -> list[Any]:
        """This environment's warm parents."""
        return [
            sb
            for sb in await self.api.list(labels={**self.base, ROLE: WARM_PARENT}, **self.connection)
            if sb.state not in ("deleting", "deleted")
        ]

    async def retire_parent(self, parent_id: str) -> None:
        """Delete one of this environment's warm parents. Sessions forked from it keep their copies."""
        parent = await self.api.connect(parent_id, **self.connection)
        have = parent.labels or {}
        if any(have.get(k) != v for k, v in {**self.base, ROLE: WARM_PARENT}.items()):
            raise PermissionError(f"{parent_id} is not a warm parent of this environment; nothing was deleted")
        await parent.delete()
