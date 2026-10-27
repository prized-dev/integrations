"""The orchestrator end to end against the fake Outposts service, fake sandboxes and a fake remote."""

from __future__ import annotations

import json
import os
import subprocess
import time

from conftest import OUTPOST, settle
from fake_outposts import TOKEN

ALLOWED_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "LANG",
    "TZ",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DEVIN_OUTPOST_GATEWAY_URL",
    "DEVIN_OUTPOST_CONNECT_TOKEN",
    "DEVIN_OUTPOST_SESSION_ID",
    "DEVIN_REMOTE_STATE_DIR",
    "DEVIN_CHROME_PATH",
    "DEVIN_OUTPOST_DESKTOP",
}


def sandboxes(world, sid=None):
    return [
        sb
        for sb in world.sandboxes.values()
        if sb.state != "deleted" and (sid is None or sb.labels.get("devin-session") == sid)
    ]


def state_file(sb, sid, name):
    return os.path.join(sb.home, ".devin", "worker", "sessions", sid, name)


def read(path):
    with open(path) as fh:
        return fh.read()


def serving(fake, sid, attached=1):
    return lambda: fake.row(sid).attached >= attached and fake.row(sid).session_status == "running"


def test_a_new_session_gets_its_own_sandbox_and_a_clean_worker(fake, world, make):
    orch = make()
    fake.enqueue("devin-s1")
    settle(orch, serving(fake, "devin-s1"))

    [sb] = sandboxes(world)
    assert sb.labels == {
        "managed-by": "prized-devin-outposts",
        "task": "devin-outposts",
        "devin-session": "devin-s1",
        "devin-outpost": OUTPOST,
    }
    assert fake.row("devin-s1").acceptor_id == "prized-test-1"
    # The spawn contract: only the allowed variables, launched from the working directory.
    keys = set(json.loads(read(state_file(sb, "devin-s1", "env-keys.json"))))
    assert {
        "DEVIN_OUTPOST_GATEWAY_URL",
        "DEVIN_OUTPOST_CONNECT_TOKEN",
        "DEVIN_OUTPOST_SESSION_ID",
        "DEVIN_REMOTE_STATE_DIR",
    } <= keys
    assert keys - ALLOWED_ENV <= {"PWD", "SHLVL", "_", "OLDPWD"}, keys - ALLOWED_ENV
    assert json.loads(read(state_file(sb, "devin-s1", "launch.json")))["cwd"] == os.path.join(sb.home, "work")
    assert os.path.exists(os.path.join(sb.home, "work", ".devin-outpost-stamps", "devin-s1"))
    # The account token never enters the sandbox; only the session's connect token does.
    found = subprocess.run(["grep", "-rl", TOKEN, sb.home], capture_output=True, text=True)
    assert found.stdout == ""


def test_sleep_pauses_with_processes_and_wake_resumes_the_same_sandbox(fake, world, make):
    orch = make()
    fake.enqueue("devin-s2")
    settle(orch, serving(fake, "devin-s2"))
    [sb] = sandboxes(world)
    devserver = read(state_file(sb, "devin-s2", "devserver.pid"))

    fake.suspend("devin-s2")
    settle(orch, lambda: sb.state == "paused" and fake.row("devin-s2").phase == "pending")
    counter = state_file(sb, "devin-s2", "devserver.count")
    frozen = read(counter)
    time.sleep(0.6)
    assert read(counter) == frozen, "a paused sandbox runs nothing"
    assert orch.tracks == {}

    fake.wake("devin-s2")
    settle(orch, serving(fake, "devin-s2", attached=2))
    assert sandboxes(world) == [sb], "the session came back to its own sandbox"
    assert ("resume", sb.id) in world.calls
    assert [c for c in world.calls if c[0] in ("create", "fork")] == [("create", sb.id)]
    assert read(state_file(sb, "devin-s2", "devserver.pid")) == devserver, "the dev server kept running"
    time.sleep(0.6)
    assert int(read(counter)) > int(frozen)
    assert read(state_file(sb, "devin-s2", "launches")) == "2"
    assert fake.row("devin-s2").claims == 2


def test_terminated_session_deletes_its_sandbox(fake, world, make):
    orch = make()
    fake.enqueue("devin-s3")
    settle(orch, serving(fake, "devin-s3"))
    [sb] = sandboxes(world)
    fake.terminate("devin-s3")
    settle(orch, lambda: sb.state == "deleted" and fake.row("devin-s3").phase == "pending")
    assert sandboxes(world) == []


def test_a_restarted_orchestrator_reattaches_without_a_second_worker(fake, world, make):
    first = make()
    fake.enqueue("devin-s4")
    settle(first, serving(fake, "devin-s4"))
    [sb] = sandboxes(world)

    again = make()  # same acceptor id, empty memory: a restart
    settle(again, lambda: "devin-s4" in again.tracks and again.tracks["devin-s4"].remote == "alive")
    assert ("devin-s4", "attached", "a worker was already serving") in again.events
    assert read(state_file(sb, "devin-s4", "launches")) == "1"
    assert sandboxes(world) == [sb]


