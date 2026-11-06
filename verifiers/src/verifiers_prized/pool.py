"""Parents and their copies: build the environment once in a parent sandbox, then fork it into
one copy per rollout.

Rollouts that start together are forked from the parent in one request. Copies start on their
parent's machine; when it is full (409 fork_capacity) the request is retried with the number that
fits, the parent is given a short rest, and once every parent is full another one is built, up to
`max_parents`. Workspace refusals (quota, capacity, rate limits), of a fork or of a parent's create,
are waited out for up to `capacity_wait` seconds per rollout; time spent waiting for a parent's
`setup` does not count.

Every sandbox made here is tracked until it is deleted, and an atexit hook deletes what is left
(one request by the run's label for each API key used, then one by one), so a Ctrl-C does not leave
a paid sandbox behind. SIGKILL runs no hook: the sandboxes carry the label `vfp-run=<RUN_ID>` to
find them by.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import contextlib
import copy
import logging
import math
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

import prized
from prized import PrizedError
from verifiers.v1.errors import SandboxError

from .config import DEFAULT_WORKDIR, PrizedConfig

# Under verifiers' own library logger, so its CLIs (vf-run, vf-validate) show and file these lines,
# the parent's id and the run's label among them, at the level they log at.
logger = logging.getLogger("verifiers.v1.runtimes.backends.prized")


class _LastResort(logging.Handler):
    """Warnings to stderr when nothing else would show them: verifiers.v1 carries a NullHandler,
    which keeps logging's own last resort from printing them for a script or notebook that never
    set up logging."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        node: logging.Logger | None = logger
        while node is not None:
            if any(h is not self and not isinstance(h, logging.NullHandler) for h in node.handlers):
                return  # a real handler up the chain shows it
            if not node.propagate:
                break
            node = node.parent
        if logging.lastResort is not None:
            logging.lastResort.handle(record)


logger.addHandler(_LastResort())

T = TypeVar("T")

RUN_ID = uuid.uuid4().hex[:12]
"""This process's run: every sandbox it makes carries the label `vfp-run=<RUN_ID>`."""

LINGER = 0.02
"""Seconds a fork waits for other rollouts starting at the same moment, to fork them together."""
FORK_BATCH_MAX = 64
START_TIMEOUT = 300.0
"""Seconds a create or a fork may take, its retries and the wait until running included."""
BACKOFF_MIN, BACKOFF_MAX = 0.5, 10.0

# Refusals that pass: wait, then fork again. The rest fail the rollouts they were for.
_TRANSIENT = frozenset(
    {
        "network_error",
        "timeout",
        "bad_response",
        "sandbox_error",  # a copy did not start, so the fork was undone
        "sandbox_capacity",
        "sandbox_quota_exceeded",
        "rate_limited",
        "sandbox_fork_disabled",
        "resolve_failed",
        "sandbox_not_ready",
        "sandbox_unreachable",
    }
)
_PARENT_GONE = frozenset({"not_found", "sandbox_deleted", "parent_not_running"})
"""Refusals that may mean the parent is gone; `_parent_gone` asks the API before acting on one (a
copy that failed, undoing the fork, raises `sandbox_deleted` too, and `parent_not_running` is a
parent between states)."""
_GONE_STATES = frozenset({"deleting", "deleted", "error"})
_CANNOT_FORK = frozenset({"fork_unsupported_host", "parent_not_forkable"})
"""This parent cannot fork (its machine, or its memory gone): a parent built here is replaced. Not
`parent_too_big`, which a new parent of the same shape would meet again."""
MAX_REPLACEMENTS = 3
"""Parents built here that could not fork before their first copy, in a row with no fork in between;
past it the pool stops building and every rollout fails (each replacement runs `setup` again). A
parent that has forked before (its machine retiring later, say) is replaced without counting."""
_BUSY_STATES = frozenset({"starting", "pausing", "resizing"})
"""A sandbox between states: `parent = "<id>"` in one of them is connected to again after a pause."""


