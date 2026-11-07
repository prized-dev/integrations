"""When a step fails or a process goes away halfway: a launch whose answer is lost, a status check
that fails, a restart while a worker runs, a sandbox that paused itself with its worker inside, an
ack that didn't land, and the shutdown."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from conftest import COLLECT_CMD, LAUNCH_CMD, STATUS_CMD, craft_run, harness, run, settings_for
from mock_cma import ENV_ID
from prized import PrizedError

from prized_managed_agents import worker as w
from prized_managed_agents.cma import CMA, WorkItem
from prized_managed_agents.orchestrator import HandoffError, Orchestrator, _timeout
from prized_managed_agents.sandboxes import SessionSandboxes


def flaky() -> PrizedError:
    return PrizedError("internal_error", status=502, message="the edge dropped the connection")


def work_file(sb, name: str) -> Path:
    return Path(sb.home, "work", name)


def order(h, sid: str) -> list[str]:
    return [n.what for n in h.orch.notes if n.session == sid]


def test_a_status_check_that_fails_after_the_launch_leaves_the_worker_alone_and_acks(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            world.fail_exec.append((STATUS_CMD, flaky()))  # the first status check: right after the launch
            s = mock.new_session()
            item = mock.run(s.id, "echo served > served.txt")
            await h.until(lambda: h.noted("paused", s.id))
            assert not world.fail_exec, "the failure was used"
            assert h.noted("status unread", s.id) and not h.noted("handoff failed", s.id)
            assert order(h, s.id).index("worker started") < order(h, s.id).index("acked")
            assert mock.item(item.id).acked_at is not None and mock.item(item.id).claims == 1
            assert work_file(h.sandbox_of(s.id), "served.txt").read_text() == "served\n"
            # Ended by its own worker, not stopped by the orchestrator.
            assert mock.item(item.id).stopped_by == "sessions_token"
            assert ("POST", f"/v1/environments/{ENV_ID}/work/{item.id}/stop", "environment_key") not in mock.requests

    run(go())


def test_a_launch_whose_answer_is_lost_is_kept_when_the_sandbox_says_it_runs(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            world.fail_after_exec.append((LAUNCH_CMD, flaky()))  # ant starts; the answer never comes
            s = mock.new_session()
            item = mock.run(s.id, "echo served > served.txt")
            await h.until(lambda: h.noted("paused", s.id))
            assert not world.fail_after_exec and not h.noted("handoff failed", s.id)
            assert len(h.noted("worker started", s.id)) == 1 and mock.item(item.id).claims == 1
            assert work_file(h.sandbox_of(s.id), "served.txt").read_text() == "served\n"
            assert mock.item(item.id).stopped_by == "sessions_token"

    run(go())


def test_a_launch_that_never_ran_fails_cleanly_and_the_item_comes_back(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, handoff_timeout_s=2, reconcile_every_s=0.3) as h:
            world.fail_exec.append((LAUNCH_CMD, flaky()))  # refused before it ran
            s = mock.new_session()
            item = mock.run(s.id, "echo served > served.txt")
            await h.until(lambda: h.noted("handoff failed", s.id))
            assert mock.item(item.id).acked_at is None
            assert h.noted("deleted", s.id) and "made for the failed handoff" in h.noted("deleted", s.id)[0].detail
            await h.until(lambda: h.noted("paused", s.id), timeout=20)
            assert mock.item(item.id).claims == 2
            assert work_file(h.sandbox_of(s.id), "served.txt").read_text() == "served\n"
            assert len(world.live()) == 1

    run(go())


def test_a_failed_handoff_deletes_its_fresh_sandbox_even_when_its_cleanup_fails(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, max_handoff_attempts=1) as h:
            world.fail["write"] = flaky()  # writing the secret fails, after the run was prepared
            world.fail_exec.append((COLLECT_CMD, flaky()))  # and so does the first cleanup step
            s = mock.new_session()
            mock.run(s.id, "true")
            await h.until(lambda: h.noted("gave up", s.id))
            assert not world.fail and not world.fail_exec
            assert h.noted("cleanup failed", s.id)
            await h.until(lambda: not world.live(), why="the sandbox made for it to be deleted")

    run(go())


def test_a_long_work_id_gets_a_short_run_id_and_is_served(mock, world, ant_dist):
    long_id = "work_" + "x" * 123  # 128 characters, the most a work id may have here
    mock.next_work_id = long_id

    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            item = mock.run(s.id, "echo long > long.txt")
            assert item.id == long_id
            await h.until(lambda: h.noted("paused", s.id))
            sb = h.sandbox_of(s.id)
            assert work_file(sb, "long.txt").read_text() == "long\n"
            (rdir,) = Path(sb.home, w.RUNS).iterdir()
            assert len(rdir.name) <= 89 and (rdir / "work").read_text() == long_id
            assert mock.item(long_id).stopped_by == "sessions_token" and world.live() == [sb]

    run(go())


def test_a_restart_never_pauses_a_sandbox_whose_worker_is_alive(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as first:
            s = mock.new_session()
            mock.run(s.id, "sleep 2.5; echo finished > out.txt")
            await first.until(lambda: first.noted("acked", s.id))
            sb = first.sandbox_of(s.id)
        # An earlier run of the session that ended while no one watched, listed before the live one.
        craft_run(sb, s.id, "work_0000old", now=time.time() - 120, done="0")
        assert sorted(p.name for p in Path(sb.home, w.RUNS).iterdir())[0].startswith("work_0000old")
        before = len(world.calls)
        async with harness(mock, world, ant_dist) as second:
            await second.until(lambda: second.noted("paused", s.id), timeout=20)
            notes = [(n.what, n.detail) for n in second.orch.notes if n.session == s.id]
            ended = [i for i, (what, detail) in enumerate(notes) if what == "run ended" and "0000old" not in detail]
            paused = [i for i, (what, _) in enumerate(notes) if what == "paused"]
            assert ended and paused and paused[0] > ended[0], notes
            assert world.calls[before:].count(("pause", sb.id)) == 1
            assert work_file(sb, "out.txt").read_text() == "finished\n"

    run(go())


def test_a_resumed_sandboxs_stale_worker_is_stopped_before_the_new_one_starts(mock, world, ant_dist):
    async def go():
        world.start_auto_pause(0.8)
        async with harness(mock, world, ant_dist) as first:
            s = mock.new_session()
            item = mock.run(s.id, "sleep 30")
            await first.until(lambda: first.noted("acked", s.id))
            sb = first.sandbox_of(s.id)
            (stale,) = list(first.orch.runs)
        pgid = int(Path(sb.home, w.RUNS, stale, "pid").read_text())
        # With no orchestrator the sandbox pauses itself, freezing the worker; its lease lapses and
        # the queue hands the same item out again.
        deadline = time.monotonic() + 15
        while not (sb.state == "paused" and mock.item(item.id).state == "queued"):
            assert time.monotonic() < deadline, (sb.state, mock.item(item.id).state)
            await asyncio.sleep(0.05)
        with mock.lock:
            s.commands.append("echo fresh > fresh.txt")
        async with harness(mock, world, ant_dist, webhook_key=None) as h:
            await h.until(lambda: h.noted("paused", s.id), timeout=20)
            assert ("resume", sb.id) in world.calls
            assert order(h, s.id).index("stopping stale worker") < order(h, s.id).index("worker started")
            assert work_file(sb, "fresh.txt").read_text() == "fresh\n"
            with pytest.raises(ProcessLookupError):
                os.killpg(pgid, 0)
            assert Path(sb.home, w.RUNS, stale, "collected").exists()
            # The same work id: killed outright, so its own stop on the way out never ran.
            assert "stop ->" not in Path(sb.home, w.RUNS, stale, "worker.log").read_text()
            assert mock.item(item.id).stopped_by == "sessions_token"

    run(go())


def test_a_worker_from_before_a_key_rotation_is_stopped_too_when_its_session_runs_again(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            sb = await world.api().create(labels=h.orch.boxes.labels(s.id))
            await sb.exec("setsid -f sh -c 'echo $$ > \"$HOME/g\"; exec sleep 60' < /dev/null > /dev/null 2>&1")
            while not Path(sb.home, "g").exists() or not Path(sb.home, "g").read_text().strip():
                await asyncio.sleep(0.02)
            pgid = int(Path(sb.home, "g").read_text())
            old = craft_run(sb, s.id, "work_before", key="sk-ant-oat01-the-key-before-rotation", pid=str(pgid))
            await sb.pause()
            mock.run(s.id, "echo after > after.txt")
            await h.until(lambda: h.noted("paused", s.id) and work_file(sb, "after.txt").exists(), timeout=20)
            assert order(h, s.id).index("stopping stale worker") < order(h, s.id).index("worker started")
            with pytest.raises(ProcessLookupError):
                os.killpg(pgid, 0)
            assert Path(sb.home, w.RUNS, old, "collected").exists() and not h.noted("adopted", s.id)

    run(go())


def test_an_item_that_comes_again_after_a_lost_ack_gets_a_new_worker_if_the_old_one_died(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, webhook_key=None, probe_every_s=3600) as h:
            mock.fail_next["ack"] = [500] * 4  # every attempt at the first ack fails
            s = mock.new_session(metadata={"stub_die_once_after": "1"})
            item = mock.run(s.id, "echo served > served.txt")
            await h.until(lambda: h.noted("ack failed", s.id))
            # The worker dies without a word; its lease lapses and the item comes again.
            await h.until(lambda: len(h.noted("worker started", s.id)) == 2, timeout=30)
            await h.until(lambda: work_file(h.sandbox_of(s.id), "served.txt").exists())
            assert any("worker is gone" in n.detail for n in h.noted("run ended", s.id))
            assert mock.item(item.id).acked_at is not None and h.noted("acked", s.id)

    run(go())


def test_the_sweep_adopts_a_live_run_no_one_supervises(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as first:
            s = mock.new_session()
            mock.run(s.id, "sleep 2; echo finished > out.txt")
            await first.until(lambda: first.noted("acked", s.id))
            sb = first.sandbox_of(s.id)
        world.fail_exec.append((STATUS_CMD, flaky()))  # the restart can't read the sandbox
        async with harness(mock, world, ant_dist) as h:
            await h.until(lambda: h.noted("adopted", s.id))  # the first sweep picks it up instead
            assert not world.fail_exec
            await h.until(lambda: h.noted("paused", s.id), timeout=20)
            assert work_file(sb, "out.txt").read_text() == "finished\n"
            assert order(h, s.id).index("run ended") < order(h, s.id).index("paused")

    run(go())


def test_the_sweep_leaves_a_handoff_under_way_alone_but_not_forever(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            await asyncio.sleep(0.3)  # past the first sweep
            s = mock.new_session()
            sb = await world.api().create(labels=h.orch.boxes.labels(s.id))
            young = craft_run(sb, s.id, "work_young")  # prepared, not launched yet: starting
            await h.orch.sweep()
            assert sb.state == "running" and not Path(sb.home, w.RUNS, young, "collected").exists()
            Path(sb.home, w.RUNS, young).rename(Path(sb.home, "parked"))
            old = craft_run(sb, s.id, "work_old", now=time.time() - 3600)  # a handoff that died
            await h.orch.sweep()
            assert sb.state == "paused" and Path(sb.home, w.RUNS, old, "collected").exists()

    run(go())


def test_a_run_adopted_after_a_restart_keeps_its_start_time(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as first:
            s = mock.new_session()
            mock.run(s.id, "sleep 3")
            await first.until(lambda: first.noted("acked", s.id))
        await asyncio.sleep(1.2)
        async with harness(mock, world, ant_dist) as second:
            (r,) = second.orch.runs.values()
            # RUN_TIMEOUT counts from the run's start, carried in its id, not from the restart.
            assert time.monotonic() - r.started >= time.time() - w.started_at(r.run_id) - 0.2 >= 1.0

    run(go())


def test_shutdown_waits_for_work_started_while_it_waits(mock, world, ant_dist):
    async def go():
        s = settings_for(mock, ant_dist)
        cma = CMA(s.base_url, s.environment_id, s.environment_key)
        orch = Orchestrator(s, cma, SessionSandboxes(s, api=world.api()))
        orch._started = True
        done: list[str] = []

        async def late() -> None:
            await asyncio.sleep(0.3)
            done.append("late")

        async def first() -> None:
            await asyncio.sleep(0.2)
            orch._spawn(late())  # spawned after the stop, as a failed handoff spawns its give-up

        orch._spawn(first())
        runner = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)
        orch.stop()
        await asyncio.wait_for(runner, 10)
        await cma.close()
        assert done == ["late"]

    run(go())


@pytest.mark.skipif(sys.version_info < (3, 11), reason="3.10 has no Task.uncancel to tell the two apart")
def test_a_timeout_never_swallows_the_callers_cancel():
    async def go():
        async def body() -> None:
            async with _timeout(0.02):
                asyncio.get_running_loop().call_later(0.01, task.cancel)  # the caller's cancel
                time.sleep(0.1)  # both are due when the loop next runs
                await asyncio.sleep(5)

        task = asyncio.create_task(body())
        with pytest.raises(asyncio.CancelledError):
            await task

        async def alone() -> None:
            async with _timeout(0.02):
                await asyncio.sleep(5)

        with pytest.raises(TimeoutError):
            await alone()

    run(go())


def test_bookkeeping_stays_bounded(mock, world, ant_dist, monkeypatch):
    monkeypatch.setattr("prized_managed_agents.orchestrator.MAX_FAILURES", 3)
    s = settings_for(mock, ant_dist)
    cma = CMA(s.base_url, s.environment_id, s.environment_key)
    orch = Orchestrator(s, cma, SessionSandboxes(s, api=world.api()))
    for n in range(5):
        orch._handoff_failed(WorkItem(f"work_{n}", "sesn_1", "session", s.environment_id, "x"), HandoffError("no"))
    assert list(orch.failures) == ["work_2", "work_3", "work_4"]
    run(cma.close())

    async def go():
        async with harness(mock, world, ant_dist) as h:
            sessions = [mock.new_session() for _ in range(3)]
            for x in sessions:
                mock.run(x.id, "true")
            await h.until(lambda: all(h.noted("paused", x.id) for x in sessions))
            await asyncio.sleep(0.3)
            assert not h.orch._locks and not h.orch.failures

    run(go())