def test_two_orchestrators_never_serve_one_session_twice(fake, world, make):
    a, b = make("prized-a"), make("prized-b")
    fake.enqueue("devin-s5")
    for _ in range(3):
        a.tick()
        b.tick()
        a.drain(30)
        b.drain(30)
    assert len(sandboxes(world, "devin-s5")) == 1
    assert fake.row("devin-s5").claims == 1


def test_claims_are_renewed_before_they_expire(fake, world, make):
    fake.claim_ttl = 2.0
    orch = make(renew_margin=1.5)
    fake.enqueue("devin-s6")
    settle(orch, serving(fake, "devin-s6"))
    settle(orch, lambda: fake.row("devin-s6").claims >= 3, timeout=20)
    assert fake.row("devin-s6").acceptor_id == "prized-test-1"
    assert read(state_file(sandboxes(world)[0], "devin-s6", "launches")) == "1"


def test_a_crashed_worker_is_started_again(fake, world, make):
    orch = make()
    fake.enqueue("devin-s7")
    settle(orch, serving(fake, "devin-s7"))
    [sb] = sandboxes(world)
    pid = read(os.path.join(sb.home, ".prized-outposts", "devin-s7", "remote.pid")).strip()
    os.kill(int(pid), 9)
    settle(
        orch,
        lambda: (
            os.path.exists(state_file(sb, "devin-s7", "launches"))
            and read(state_file(sb, "devin-s7", "launches")) == "2"
            and orch.tracks.get("devin-s7")
            and orch.tracks["devin-s7"].remote == "alive"
        ),
    )
    assert any(e[1] == "restarting" and "137" in e[2] for e in orch.events), [
        e for e in orch.events if e[1] == "restarting"
    ]


def test_a_binary_that_fails_its_checksum_never_runs(fake, world, make):
    orch = make()
    fake.enqueue("devin-s8", remote_binary_sha="bad0bad0bad")
    settle(orch, lambda: any(e[0] == "devin-s8" and "checksum mismatch" in e[2] for e in orch.events))
    [sb] = sandboxes(world)
    cache = os.path.join(sb.home, ".devin", "worker", "cache")
    assert not any(n.startswith("devin-remote_bad0bad0bad") for n in os.listdir(cache))
    assert fake.row("devin-s8").phase == "pending", "the claim went back to the queue"
    assert fake.row("devin-s8").attached == 0


def test_a_session_with_a_network_policy_is_refused_with_a_message(fake, world, make):
    orch = make(refuse=lambda e: "network policy" if e.network_policy.get("enabled") else None)
    fake.enqueue("devin-s9", network_policy={"enabled": True, "allow": [{"hostname": "github.com"}]})
    for _ in range(3):
        orch.tick()
        orch.drain(30)
    assert fake.row("devin-s9").messages == ["Prized can't serve this session: network policy"]
    assert fake.row("devin-s9").claims == 0
    assert sandboxes(world) == []


def test_new_sessions_fork_the_warm_parent(fake, world, make):
    from prized_outposts.machines import Machines, MachineSpec

    parent = Machines(MachineSpec(), owner="prized-devin-outposts", sandbox_api=world.api()).make_parent(
        lambda sb: sb.exec("mkdir -p repos/app && echo warm > repos/app/README")
    )
    assert parent.state == "paused"
    orch = make(warm_parent=parent.id)
    fake.enqueue("devin-s10")
    settle(orch, serving(fake, "devin-s10"))
    [child] = sandboxes(world, "devin-s10")
    assert ("fork", child.id) in world.calls
    assert read(os.path.join(child.home, "work", "repos", "app", "README")) == "warm\n"
    assert child.labels["devin-session"] == "devin-s10"


def test_sweep_deletes_terminated_sessions_and_keeps_sleepers(fake, world, make):
    orch = make()
    for sid in ("devin-a", "devin-b"):
        fake.enqueue(sid)
    settle(orch, lambda: serving(fake, "devin-a")() and serving(fake, "devin-b")())
    for sid in ("devin-a", "devin-b"):
        fake.suspend(sid)
    settle(orch, lambda: all(sb.state == "paused" for sb in sandboxes(world)) and not orch.tracks)
    fake.vanish("devin-a")  # a sleeper whose row the sweeper removed
    fake.terminate("devin-b")
    orch.tick()
    orch.drain(30)
    deleted = orch.sweep()
    assert [sb.labels["devin-session"] for sb in sandboxes(world)] == ["devin-a"]
    assert len(deleted) == 1


