"""The orchestrator: serves one outpost's queue with Prized sandboxes.

Each pass lists the outpost's queue (a watch stream wakes it early), checks the remotes it runs,
asks ``plan`` what to do, and runs those actions on a thread pool, one at a time per session.
It holds the Devin token and the Prized key itself; a sandbox only ever gets its own session's
connect token.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

from prized import PrizedError

from ..machines import GONE, Machines
from . import remote as rm
from .api import ClaimConflict, Entry, NotQueued, OutpostsAPI, OutpostsError
from .plan import Action, Rules, Track, plan

log = logging.getLogger("prized_outposts.devin")

SESSION = "devin-session"
OUTPOST = "devin-outpost"


class Orchestrator:
    def __init__(
        self,
        api: OutpostsAPI,
        machines: Machines,
        remote: rm.RemoteSource,
        *,
        outpost_id: str,
        rules: Rules,
        workdir: str = "/home/prized/work",
        chrome_path: str | None = None,
        desktop: bool = False,
        poll_interval: float = 5.0,
        sweep_interval: float = 120.0,
        clock: Callable[[], float] = time.time,
        workers: int | None = None,
    ) -> None:
        self.api = api
        self.machines = machines
        self.remote = remote
        self.outpost_id = outpost_id
        self.rules = rules
        self.workdir = workdir
        self.chrome_path = chrome_path
        self.desktop = desktop
        self.poll_interval = poll_interval
        self.min_pass = min(1.0, poll_interval)
        self.sweep_interval = sweep_interval
        self.clock = clock
        self.tracks: dict[str, Track] = {}
        self.sandboxes: dict[str, Any] = {}
        self.entries: dict[str, Entry] = {}
        self.notified: set[str] = set()
        self.bannered: set[str] = set()  # sessions showing our give-up message
        self.failures: dict[str, int] = {}  # failed claims in a row, for the backoff
        self.events: list[tuple[str, str, str]] = []  # (session, what, detail): for tests and status
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=workers or max(4, rules.max_concurrent * 2 + 2))
        self._last_sweep = 0.0

    # -- the loop ------------------------------------------------------------------------------

    def run(self, stop: threading.Event, *, watch: bool = True) -> None:
        log.info("serving outpost %s as %s", self.outpost_id, self.rules.acceptor_id)
        watcher = None
        if watch:
            watcher = threading.Thread(
                target=self._watch_loop, args=(stop,), daemon=True, name="outposts-watch"
            )
            watcher.start()
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception:  # keep serving; the next pass converges
                log.exception("pass failed")
            self._wake.wait(self.poll_interval)
            self._wake.clear()
            stop.wait(max(0.0, self.min_pass - (time.monotonic() - started)))  # at most one pass a second
        log.info("stopping: sessions keep running in their sandboxes; restart to pick them up again")

    def _watch_loop(self, stop: threading.Event) -> None:
        cursor = None
        backoff = 1.0
        while not stop.is_set():
            try:
                if cursor is None:
                    _, cursor = self.api.list_all(self.outpost_id)
                opened = time.monotonic()
                for _kind, _entry, event_cursor in self.api.watch(self.outpost_id, cursor, stop):
                    cursor = event_cursor or cursor
                    self._wake.set()
                lasted = time.monotonic() - opened
            except Exception as err:  # the stream ends every few minutes; reconnect
                log.debug("watch ended: %s", err)
                lasted = 0.0
            if lasted >= 30.0:
                backoff = 1.0  # a normal stream that ran its course
            else:
                stop.wait(backoff)  # one that ended at once (an error, a proxy): don't hammer the API
                backoff = min(backoff * 2, 30.0)

    def tick(self) -> list[Action]:
        entries, _ = self.api.list_all(self.outpost_id)
        now = self.clock()
        with self._lock:
            self.entries = {e.session_id: e for e in entries}
        self._probe(now)
        with self._lock:
            for e in entries:
                t = self.tracks.get(e.session_id)
                if t is not None and e.session_status == "suspended" and t.suspended_seen is None:
                    t.suspended_seen = now
            actions = plan(entries, self.tracks, self.rules, now, self.notified)
            for a in actions:
                t = self.tracks.setdefault(a.session_id, Track(a.session_id))
                t.busy = True
        futures = [self._pool.submit(self._run_action, a) for a in actions]
        if now - self._last_sweep >= self.sweep_interval:
            self._last_sweep = now
            futures.append(self._pool.submit(self.sweep))
        self._futures = futures
        return actions

    def drain(self, timeout: float = 120.0) -> None:
        """Wait for the actions the last pass started (tests and shutdown)."""
        wait(getattr(self, "_futures", []), timeout=timeout)

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    # -- probes --------------------------------------------------------------------------------

    def _probe(self, now: float) -> None:
        with self._lock:
            targets = [
                (sid, self.sandboxes[sid])
                for sid, t in self.tracks.items()
                if not t.busy and sid in self.sandboxes
            ]
        futures: list[tuple[str, Future]] = [
            (sid, self._pool.submit(sb.exec, rm.status_script(sid), timeout=20)) for sid, sb in targets
        ]
        for sid, fut in futures:
            reached = True
            try:
                state, num = rm.parse_status(fut.result(timeout=60).stdout)
            except PrizedError as err:
                state, num, reached = ("none" if err.code in GONE else "unknown"), None, err.code in GONE
                if err.code in GONE:
                    with self._lock:
                        self.sandboxes.pop(sid, None)
            except Exception:
                state, num, reached = "unknown", None, False
            with self._lock:
                t = self.tracks.get(sid)
                if t is None:
                    continue
                t.probe_failures = 0 if reached else t.probe_failures + 1
                if state == "exited" and t.remote != "exited":
                    t.exited_seen = now
                t.remote, t.exit_code = state, (num if state == "exited" else None)
                if state == "alive":
                    t.pid = num

    # -- actions -------------------------------------------------------------------------------

    def _run_action(self, action: Action) -> None:
        sid = action.session_id
        with self._lock:
            track = self.tracks.get(sid)  # this action's own track; a later pass may make a new one
        try:
            getattr(self, f"_do_{action.kind}")(sid, action.reason)
            with self._lock:
                track.attempts = 0
                if action.kind == "claim":
                    self.failures.pop(sid, None)
        except (ClaimConflict, NotQueued) as err:
            self._note(sid, "lost", f"{action.kind}: {err.status}")
            self._forget(sid, track)
        except Exception as err:
            self._note(sid, "error", f"{action.kind}: {_describe(err)}")
            if action.kind == "claim":
                # Nothing serves this fresh claim: hand it back, forget the session, and wait before
                # claiming it again, longer each time.
                self._release_quietly(sid)
                with self._lock:
                    n = self.failures[sid] = self.failures.get(sid, 0) + 1
                    self.rules.cooldown[sid] = self.clock() + min(5.0 * 2 ** (n - 1), 600.0)
                self._forget(sid, track)
            else:
                # Keep the claim: a worker may still be serving (this could be a Prized blip). Retry later.
                with self._lock:
                    track.attempts += 1
                    track.next_try = self.clock() + min(5.0 * 2 ** (track.attempts - 1), 120.0)
        finally:
            with self._lock:
                track.busy = False
            self._wake.set()

    def _do_claim(self, sid: str, _reason: str) -> None:
        entry = self.api.claim(sid)
        self._note(sid, "claimed", entry.kind)
        self._serve(sid, entry)

    def _do_start(self, sid: str, reason: str) -> None:
        entry = self.api.claim(sid)  # renews our claim and hands out a fresh connect token
        self._note(sid, "restarting", reason)
        self._serve(sid, entry)

    def _serve(self, sid: str, claimed: Entry) -> None:
        if not claimed.connect_token or not claimed.gateway_url:
            raise OutpostsError(200, "the claim carried no connect token or gateway URL")
        queued = self.entries.get(sid)
        key = {SESSION: sid}
        made = self.machines.ensure(key, extra_labels={OUTPOST: self.outpost_id})
        sb = made.sandbox
        with self._lock:
            self.sandboxes[sid] = sb
            t = self.tracks.setdefault(sid, Track(sid))
            t.sandbox_id = sb.id
        self._note(sid, made.how, f"{sb.id} in {made.seconds:.2f}s")
        pinned = claimed.remote_binary_sha or (queued.remote_binary_sha if queued else None)
        binary = self.remote.resolve(pinned)
        state = self._prepare(sb, sid, binary)
        if state == "alive":
            self._note(sid, "attached", "a worker was already serving")
        else:
            env = rm.launch_env(
                sid,
                gateway_url=claimed.gateway_url,
                connect_token=claimed.connect_token,
                binary_path=f"{rm.HOME}/.devin/worker/cache/{binary.filename}",
                workdir=self.workdir,
                chrome_path=self.chrome_path,
                desktop=self.desktop,
            )
            sb.exec(rm.launch_command(sid), env=env, background=True)
            self._wait_alive(sb, sid)
            self._note(sid, "worker started", f"devin-remote {binary.sha}")
        alive, pid = rm.parse_status(sb.exec(rm.status_script(sid), timeout=20).stdout)
        with self._lock:
            t = self.tracks[sid]
            t.remote, t.exit_code, t.exited_seen, t.suspended_seen = "alive", None, None, None
            t.pid = pid if alive == "alive" else None
            t.starts += 1 if state != "alive" else 0
            clear = sid in self.bannered
            self.bannered.discard(sid)
        if clear:
            self.api.operator_message(sid, "")

    def _prepare(self, sb: Any, sid: str, binary: rm.Binary) -> str:
        script = rm.prepare_script(sid, binary, self.workdir)
        result = sb.exec(script, timeout=600)
        if result.exit_code == 4 and binary.local_path:
            rm.ensure_binary_uploaded(sb, binary)
            result = sb.exec(script, timeout=600)
        if result.exit_code != 0:
            raise RuntimeError(
                f"preparing the sandbox failed ({result.exit_code}): {result.stderr.strip()[-300:]}"
            )
        state, _ = rm.parse_status(result.stdout)
        return state

    def _wait_alive(self, sb: Any, sid: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state, num = rm.parse_status(sb.exec(rm.status_script(sid), timeout=20).stdout)
            if state == "alive":
                return
            if state == "exited":
                raise RuntimeError(
                    f"devin-remote exited at once ({num}); its log is {rm.run_dir(sid)}/remote.log"
                )
            if time.monotonic() > deadline:
                raise RuntimeError("devin-remote did not start")
            time.sleep(0.3)

    def _do_renew(self, sid: str, _reason: str) -> None:
        entry = self.api.claim(sid)
        with self._lock:
            if sid in self.entries:
                self.entries[sid].claim_deadline = entry.claim_deadline

    def _do_suspend(self, sid: str, _reason: str) -> None:
        sb = self.sandboxes.get(sid)
        if sb is not None:
            self._stop_remote(sb, sid)
        try:
            paused = self.machines.hibernate({SESSION: sid})
            self._note(sid, "paused", paused.id if paused is not None else "no sandbox")
        except PrizedError as err:
            if err.code not in GONE:
                raise
        with self._lock:
            self.sandboxes.pop(sid, None)  # a probe would resume it
        self.api.release(sid)
        self._note(sid, "released", "suspended")
        self._forget(sid)

    def _do_terminate(self, sid: str, _reason: str) -> None:
        deleted = self.machines.destroy({SESSION: sid})
        self._note(sid, "deleted", ",".join(deleted) or "no sandbox")
        self.api.release(sid)
        self._forget(sid)

    def _do_give_up(self, sid: str, reason: str) -> None:
        with self._lock:
            sb = self.sandboxes.pop(sid, None)  # a probe would resume it once paused
        if sb is not None:
            self._stop_remote(sb, sid)
        try:
            self.machines.hibernate({SESSION: sid})
        except PrizedError as err:
            if err.code not in GONE:
                log.warning("%s: could not pause after giving up: %s", sid, err.code)
        self.api.operator_message(
            sid,
            f"Prized: the Devin worker for this session stopped ({reason}). Its sandbox is paused with "
            "its files; the outpost will try again in about 10 minutes.",
        )
        self.bannered.add(sid)
        self.api.release(sid)
        self.rules.cooldown[sid] = self.clock() + 600
        self._note(sid, "gave up", reason)
        self._forget(sid)

    def _do_refuse(self, sid: str, reason: str) -> None:
        self.notified.add(sid)
        self.api.operator_message(sid, f"Prized can't serve this session: {reason}")
        self._note(sid, "refused", reason)
        self._forget(sid)

    def _do_drop(self, sid: str, reason: str) -> None:
        with self._lock:
            sb = self.sandboxes.pop(sid, None)
            t = self.tracks.get(sid)
            pid, had = (t.pid, bool(t.sandbox_id)) if t is not None else (None, False)
        if sb is not None:
            self._stop_remote(sb, sid, only_pid=pid)
        if sb is not None or had:
            try:
                self.machines.hibernate({SESSION: sid})  # found by label, so a retry still pauses it
            except PrizedError as err:
                if err.code not in GONE:
                    raise
        self._note(sid, "dropped", reason)
        self._forget(sid)

    def _do_abandon(self, sid: str, reason: str) -> None:
        # Another acceptor holds the session now. Stop the worker we started, and only that one: in the
        # same Prized workspace the new holder uses this same sandbox and may run its own.
        with self._lock:
            sb = self.sandboxes.pop(sid, None)
            pid = self.tracks[sid].pid if sid in self.tracks else None
        if sb is not None and pid is not None:
            self._stop_remote(sb, sid, only_pid=pid)
        self._note(sid, "abandoned", reason)
        self._forget(sid)

    def _stop_remote(self, sb: Any, sid: str, only_pid: int | None = None) -> None:
        try:
            if sb.state == "running":
                sb.exec(rm.stop_script(sid, only_pid=only_pid), timeout=30)
        except PrizedError as err:
            if err.code not in GONE:
                log.info("%s: could not stop the worker: %s", sid, err.code)

    def _release_quietly(self, sid: str) -> None:
        try:
            self.api.release(sid)
        except Exception as err:
            log.info("%s: release failed: %s", sid, _describe(err))

    def _forget(self, sid: str, track: Track | None = None) -> None:
        with self._lock:
            if track is not None and self.tracks.get(sid) is not track:
                return  # a newer pass owns this session now
            self.tracks.pop(sid, None)
            self.sandboxes.pop(sid, None)

    # -- janitor -------------------------------------------------------------------------------

    def sweep(self) -> list[str]:
        """Delete this outpost's sandboxes whose session terminated. Paused ones of sessions that
        merely left the queue (sleepers do) are kept until their auto-delete."""
        with self._lock:
            entries = dict(self.entries)
            served = set(self.tracks)

        def keep(sb: Any) -> bool:
            labels = sb.labels or {}
            sid = labels.get(SESSION)
            if not sid or labels.get(OUTPOST) != self.outpost_id or sid in served:
                return True
            entry = entries.get(sid)
            if entry is None:
                try:
                    entry = self.api.get(sid)
                except OutpostsError:
                    return True
            if entry is None:
                return True  # unknown: maybe asleep; auto-delete takes it if it never wakes
            return entry.session_status != "terminated"

        deleted = self.machines.sweep(keep)
        for sbx in deleted:
            self._note("-", "swept", sbx)
        return deleted

    def _note(self, sid: str, what: str, detail: str = "") -> None:
        self.events.append((sid, what, detail))
        log.info("%s: %s%s", sid, what, f" ({detail})" if detail else "")


def _describe(err: BaseException) -> str:
    if isinstance(err, PrizedError):
        return f"prized {err.code}" + (f" ({err.reason})" if err.reason else "")
    return f"{type(err).__name__}: {str(err)[:300]}"