def passes(err: PrizedError) -> bool:
    """A refusal that passes with time (wait, then send it again), for a fork or a create alike. The
    free tier's run limit does not."""
    return (err.code in _TRANSIENT or err.status in (429, 500, 502, 503, 504)) and not (
        err.code == "sandbox_quota_exceeded" and err.reason == "max_runtime"
    )


# The SDK, as a module attribute so the tests can put a fake in its place.
sdk: Any = prized


def backoff(strikes: int) -> float:
    return float(min(BACKOFF_MIN * 2 ** max(strikes - 1, 0), BACKOFF_MAX))


async def shielded(work: Awaitable[T]) -> T:
    """Run `work` to the end even if the caller is cancelled, then re-raise the cancellation."""
    task = asyncio.ensure_future(work)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as err:
            cancelled = err
        except BaseException:  # noqa: BLE001 - the task's own failure, raised below
            pass
    if cancelled is not None:
        raise cancelled
    return task.result()


def fresh(err: BaseException) -> BaseException:
    """A copy of `err` to raise or hand on: raising one instance again and again (to every later
    rollout, say) makes its traceback grow without end."""
    try:
        new = copy.copy(err)
    except Exception:  # noqa: BLE001 - an error that cannot be copied is passed on as text
        new = SandboxError(str(err))
    new.__cause__ = err.__cause__
    return new


# --- credentials and the tracked sandboxes -------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    base_url: str | None = None

    @property
    def kwargs(self) -> dict[str, Any]:
        return {"api_key": self.api_key, **({"base_url": self.base_url} if self.base_url else {})}

    @property
    def key_id(self) -> str:
        """An opaque name for the key, the same for the same key in this process: it stands in for
        the key where one is compared or printed (a pool's recipe, the exit sweep)."""
        return _key_ids.setdefault(self.api_key, uuid.uuid4().hex[:16])


_key_ids: dict[str, str] = {}


def credentials(config: PrizedConfig) -> Credentials:
    key = os.environ.get(config.api_key_env, "").strip()
    if not key:
        raise SandboxError(
            f"verifiers-prized needs a Prized API key in ${config.api_key_env} "
            "(https://prized.dev/onboarding/sandboxes?via=verifiers)"
        )
    return Credentials(key, config.base_url)


_tracked: dict[str, Credentials] = {}
_used: dict[str, Credentials] = {}
"""Every API key this process created or forked with (by key_id): swept by the run's label at exit,
even with nothing tracked, for a copy whose fork was answered after a second Ctrl-C ended the loop."""
_tracked_lock = threading.Lock()
_atexit_armed = False


def _arm(creds: Credentials) -> None:
    """Remember `creds` for the exit sweep and register it once. Call with `_tracked_lock` held."""
    global _atexit_armed
    _used.setdefault(creds.key_id, creds)
    if not _atexit_armed:
        _atexit_armed = True
        atexit.register(delete_leftovers)


def making(creds: Credentials) -> None:
    """A create or fork with `creds` is about to be sent: sweep its workspace at exit."""
    with _tracked_lock:
        _arm(creds)


def track(sandbox_id: str, creds: Credentials) -> None:
    with _tracked_lock:
        _tracked[sandbox_id] = creds
        _arm(creds)


def untrack(sandbox_id: str) -> None:
    with _tracked_lock:
        _tracked.pop(sandbox_id, None)


def is_tracked(sandbox_id: str) -> bool:
    with _tracked_lock:
        return sandbox_id in _tracked


def delete_now(sandbox_id: str, creds: Credentials) -> None:
    """Delete one sandbox synchronously (no event loop needed): best effort, idempotent."""
    with contextlib.suppress(Exception):
        sandbox = sdk.Sandbox.connect(sandbox_id, **creds.kwargs)
        try:
            sandbox.delete(timeout=30)
        finally:
            sandbox.close()
    untrack(sandbox_id)


