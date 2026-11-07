"""The orchestrator: claims a self-hosted environment's work and runs each session in its sandbox.

The flow follows Anthropic's own dispatcher (anthropic-cli ``sandboxes/dispatcher``), with Prized
sandboxes in place of Kubernetes pods:

1. **Claim.** A ``session.status_run_started`` webhook (or, without webhooks, a long poll) wakes the
   intake, which polls the queue until it is empty. It polls only while a handoff slot is free, so
   it never claims work it can't start.
2. **Hand off.** The session's sandbox is resumed (files and processes as the last run left them),
   or a warm parent is forked, or a sandbox is created. Runs a resumed sandbox still holds (a worker
   frozen when the sandbox paused itself) are stopped first, so two workers never share it. ant is
   installed if missing (checked against its sha256), the work item's secret is written into the
   sandbox, and ``ant beta:worker run`` starts, detached. Only then is the item **acked**: a handoff
   that fails before the launch leaves it unacked, and the queue hands it out again after the
   reclaim window (twice the handoff timeout). A sandbox made for the failed attempt is deleted; one
   that holds the session's files is paused. After ``max_handoff_attempts`` failures the item is
   stopped instead, so it doesn't come back forever. Once ant is launched, nothing stops it unless
   the sandbox says it has exited: a status check that fails leaves the run to the probes.
3. **Supervise.** Every ``probe_every_s`` one status command per sandbox checks its workers. That
   is also what keeps the sandbox awake: Prized pauses a sandbox after ``auto_pause_s`` without
   commands, so if this process dies its sandboxes pause themselves.
4. **Finish.** When a worker exits (a minute after the session goes idle, by default), the
   orchestrator asks the API about the session: terminated or gone, and the sandbox is deleted;
   otherwise it is paused, memory and all, for the next run, once a status check shows nothing else
   runs in it. ``session.status_terminated`` and ``session.deleted`` webhooks, and a sweep every
   ``sweep_every_s``, delete the rest.

The environment key stays in this process. A sandbox gets one work item's secret, which reaches
that one session. What a sandbox reports about its runs is believed only for runs whose ids carry
this environment's signature and whose session is the sandbox's own (``worker.genuine``), so the
agent, which can write anything in its sandbox, can't make up runs.

Run one orchestrator per environment, and on a restart stop the old process before starting the new
one: two at once would each pause sandboxes the other is handing off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from prized import PrizedError

from . import worker as w
from .cma import CMA, CMAError, Session, WorkItem
from .config import LOOPBACK, Settings
from .sandboxes import SESSION, Ensured, SessionSandboxes, gone, idle_since
from .webhooks import Event

log = logging.getLogger("prized_managed_agents")

_TEMPLATE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SANDBOX_ID = re.compile(r"sbx_[A-Za-z0-9]{1,64}")
SETTLE_S = 1.0  # a worker that dies at once (a bad secret, a broken binary) dies within this
RETIRE_WAIT_S = 30.0  # how long an earlier run of the session gets to finish before it is stopped
# The launch comes after the handoff timeout: the launch command (30 s), the settle and a status
# check (20 s). A run still starting after both counts as dead.
LAUNCH_S = 60.0
MAX_FAILURES = 10_000  # failed handoffs remembered (by work id), oldest forgotten first
MAX_REMEMBERED = 10_000  # each session's last sandbox, so a vanished one is named on the next run
LOW_RUN_S = 10 * 60  # on the free tier, say so when a sandbox has less running left than this
REASON_WAIT_S = 5.0  # how long asking Prized why a sandbox is gone may take, inside a handoff
MAX_STRAYS = 20  # leftover workers stopped per pass; more than that is the agent's doing


class HandoffError(Exception):
    pass


@dataclass
class Run:
    run_id: str
    session_id: str
    work_id: str
    sandbox: Any
    started: float
    acked: bool
    probe_failures: int = 0
    stopping: bool = False


@dataclass
class Note:
    session: str
    what: str
    detail: str = ""
    at: float = field(default_factory=time.time)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        cma: CMA,
        boxes: SessionSandboxes,
        *,
        clock: Callable[[], float] = time.monotonic,
        account: Any = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.s = settings
        self.cma = cma
        self.boxes = boxes
        self.clock = clock
        self.now = now
        self.account = account  # a PrizedAccount: why the free tier deleted a sandbox
        self.last_box: OrderedDict[str, str] = OrderedDict()  # session -> the sandbox it last ran in
        self.runs: dict[str, Run] = {}  # by run id
        self.notes: list[Note] = []
        self.failures: OrderedDict[str, int] = OrderedDict()  # failed handoffs, by work id
        self._key = w.run_key(settings.environment_key)  # signs new runs
        # Recognises runs: also the key before a rotation, while its runs may still be under way.
        self._keys = [self._key] + (
            [w.run_key(settings.previous_environment_key)] if settings.previous_environment_key else []
        )
        self._busy: set[str] = set()  # sessions with a handoff under way
        self._locks: dict[str, list[Any]] = {}  # session -> [lock, holders and waiters]
        self._slots = asyncio.Semaphore(settings.max_handoffs)
        self._handoffs = 0
        self._wake = asyncio.Event()
        self._probe_now = asyncio.Event()
        self._stopping = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        # Never evict a sandbox whose session is being handed off or has a run (a frozen one too).
        boxes.busy = lambda: self._busy | {r.session_id for r in self.runs.values()}

    # -- the loops -----------------------------------------------------------------------------

    async def start(self) -> None:
        """Check the environment key and the Prized key, and pick up runs a previous process left."""
        log.info("the environment key goes to %s", self.s.base_url)
        if urlsplit(self.s.base_url).hostname in LOOPBACK:
            log.warning(
                "%s is this machine: the worker in each sandbox gets it too, where it means the sandbox "
                "itself, so this works only with sandboxes on this machine (the tests)",
                self.s.base_url,
            )
        if self.s.ignored_base_url:
            log.warning(
                "ANTHROPIC_BASE_URL=%s is ignored: the environment key goes only to %s (set %s to change that)",
                self.s.ignored_base_url,
                self.s.base_url,
                "ORCH_ANTHROPIC_BASE_URL",
            )
        try:
            await self.cma.stats()
        except CMAError as err:
            hint = " (is ANTHROPIC_ENVIRONMENT_KEY this environment's key?)" if err.auth else ""
            raise SystemExit(f"can't reach environment {self.s.environment_id}'s work queue: {err}{hint}") from err
        await self.reconcile()
        self._started = True

    async def run(self) -> None:
        if not self._started:
            await self.start()
        mode = "webhooks, with a backstop poll" if self.s.webhooks else "long polling"
        log.info("serving %s by %s", self.s.environment_id, mode)
        loops = [self._intake_loop(), self._probe_loop(), self._sweep_loop()]
        tasks = [asyncio.create_task(c) for c in loops]
        try:
            await self._stopping.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Handoffs under way finish (bounded by their timeout), and so does what they start;
            # runs keep going in their sandboxes, which pause themselves after AUTO_PAUSE unless a
            # new process takes over.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.s.handoff_timeout_s + LAUNCH_S
            while True:
                pending = [t for t in self._tasks if not t.done()]
                left = deadline - loop.time()
                if not pending or left <= 0:
                    break
                await asyncio.wait(pending, timeout=left)
            log.info("stopped: %d runs keep going in their sandboxes; a restart picks them up", len(self.runs))

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def wake(self) -> None:
        """Drain the queue now (a webhook said a session started running)."""
        self._wake.set()

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @contextlib.asynccontextmanager
    async def _lock(self, sid: str) -> AsyncIterator[None]:
        """The session's lock, forgotten once no one holds or waits for it."""
        entry = self._locks.get(sid)
        if entry is None:
            entry = self._locks[sid] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(sid) is entry:
                del self._locks[sid]

    async def _intake_loop(self) -> None:
        failures = 0
        while not self.stopping:
            try:
                if self.s.webhooks:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=self.s.reconcile_every_s)
                    self._wake.clear()
                    # A short long-poll, in case the item lands just after its webhook.
                    await self.drain(block_ms=999)
                else:
                    await self.drain(block_ms=999, forever=True)
                failures = 0
            except asyncio.CancelledError:
                raise
            except CMAError as err:
                failures += 1
                pause = min(60.0, 2.0**failures) if not err.auth else 60.0
                log.error("polling the work queue failed (%s); trying again in %.0fs", err, pause)
                await self._sleep_unless_stopping(pause)
            except Exception:
                failures += 1
                log.exception("intake pass failed")
                await self._sleep_unless_stopping(min(60.0, 2.0**failures))

    async def drain(self, *, block_ms: int | None, forever: bool = False) -> int:
        """Claim work until the queue is empty (or, ``forever``, until stopped). Returns how many
        items were taken."""
        taken = 0
        while not self.stopping:
            while self._active_count() >= self.s.max_sessions and not self.stopping:
                await self._sleep_unless_stopping(1.0)  # full: the items wait in the queue
            await self._slots.acquire()
            try:
                item = await self.cma.poll(block_ms=block_ms, reclaim_older_than_ms=self.s.reclaim_ms)
            except BaseException:
                self._slots.release()
                raise
            if item is None:
                self._slots.release()
                if not forever:
                    return taken
                await self._sleep_unless_stopping(random.uniform(1.0, 3.0))
                continue
            taken += 1
            self._handoffs += 1
            self._spawn(self._handle_then_release(item))
        return taken

    def _active_count(self) -> int:
        return len(self.runs) + self._handoffs

    async def _handle_then_release(self, item: WorkItem) -> None:
        try:
            await self.handle(item)
        except Exception:
            log.exception("%s: handling work item %s failed", item.session_id, item.id)
        finally:
            self._handoffs -= 1
            self._slots.release()

    async def _sleep_unless_stopping(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # -- one work item -------------------------------------------------------------------------

    async def handle(self, item: WorkItem) -> None:
        sid = item.session_id
        if item.kind != "session":
            # The queue also sends healthcheck items. Take them off it and end them.
            await self._ack_and_stop(item, f"{item.kind or 'untyped'} work item")
            return
        if not w.SAFE_ID.fullmatch(sid) or not w.SAFE_ID.fullmatch(item.id):
            await self._ack_and_stop(item, "a session or work id with unexpected characters")
            return
        if not item.secret:
            # Without a secret the worker would need the environment key, which can claim every
            # session in the environment; that never goes into a sandbox.
            await self._ack_and_stop(item, "the work item carries no secret")
            return
        if sid in self._busy:
            self._note(sid, "busy", f"{item.id} comes again after this handoff")
            return
        self._busy.add(sid)
        try:
            async with self._lock(sid):
                for run in self._runs_of(sid):
                    if run.work_id == item.id and not run.acked:
                        # Our ack of this item didn't land, so the queue handed it out again. If its
                        # worker still runs, ack it now; if not, start over with the new secret.
                        if await self._still_running(run):
                            await self._ack_run(run)
                            return
                        await self._finish(run, "its worker is gone and the item came again", keep=True)
                await self._handoff(item)
        finally:
            self._busy.discard(sid)

    async def _still_running(self, run: Run) -> bool:
        try:
            statuses = {st.run_id: st for st in await self._runs_in(run.sandbox)}
        except Exception as err:
            return not gone(err)  # unknown: keep the worker, as before the item came again
        st = statuses.get(run.run_id)
        return st is not None and st.state == "alive"  # launched already: "starting" means it never ran

    async def _handoff(self, item: WorkItem) -> None:
        sid = item.session_id
        rid = w.run_id(self._key, sid, item.id)  # bounded length, whatever the work id's
        made: Ensured | None = None
        prepared = False
        try:
            async with _timeout(self.s.handoff_timeout_s):
                await self._retire_earlier(sid, item.id)
                session = await self._session_or_none(sid)
                if session is not None and session.status == "terminated":
                    raise _Moot("the session has terminated")
                template, fork_from = self._overrides(session)
                made = await self.boxes.ensure(sid, template=template, fork_from=fork_from)
                sb = made.sandbox
                for victim, owner, since in made.evicted:
                    when = f"paused since {since:%Y-%m-%d %H:%MZ}" if since else "paused"
                    self._note(
                        owner, "evicted", f"{victim}, {when}: the free tier's disk was full and {sid} needed a sandbox"
                    )
                self._note(sid, made.how, sb.id)
                await self._name_lost_sandbox(sid, made)
                if made.how != "created":
                    await self._clear_leftovers(sb, item.id)
                # The run's directory first: from here the sandbox itself says a handoff is under way
                # (a run "starting"), through the ant download too.
                prepared = True
                prep = w.env(rid, PMA_SESSION=sid, PMA_WORK=item.id)
                await self._exec(sb, w.PREPARE_RUN, prep, timeout=30, what="preparing the run")
                await w.install_ant(sb, self.s)
                assert item.secret is not None
                await sb.files.write(w.secret_path(rid), item.secret, mode=0o600)
        except BaseException as err:
            await asyncio.shield(self._undo(sid, made, rid if prepared else None))
            if isinstance(err, _Moot):
                await self._ack_and_stop(item, str(err))
                await self._delete_session_sandboxes(sid, str(err))
                return
            if isinstance(err, asyncio.CancelledError):
                raise
            self._handoff_failed(item, err)
            return
        await self._launch(item, made, rid)

    async def _name_lost_sandbox(self, sid: str, made: Ensured) -> None:
        """Remember the session's sandbox; if it had another one that is gone, say why, since the
        session's files went with it. On the free tier, say when its run time is nearly used up."""
        sb = made.sandbox
        before = self.last_box.get(sid)  # updated once a worker runs here (_remember), not before
        if made.fresh and before and before != sb.id:
            why = await self._gone_reason(before) or "it was deleted"
            self._note(sid, "sandbox gone", f"{before}: {why}; {sb.id} starts without the session's files")
        expires = getattr(sb, "expires_at", None)
        if self.s.free and expires is not None:
            left = (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)) - self.now()
            if left.total_seconds() < LOW_RUN_S:
                self._note(
                    sid,
                    "run time low",
                    f"{sb.id} has {max(0, int(left.total_seconds() // 60))} minutes of running left on the free tier; "
                    "then it is deleted with its files",
                )

    def _remember(self, sid: str, sandbox_id: str) -> None:
        self.last_box.pop(sid, None)
        self.last_box[sid] = sandbox_id
        while len(self.last_box) > MAX_REMEMBERED:
            self.last_box.popitem(last=False)

    async def _gone_reason(self, sandbox_id: str) -> str | None:
        if self.account is None:
            return None
        try:
            return await asyncio.wait_for(self.account.gone_reason(sandbox_id), REASON_WAIT_S)
        except Exception as err:  # a reason is nice to have; never fail a handoff or a probe on it
            log.debug("reading why %s is gone failed: %s", sandbox_id, err)
            return None

    async def _launch(self, item: WorkItem, made: Ensured, rid: str) -> None:
        """Start ant, then decide from what the sandbox says, outside the handoff timeout. From the
        launch on the worker may hold the secret and be serving the session, and stopping it would
        stop the work item too, so it is stopped only when the sandbox says it has exited."""
        sid, sb = item.session_id, made.sandbox
        launch_env = w.env(
            rid,
            PMA_WORKDIR=self.s.workdir,
            PMA_MAX_IDLE=str(self.s.max_idle_s),
            ANTHROPIC_SESSION_ID=sid,
            ANTHROPIC_WORK_ID=item.id,
            ANTHROPIC_ENVIRONMENT_ID=self.s.environment_id,
        )
        if self.s.base_url.rstrip("/") != "https://api.anthropic.com":
            launch_env["ANTHROPIC_BASE_URL"] = self.s.base_url
        out, launch_err = "", None
        try:
            out = await self._exec(sb, w.LAUNCH, launch_env, timeout=30, what="starting ant")
        except asyncio.CancelledError:
            raise  # shutting down: if ant started, a restart adopts it
        except Exception as err:
            launch_err = err
        await asyncio.sleep(SETTLE_S)
        try:
            st: w.RunStatus | None = {s.run_id: s for s in await self._runs_in(sb)}.get(rid)
            read_err = None
        except Exception as err:
            st, read_err = None, err
        lost = next((e for e in (launch_err, read_err) if e is not None and gone(e)), None)
        if st is not None and st.state == "alive":
            ack = True  # it runs, whatever the launch command's answer was
        elif lost is not None:
            self._handoff_failed(item, lost)  # the sandbox is gone, and its worker with it
            return
        elif read_err is None or (launch_err is None and not out.startswith("started")):
            # The sandbox says the worker isn't running (it exited, was killed, or never started),
            # or the launch said it exited: nothing to stop, so clean up and leave it to the queue.
            tail = ""
            with contextlib.suppress(Exception):
                tail = await self._collect(sb, rid, tail=15)
            if launch_err is not None:
                why: BaseException = launch_err
            else:
                code: Any = st.number if st is not None and st.state == "exited" else None
                if code is None and out.startswith("exited"):
                    code = out.split()[-1]
                why = HandoffError(f"ant exited at once (exit {code}): {tail.strip()[-600:] or out.strip()}")
            await asyncio.shield(self._undo(sid, made, None))
            self._handoff_failed(item, why)
            return
        elif launch_err is None:
            ack = True  # ant started and only the status check failed: the probes see how it goes
            self._note(sid, "status unread", f"{item.id}: {_describe(read_err)}; the probes decide")
        else:
            # Neither command answered: ant may or may not be running. Leave the item unacked: if it
            # comes again, a live worker gets the ack and a dead one is replaced.
            ack = False
            self._note(sid, "launch unclear", f"{item.id}: {_describe(launch_err)}")
        run = Run(rid, sid, item.id, sb, self.clock(), acked=False)
        self.runs[rid] = run
        self._remember(run.session_id, run.sandbox.id)
        self.failures.pop(item.id, None)
        self._note(sid, "worker started", f"{item.id} in {sb.id}")
        if ack:
            await self._ack_run(run)

    async def _ack_run(self, run: Run) -> None:
        # The sandbox holds the secret now; an ack cut short would only redeliver the item.
        try:
            run.acked = await asyncio.shield(self.cma.ack(run.work_id))
        except CMAError as err:
            self._note(run.session_id, "ack failed", f"{run.work_id}: {err}; acked when it comes again")
            return
        self._note(run.session_id, "acked", run.work_id)
        with contextlib.suppress(Exception):
            await self._exec(run.sandbox, w.MARK_ACKED, w.env(run.run_id), timeout=20, what="marking the ack")

    def _handoff_failed(self, item: WorkItem, err: BaseException | None) -> None:
        sid = item.session_id
        n = self.failures.pop(item.id, 0) + 1
        why = _describe(err) if err is not None else "unknown"
        if n >= self.s.max_handoff_attempts:
            self._note(sid, "gave up", f"{item.id} after {n} tries: {why}")
            self._spawn(self._ack_and_stop(item, f"{n} handoffs failed"))
            return
        self.failures[item.id] = n
        while len(self.failures) > MAX_FAILURES:
            self.failures.popitem(last=False)
        self._note(sid, "handoff failed", f"{item.id} (try {n} of {self.s.max_handoff_attempts}): {why}")

    async def _undo(self, sid: str, made: Ensured | None, rid: str | None) -> None:
        """Clean up a handoff that failed before its worker ran: drop the run and its secret, and
        delete the sandbox if this attempt made it (it holds nothing of the session's), else pause it.
        Each step is tried whatever happened to the one before."""
        if made is None:
            return
        sb = made.sandbox
        if rid is not None:
            try:
                await self._collect(sb, rid)
            except Exception as err:
                if not gone(err):
                    self._note(sid, "cleanup failed", f"{sb.id}: {_describe(err)}")
        if made.fresh:
            try:
                await sb.delete()
                self._note(sid, "deleted", f"{sb.id}, made for the failed handoff")
            except Exception as err:
                if not gone(err):
                    self._note(sid, "cleanup failed", f"{sb.id}: {_describe(err)}")
            return
        await self._pause_if_idle(sb, sid, "after the failed handoff", handing_off=True)

    async def _ack_and_stop(self, item: WorkItem, why: str) -> None:
        self._note(item.session_id or "-", "refused", f"{item.id}: {why}")
        try:
            await self.cma.ack(item.id)
            await self.cma.stop(item.id, force=True)
        except CMAError as err:
            log.warning("%s: ending work item %s failed: %s", item.session_id, item.id, err)

    async def _retire_earlier(self, sid: str, work_id: str) -> None:
        """A new work item means the session's earlier worker has let go of its own; give it a
        moment to exit, then stop it, so two workers never share the sandbox's tools."""
        earlier = self._runs_of(sid)
        deadline = self.clock() + RETIRE_WAIT_S
        while earlier:
            try:
                alive = {st.run_id for st in await self._runs_in(earlier[0].sandbox) if st.state == "alive"}
            except Exception as err:
                if not gone(err):
                    raise
                alive = set()
            for run in [r for r in earlier if r.run_id not in alive]:
                await self._finish(run, "ended", keep=True)
            earlier = self._runs_of(sid)
            if not earlier or self.clock() >= deadline:
                break
            await asyncio.sleep(1.0)
        for run in earlier:
            self._note(sid, "stopping earlier worker", run.work_id)
            await self._stop_run(run, kill=run.work_id == work_id)
            await self._finish(run, "replaced by a new work item", keep=True)

    async def _clear_leftovers(self, sb: Any, work_id: str) -> None:
        """Runs the sandbox holds that no one supervises (a worker frozen when the sandbox paused
        itself, or left by an orchestrator that died) go before a new worker starts: live ones are
        killed, ended ones collected. Killed, not asked to stop: the new item means the old one is
        over, and a worker's own stop could end the new item if the queue reused its id. Unsigned
        ones go too (a worker from before the environment key was rotated): this is the session's
        own sandbox, just before its own worker starts, so a forged one can only kill what the agent
        pointed it at, and at most MAX_STRAYS a time."""
        sid = (sb.labels or {}).get(SESSION, "")
        statuses = [st for st in await self._status(sb) if st.run_id not in self.runs]
        live = [st for st in statuses if st.state in ("alive", "stray")]
        for st in live[:MAX_STRAYS]:
            self._note(sid, "stopping stale worker", f"{st.work_id} in {sb.id}")
            await self._stop(sb, sid, st.work_id, st.run_id, kill=True)
        if len(live) > MAX_STRAYS:
            self._note(sid, "too many stale workers", f"{sb.id}: {len(live) - MAX_STRAYS} left running")
        for st in statuses[: MAX_STRAYS * 2]:
            with contextlib.suppress(Exception):
                await self._collect(sb, st.run_id)

    def _overrides(self, session: Session | None) -> tuple[str | None, str | None]:
        if session is None or not self.s.session_overrides:
            return None, None
        template = session.metadata.get("prized_template") or None
        fork_from = session.metadata.get("prized_fork_from") or None
        if template is not None and not _TEMPLATE.fullmatch(template):
            raise HandoffError(f"prized_template {template!r} is not a template name")
        if fork_from is not None and not _SANDBOX_ID.fullmatch(fork_from):
            raise HandoffError(f"prized_fork_from {fork_from!r} is not a sandbox id")
        return template, fork_from

    # -- supervising ---------------------------------------------------------------------------

    async def _probe_loop(self) -> None:
        while not self.stopping:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._probe_now.wait(), timeout=self.s.probe_every_s)
            self._probe_now.clear()
            try:
                await self.probe_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("probe pass failed")

    def probe_soon(self, delay: float = 0.0) -> None:
        if delay <= 0:
            self._probe_now.set()
        else:
            asyncio.get_running_loop().call_later(delay, self._probe_now.set)

    async def probe_all(self) -> None:
        boxes: dict[str, Any] = {}
        for run in list(self.runs.values()):
            if run.session_id not in self._busy:
                boxes.setdefault(run.sandbox.id, run.sandbox)
        await asyncio.gather(*(self.probe(sb) for sb in boxes.values()))

    async def probe(self, sb: Any) -> None:
        """Check every run in one sandbox (which also keeps it awake) and finish the ended ones."""
        mine = [r for r in self.runs.values() if r.sandbox.id == sb.id]
        if not mine:
            return
        try:
            statuses = {st.run_id: st for st in await self._runs_in(sb)}
        except Exception as err:
            if gone(err):
                why = await self._gone_reason(sb.id)
                for run in mine:
                    self._note(
                        run.session_id, "sandbox gone", f"{sb.id}; ending {run.work_id}" + (f": {why}" if why else "")
                    )
                    self.runs.pop(run.run_id, None)
                    with contextlib.suppress(CMAError):
                        await self.cma.stop(run.work_id, force=True)
                return
            for run in mine:
                run.probe_failures += 1
                if run.probe_failures in (4, 20, 100):
                    self._note(run.session_id, "unreachable", f"{sb.id}: {_describe(err)}")
                if self.clock() - run.started > self.s.run_timeout_s:
                    self.runs.pop(run.run_id, None)
                    self._note(run.session_id, "dropped", f"{run.work_id}: past RUN_TIMEOUT and unreachable")
                    with contextlib.suppress(CMAError):
                        await self.cma.stop(run.work_id, force=True)
            return
        for run in mine:
            run.probe_failures = 0
            if run.stopping:
                continue  # its stop finishes it, under the session's lock
            st = statuses.get(run.run_id)
            if st is None or st.state != "alive":  # a run of ours went through its launch: no "starting"
                why = f"exit {st.number}" if st is not None and st.state == "exited" else "worker gone"
                self._spawn(self._finish_locked(run, why))
            elif self.clock() - run.started > self.s.run_timeout_s:
                run.stopping = True
                self._note(run.session_id, "run timeout", f"{run.work_id} ran past RUN_TIMEOUT")
                self._spawn(self._timeout_run(run))

    async def _timeout_run(self, run: Run) -> None:
        # Under the lock from the stop to the pause: a pause while the stop command still runs
        # would freeze that command with the sandbox.
        async with self._lock(run.session_id):
            await self._stop_run(run)
            await self._finish(run, "run timeout")

    async def _stop_run(self, run: Run, *, kill: bool = False) -> None:
        await self._stop(run.sandbox, run.session_id, run.work_id, run.run_id, kill=kill)

    async def _stop(self, sb: Any, sid: str, work_id: str, rid: str, *, kill: bool = False) -> None:
        extra = {"PMA_GRACE": str(self.s.stop_grace_s), **({"PMA_SIGNAL": "KILL"} if kill else {})}
        wait = self.s.stop_grace_s + 20
        try:
            await self._exec(sb, w.STOP, w.env(rid, **extra), timeout=wait, what="stopping the worker")
        except Exception as err:
            if not gone(err):
                self._note(sid, "stop failed", f"{work_id}: {_describe(err)}")

    async def _finish_locked(self, run: Run, why: str) -> None:
        async with self._lock(run.session_id):
            await self._finish(run, why)

    async def _finish(self, run: Run, why: str, *, keep: bool = False) -> None:
        """A run ended: collect it, then pause the sandbox, or delete it if the session is over.
        ``keep`` leaves the sandbox as it is (a new run is about to use it). Hold the session's lock."""
        if self.runs.pop(run.run_id, None) is None:
            return
        sid, sb = run.session_id, run.sandbox
        tail = ""
        with contextlib.suppress(Exception):
            tail = await self._collect(sb, run.run_id, tail=0 if why == "exit 0" else 15)
        self._note(sid, "run ended", f"{run.work_id}: {why}")
        if tail.strip():
            log.warning("%s: the end of ant's log for %s:\n%s", sid, run.work_id, tail.rstrip())
        if not keep:
            await self._wind_down(sb, sid)

    async def _wind_down(self, sb: Any, sid: str) -> None:
        """No run of ours is left in the sandbox: delete it if the session is over, else pause it.
        Hold the session's lock."""
        if self._runs_of(sid) or sid in self._busy:
            return
        state = await self._session_state(sid)
        if state in ("ended", "gone"):
            await self._delete_session_sandboxes(sid, f"session {state}")
            return
        await self._pause_if_idle(sb, sid)

    async def _pause_if_idle(self, sb: Any, sid: str, why: str = "", *, handing_off: bool = False) -> None:
        """Pause the sandbox unless something still runs in it: what the sandbox itself says decides,
        not only what this process knows (a live run it holds is adopted). Hold the session's lock.
        ``handing_off``: the caller is this session's handoff, so its being busy is no reason to wait."""
        if any(r.sandbox.id == sb.id for r in self.runs.values()):
            return
        try:
            await sb.refresh()  # a command in a paused sandbox would resume it
            if sb.state != "running":
                return
            statuses = await self._runs_in(sb)
        except Exception as err:
            if not gone(err):
                self._note(sid, "pause skipped", f"{sb.id}: its runs can't be read ({_describe(err)})")
            return
        if await self._take_over(sb, statuses):
            return
        if not handing_off and sid in self._busy:
            return  # a handoff is waiting for the lock: it uses the sandbox as it is
        try:
            await self.boxes.pause(sb)
            self._note(sid, "paused", f"{sb.id}{', ' + why if why else ''}")
        except Exception as err:
            if not gone(err):
                self._note(sid, "pause failed", f"{sb.id}: {_describe(err)}; it pauses itself after AUTO_PAUSE")

    async def _take_over(self, sb: Any, statuses: list[w.RunStatus], *, why: str = "") -> bool:
        """Supervise what a sandbox none of our runs use holds: the newest live run is adopted, unless
        it is past RUN_TIMEOUT. Other live ones, and collected runs whose process group is alive
        again (the agent can write a pid file), are killed, at most MAX_STRAYS a pass: one worker
        per sandbox. Ended ones are collected. True if a run lives (or is starting) in it. Hold the
        session's lock."""
        sid = (sb.labels or {}).get(SESSION, "")
        alive = sorted((st for st in statuses if st.state == "alive"), key=lambda st: st.started or 0, reverse=True)
        keep = alive[0] if alive and self._age(alive[0]) <= self.s.run_timeout_s else None
        starting = [st for st in statuses if st.state == "starting" and self._live(st)]
        stops = 0
        for st in statuses:
            if st is keep or st in starting or st.run_id in self.runs:
                continue  # (a run starting is another process's handoff under way: leave it be)
            if st.state in ("alive", "stray"):
                if stops >= MAX_STRAYS:
                    continue
                stops += 1
                late = keep is None and bool(alive) and st is alive[0]
                self._note(sid, "run timeout" if late else "stopping extra worker", f"{st.work_id} in {sb.id}")
                await self._stop(sb, sid, st.work_id, st.run_id, kill=True)
            tail = ""
            with contextlib.suppress(Exception):
                tail = await self._collect(sb, st.run_id, tail=0 if st.number == 0 else 15)
            if st.state not in ("alive", "stray"):
                self._note(sid, "run ended", f"{st.work_id}: {why or 'while no orchestrator watched'}")
                if tail.strip():
                    log.warning("%s: the end of ant's log for %s:\n%s", sid, st.work_id, tail.rstrip())
        if keep is not None:
            self._adopt(sb, keep)
            return True
        return bool(starting)

    def _age(self, st: w.RunStatus) -> float:
        return max(0.0, time.time() - (st.started or time.time()))

    def _adopt(self, sb: Any, st: w.RunStatus) -> None:
        age = self._age(st)
        self.runs[st.run_id] = Run(st.run_id, st.session_id, st.work_id, sb, self.clock() - age, acked=st.acked)
        self._remember(st.session_id, sb.id)
        self._note(st.session_id, "adopted", f"{st.work_id} in {sb.id}")

    # -- sessions that end, and the sweep ------------------------------------------------------

    async def on_event(self, event: Event) -> str:
        """Act on a verified webhook event. Returns what was done, for the reply and the log."""
        if event.type == "session.status_run_started":
            self.wake()
            return "draining the queue"
        if event.type in ("session.status_terminated", "session.deleted"):
            self._spawn(self.session_over(event.resource_id))
            return "checking the session"
        if event.type == "session.status_idled":
            # The worker exits MAX_IDLE after an idle with end_turn: look then, and pause.
            if any(r.session_id == event.resource_id for r in self.runs.values()):
                self.probe_soon(self.s.max_idle_s + 2)
            return "noted"
        return "ignored"

    async def session_over(self, sid: str) -> None:
        """Delete the session's sandboxes if the API confirms it terminated or is gone. Events can
        arrive out of order, so the fetched session decides, not the event."""
        if not w.SAFE_ID.fullmatch(sid):
            return
        async with self._lock(sid):
            state = await self._session_state(sid)
            if state not in ("ended", "gone"):
                return
            for run in self._runs_of(sid):
                await self._stop_run(run)
                self.runs.pop(run.run_id, None)
            await self._delete_session_sandboxes(sid, f"session {state}")

    async def _delete_session_sandboxes(self, sid: str, why: str) -> None:
        try:
            deleted = await self.boxes.delete_session(sid)
        except Exception as err:
            self._note(sid, "delete failed", _describe(err))
            return
        if deleted:
            self._note(sid, "deleted", f"{', '.join(deleted)} ({why})")

    async def _sweep_loop(self) -> None:
        while not self.stopping:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sweep failed")
            await self._sleep_unless_stopping(self.s.sweep_every_s)

    async def sweep(self) -> list[str]:
        """Delete the sandboxes of sessions that ended (a lost webhook, a stopped orchestrator), and
        pause running ones that no run uses (a live run no one supervises is adopted instead).
        Paused ones of live sessions are kept until KEEP_PAUSED deletes them."""
        swept: list[str] = []
        served = {r.sandbox.id for r in self.runs.values()}
        for sb in await self.boxes.managed():
            sid = (sb.labels or {}).get(SESSION, "")
            if sb.id in served or sid in self._busy or not w.SAFE_ID.fullmatch(sid):
                continue
            async with self._lock(sid):
                if sid in self._busy or any(r.sandbox.id == sb.id for r in self.runs.values()):
                    continue
                state = await self._session_state(sid)
                if state in ("ended", "gone"):
                    try:
                        await sb.delete()
                        swept.append(sb.id)
                        self._note(sid, "swept", f"{sb.id} (session {state})")
                    except Exception as err:
                        if not gone(err):
                            self._note(sid, "sweep failed", f"{sb.id}: {_describe(err)}")
                elif sb.state == "running":
                    await self._pause_if_idle(sb, sid, "which no run was using")
                elif sb.state == "paused" and self._past_keep_paused(sb):
                    # Copies of a warm parent inherit its auto-delete (off, or WARM_PARENT_KEEP), so
                    # KEEP_PAUSED is held here for them; Prized holds it for the others too. The list
                    # is from the start of the pass: read the sandbox again, since a short run may
                    # have used and paused it since.
                    try:
                        await sb.refresh()
                    except Exception as err:
                        if not gone(err):
                            self._note(sid, "sweep failed", f"{sb.id}: {_describe(err)}")
                        continue
                    if sb.state != "paused" or not self._past_keep_paused(sb):
                        continue
                    try:
                        await sb.delete()
                        swept.append(sb.id)
                        self._note(sid, "swept", f"{sb.id} (paused past KEEP_PAUSED)")
                    except Exception as err:
                        if not gone(err):
                            self._note(sid, "sweep failed", f"{sb.id}: {_describe(err)}")
        return swept

    def _past_keep_paused(self, sb: Any) -> bool:
        keep, since = self.s.keep_paused_s, idle_since(sb)
        return bool(keep) and since is not None and (self.now() - since).total_seconds() > keep

    async def reconcile(self) -> None:
        """Pick up what a previous process left: a live worker in each running sandbox is supervised
        again (at most one per sandbox), and ended runs are finished."""
        try:
            boxes = await self.boxes.managed()
        except PrizedError as err:
            raise SystemExit(
                f"can't list Prized sandboxes: {err} (is PRIZED_API_KEY a key with Sandboxes: Write?)"
            ) from err
        for sb in boxes:
            sid = (sb.labels or {}).get(SESSION, "")
            if sb.state != "running" or not w.SAFE_ID.fullmatch(sid):
                continue
            async with self._lock(sid):
                try:
                    statuses = await self._runs_in(sb)
                except Exception as err:
                    log.warning("reconcile: can't read %s: %s", sb.id, _describe(err))
                    continue
                if await self._take_over(sb, statuses) or not statuses:
                    continue
                await self._wind_down(sb, sid)  # runs ended while no one watched

    # -- helpers -------------------------------------------------------------------------------

    def _runs_of(self, sid: str) -> list[Run]:
        return [r for r in self.runs.values() if r.session_id == sid]

    def _live(self, st: w.RunStatus) -> bool:
        """For a run no one here supervises: alive, or still starting within the time a handoff may
        take. (A run of ours went through its launch, so for it only "alive" counts.)"""
        if st.state == "alive":
            return True
        if st.state != "starting" or st.started is None:
            return False
        return time.time() - st.started < self.s.handoff_timeout_s + LAUNCH_S

    async def _session_or_none(self, sid: str) -> Session | None:
        """The session, for its metadata. A failed read doesn't block the run."""
        try:
            session = await self.cma.session(sid)
        except CMAError as err:
            log.warning("%s: reading the session failed (%s); serving it without its metadata", sid, err)
            return None
        if session is None:
            raise _Moot("the session no longer exists")
        return session

    async def _session_state(self, sid: str) -> str:
        """alive, ended, gone, or unknown (the API didn't answer: nothing is deleted then)."""
        try:
            session = await self.cma.session(sid)
        except CMAError:
            return "unknown"
        if session is None:
            return "gone"
        return "ended" if session.status == "terminated" else "alive"

    async def _status(self, sb: Any) -> list[w.RunStatus]:
        r = await sb.exec(w.STATUS, env=w.env(), timeout=20)
        if r.exit_code != 0:
            raise HandoffError(f"the status check failed ({r.exit_code}): {r.stderr.strip()[-200:]}")
        return w.parse_status(r.stdout)

    async def _runs_in(self, sb: Any) -> list[w.RunStatus]:
        """The sandbox's runs that an orchestrator of this environment started for the sandbox's own
        session. Anything else in its status (the agent can write the files it is made from) is
        ignored: it can't add runs, take slots, keep the sandbox awake or touch another session."""
        sid = (sb.labels or {}).get(SESSION, "")
        out = []
        for st in await self._status(sb):
            if st.session_id == sid and w.genuine(self._keys, st):
                out.append(st)
            else:
                log.debug("%s: ignoring a run line that isn't ours: %s", sb.id, st.run_id)
        return out

    async def _collect(self, sb: Any, rid: str, *, tail: int = 0) -> str:
        r = await sb.exec(w.COLLECT, env=w.env(rid, PMA_TAIL=str(tail)), timeout=20)
        return r.stdout if r.exit_code == 0 else ""

    async def _exec(self, sb: Any, script: str, env: dict[str, str], *, timeout: float, what: str = "") -> str:
        r = await sb.exec(script, env=env, timeout=timeout)
        if r.exit_code != 0:
            raise HandoffError(f"{what or 'a command'} failed ({r.exit_code}): {r.stderr.strip()[-300:]}")
        return r.stdout

    def _note(self, session: str, what: str, detail: str = "") -> None:
        self.notes.append(Note(session, what, detail))
        del self.notes[:-500]
        log.info("%s: %s%s", session, what, f" ({detail})" if detail else "")


class _Moot(Exception):
    """The work item's session is over; end the item instead of serving it."""


def _describe(err: BaseException) -> str:
    if isinstance(err, PrizedError):
        return f"prized {err.code}" + (f" ({err.reason})" if err.reason else "") + f": {err.message[:200]}"
    if isinstance(err, (asyncio.TimeoutError, TimeoutError)):
        return "timed out"
    return f"{type(err).__name__}: {str(err)[:300]}"


@contextlib.asynccontextmanager
async def _timeout(seconds: float) -> Any:
    """asyncio.timeout for Python 3.10 too."""
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    fired = False

    def fire() -> None:
        nonlocal fired
        fired = True
        task.cancel()

    handle = loop.call_later(seconds, fire)
    try:
        yield
    except asyncio.CancelledError as err:
        if fired:
            uncancel = getattr(task, "uncancel", None)
            # 3.11+: take back our own cancellation; if someone else cancelled too, theirs goes on.
            if uncancel is not None and uncancel() > 0:
                raise
            raise TimeoutError(f"timed out after {seconds:g}s") from err
        raise
    finally:
        handle.cancel()
