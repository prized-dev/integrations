"""Session machines: one Prized sandbox per session, kept with its memory while the session sleeps.

This is the protocol-agnostic core. A front end (the Devin Outposts orchestrator in
``prized_outposts.devin``; a Cursor Self-Hosted Machines spawn hook through the
``prized-machines`` command) names each session with a *key*, a label such as
``{"devin-session": "devin-123"}``, and asks for four things:

- ``ensure(key)``: a running sandbox for the session. A paused one is resumed with its memory and
  processes; otherwise a warm parent is forked, or a new sandbox is created.
- ``hibernate(key)``: pause it. Memory and disk are kept and no compute is billed.
- ``destroy(key)``: delete it.
- ``sweep(keep)``: delete the sandboxes of sessions that are gone.

Everything is found again by labels, so a restarted front end (or a second one) picks up the same
sandboxes. Nothing here ever runs a command in a paused sandbox, since that would resume it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from prized import PrizedError, Sandbox

log = logging.getLogger("prized_outposts.machines")

MANAGED_BY = "managed-by"
ROLE = "prized-outposts-role"
LIVE = ("starting", "running", "pausing", "paused", "resizing")
# Codes that mean "this sandbox will not come back"; anything else is worth another try later.
GONE = ("not_found", "sandbox_deleted")


@dataclass
class MachineSpec:
    """How the sandboxes for one pool are made."""

    template: str = "prized-base-v1"
    vcpu: int | None = None
    memory_gb: float | None = None
    disk_gb: int | None = None
    max_runtime_min: int | None = None
    # Our own status polls count as activity, so a served session never idles out. If the front
    # end stops polling (its machine went down), the sandbox pauses itself after this long.
    auto_pause_min: int = 15
    # How long a paused session waits for its next wake before Prized deletes it (0: forever).
    auto_delete_min: int = 7 * 24 * 60
    warm_parent: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class Ensured:
    sandbox: Any
    how: str  # "running", "resumed", "forked" or "created"
    seconds: float


class Machines:
    def __init__(
        self,
        spec: MachineSpec,
        *,
        owner: str,
        sandbox_api: Any = Sandbox,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        **connection: Any,
    ) -> None:
        self.spec = spec
        self.owner = owner
        self.api = sandbox_api
        self.clock = clock
        self.sleep = sleep
        self.connection = connection
        self.base = {MANAGED_BY: owner, **spec.labels}

    # -- finding -------------------------------------------------------------------------------

    def labels_for(self, key: Mapping[str, str]) -> dict[str, str]:
        return {**self.base, **key}

    def find(self, key: Mapping[str, str]) -> Any | None:
        """The session's sandbox, newest first, or None. Deleting and failed ones don't count."""
        found = [
            sb for sb in self.api.list(labels=self.labels_for(key), **self.connection) if sb.state in LIVE
        ]
        if len(found) > 1:
            log.warning("%d sandboxes carry %s; using the newest, %s", len(found), dict(key), found[0].id)
        return found[0] if found else None

    def managed(self) -> list[Any]:
        return [sb for sb in self.api.list(labels=self.base, **self.connection) if sb.state in LIVE]

    # -- lifecycle -----------------------------------------------------------------------------

    def ensure(self, key: Mapping[str, str], *, extra_labels: Mapping[str, str] | None = None) -> Ensured:
        start = self.clock()
        sb = self.find(key)
        if sb is not None:
            if sb.state in ("paused", "pausing"):
                sb.resume()
                return Ensured(sb, "resumed", self.clock() - start)
            self._wait_running(sb)  # `starting` or `resizing` settle by themselves
            return Ensured(sb, "running", self.clock() - start)
        labels = {**self.labels_for(key), **(extra_labels or {})}
        if self.spec.warm_parent:
            try:
                parent = self.api.connect(self.spec.warm_parent, **self.connection)
                child = parent.fork(1, labels=labels)[0]
                return Ensured(child, "forked", self.clock() - start)
            except PrizedError as err:
                log.warning(
                    "fork of warm parent %s failed (%s); creating instead", self.spec.warm_parent, err.code
                )
        sb = self.api.create(
            self.spec.template,
            vcpu=self.spec.vcpu,
            memory_gb=self.spec.memory_gb,
            disk_gb=self.spec.disk_gb,
            labels=labels,
            max_runtime_min=self.spec.max_runtime_min,
            auto_pause_min=self.spec.auto_pause_min,
            auto_delete_min=self.spec.auto_delete_min,
            **self.connection,
        )
        return Ensured(sb, "created", self.clock() - start)

    def _wait_running(self, sb: Any, timeout: float = 300.0) -> None:
        deadline = self.clock() + timeout
        while sb.state != "running":
            if sb.state not in LIVE or self.clock() > deadline:
                raise PrizedError(
                    code="sandbox_not_running",
                    message=f"{sb.id} is {sb.state}, not running",
                    sandbox_id=sb.id,
                )
            self.sleep(1.0)
            sb.refresh()

    def hibernate(self, key: Mapping[str, str]) -> Any | None:
        """Pause the session's sandbox (memory kept). Returns it, or None if there is none."""
        sb = self.find(key)
        if sb is None:
            return None
        if sb.state != "paused":
            sb.pause()
        return sb

    def destroy(self, key: Mapping[str, str]) -> list[str]:
        if not key:
            raise ValueError("destroy needs a session key")
        return list(self.api.delete_by_labels(self.labels_for(key), **self.connection))

    def sweep(self, keep: Callable[[Any], bool]) -> list[str]:
        """Delete managed session sandboxes that ``keep`` rejects. Warm parents are never swept."""
        deleted = []
        for sb in self.managed():
            if (sb.labels or {}).get(ROLE) == "warm-parent":
                continue
            if keep(sb):
                continue
            try:
                sb.delete()
                deleted.append(sb.id)
            except PrizedError as err:
                if err.code not in GONE:
                    log.warning("could not delete %s: %s", sb.id, err.code)
        return deleted

    # -- warm parents --------------------------------------------------------------------------

    def make_parent(self, prepare: Callable[[Any], None], *, labels: Mapping[str, str] | None = None) -> Any:
        """Create a sandbox, let ``prepare`` set it up, and pause it as a parent to fork sessions from."""
        sb = self.api.create(
            self.spec.template,
            vcpu=self.spec.vcpu,
            memory_gb=self.spec.memory_gb,
            disk_gb=self.spec.disk_gb,
            labels={**self.base, ROLE: "warm-parent", **(labels or {})},
            max_runtime_min=self.spec.max_runtime_min,
            # Children inherit these two, so they are the sessions' values.
            auto_pause_min=self.spec.auto_pause_min,
            auto_delete_min=self.spec.auto_delete_min,
            **self.connection,
        )
        try:
            prepare(sb)
            sb.pause()
        except BaseException:
            sb.delete()
            raise
        return sb


def label_pairs(values: Iterable[str]) -> dict[str, str]:
    """``["a=b", "c=d"]`` -> ``{"a": "b", "c": "d"}``; refuses an entry without ``=``."""
    out = {}
    for item in values:
        k, sep, v = item.partition("=")
        if not sep or not k:
            raise ValueError(f"expected key=value, got {item!r}")
        out[k] = v
    return out