def delete_leftovers() -> None:
    """At interpreter exit: delete every sandbox this process made and still holds. One request
    per API key by the run's label, then one by one for any that request missed."""
    with _tracked_lock:
        keys = {**_used, **{c.key_id: c for c in _tracked.values()}}
    for creds in keys.values():
        with contextlib.suppress(Exception):
            for sandbox_id in sdk.Sandbox.delete_by_labels({"vfp-run": RUN_ID}, **creds.kwargs):
                untrack(sandbox_id)
    with _tracked_lock:
        left = dict(_tracked)
    for sandbox_id, creds in left.items():
        delete_now(sandbox_id, creds)


async def discard(sandbox: Any) -> None:
    """Delete a sandbox this process made and release its handle: best effort, logged."""
    try:
        await sandbox.delete()
        untrack(sandbox.id)
    except PrizedError as err:
        if err.status == 404:
            untrack(sandbox.id)
        else:
            logger.warning("verifiers-prized: could not delete sandbox %s: %s", sandbox.id, err)
    except Exception as err:  # noqa: BLE001 - teardown is best effort; atexit tries again
        logger.warning("verifiers-prized: could not delete sandbox %s: %s", sandbox.id, err)
    finally:
        with contextlib.suppress(Exception):
            await sandbox.close()


# --- building a sandbox ---------------------------------------------------------------------------


class SetupError(SandboxError):
    """A `setup` command failed. It would fail again, so the pool does not retry it."""


class ParentBusy(SandboxError):
    """`parent = "<id>"` is between states: waited out like a refusal of room."""


@dataclass(frozen=True)
class Recipe:
    """What a parent is made of; rollouts whose configs share a recipe fork the same parents."""

    template: str
    vcpu: int
    memory_mb: int
    disk_gb: int
    setup: tuple[str, ...]
    setup_env: tuple[str, ...]
    setup_timeout: float
    parent: str | None
    pause_parent: bool
    max_parents: int
    capacity_wait: float
    max_runtime_min: int | None
    labels: tuple[tuple[str, str], ...]
    base_url: str | None
    key_id: str

    @classmethod
    def of(cls, config: PrizedConfig, creds: Credentials) -> Recipe:
        # A task's resources reach the config through model_copy, which skips validation: clamp
        # them into what a sandbox can have here instead of refusing the rollout.
        vcpu = min(32, max(1, math.ceil(config.cpu)))
        memory_mb = min(128 * 1024, max(1024, round(config.memory * 1024)))
        disk_gb = min(128, max(10, math.ceil(config.disk)))
        own = config.parent is None
        return cls(
            template=config.template if own else "",
            vcpu=vcpu if own else 0,
            memory_mb=memory_mb if own else 0,
            disk_gb=disk_gb if own else 0,
            setup=tuple(config.setup) if own else (),
            setup_env=tuple(config.setup_env) if own else (),
            setup_timeout=config.setup_timeout,
            parent=config.parent,
            pause_parent=config.pause_parent,
            max_parents=config.max_parents if own else 1,
            capacity_wait=config.capacity_wait,
            max_runtime_min=config.max_runtime_min,
            labels=tuple(sorted(config.labels.items())),
            base_url=config.base_url,
            key_id=creds.key_id,
        )

    def labels_for(self, role: str, run: str | None = None) -> dict[str, str]:
        return {**dict(self.labels), "verifiers-prized": role, "vfp-run": run or RUN_ID}


