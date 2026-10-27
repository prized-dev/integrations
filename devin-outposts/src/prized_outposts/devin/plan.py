"""What to do next for each queued session: a pure function of the queue and what we know locally.

Kept free of I/O so the whole session <-> sandbox mapping is unit-tested without Devin or Prized.
The rules follow Cognition's own operator (github.com/CognitionAI/devin-outpost-k8s) and the
reference at https://docs.devin.ai/cloud/outposts/reference.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .api import Entry

FINISHED = ("suspended", "terminated")


@dataclass
class Track:
    """What this orchestrator knows about a session it serves."""

    session_id: str
    sandbox_id: str | None = None
    remote: str = "unknown"  # alive | exited | gone | none | unknown (not probed yet)
    exit_code: int | None = None
    exited_seen: float | None = None  # first time we saw the remote ended while the session wasn't
    suspended_seen: float | None = None  # first time we saw the session suspended
    starts: int = 0
    pid: int | None = None  # the run.sh we launched or attached to
    probe_failures: int = 0  # probes in a row that couldn't reach the sandbox
    attempts: int = 0  # failed actions in a row
    next_try: float = 0.0
    busy: bool = False


@dataclass(frozen=True)
class Action:
    kind: str  # claim | start | renew | suspend | terminate | give_up | refuse | drop | abandon
    session_id: str
    reason: str = ""


@dataclass
class Rules:
    acceptor_id: str
    max_concurrent: int = 4
    renew_margin: float = 60.0  # renew a claim this long before its deadline
    exit_grace: float = 20.0  # a clean exit while the session still looks live: wait for the status
    suspend_grace: float = 30.0  # let the remote exit by itself after a suspend
    restart_limit: int = 3  # launches per claim before giving up
    probe_tolerance: int = 3  # unreachable probes in a row before a worker counts as gone
    refuse: Callable[[Entry], str | None] = lambda entry: None
    platforms: tuple[str, ...] = ("linux",)
    cooldown: dict[str, float] = field(default_factory=dict)  # session -> no claims until then


def plan(
    entries: Iterable[Entry],
    tracks: dict[str, Track],
    rules: Rules,
    now: float,
    notified: set[str] | None = None,
) -> list[Action]:
    notified = notified if notified is not None else set()
    entries = list(entries)
    actions: list[Action] = []
    active = 0
    seen = set()
    for e in entries:
        seen.add(e.session_id)
        t = tracks.get(e.session_id)
        ours = e.phase == "claimed" and e.acceptor_id == rules.acceptor_id
        if t is not None and t.busy:
            if e.session_status not in FINISHED:
                active += 1  # being served, or a claim in flight
            continue
        if not ours:
            if t is not None and e.phase == "claimed":
                actions.append(Action("abandon", e.session_id, "claimed by another acceptor"))
            elif t is not None:
                # Our claim lapsed (it expires only while the session isn't running): stop, pause, forget,
                # so the session can be claimed again like any other.
                actions.append(Action("drop", e.session_id, "our claim lapsed"))
            continue
        if t is not None and now < t.next_try:
            if e.session_status not in FINISHED:
                active += 1
            continue
        if e.session_status == "terminated":
            actions.append(Action("terminate", e.session_id))
            continue
        if e.session_status == "suspended":
            waited = now - (t.suspended_seen if t and t.suspended_seen is not None else now)
            if t is not None and t.remote == "alive" and waited < rules.suspend_grace:
                continue  # the remote exits by itself; give it a moment
            actions.append(Action("suspend", e.session_id))
            continue
        active += 1
        near = e.claim_deadline is None or e.claim_deadline - now < rules.renew_margin
        if (
            t is not None
            and t.remote == "unknown"
            and t.sandbox_id
            and t.probe_failures < rules.probe_tolerance
        ):
            # A probe that couldn't reach the sandbox says nothing about the worker yet: keep the claim.
            if near:
                actions.append(Action("renew", e.session_id))
            continue
        if t is None or t.remote in ("none", "gone", "unknown"):
            if t is not None and t.starts >= rules.restart_limit:
                actions.append(Action("give_up", e.session_id, f"the worker stopped {t.starts} times"))
            else:
                actions.append(Action("start", e.session_id, "no worker running"))
        elif t.remote == "exited":
            lag = now - (t.exited_seen if t.exited_seen is not None else now)
            if t.exit_code == 0 and lag < rules.exit_grace:
                continue  # Devin's status trails a clean exit by a few seconds
            if t.starts >= rules.restart_limit:
                actions.append(Action("give_up", e.session_id, f"the worker stopped {t.starts} times"))
            else:
                actions.append(Action("start", e.session_id, f"worker exited {t.exit_code}"))
        elif near:
            actions.append(Action("renew", e.session_id))

    for sid, t in tracks.items():
        if sid not in seen and not t.busy:
            actions.append(Action("drop", sid, "no longer in the queue"))

    pending = [
        e
        for e in entries
        if e.phase == "pending"
        and e.session_status not in FINISHED  # a released sleeper leaves a pending row behind
        and e.session_id not in tracks
        and rules.cooldown.get(e.session_id, 0.0) <= now
    ]
    pending.sort(key=lambda e: (e.created_at or 0.0, e.session_id))
    slots = max(0, rules.max_concurrent - active)
    for e in pending:
        why = None
        if (e.platform or "linux") not in rules.platforms:
            continue  # leave it for a worker on that platform
        if e.image_build:
            continue  # image builds need a container registry push; leave them to a pool that has one
        why = rules.refuse(e)
        if why:
            if e.session_id not in notified:
                actions.append(Action("refuse", e.session_id, why))
            continue
        if slots <= 0:
            break
        slots -= 1
        actions.append(Action("claim", e.session_id))
    return actions
