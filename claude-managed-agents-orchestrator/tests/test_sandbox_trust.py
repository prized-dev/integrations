"""The agent's tools run in the sandbox as the same user as the worker, so it can write anything the
status script reads. Nothing it writes may add runs, take slots, keep a sandbox awake, or reach
another session."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from conftest import COLLECT_CMD, LAUNCH_CMD, STATUS_CMD, craft_run, harness, run
from mock_cma import ENV_KEY

from prized_managed_agents import worker as w

# A tool call that forges runs: five "alive" ones for its own session, signed with nothing; a line
# for another session smuggled in through a newline in a file; and a run named for the other
# session's next work item, unacked, which the redelivery path would once have acked without a
# worker. Every "alive" one points at a real process group the agent keeps running.
FORGE = r"""set -e
R=$HOME/.prized-managed-agents/runs
setsid -f sh -c 'echo $$ > "$HOME/forged.pgid"; exec sleep 300' < /dev/null > /dev/null 2>&1
while [ ! -s "$HOME/forged.pgid" ]; do sleep 0.05; done
g=$(cat "$HOME/forged.pgid")
for n in 1 2 3 4 5; do
  d=$R/work_forged_$n-1800000000-0000000$n-00000000000000000000
  mkdir -p "$d"; printf %s "$ANTHROPIC_SESSION_ID" > "$d/session"
  printf %s "work_forged_$n" > "$d/work"; echo "$g" > "$d/pid"