def _tail(text: str, limit: int = 2000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


async def build_sandbox(
    recipe: Recipe,
    creds: Credentials,
    *,
    role: str,
    pause: bool,
    tracked: bool = True,
    run: str | None = None,
    on_created: Callable[[], None] | None = None,
) -> Any:
    """Create a sandbox and run `setup` in it, then pause it if asked (`on_created` is called once the
    create has answered). On any failure, or a cancellation, the sandbox is deleted before the error
    goes on."""
    missing = [name for name in recipe.setup_env if name not in os.environ]
    if missing:
        raise SetupError(f"setup_env names variables that are not set here: {', '.join(missing)}")
    env = {name: os.environ[name] for name in recipe.setup_env}
    run = run or RUN_ID
    began = time.monotonic()
    if tracked:
        making(creds)
    sandbox = await sdk.AsyncSandbox.create(
        recipe.template,
        vcpu=recipe.vcpu,
        memory_mb=recipe.memory_mb,
        disk_gb=recipe.disk_gb,
        labels=recipe.labels_for(role, run),
        max_runtime_min=recipe.max_runtime_min,
        timeout=START_TIMEOUT,
        **creds.kwargs,
    )
    if tracked:
        track(sandbox.id, creds)
    if on_created is not None:
        on_created()
    try:
        logger.info(
            "verifiers-prized: %s %s up in %.1fs (label vfp-run=%s); running %d setup command(s)",
            role,
            sandbox.id,
            time.monotonic() - began,
            run,
            len(recipe.setup),
        )
        for step, command in enumerate(recipe.setup, 1):
            try:
                result = await sandbox.exec(
                    ["sh", "-c", command],
                    cwd=DEFAULT_WORKDIR,
                    env=env or None,
                    timeout=recipe.setup_timeout,
                )
            except prized.CommandTimeoutError as err:
                raise SetupError(
                    f"setup command {step} ran past setup_timeout ({recipe.setup_timeout:g}s): {command!r}"
                ) from err
            if result.exit_code != 0:
                raise SetupError(
                    f"setup command {step} exited {result.exit_code}: {command!r}\n"
                    f"stderr: {_tail(result.stderr)}\nstdout: {_tail(result.stdout, 500)}"
                )
        if pause:
            await sandbox.pause()
        logger.info(
            "verifiers-prized: %s %s ready in %.1fs%s",
            role,
            sandbox.id,
            time.monotonic() - began,
            " (paused)" if pause else "",
        )
        return sandbox
    except BaseException:
        await shielded(discard(sandbox))
        raise


# --- the pool -------------------------------------------------------------------------------------


@dataclass(eq=False)
class Parent:
    sandbox: Any
    owned: bool
    forking: bool = False
    rest_until: float = 0.0
    strikes: int = 0
    fits: int | None = None
    """How many copies the last fork_capacity said fit, when fewer than were asked for."""
    forked: bool = False
    """It has made a copy: when it can no longer fork, its replacement does not count toward
    MAX_REPLACEMENTS."""


@dataclass(eq=False)
class _Waiter:
    future: asyncio.Future[tuple[Any, Parent]]
    since: float
    """When it came: rollouts that come together are forked together (LINGER)."""
    refused_at: float | None = None
    """When it was first refused room (a fork or a parent's create refused): its capacity_wait
    counts from here, with the time a parent build took left out."""


class ParentPool:
    """The parents of one recipe on one event loop, and the rollouts waiting for a copy."""

    def __init__(self, recipe: Recipe, creds: Credentials) -> None:
        self.loop = asyncio.get_running_loop()
        self.recipe = recipe
        self.creds = creds
        self.parents: list[Parent] = []
        self.forks = 0
        """Fork requests sent (for tests and logs)."""
        self._building = 0
        self._build_error: BaseException | None = None
        self._sticky_error: SandboxError | None = None
        self._waiters: collections.deque[_Waiter] = collections.deque()
        self._blocked_until = 0.0
        self._strikes = 0
        self._build_retry_at = 0.0
        """No parent is built before this: the last build failed."""
        self._build_strikes = 0
        self._replaced = 0
        self._last_refusal: str | None = None
        """The last refusal of room (a fork's or a parent's build), named when capacity_wait runs out."""
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._deletes: set[asyncio.Task[Any]] = set()
        """The tasks in `_tasks` that delete a sandbox: `close` waits for them, never cancels them."""
        self._closed = False
        self._max_parents = recipe.max_parents

    # -- the rollout's side

    async def acquire(self) -> tuple[Any, Parent]:
        """A running copy of a parent, and that parent. Cancelling this never leaves a copy."""
        if self._closed:
            raise SandboxError("verifiers-prized: this runtime's pool is closed")
        if self._sticky_error is not None:
            raise fresh(self._sticky_error)
        waiter = _Waiter(self.loop.create_future(), time.monotonic())
        self._waiters.append(waiter)
        self._poke()
        try:
            return await waiter.future
        except asyncio.CancelledError:
            future = waiter.future
            if future.done() and not future.cancelled() and future.exception() is None:
                child, parent = future.result()
                await shielded(discard(child))
                self.released(parent)
            raise

    def released(self, parent: Parent) -> None:
        """A copy of `parent` was deleted: its machine has room again."""
        parent.rest_until = 0.0
        parent.strikes = 0
        parent.fits = None
        if self._waiters:
            self._poke()

    async def close(self) -> None:
        """Fail the waiting rollouts and delete the parents this pool built."""
        self._closed = True
        # Builds and forks are cancelled; deletes already under way (a replaced parent's) finish.
        for task in [*self._tasks, *([self._dispatcher] if self._dispatcher else [])]:
            if task not in self._deletes:
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._fail(list(self._waiters), SandboxError("verifiers-prized: runtime pool closed"))
        self._waiters.clear()
        parents, self.parents = self.parents, []
        await asyncio.gather(*(discard(p.sandbox) for p in parents if p.owned))
        for parent in parents:
            if not parent.owned:
                with contextlib.suppress(Exception):
                    await parent.sandbox.close()

    # -- the dispatcher

    def _poke(self) -> None:
        self._wake.set()
        if not self._closed and (self._dispatcher is None or self._dispatcher.done()):
            self._dispatcher = self.loop.create_task(self._dispatch())

    def _spawn(self, work: Awaitable[Any], *, deletes: bool = False) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if deletes:
            self._deletes.add(task)
            task.add_done_callback(self._deletes.discard)

    def _fail(self, waiters: list[_Waiter], error: BaseException) -> None:
        for waiter in waiters:
            if not waiter.future.done():
                waiter.future.set_exception(fresh(error))

    def _drop_done(self) -> None:
        """Forget waiters that went away (cancelled) or were answered."""
        self._waiters = collections.deque(w for w in self._waiters if not w.future.done())

    def _refuse(self, waiters: Iterable[_Waiter], now: float) -> None:
        """These waiters were refused room: their capacity_wait starts, if it has not."""
        for waiter in waiters:
            if waiter.refused_at is None:
                waiter.refused_at = now

    def _deadline(self, waiter: _Waiter) -> float | None:
        return None if waiter.refused_at is None else waiter.refused_at + self.recipe.capacity_wait

    def _expire(self, now: float) -> None:
        """Fail the waiters refused room for longer than capacity_wait. Called only when no parent
        can fork now and none is being built: waiting for `setup` is not waiting for room."""
        keep: collections.deque[_Waiter] = collections.deque()
        for waiter in self._waiters:
            if waiter.future.done():
                continue
            deadline = self._deadline(waiter)
            if deadline is not None and now > deadline:
                last = f" (the last refusal: {self._last_refusal})" if self._last_refusal else ""
                waiter.future.set_exception(
                    SandboxError(
                        f"verifiers-prized: no room for a copy within capacity_wait "
                        f"({self.recipe.capacity_wait:g}s): every parent's machine is full or the "
                        f"workspace is at a quota{last}"
                    )
                )
                continue
            keep.append(waiter)
        self._waiters = keep

    def _may_build(self, now: float) -> bool:
        if self._building or len(self.parents) >= self._max_parents or now < self._build_retry_at:
            return False
        if not self.parents:
            return True
        # Another parent only when every one we have is resting after a full machine, and the
        # workspace itself is not refusing.
        return now >= self._blocked_until and all(p.rest_until > now and not p.forking for p in self.parents)

    async def _dispatch(self) -> None:
        while not self._closed:
            self._wake.clear()
            now = time.monotonic()
            self._drop_done()
            if not self._waiters:
                return
            if self._build_error is not None and not self.parents and not self._building:
                error, self._build_error = self._build_error, None
                self._fail(list(self._waiters), error)
                self._waiters.clear()
                return
            ready = [
                p
                for p in self.parents
                if not p.forking and p.rest_until <= now and now >= self._blocked_until
            ]
            if ready:
                first = min(w.since for w in self._waiters)
                if now - first < LINGER:
                    await asyncio.sleep(LINGER - (now - first))
                    self._drop_done()
                for parent in ready:
                    if not self._waiters:
                        break
                    count = min(len(self._waiters), parent.fits or FORK_BATCH_MAX)
                    batch = [self._waiters.popleft() for _ in range(count)]
                    parent.forking = True
                    self._spawn(self._fork_into(parent, batch))
                continue
            if self._may_build(now):
                self._building += 1
                self._spawn(self._build())
                continue
            # Nothing to do until a parent rests, a refusal passes, a fork or a build ends. A
            # refused waiter's capacity_wait runs out here, but not while a parent is being built.
            wake_at: list[float] = []
            if not self._building:
                self._expire(now)
                if not self._waiters:
                    return
                wake_at += [d for d in map(self._deadline, self._waiters) if d is not None]
            wake_at += [p.rest_until for p in self.parents if p.rest_until > now]
            wake_at += [t for t in (self._blocked_until, self._build_retry_at) if t > now]
            timeout = max(0.0, min(wake_at) - now) if wake_at else None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)

    async def _build(self) -> None:
        created: list[float] = []
        try:
            if self.recipe.parent is not None:
                sandbox = await sdk.AsyncSandbox.connect(self.recipe.parent, **self.creds.kwargs)
                if sandbox.state not in ("running", "paused"):
                    state = sandbox.state
                    with contextlib.suppress(Exception):
                        await sandbox.close()
                    message = (
                        f"verifiers-prized: parent {self.recipe.parent} is {state}; "
                        "only a running or paused sandbox can be forked"
                    )
                    # Between states (resuming, pausing, resizing): connect again after a pause.
                    raise ParentBusy(message) if state in _BUSY_STATES else SetupError(message)
                self.parents.append(Parent(sandbox, owned=False))
            else:
                sandbox = await build_sandbox(
                    self.recipe,
                    self.creds,
                    role="parent",
                    pause=self.recipe.pause_parent,
                    on_created=lambda: created.append(time.monotonic()),
                )
                self.parents.append(Parent(sandbox, owned=True))
            self._build_strikes = 0
        except Exception as err:  # noqa: BLE001 - handed to the waiting rollouts
            self._build_failed(err, created[0] if created else None)
        else:
            self._built(created[0] if created else None)
        finally:
            self._building -= 1
            self._poke()

    def _built(self, created: float | None) -> None:
        """A build ended. The time from its create's answer (`created`) to now, its `setup` and
        pause, does not count against the capacity_wait of the rollouts refused room before or
        during it; the create itself does (an SDK retrying a full workspace is waiting for room)."""
        if created is None:
            return
        now = time.monotonic()
        for waiter in self._waiters:
            if waiter.refused_at is not None:
                waiter.refused_at += now - max(created, waiter.refused_at)

    def _build_failed(self, err: Exception, created: float | None) -> None:
        # The setup it ran does not count; the failure is a refusal of room for every waiter.
        self._built(created)
        self._last_refusal = str(err)
        self._refuse(self._waiters, time.monotonic())
        # The next build waits, so a refusing workspace does not get a create per wake-up.
        self._build_strikes += 1
        pause = backoff(self._build_strikes)
        if isinstance(err, PrizedError):
            pause = max(err.retry_after or 0.0, pause)
        self._build_retry_at = time.monotonic() + pause
        transient = isinstance(err, ParentBusy) or (isinstance(err, PrizedError) and passes(err))
        if isinstance(err, PrizedError) and err.status == 404 and self.recipe.parent is not None:
            err = SetupError(f"verifiers-prized: parent {self.recipe.parent} not found: {err}")
        elif not isinstance(err, SandboxError):
            err = SandboxError(f"verifiers-prized: could not start a parent: {err}")
        if self.parents:
            # The rollouts keep the parents they have; a failed setup stops further builds.
            logger.warning("verifiers-prized: could not add a parent: %s", err)
            if isinstance(err, SetupError):
                self._max_parents = len(self.parents)
            return
        if isinstance(err, SetupError):
            self._sticky_error = err  # it would fail again: later rollouts fail at once
        if transient:
            # A quota, no capacity, a rate limit: the waiting rollouts wait it out (capacity_wait).
            logger.warning("verifiers-prized: %s; trying again in %.1fs", err, pause)
            return
        self._build_error = err

    async def _fork_into(self, parent: Parent, batch: list[_Waiter]) -> None:
        children: list[Any] = []
        try:
            live = [w for w in batch if not w.future.done()]
            if not live:
                return
            self.forks += 1
            making(self.creds)
            began = time.monotonic()
            try:
                children = list(
                    await parent.sandbox.fork(
                        len(live),
                        labels=self.recipe.labels_for("rollout"),
                        timeout=START_TIMEOUT,
                    )
                )
            except PrizedError as err:
                self._refused(parent, err, live, await self._parent_gone(parent, err))
                return
            except Exception as err:  # noqa: BLE001 - handed to the rollouts it was for
                self._fail(live, SandboxError(f"verifiers-prized: fork failed: {err}"))
                return
            for child in children:
                track(child.id, self.creds)
            logger.debug(
                "verifiers-prized: forked %d cop%s of %s in %.2fs: %s",
                len(children),
                "y" if len(children) == 1 else "ies",
                parent.sandbox.id,
                time.monotonic() - began,
                " ".join(child.id for child in children),
            )
            parent.rest_until, parent.strikes, parent.fits = 0.0, 0, None
            parent.forked = True
            self._strikes = self._replaced = 0
            # A rollout that went away while its copy was being made hands it to the next one.
            takers = live + list(self._waiters)
            handed = 0
            for waiter in takers:
                if handed == len(children):
                    break
                if waiter.future.done():
                    continue
                waiter.future.set_result((children[handed], parent))
                handed += 1
            spare, children = children[handed:], []
            self._waiters = collections.deque(w for w in self._waiters if not w.future.done())
            if spare:
                await shielded(asyncio.gather(*(discard(c) for c in spare), return_exceptions=True))
                self.released(parent)
        except BaseException as err:
            # Cancelled (the pool is closing): copies not handed out yet are deleted, and the
            # rollouts this fork was for, out of the queue `close` fails, are failed here.
            await shielded(asyncio.gather(*(discard(c) for c in children), return_exceptions=True))
            why = (
                "runtime pool closed" if isinstance(err, asyncio.CancelledError) else f"fork failed: {err!r}"
            )
            self._fail(batch, SandboxError(f"verifiers-prized: {why}"))
            raise
        finally:
            parent.forking = False
            self._poke()

    async def _parent_gone(self, parent: Parent, err: PrizedError) -> bool:
        """Whether a refused fork means the parent itself is gone (deleted, failed), asked of the
        API: not a copy that failed (its own id on the error) or a parent between states."""
        if err.sandbox_id not in (None, parent.sandbox.id):
            return False
        if err.code not in _PARENT_GONE and err.status != 404:
            return False
        try:
            await parent.sandbox.refresh()
        except PrizedError as check:
            return check.status == 404
        except Exception:  # noqa: BLE001 - unknown: treated as a refusal that passes
            return False
        return parent.sandbox.state in _GONE_STATES

    def _refused(self, parent: Parent, err: PrizedError, live: list[_Waiter], gone: bool) -> None:
        now = time.monotonic()
        body: Mapping[str, Any] = err.body or {}
        requeue = refusal = True  # back in line, and refused room (their capacity_wait runs)
        cannot_fork = err.code in _CANNOT_FORK and err.reason != "parent_too_big"
        if err.code == "fork_capacity":
            fits = body.get("fits")
            if isinstance(fits, int) and 0 < fits < len(live):
                parent.fits = fits
            else:
                parent.strikes += 1
                parent.rest_until = now + backoff(parent.strikes)
        elif gone and not parent.owned:
            self.parents = [p for p in self.parents if p is not parent]
            self._spawn(parent.sandbox.close())  # yours: its handle is released, nothing more
            stuck = SetupError(f"verifiers-prized: parent {parent.sandbox.id} can no longer fork: {err}")
            self._sticky_error = stuck
            self._fail(live, stuck)
            self._fail(list(self._waiters), stuck)
            self._waiters.clear()
            requeue = False
        elif (gone or cannot_fork) and parent.owned:
            # Gone, or unable to fork: delete it (discard untracks it) and build another, unless
            # the last few could not fork either: then setup is not run again in this process.
            self.parents = [p for p in self.parents if p is not parent]
            self._spawn(discard(parent.sandbox), deletes=True)
            if not parent.forked:
                self._replaced += 1
            refusal = False
            if self._replaced > MAX_REPLACEMENTS:
                failed = SetupError(
                    f"verifiers-prized: {self._replaced} new parents in a row could not fork, so no more "
                    f"are built in this process; the last: {err}"
                )
                self._sticky_error = failed
                self._fail(live, failed)
                self._fail(list(self._waiters), failed)
                self._waiters.clear()
                requeue = False
            else:
                logger.warning(
                    "verifiers-prized: parent %s can no longer fork (%s); building another",
                    parent.sandbox.id,
                    err,
                )
        elif not gone and (err.code in _PARENT_GONE or err.status == 404):
            # A copy failed (the fork was undone), or the parent is between states: rest it.
            parent.strikes += 1
            parent.rest_until = now + backoff(parent.strikes)
        elif passes(err):
            admitted = body.get("admitted")
            if isinstance(admitted, int) and 0 < admitted < len(live):
                parent.fits = admitted
            else:
                self._strikes += 1
                self._blocked_until = now + max(err.retry_after or 0.0, backoff(self._strikes))
        else:
            self._fail(live, SandboxError(f"verifiers-prized: Prized refused the fork: {err}"))
            requeue = False
        if requeue:
            back = [w for w in live if not w.future.done()]
            if refusal:
                self._last_refusal = str(err)
                self._refuse(back, now)
            self._waiters.extendleft(reversed(back))