def test_the_api_pages_and_watches(fake):
    from threading import Event

    from prized_outposts.devin.api import ClaimConflict, OutpostsAPI

    api = OutpostsAPI(fake.url + "/opbeta", TOKEN, "me")
    for i in range(5):
        fake.enqueue(f"devin-p{i}")
    entries, cursor = api.list_all(OUTPOST)
    assert sorted(e.session_id for e in entries) == [f"devin-p{i}" for i in range(5)]
    stop = Event()
    fake.watch_max = 1.0
    claimed = api.claim("devin-p0")
    assert claimed.connect_token == "ctok-devin-p0-1" and claimed.gateway_url
    seen = [(kind, e.session_id, e.phase) for kind, e, _ in api.watch(OUTPOST, cursor, stop)]
    assert ("MODIFIED", "devin-p0", "claimed") in seen
    other = OutpostsAPI(fake.url, TOKEN, "someone-else")
    try:
        other.claim("devin-p0")
        raise AssertionError("expected a conflict")
    except ClaimConflict:
        pass
    assert other.release("devin-p0") is False
    assert api.release("devin-p0") is True
    assert "ctok" not in repr(claimed)


# -- failures (regressions from review) --------------------------------------------------------


def test_a_failed_claim_is_handed_back_and_retried_after_a_backoff(fake, world, make):
    orch = make()
    world.fail_create = "sandbox_capacity"
    fake.enqueue("devin-f1")
    settle(orch, lambda: any(e[0] == "devin-f1" and e[1] == "error" for e in orch.events))
    assert fake.row("devin-f1").phase == "pending", "the claim went back to the queue"
    assert "devin-f1" not in orch.tracks and "devin-f1" in orch.rules.cooldown
    world.fail_create = None
    orch.rules.cooldown["devin-f1"] = 0.0  # the backoff has passed
    settle(orch, serving(fake, "devin-f1"))
    assert fake.row("devin-f1").claims == 2


def test_a_lapsed_claim_on_a_sleeping_session_pauses_its_sandbox(fake, world, make):
    fake.claim_ttl = 1.0
    orch = make(renew_margin=0.0)
    fake.enqueue("devin-f2")
    settle(orch, serving(fake, "devin-f2"))
    [sb] = sandboxes(world)
    fake.suspend("devin-f2")
    time.sleep(2.5)  # the orchestrator stalled; the claim expired while the session slept
    settle(orch, lambda: sb.state == "paused" and not orch.tracks)
    fake.wake("devin-f2")
    settle(orch, serving(fake, "devin-f2", attached=2))
    assert sandboxes(world) == [sb]


def test_a_prized_blip_keeps_the_claim_and_the_worker(fake, world, make):
    from prized import PrizedError

    orch = make()
    fake.enqueue("devin-f3")
    settle(orch, serving(fake, "devin-f3"))
    sb = orch.sandboxes["devin-f3"]
    real_exec, real_list = sb.exec, orch.machines.api.list

    def down(*a, **k):
        raise PrizedError("network_error")

    sb.exec = down
    orch.machines.api.list = staticmethod(down)
    for _ in range(2):
        orch.tick()
        orch.drain(30)
    sb.exec = real_exec
    orch.machines.api.list = real_list
    for _ in range(5):
        orch.tick()
        orch.drain(30)
    row = fake.row("devin-f3")
    assert (row.phase, row.acceptor_id) == ("claimed", "prized-test-1")
    assert orch.tracks["devin-f3"].remote == "alive"
    assert read(state_file(sb, "devin-f3", "launches")) == "1", "the worker was never restarted"


def test_stopping_by_pid_never_touches_another_worker(fake, world, make):
    from prized_outposts.devin import remote as rm

    orch = make()
    fake.enqueue("devin-f4")
    settle(orch, serving(fake, "devin-f4"))
    sb = orch.sandboxes["devin-f4"]
    assert sb.exec(rm.stop_script("devin-f4", only_pid=1)).stdout.strip() == "not ours"
    assert rm.parse_status(sb.exec(rm.status_script("devin-f4")).stdout)[0] == "alive"
    pid = orch.tracks["devin-f4"].pid
    assert sb.exec(rm.stop_script("devin-f4", only_pid=pid)).stdout.strip() == "stopped"
    assert rm.parse_status(sb.exec(rm.status_script("devin-f4")).stdout)[0] == "exited"


def test_a_give_up_message_is_cleared_once_the_session_is_served_again(fake, world, make):
    orch = make(restart_limit=1)
    fake.enqueue("devin-f5")
    settle(orch, serving(fake, "devin-f5"))
    sb = orch.sandboxes["devin-f5"]
    os.kill(int(read(os.path.join(sb.home, ".prized-outposts", "devin-f5", "remote.pid")).strip()), 9)
    settle(orch, lambda: any(e[1] == "gave up" for e in orch.events))
    assert fake.row("devin-f5").messages[-1].startswith("Prized: the Devin worker")
    orch.rules.cooldown["devin-f5"] = 0.0
    settle(orch, serving(fake, "devin-f5", attached=2))
    assert fake.row("devin-f5").messages[-1] == ""
