from prized_outposts.devin.api import Entry
from prized_outposts.devin.plan import Rules, Track, plan

ME = "me"


def entry(sid, phase="pending", status="pending", acceptor=None, deadline=None, created=0.0, **kw):
    return Entry(
        sid,
        "op",
        phase=phase,
        session_status=status,
        acceptor_id=acceptor,
        claim_deadline=deadline,
        created_at=created,
        **kw,
    )


def kinds(actions):
    return [(a.kind, a.session_id) for a in actions]


def test_claims_oldest_pending_up_to_the_limit():
    es = [entry("b", created=2), entry("a", created=1), entry("c", created=3)]
    assert kinds(plan(es, {}, Rules(ME, max_concurrent=2), 0)) == [("claim", "a"), ("claim", "b")]


def test_never_claims_a_released_sleeper_or_a_finished_session():
    es = [entry("s", status="suspended"), entry("t", status="terminated")]
    assert plan(es, {}, Rules(ME), 0) == []


def test_leaves_other_platforms_and_image_builds_alone():
    es = [entry("mac", platform="macos"), entry("img", image_build=True)]
    assert plan(es, {}, Rules(ME), 0) == []


def test_refusal_is_announced_once():
    es = [entry("np", network_policy={"enabled": True})]
    rules = Rules(ME, refuse=lambda e: "policy" if e.network_policy.get("enabled") else None)
    assert kinds(plan(es, {}, rules, 0)) == [("refuse", "np")]
    assert plan(es, {}, rules, 0, notified={"np"}) == []


def test_our_running_sessions_count_toward_the_limit():
    es = [entry("mine", "claimed", "running", ME, deadline=1000), entry("new", created=5)]
    tracks = {"mine": Track("mine", remote="alive")}
    assert plan(es, tracks, Rules(ME, max_concurrent=1), 0) == []


def test_renews_before_the_deadline_only():
    es = [entry("x", "claimed", "running", ME, deadline=100)]
    tracks = {"x": Track("x", remote="alive")}
    assert plan(es, tracks, Rules(ME, renew_margin=60), 10) == []
    assert kinds(plan(es, tracks, Rules(ME, renew_margin=60), 50)) == [("renew", "x")]


def test_restart_recovery_starts_what_we_hold_without_a_worker():
    es = [entry("x", "claimed", "running", ME, deadline=1000)]
    assert kinds(plan(es, {}, Rules(ME), 0)) == [("start", "x")]


def test_clean_exit_waits_for_the_status_then_restarts():
    es = [entry("x", "claimed", "running", ME, deadline=1000)]
    tracks = {"x": Track("x", remote="exited", exit_code=0, exited_seen=100, starts=1)}
    assert plan(es, tracks, Rules(ME, exit_grace=20), 110) == []
    assert kinds(plan(es, tracks, Rules(ME, exit_grace=20), 121)) == [("start", "x")]


def test_crash_restarts_then_gives_up():
    es = [entry("x", "claimed", "running", ME, deadline=1000)]
    crashed = Track("x", remote="exited", exit_code=1, exited_seen=0, starts=1)
    assert kinds(plan(es, {"x": crashed}, Rules(ME, restart_limit=3), 1)) == [("start", "x")]
    crashed.starts = 3
    assert kinds(plan(es, {"x": crashed}, Rules(ME, restart_limit=3), 1)) == [("give_up", "x")]


def test_suspend_waits_for_the_remote_to_leave_then_pauses():
    es = [entry("x", "claimed", "suspended", ME, deadline=1000)]
    alive = {"x": Track("x", remote="alive", suspended_seen=100)}
    assert plan(es, alive, Rules(ME, suspend_grace=30), 110) == []
    assert kinds(plan(es, alive, Rules(ME, suspend_grace=30), 131)) == [("suspend", "x")]
    exited = {"x": Track("x", remote="exited", exit_code=0, suspended_seen=100)}
    assert kinds(plan(es, exited, Rules(ME), 101)) == [("suspend", "x")]


def test_terminated_is_deleted():
    es = [entry("x", "claimed", "terminated", ME)]
    assert kinds(plan(es, {"x": Track("x", remote="alive")}, Rules(ME), 0)) == [("terminate", "x")]


def test_busy_and_backing_off_sessions_are_left_alone():
    es = [entry("x", "claimed", "running", ME, deadline=1)]
    assert plan(es, {"x": Track("x", busy=True)}, Rules(ME), 0) == []
    assert plan(es, {"x": Track("x", remote="none", next_try=50)}, Rules(ME), 10) == []


def test_lost_and_vanished_sessions():
    es = [entry("theirs", "claimed", "running", "other")]
    tracks = {"theirs": Track("theirs", remote="alive"), "gone": Track("gone", remote="alive")}
    assert sorted(kinds(plan(es, tracks, Rules(ME), 0))) == [("abandon", "theirs"), ("drop", "gone")]


def test_cooldown_holds_a_session_back():
    es = [entry("x")]
    assert plan(es, {}, Rules(ME, cooldown={"x": 100.0}), 50) == []
    assert kinds(plan(es, {}, Rules(ME, cooldown={"x": 100.0}), 101)) == [("claim", "x")]
