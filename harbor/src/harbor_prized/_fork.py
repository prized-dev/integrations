"""Fork mode for harbor_prized (``--ek fork_mode=true``): one paused parent per
task, and each trial of the task runs in a fork of it.

The first trial of a task that a later trial will need again builds the
parent: the same start as fresh mode, then the pull credentials are removed
and the sandbox is paused. No trial command ever runs in it. Each trial, the
builder included, forks one copy and runs in that copy, so every attempt
starts from the same post-start state and none sees another's. A copy is
deleted at the end of its trial, like a fresh sandbox.

Paused parents count toward the workspace's disk quota, so they are held to
a disk budget (``fork_disk_budget_gb``). Harbor writes its plan to the job's
``lock.json`` before any trial starts: every trial's task digest, in the order
the trials start. Each trial's own ``lock.json`` names its task. A parent is
built only if a later trial of its task is planned, and deleted once the last
of them has its copy. When the budget is full, the idle parent needed
farthest ahead in the plan makes room, and only for a task needed sooner.
Without a plan, the least recently used one makes room. A trial that gets no
parent, or whose fork is refused, starts fresh.

In Docker mode (``_docker.py``) the parent is the sandbox right after the
build and pulls: the images are there, no container has started. Its key is
the environment's content (Harbor's ``environment_id``), the template and the
shape, not the task, so tasks that share a Dockerfile share one build. The
plan names tasks, not environments, so a key seen from a second task is kept
as long as the budget allows (the plan can't say when the next task that
shares it comes). A copy that fails to come up counts as a refused fork.

Parents are labelled ``harbor-fork-parent=1`` and ``harbor-fork-run=<this
process>``, as well as with the run's own labels. Any still alive when the
process exits (Ctrl-C, an error) are deleted by label at exit. One left by a
killed process is deleted after ``paused_ttl_min`` paused, like a paused
trial sandbox.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import math
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prized import AsyncSandbox, PrizedError, Sandbox

logger = logging.getLogger(__name__)

RUN_ID = uuid.uuid4().hex[:12]  # this process's parents carry it
PARENT_LABEL = "harbor-fork-parent"
RUN_LABEL = "harbor-fork-run"
COPY_LABEL = "harbor-fork-of"  # on each copy: its parent's id
DEFAULT_BUDGET_GB = 120  # four 30 GB Terminal-Bench parents
# Refused forks after which a task starts fresh for the rest of the run.
MAX_FAILURES = 2
WAIT_FULL_SEC = 5.0  # before forking again on a full machine
WAIT_SETTLE_SEC = 2.0  # before forking again a parent between states
WAIT_GONE_SEC = 30.0  # for parents to go, after a quota refused a start
# Quotas a paused parent counts toward (a refusal without a reason counts too).
_DISK_QUOTAS = {"retained_disk", "sandbox_limit", "paused_limit", None}
_FAR = math.inf  # the next use of a parent the plan doesn't know


@dataclass(frozen=True)
class ImageFacts:
    """What a start learns from the image, which a copy can't ask its parent."""

    env: dict[str, str]
    workdir: str
    user: str | None


@dataclass(eq=False)
class Parent:
    key: str
    disk_gb: int
    # True once paused and forkable, False if it never will be.
    ready: asyncio.Future[bool]
    sandbox: AsyncSandbox | None = None  # set once ready
    facts: ImageFacts | None = None
    leases: int = 0
    last_used: float = field(default_factory=time.monotonic)
    deleted: bool = False
    # Set once its sandbox is deleted, or once it is clear there never was one.
    gone: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def usable(self) -> bool:
        return self.ready.done() and self.ready.result()


@dataclass(eq=False)
class Lease:
    """A trial's hold on a parent, from asking for it until its fork is done:
    a parent is never deleted while a lease on it is out."""

    parent: Parent
    builder: bool
    released: bool = False


