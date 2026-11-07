"""The orchestrator: claims a self-hosted environment's work and runs each session in its sandbox.

The flow follows Anthropic's own dispatcher (anthropic-cli ``sandboxes/dispatcher``), with Prized
sandboxes in place of Kubernetes pods:

1. **Claim.** A ``session.status_run_started`` webhook (or, without webhooks, a long poll) wakes the
   intake, which polls the queue until it is empty. It polls only while a handoff slot is free, so
   it never claims work it can't start.
2. **Hand off.** The session's sandbox is resumed (files and processes as the last run left them),
   or a warm parent is forked, or a sandbox is created. ant is installed if missing (checked against
   its sha256), the work item's secret is written into the sandbox, and ``ant beta:worker run``
   starts, detached. Only then is the item **acked**: a handoff that fails leaves it unacked, and
   the queue hands it out again after the reclaim window (twice the handoff timeout). A sandbox made
   for the failed attempt is deleted; one that holds the session's files is paused. After
   ``max_handoff_attempts`` failures the item is stopped instead, so it doesn't come back forever.
3. **Supervise.** Every ``probe_every_s`` one status command per sandbox checks its workers. That
   is also what keeps the sandbox awake: Prized pauses a sandbox after ``auto_pause_s`` without
   commands, so if this process dies its sandboxes pause themselves.
4. **Finish.** When a worker exits (a minute after the session goes idle, by default), the
   orchestrator asks the API about the session: terminated or gone, and the sandbox is deleted;
   otherwise it is paused, memory and all, for the next run. ``session.status_terminated`` and
   ``session.deleted`` webhooks, and a sweep every ``sweep_every_s``, delete the rest.

The environment key stays in this process. A sandbox gets one work item's secret, which reaches
that one session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from prized import PrizedError

from . import worker as w
from .cma import CMA, CMAError, Session, WorkItem
from .config import Settings
from .sandboxes import SESSION, Ensured, SessionSandboxes, gone
from .webhooks import Event

log = logging.getLogger("prized_managed_agents")

_TEMPLATE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SANDBOX_ID = re.compile(r"sbx_[A-Za-z0-9]{1,64}")
SETTLE_S = 1.0  # a worker that dies at once (a bad secret, a broken binary) dies within this
RETIRE_WAIT_S = 30.0  # how long an earlier run of the session gets to finish before it is stopped


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
    ) -> None:
        self.s = settings
        self.cma = cma
        self.boxes = boxes
        self.clock = clock
        self.runs: dict[str, Run] = {}  # by run id
        self.notes: list[Note] = []
        self.failures: dict[str, int] = defaultdict(int)  # failed handoffs, by work id
        self._busy: set[str] = set()  # sessions with a handoff under way
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._slots = asyncio.Semaphore(settings.max_handoffs)
        self._handoffs = 0
        self._wake = asyncio.Event()
        self._probe_now = asyncio.Event()
        self._stopping = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started = False

    # -- the loops -----------------------------------------------------------------------------

    async def start(self) -> None:
        """Check the environment key and the Prized key, and pick up runs a previous process left."""
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
            # Handoffs under way finish (bounded by their timeout); runs keep going in their
            # sandboxes, which pause themselves after AUTO_PAUSE unless a new process takes over.
            pending = [t for t in self._tasks if not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=self.s.handoff_timeout_s + 15)
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
            async with self._locks[sid]:
                for run in self._runs_of(sid):
                    if run.work_id == item.id and not run.acked:
                        # Our earlier ack of this item failed: the worker that holds it is running.
                        await self._ack_run(run)
                        return
                await self._handoff(item)
        finally:
            self._busy.discard(sid)

    async def _handoff(self, item: WorkItem) -> None:
        sid = item.session_id
        made: Ensured | None = None
        rid: str | None = None
        launched = False
        try:
            async with _timeout(self.s.handoff_timeout_s):
                await self._retire_earlier(sid)
                session = await self._session_or_none(sid)
                if session is not None and session.status == "terminated":
                    raise _Moot("the session has terminated")
                template, fork_from = self._overrides(session)
                made = await self.boxes.ensure(sid, template=template, fork_from=fork_from)
                sb = made.sandbox
                self._note(sid, made.how, sb.id)
                await w.install_ant(sb, self.s)
                rid = w.run_id(item.id)
                await self._exec(sb, w.PREPARE_RUN, w.env(rid), timeout=30, what="preparing the run")
                assert item.secret is not None
                await sb.files.write(w.secret_path(rid), item.secret, mode=0o600)
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
                out = await self._exec(sb, w.LAUNCH, launch_env, timeout=30, what="starting ant")
                launched = True
                await asyncio.sleep(SETTLE_S)
                status = {r.run_id: r for r in await self._status(sb)}.get(rid)
                if status is None or status.state != "alive":
                    tail = await self._collect(sb, rid, tail=15)
                    code = status.number if status is not None else None
                    raise HandoffError(f"ant exited at once (exit {code}): {tail.strip()[-600:] or out.strip()}")
        except BaseException as err:
            await asyncio.shield(self._undo(sid, made, rid, launched))
            if isinstance(err, _Moot):
                await self._ack_and_stop(item, str(err))
                await self._delete_session_sandboxes(sid, str(err))
                return
            if isinstance(err, asyncio.CancelledError):
                raise
            self._handoff_failed(item, err)
            return
        run = Run(rid, sid, item.id, made.sandbox, self.clock(), acked=False)
        self.runs[rid] = run
        self.failures.pop(item.id, None)
        self._note(sid, "worker started", f"{item.id} in {made.sandbox.id}")
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

    def _handoff_failed(self, item: WorkItem, err: BaseException) -> None:
        sid = item.session_id
        self.failures[item.id] += 1
        n = self.failures[item.id]
        why = _describe(err)
        if n >= self.s.max_handoff_attempts:
            self.failures.pop(item.id, None)
            self._note(sid, "gave up", f"{item.id} after {n} tries: {why}")
            self._spawn(self._ack_and_stop(item, f"{n} handoffs failed"))
        else:
            self._note(sid, "handoff failed", f"{item.id} (try {n} of {self.s.max_handoff_attempts}): {why}")

    async def _undo(self, sid: str, made: Ensured | None, rid: str | None, launched: bool) -> None:
        """Clean up a handoff that failed: stop its worker, drop the secret, and delete the sandbox
        if this attempt made it (it holds nothing of the session's), else pause it."""
        if made is None:
            return
        sb = made.sandbox
        try:
            if rid is not None:
                if launched:
                    await self._exec(
                        sb, w.STOP, w.env(rid, PMA_GRACE=str(self.s.stop_grace_s)), timeout=self.s.stop_grace_s + 20
                    )
                await self._collect(sb, rid)
            if made.fresh:
                await sb.delete()
                self._note(sid, "deleted", f"{sb.id}, made for the failed handoff")
            elif not self._runs_of(sid):
                await self.boxes.pause(sb)
                self._note(sid, "paused", f"{sb.id} after the failed handoff")
        except Exception as err:
            if not gone(err):
                self._note(sid, "cleanup failed", f"{sb.id}: {_describe(err)}")

    async def _ack_and_stop(self, item: WorkItem, why: str) -> None:
        self._note(item.session_id or "-", "refused", f"{item.id}: {why}")
        try:
            await self.cma.ack(item.id)
            await self.cma.stop(item.id, force=True)
        except CMAError as err:
            log.warning("%s: ending work item %s failed: %s", item.session_id, item.id, err)

    async def _retire_earlier(self, sid: str) -> None:
        """A new work item means the session's earlier worker has let go of its own; give it a
        moment to exit, then stop it, so two workers never share the sandbox's tools."""
        earlier = self._runs_of(sid)
        deadline = self.clock() + RETIRE_WAIT_S
        while earlier:
            try:
                alive = {st.run_id for st in await self._status(earlier[0].sandbox) if st.state == "alive"}
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
            await self._stop_run(run)
            await self._finish(run, "replaced by a new work item", keep=True)

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
            statuses = {r.run_id: r for r in await self._status(sb)}
        except Exception as err:
            if gone(err):
                for run in mine:
                    self._note(run.session_id, "sandbox gone", f"{sb.id}; ending {run.work_id}")
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
            if st is None or st.state != "alive":
                why = "worker gone" if st is None or st.state == "lost" else f"exit {st.number}"
                self._spawn(self._finish_locked(run, why))
            elif self.clock() - run.started > self.s.run_timeout_s:
                run.stopping = True
                self._note(run.session_id, "run timeout", f"{run.work_id} ran past RUN_TIMEOUT")
                self._spawn(self._timeout_run(run))

    async def _timeout_run(self, run: Run) -> None:
        # Under the lock from the stop to the pause: a pause while the stop command still runs
        # would freeze that command with the sandbox.
        async with self._locks[run.session_id]:
            await self._stop_run(run)
            await self._finish(run, "run timeout")

    async def _stop_run(self, run: Run) -> None:
        try:
            await self._exec(
                run.sandbox,
                w.STOP,
                w.env(run.run_id, PMA_GRACE=str(self.s.stop_grace_s)),
                timeout=self.s.stop_grace_s + 20,
                what="stopping the worker",
            )
        except Exception as err:
            if not gone(err):
                self._note(run.session_id, "stop failed", f"{run.work_id}: {_describe(err)}")

    async def _finish_locked(self, run: Run, why: str) -> None:
        async with self._locks[run.session_id]:
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
        if keep or self._runs_of(sid) or sid in self._busy:
            return
        state = await self._session_state(sid)
        if state in ("ended", "gone"):
            await self._delete_session_sandboxes(sid, f"session {state}")
            return
        try:
            await self.boxes.pause(sb)
            self._note(sid, "paused", sb.id)
        except Exception as err:
            if not gone(err):
                self._note(sid, "pause failed", f"{sb.id}: {_describe(err)}; it pauses itself after AUTO_PAUSE")

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
        async with self._locks[sid]:
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
        pause running ones that no run of ours uses. Paused ones of live sessions are kept until
        KEEP_PAUSED deletes them."""
        swept: list[str] = []
        served = {r.sandbox.id for r in self.runs.values()}
        for sb in await self.boxes.managed():
            sid = (sb.labels or {}).get(SESSION, "")
            if sb.id in served or sid in self._busy or not w.SAFE_ID.fullmatch(sid):
                continue
            async with self._locks[sid]:
                if sb.id in {r.sandbox.id for r in self.runs.values()}:
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
                    with contextlib.suppress(Exception):
                        if not any(st.state == "alive" for st in await self._status(sb)):
                            await self.boxes.pause(sb)
                            self._note(sid, "paused", f"{sb.id}, which no run was using")
        return swept

    async def reconcile(self) -> None:
        """Pick up what a previous process left: running workers are supervised again, and ended
        runs are finished."""
        try:
            boxes = await self.boxes.managed()
        except PrizedError as err:
            raise SystemExit(
                f"can't list Prized sandboxes: {err} (is PRIZED_API_KEY a key with Sandboxes: Write?)"
            ) from err
        for sb in boxes:
            if sb.state != "running":
                continue
            try:
                statuses = await self._status(sb)
            except Exception as err:
                log.warning("reconcile: can't read %s: %s", sb.id, _describe(err))
                continue
            for st in statuses:
                run = Run(st.run_id, st.session_id, st.work_id, sb, self.clock(), acked=st.acked)
                self.runs[st.run_id] = run
                if st.state == "alive":
                    self._note(st.session_id, "adopted", f"{st.work_id} in {sb.id}")
                else:
                    async with self._locks[st.session_id]:
                        await self._finish(run, "ended while no orchestrator watched")

    # -- helpers -------------------------------------------------------------------------------

    def _runs_of(self, sid: str) -> list[Run]:
        return [r for r in self.runs.values() if r.session_id == sid]

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
            if uncancel is not None:
                uncancel()  # 3.11+: this cancellation was ours, not the caller's
            raise TimeoutError(f"timed out after {seconds:g}s") from err
        raise
    finally:
        handle.cancel()