_pools: dict[tuple[int, Recipe], ParentPool] = {}
_pools_lock = threading.Lock()


def pool_for(config: PrizedConfig, creds: Credentials) -> ParentPool:
    """This event loop's pool for the config's recipe."""
    loop = asyncio.get_running_loop()
    recipe = Recipe.of(config, creds)
    with _pools_lock:
        for key in [k for k, p in _pools.items() if p.loop.is_closed()]:
            del _pools[key]
        pool = _pools.get((id(loop), recipe))
        if pool is None or pool.loop is not loop:
            pool = _pools[(id(loop), recipe)] = ParentPool(recipe, creds)
        return pool


async def close_pools() -> None:
    """Delete the parents this event loop built and forget them. The atexit hook does this at
    interpreter exit; call it to free them sooner (a notebook, a long-lived service)."""
    loop = asyncio.get_running_loop()
    with _pools_lock:
        mine = [(k, p) for k, p in _pools.items() if p.loop is loop]
        for key, _ in mine:
            del _pools[key]
    await asyncio.gather(*(pool.close() for _, pool in mine))


def _after_fork_in_child() -> None:
    """A forked child is a run of its own: a new RUN_ID, and none of the parent process's sandboxes
    tracked (its exit must not delete them, by label or one by one) or pools (their event loop is
    the parent's). The locks are new too: one held by another thread at the fork stays held."""
    global RUN_ID, _tracked_lock, _pools_lock
    RUN_ID = uuid.uuid4().hex[:12]
    _tracked_lock = threading.Lock()
    _pools_lock = threading.Lock()
    _tracked.clear()
    _used.clear()
    _pools.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)