class Plan:
    """The order a job's trials start in, by task digest (the job's lock.json)."""

    def __init__(self, tasks: list[str]) -> None:
        self._uses: dict[str, list[int]] = {}
        for position, task in enumerate(tasks):
            self._uses.setdefault(task, []).append(position)

    @classmethod
    def load(cls, job_dir: Path) -> Plan | None:
        """The job's plan, less the trials a run before this one finished
        (``harbor job resume`` reruns only the others). It is read when the
        first trial starts, before any trial of this run can finish."""
        try:
            trials = json.loads((job_dir / "lock.json").read_text())["trials"]
            tasks = [trial["task"]["digest"] for trial in trials]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not tasks or not all(isinstance(task, str) for task in tasks):
            return None
        for result in job_dir.glob("*/result.json"):
            done = task_of(result.parent)
            if done in tasks:
                tasks.remove(done)  # its first planned trial: Harbor matches in order
        return cls(tasks)

    def knows(self, task: str | None) -> bool:
        return task is not None and task in self._uses

    def use_after(self, task: str, arrived: int) -> int | None:
        """The plan position of the task's next trial once ``arrived`` have
        asked, or None when no later trial of it is planned."""
        uses = self._uses[task]
        return uses[arrived] if arrived < len(uses) else None


def task_of(trial_dir: Path) -> str | None:
    """The task digest in the trial's lock.json, which Harbor writes before
    it builds the trial's environment."""
    try:
        task = json.loads((trial_dir / "lock.json").read_text())["task"]["digest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return task if isinstance(task, str) else None


class ParentCache:
    """This process's parents, for one event loop. Every change of state is
    synchronous (asyncio runs one task at a time), so no lock is needed: the
    awaits (builds, forks, deletes) happen outside, under leases."""

    def __init__(self, budget_gb: int) -> None:
        self.budget_gb = max(0, int(budget_gb))
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self._parents: dict[str, Parent] = {}
        self._made: list[Parent] = []  # every parent, to wait for after a quota refusal
        self._arrived: Counter[tuple[str, str | None]] = Counter()
        self._key_tasks: dict[str, set[str | None]] = {}
        self._trials: set[tuple[str, str]] = set()
        self.quota_refused = False
        self._failures: Counter[str] = Counter()
        self._plans: dict[Path, Plan | None] = {}
        self._tasks: dict[str, tuple[Plan | None, str | None]] = {}

    def plan(self, job_dir: Path) -> Plan | None:
        if job_dir not in self._plans:
            self._plans[job_dir] = Plan.load(job_dir)
        return self._plans[job_dir]

    def parents(self) -> list[Parent]:
        return list(self._parents.values())

    def _next_use(self, key: str) -> float | None:
        """Where the key's next trial is in the plan: None if none is
        planned, _FAR if the plan doesn't know its task."""
        if len(self._key_tasks.get(key, ())) > 1:
            return _FAR  # shared by tasks: the plan can't say when it's next needed
        plan, task = self._tasks.get(key, (None, None))
        if plan is None or task is None or not plan.knows(task):
            return _FAR
        position = plan.use_after(task, self._arrived[(key, task)])
        return None if position is None else float(position)

    def _rank(self, parent: Parent) -> tuple[float, float]:
        """Eviction order: needed farthest ahead (or never) first, then the
        least recently used."""
        later = self._next_use(parent.key)
        return (_FAR if later is None else later, -parent.last_used)

    def acquire(
        self,
        key: str,
        task: str | None,
        plan: Plan | None,
        disk_gb: int,
        trial: str,
    ) -> tuple[Lease | None, list[Parent]]:
        """A lease on the key's parent (to build, when ``builder``), or None
        to start fresh; with the parents evicted to make room, which the
        caller deletes. A trial counts once toward its task's plan, however
        often it starts (a start retried, Harbor's own retries)."""
        self._tasks.setdefault(key, (plan, task))
        self._key_tasks.setdefault(key, set()).add(task)
        if (key, trial) not in self._trials:
            self._trials.add((key, trial))
            self._arrived[(key, task)] += 1
        if self.closed or self._failures[key] >= MAX_FAILURES:
            return None, []
        parent = self._parents.get(key)
        if parent is not None:
            parent.leases += 1
            parent.last_used = time.monotonic()
            return Lease(parent, builder=False), []
        later = self._next_use(key)
        if later is None:  # no later trial of this task: a parent would only cost time
            return None, []
        evicted = self._make_room(disk_gb, later)
        if evicted is None:
            return None, []
        parent = Parent(key, disk_gb, self.loop.create_future())
        parent.leases = 1
        self._parents[key] = parent
        self._made.append(parent)
        return Lease(parent, builder=True), evicted

    def _make_room(self, need_gb: int, later: float) -> list[Parent] | None:
        """Idle parents to evict so ``need_gb`` fits the budget, or None if it can't."""
        used = sum(p.disk_gb for p in self._parents.values())
        if used + need_gb <= self.budget_gb:
            return []
        idle = [p for p in self._parents.values() if p.leases == 0 and p.usable]
        if later == _FAR:  # no plan for this task: least recently used first
            ranked = sorted(idle, key=lambda p: p.last_used)
        else:  # only parents needed after this task, farthest first
            ranked = sorted(
                (p for p in idle if self._rank(p)[0] > later),
                key=self._rank,
                reverse=True,
            )
        picked: list[Parent] = []
        for parent in ranked:
            if used + need_gb <= self.budget_gb:
                break
            picked.append(parent)
            used -= parent.disk_gb
        if used + need_gb > self.budget_gb:
            return None
        for parent in picked:
            self._forget(parent)
        return picked

    def _forget(self, parent: Parent) -> None:
        if self._parents.get(parent.key) is parent:
            del self._parents[parent.key]

    def mark_ready(
        self, parent: Parent, sandbox: AsyncSandbox, facts: ImageFacts
    ) -> bool:
        """The builder's parent is paused: every lease can fork it. False if
        the cache closed meanwhile; the caller then deletes it."""
        parent.sandbox, parent.facts = sandbox, facts
        if self.closed or self._parents.get(parent.key) is not parent:
            parent.ready.set_result(False)
            return False
        parent.ready.set_result(True)
        return True

    def build_failed(self, parent: Parent) -> None:
        """The parent won't be built (the build deleted what it made)."""
        if not parent.ready.done():
            parent.ready.set_result(False)
        parent.gone.set()
        self._forget(parent)

    def quota_hit(self) -> list[Parent]:
        """A quota that parents count toward refused a start: they make way.
        No parent is built for the rest of the run; the idle ones are
        returned to delete now, and the others go with their last lease."""
        self.quota_refused = True
        self.budget_gb = 0
        idle = [p for p in self._parents.values() if p.leases == 0 and p.usable]
        self._parents.clear()
        return idle

    async def wait_gone(self, timeout: float) -> None:
        """Waits, up to ``timeout``, until every parent made is deleted."""
        waits = [
            asyncio.ensure_future(p.gone.wait())
            for p in self._made
            if not p.gone.is_set()
        ]
        if not waits:
            return
        try:
            await asyncio.wait(waits, timeout=timeout)
        finally:
            for waiting in waits:
                waiting.cancel()

    def drop(self, parent: Parent, *, failed: bool, never: bool = False) -> None:
        """The parent can't be forked: no new lease gets it, and its last
        lease deletes it. A task whose parents fail ``MAX_FAILURES`` times
        (once a parent, however many trials it refused), or that is
        ``never`` forkable, starts fresh from then on."""
        if never:
            self._failures[parent.key] = MAX_FAILURES
        if self._parents.get(parent.key) is not parent:
            return  # dropped already
        self._forget(parent)
        if failed and not never:
            self._failures[parent.key] += 1

    def copy_failed(self, parent: Parent) -> Parent | None:
        """A copy of the parent was made but didn't come up (Docker mode: its
        daemon or its containers). After ``MAX_FAILURES`` its key starts fresh
        and the parent goes: returned if idle, for the caller to delete,
        otherwise with its last lease."""
        self._failures[parent.key] += 1
        if self._failures[parent.key] < MAX_FAILURES:
            return None
        if self._parents.get(parent.key) is not parent:
            return None  # dropped or evicted already
        self._forget(parent)
        return parent if parent.leases == 0 and parent.sandbox is not None else None

    def release(self, lease: Lease) -> Parent | None:
        """Hands a lease back: returns a parent the caller must delete."""
        if lease.released:
            return None
        lease.released = True
        parent = lease.parent
        parent.leases -= 1
        parent.last_used = time.monotonic()
        if not parent.ready.done():
            if lease.builder:  # a build that never finished: waiters start fresh
                self.build_failed(parent)
            return None
        if parent.leases > 0:
            return None
        current = self._parents.get(parent.key) is parent
        if current and not self.closed and self._next_use(parent.key) is not None:
            return None  # a later trial is planned, or the plan doesn't know
        self._forget(parent)
        return parent if parent.sandbox is not None else None

    def close(self) -> list[Parent]:
        """No more parents; returns the idle ones to delete (leased ones are
        deleted when their leases come back)."""
        self.closed = True
        doomed = [
            p for p in self._parents.values() if p.leases == 0 and p.sandbox is not None
        ]
        self._parents.clear()
        return doomed


async def wait_ready(parent: Parent) -> bool:
    """Waits for the builder; a cancelled waiter leaves the build alone."""
    await asyncio.wait({parent.ready})
    return parent.ready.result()


async def delete_parent(parent: Parent | None, *, wait: bool = False) -> None:
    """Deletes a parent once (best effort: the exit sweep and its pause
    timeout catch one that won't go). ``wait``: until it no longer counts
    toward the quotas."""
    if parent is None or parent.sandbox is None or parent.deleted:
        return
    parent.deleted = True
    sandbox = parent.sandbox
    try:
        await sandbox.delete(wait=wait)
    except PrizedError as exc:
        logger.warning(
            f"Could not delete fork parent {sandbox.id}: {exc}. It is deleted when "
            f"this process exits, or with `prized sandbox rm {sandbox.id}`."
        )
    finally:
        parent.gone.set()
        await sandbox.close()


async def delete_parents(parents: list[Parent], *, wait: bool = False) -> None:
    await asyncio.gather(*(delete_parent(p, wait=wait) for p in parents))


def fork_refusal(exc: PrizedError) -> tuple[str, float]:
    """What a refused fork calls for, and how long to wait first:

    - ``retry``: try once more (a full machine, a parent between states, a
      copy that failed to start);
    - ``quota``: a quota refused the copy (see ``disk_quota``);
    - ``drop``: this parent can't fork (its machine is retiring, or it lost
      its memory); ``never``: no parent of this shape can (too big);
    - ``gone``: the parent no longer exists;
    - ``fresh``: anything else: start this trial fresh.
    """
    code = exc.code
    if code == "fork_capacity":
        return "retry", WAIT_FULL_SEC
    if code == "parent_not_running":
        return "retry", WAIT_SETTLE_SEC
    if code == "sandbox_error":
        return "retry", 0.0
    if code == "sandbox_quota_exceeded":
        return "quota", 0.0
    if code == "parent_not_forkable":
        return ("never" if exc.reason == "parent_too_big" else "drop"), 0.0
    if code == "fork_unsupported_host":
        return "drop", 0.0
    if exc.status == 404 or code in ("not_found", "sandbox_deleted"):
        return "gone", 0.0
    return "fresh", 0.0


def forkable(vcpu: int, memory_mb: int) -> bool:
    """Whether a sandbox of this shape can fork at all: past about 26 GB of
    memory a standard sandbox can't (``parent_too_big``), nor a large one
    (over 16 vCPU or about 58 GB) past about 105 GB."""
    gb = memory_mb / 1024
    large = vcpu > 16 or gb > 58
    return gb <= (105 if large else 26)


def disk_quota(exc: BaseException | None) -> bool:
    """A quota refusal that paused parents count toward."""
    return (
        isinstance(exc, PrizedError)
        and exc.code == "sandbox_quota_exceeded"
        and exc.reason in _DISK_QUOTAS
    )


async def make_way() -> bool:
    """A fresh start was refused by a quota parents count toward: deletes
    this loop's parents (waiting for those still in use, up to
    WAIT_GONE_SEC) and builds no more. False if there were none."""
    current = _CACHE
    if current is None or current.loop is not asyncio.get_running_loop():
        return False
    if not any(not p.gone.is_set() for p in current._made):
        return False
    await delete_parents(current.quota_hit(), wait=True)
    await current.wait_gone(WAIT_GONE_SEC)
    return True


# -- the process's cache, and its cleanup -----------------------------------

_CACHE: ParentCache | None = None
_SWEEP = {"registered": False}


def cache(budget_gb: int) -> ParentCache:
    """The cache for the running event loop (a new loop or a closed cache
    starts a new one)."""
    global _CACHE
    loop = asyncio.get_running_loop()
    if _CACHE is None or _CACHE.loop is not loop or _CACHE.closed:
        _CACHE = ParentCache(budget_gb)
    return _CACHE


def note_build() -> None:
    """A parent is being built: sweep this process's parents at exit."""
    if not _SWEEP["registered"]:
        _SWEEP["registered"] = True
        atexit.register(sweep)


def sweep() -> None:
    """At exit: deletes every parent this process made that is still there,
    by label (the event loop is gone, so with the synchronous SDK)."""
    labels = {PARENT_LABEL: "1", RUN_LABEL: RUN_ID}
    try:
        deleted = Sandbox.delete_by_labels(labels)
    except Exception as exc:  # noqa: BLE001 (at exit: say so, never raise)
        logger.warning(
            f"Could not delete fork parents labelled {RUN_LABEL}={RUN_ID}: {exc}. "
            f"`prized sandbox rm --label {RUN_LABEL}={RUN_ID}` deletes them; "
            "otherwise they go after paused_ttl_min paused."
        )
        return
    if deleted:
        logger.info(f"Deleted {len(deleted)} fork parents left at exit")


async def close_parents() -> None:
    """Deletes every parent of this event loop's cache (the job is over)."""
    current = _CACHE
    if current is None or current.loop is not asyncio.get_running_loop():
        return
    await delete_parents(current.close())


class TaskMajor:
    """``--plugin harbor_prized:TaskMajor``: runs every attempt of a task
    before the next task, so with ``--ek fork_mode=true`` a task's parent is
    needed for minutes, not for the whole job, and few parents are alive at
    once. Harbor's own order runs every task once before any twice. At the
    job's end it deletes any parent left.

    It reorders the job's trial lists (stable, by each task's first
    appearance) before the job runs, which reaches into Harbor's private
    attributes: if they aren't there, the order is left alone, with a warning.
    The job's lock.json compares trials regardless of order, so
    ``harbor job resume`` still matches.
    """

    def __init__(self, **kwargs: Any) -> None:
        if kwargs:
            raise TypeError(f"TaskMajor takes no options, not {sorted(kwargs)}")

    async def on_job_start(self, job: Any) -> None:
        trials = getattr(job, "_trial_configs", None)
        remaining = getattr(job, "_remaining_trial_configs", None)
        if not isinstance(trials, list) or not isinstance(remaining, list):
            logger.warning(
                "TaskMajor: this Harbor keeps its trials elsewhere; they run in "
                "Harbor's own order."
            )
            return
        first: dict[str, int] = {}
        for trial in trials:
            first.setdefault(_task_key(trial), len(first))

        def order(trial: Any) -> int:
            return first.get(_task_key(trial), len(first))

        job._trial_configs = sorted(trials, key=order)
        job._remaining_trial_configs = sorted(remaining, key=order)

    async def on_job_end(self, job_result: Any) -> None:
        await close_parents()


def _task_key(trial: Any) -> str:
    task = getattr(trial, "task", None)
    dump = getattr(task, "model_dump_json", None)
    return dump() if callable(dump) else repr(task)