done
d=$R/inject; mkdir -p "$d"
printf '%s\n%s' "$ANTHROPIC_SESSION_ID" "smuggled OTHER" > "$d/session"
printf %s OTHER > "$d/work"; echo "$g" > "$d/pid"
d=$R/OTHER-1800000000-00000000-00000000000000000000; mkdir -p "$d"
printf %s OTHER > "$d/session"; printf %s OTHER > "$d/work"; echo "$g" > "$d/pid"
touch forged.ok
"""


def test_forged_status_lines_add_no_runs_take_no_slots_and_reach_no_other_session(mock, world, ant_dist):
    mock.reuse_work_ids = True  # a work item's id is its session's: the agent can name the other one's

    async def go():
        b = mock.new_session()
        async with harness(mock, world, ant_dist) as first:
            a = mock.new_session()
            mock.run(a.id, FORGE.replace("OTHER", b.id), "sleep 5; echo done > a.txt")
            await first.until(
                lambda: (sb := first.sandbox_of(a.id)) is not None and Path(sb.home, "work", "forged.ok").exists()
            )
            sb = first.sandbox_of(a.id)
        # A long poll the old process left open still claims for up to a second (the mock, like a
        # server, doesn't notice the client went away): let it lapse before B's item is queued.
        await asyncio.sleep(1.2)
        # The old orchestrator is gone while A's worker still runs. The new one, with room for two
        # runs, finds A's sandbox full of forged runs.
        async with harness(mock, world, ant_dist, max_sessions=2) as h:
            assert [(r.session_id, r.work_id) for r in h.orch.runs.values()] == [(a.id, a.id)]
            # B is served, by a worker of its own, in a sandbox of its own: the forged run named for
            # its item neither took the ack nor a slot.
            mock.run(b.id, "echo b > b.txt")
            await h.until(lambda: h.noted("paused", b.id), timeout=20)
            assert Path(h.sandbox_of(b.id).home, "work", "b.txt").read_text() == "b\n"
            assert mock.item(b.id).stopped_by == "sessions_token" and b.results[-1]["exit_code"] == 0
            assert h.sandbox_of(b.id) is not sb
            # A pauses once its real run ends, though the forged "alive" runs still have a live
            # process group: they don't keep the sandbox awake.
            await h.until(lambda: h.noted("paused", a.id), timeout=20)
            assert sb.state == "paused" and Path(sb.home, "work", "a.txt").read_text() == "done\n"
            assert not h.noted("adopted", b.id) and not h.noted("stopping extra worker")
            assert all(r.session_id != b.id or r.sandbox is not sb for r in h.orch.runs.values())

    run(go())


def test_the_status_script_prints_plain_fields_only_and_only_signed_runs_count(world):
    key = w.run_key(ENV_KEY)

    async def go():
        sb = await world.api().create(labels={"anthropic-session": "sesn_a"})
        await sb.exec("setsid -f sh -c 'echo $$ > \"$HOME/g\"; exec sleep 60' < /dev/null > /dev/null 2>&1")
        while not Path(sb.home, "g").exists() or not Path(sb.home, "g").read_text().strip():
            time.sleep(0.02)
        g = Path(sb.home, "g").read_text().strip()
        real = craft_run(sb, "sesn_a", "work_a")  # no pid yet: starting
        exited = craft_run(sb, "sesn_a", "work_b", done="0")
        tampered = craft_run(sb, "sesn_a", "work_c", pid=g)
        Path(sb.home, w.RUNS, tampered, "work").write_text("work_z")  # the agent changed whose run it is
        other_key = craft_run(sb, "sesn_a", "work_d", key="sk-ant-oat01-another-environment", pid=g)
        collected_alive = craft_run(sb, "sesn_a", "work_e", pid=g, collected="")
        craft_run(sb, "sesn_a", "work_f", collected="", done="0")  # collected and over: not listed
        pid_one = craft_run(sb, "sesn_a", "work_g", pid="1")  # kill -0 -1 would say "alive"
        runs = Path(sb.home, w.RUNS)
        (runs / "inject").mkdir()
        (runs / "inject" / "session").write_text("sesn_a\nsmuggled sesn_b")
        (runs / "inject" / "work").write_text("work_b")
        (runs / "inject" / "pid").write_text(g)
        (runs / "spaces").mkdir()
        (runs / "spaces" / "session").write_text("sesn_a x")
        (runs / "spaces" / "work").write_text("work_x")
        (runs / "weird pid").mkdir()

        r = await sb.exec(w.STATUS, env=w.env())
        assert r.exit_code == 0
        assert all(len(line.split(" ")) == 6 for line in r.stdout.splitlines())
        got = {st.run_id: st for st in w.parse_status(r.stdout)}
        assert set(got) == {real, exited, tampered, other_key, collected_alive, pid_one}
        assert got[real].state == "starting" and got[exited].state == "exited" and got[exited].number == 0
        assert got[collected_alive].state == "alive" and got[collected_alive].number == int(g)
        assert got[pid_one].state == "lost"
        genuine = {rid for rid, st in got.items() if w.genuine(key, st)}
        assert genuine == {real, exited, collected_alive, pid_one}
        assert got[real].started is not None and abs(got[real].started - time.time()) < 5

    run(go())


def test_status_lines_parse_strictly():
    good = "a-1 sesn_1 work_1 alive 4242 1\na-2 sesn_1 work_2 exited 3 0\na-3 sesn_1 work_3 starting - 0"
    bad = [
        "a-4 sesn_1 work_4 alive 1 0",  # pid 1: not a worker's group
        "a-5 sesn_1 work_5 alive - 0",
        "a-6 sesn_1 work_6 zombie 3 0",
        "a-7 sesn_1 work_7 exited 3 yes",
        "a-8 sesn/1 work_8 exited 3 0",
        "a-9 sesn_1 work_9 exited 3 0 extra",
        "a-10  sesn_1 work_10 exited 3 0",
        "a-11 sesn_1 work_11 exited 99999999999999 0",
    ]
    got = w.parse_status("\n".join([good, *bad]))
    assert [(st.run_id, st.state, st.number, st.acked) for st in got] == [
        ("a-1", "alive", 4242, True),
        ("a-2", "exited", 3, False),
        ("a-3", "starting", None, False),
    ]


def test_run_ids_are_bounded_and_signed():
    key = w.run_key(ENV_KEY)
    long_work = "work_" + "x" * 123
    rid = w.run_id(key, "sesn_1", long_work)
    assert len(rid) <= 89 and w.SAFE_ID.fullmatch(rid) and rid.startswith(long_work[:48])
    st = w.RunStatus(rid, "sesn_1", long_work, "alive", 4242, True)
    assert w.genuine(key, st)
    assert not w.genuine(key, w.RunStatus(rid, "sesn_2", long_work, "alive", 4242, True))
    assert not w.genuine(key, w.RunStatus(rid, "sesn_1", long_work + "y", "alive", 4242, True))
    assert not w.genuine(w.run_key("another key"), st)
    assert w.started_at(rid) == st.started and abs(st.started - time.time()) < 5


def test_each_test_hook_picks_out_one_script():
    scripts = [w.INSTALL_ANT, w.PREPARE_RUN, w.LAUNCH, w.STATUS, w.MARK_ACKED, w.STOP, w.COLLECT]
    for needle, script in ((STATUS_CMD, w.STATUS), (LAUNCH_CMD, w.LAUNCH), (COLLECT_CMD, w.COLLECT)):
        assert [s for s in scripts if needle in s] == [script]


def test_stop_never_signals_pid_one_or_junk(world):
    # kill is replaced by a bash function that only records its arguments (bash takes exported
    # functions from the environment), so a regression here can't signal anything for real.
    fake_kill = {"BASH_FUNC_kill%%": '() { echo "$*" >> "$HOME/kills"; return 1; }'}

    async def go():
        sb = await world.api().create(labels={})
        for pid in ("1", "-1", "0", "12 34", "x", ""):
            rid = craft_run(sb, "sesn_a", "work_a", pid=pid, secret="s")
            r = await sb.exec(w.STOP, env=w.env(rid, PMA_GRACE="1", **fake_kill))
            assert r.exit_code == 0 and r.stdout.strip() == "stopped"
            assert not Path(sb.home, w.RUNS, rid, "secret").exists()
        assert not Path(sb.home, "kills").exists()
        rid = craft_run(sb, "sesn_a", "work_a", pid="4242")
        await sb.exec(w.STOP, env=w.env(rid, PMA_GRACE="1", PMA_SIGNAL="KILL", **fake_kill))
        assert Path(sb.home, "kills").read_text().splitlines()[0] == "-KILL -- -4242"

    run(go())
