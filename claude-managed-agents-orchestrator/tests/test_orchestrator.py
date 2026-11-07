"""End to end: the mock API delivers signed webhooks to the orchestrator's receiver, the
orchestrator claims work and starts the stub ant in fake sandboxes, and the stub serves the
session's "tool calls" with the work item's own token."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from conftest import harness, run, sandbox_text
from mock_cma import ENV_ID, ENV_KEY, WEBHOOK_KEY
from prized import PrizedError

from prized_managed_agents import worker as w
from prized_managed_agents.sandboxes import ENVIRONMENT


async def delivered(mock, *args, **kw):
    """Emit an event and wait until every attempt at it is done, without blocking the event loop
    the orchestrator's receiver runs on."""
    return await asyncio.to_thread(mock.emit, *args, wait=True, **kw)


def work_file(sb, name: str) -> Path:
    return Path(sb.home, "work", name)


def test_a_session_runs_in_its_own_sandbox_and_pauses_when_its_run_ends(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            item = mock.run(s.id, "echo hello > hello.txt")
            await h.until(lambda: h.noted("paused", s.id), why="the sandbox to pause")
            sb = h.sandbox_of(s.id)
            assert sb.state == "paused"
            assert work_file(sb, "hello.txt").read_text() == "hello\n"
            assert s.results == [{"exit_code": 0, "output": ""}]
            # The handoff came first, the ack after it.
            order = [n.what for n in h.orch.notes if n.session == s.id]
            assert order.index("worker started") < order.index("acked") < order.index("run ended")
            assert mock.item(item.id).state == "stopped" and mock.item(item.id).stopped_by == "sessions_token"
            assert mock.item(item.id).heartbeats >= 1
            return sb

    sb = run(go())
    assert sb.labels == {
        "test": "cma",
        "managed-by": "prized-managed-agents",
        "anthropic-environment": ENV_ID,
        "anthropic-session": sb.labels["anthropic-session"],
    }
    made = world.created[0]
    assert made["template"] == "prized-base-v1" and made["auto_pause_min"] == 15 and made["auto_delete_min"] == 7 * 1440


def test_only_the_sessions_secret_reaches_the_sandbox(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            mock.run(s.id, "env > env.txt")
            await h.until(lambda: h.noted("paused", s.id))
            return s

    s = run(go())
    seen = s.stub_env[0]
    assert seen["secret_from_file"] and not seen["has_environment_key"]
    assert "ANTHROPIC_WORK_SECRET" not in seen["names"] and "ANTHROPIC_API_KEY" not in seen["names"]
    # Not in any command's environment, not in any file, not in what the agent's commands saw.
    assert all(ENV_KEY not in v and WEBHOOK_KEY not in v for _, env in world.execs for v in env.values())
    text = sandbox_text(world)
    assert ENV_KEY not in text and WEBHOOK_KEY not in text
    # And the secret itself is gone once the run ended.
    assert not list(Path(world.root).glob(f"*/{w.RUNS}/*/secret"))
    # The only credential the stub used against the API was its sessions token.
    beats = [k for m, p, k in mock.requests if p.endswith("/heartbeat")]
    assert beats and set(beats) == {"sessions_token"}


def test_the_next_run_resumes_the_same_sandbox_with_its_files(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            mock.run(s.id, "echo one > notes.txt")
            await h.until(lambda: len(h.noted("paused", s.id)) == 1)
            first = h.sandbox_of(s.id)
            mock.run(s.id, "echo two >> notes.txt")
            await h.until(lambda: len(h.noted("paused", s.id)) == 2)
            assert h.sandbox_of(s.id) is first
            assert work_file(first, "notes.txt").read_text() == "one\ntwo\n"
            assert [c for c in world.calls if c[0] in ("create", "resume")] == [
                ("create", first.id),
                ("resume", first.id),
            ]
            assert len(h.noted("resumed", s.id)) == 1

    run(go())


def test_a_terminated_or_deleted_session_loses_its_sandbox(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            a, b = mock.new_session(), mock.new_session()
            mock.run(a.id, "true")
            mock.run(b.id, "true")
            await h.until(lambda: h.noted("paused", a.id) and h.noted("paused", b.id))
            mock.terminate(a.id)
            mock.delete(b.id)
            await h.until(lambda: h.sandbox_of(a.id) is None and h.sandbox_of(b.id) is None, why="both deleted")

    run(go())


def test_a_session_terminated_mid_run_is_stopped_and_deleted(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            mock.run(s.id, "sleep 30")
            await h.until(lambda: h.noted("acked", s.id))
            mock.terminate(s.id)
            await h.until(lambda: h.sandbox_of(s.id) is None, why="the sandbox to go")
            assert not h.orch.runs

    run(go())


def test_retried_and_duplicate_deliveries_act_once(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            mock.run(s.id, "true", webhook=False)
            event = await delivered(mock, "session.status_run_started", s.id, duplicate=True)
            await h.until(lambda: h.noted("paused", s.id))
            assert [st for e, _, st in mock.deliveries if e == event] == [200, 200]
            assert h.app.counts["duplicate"] == 1
            assert len(h.noted("worker started", s.id)) == 1

    run(go())


def test_forged_or_stale_deliveries_are_refused_and_claim_nothing(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, reconcile_every_s=3600) as h:
            s = mock.new_session()
            mock.run(s.id, "true", webhook=False)
            forged = await delivered(
                mock, "session.status_run_started", s.id, key="whsec_" + "Zm9yZ2VkLWtleS1mb3JnZWQta2V5LWZvcmdlZA=="
            )
            stale = await delivered(mock, "session.status_run_started", s.id, stale=True)
            assert [st for e, _, st in mock.deliveries if e == forged] == [401, 401, 401]
            assert [st for e, _, st in mock.deliveries if e == stale] == [401, 401, 401]
            await asyncio.sleep(0.3)
            assert not h.noted("worker started") and mock.items_of(s.id)[0].claimed_at is None
            assert h.app.counts["refused"] == 6

    run(go())


def test_a_lost_webhook_is_caught_by_the_backstop_poll(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, reconcile_every_s=0.5) as h:
            s = mock.new_session()
            mock.endpoint_disabled = "lost on purpose"
            mock.run(s.id, "true")
            await h.until(lambda: h.noted("paused", s.id))

    run(go())


def test_without_a_signing_key_it_long_polls(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, webhook_key=None) as h:
            assert h.app is None
            s = mock.new_session()
            mock.run(s.id, "echo polled > p.txt")
            await h.until(lambda: h.noted("paused", s.id))
            assert work_file(h.sandbox_of(s.id), "p.txt").read_text() == "polled\n"

    run(go())


def test_a_failed_handoff_is_not_acked_leaves_nothing_and_succeeds_when_the_item_comes_back(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, handoff_timeout_s=2, reconcile_every_s=0.3) as h:
            world.fail["create"] = PrizedError("sandbox_quota_exceeded", status=429, reason="running_vcpu")
            s = mock.new_session()
            item = mock.run(s.id, "true")
            await h.until(lambda: h.noted("handoff failed", s.id))
            assert mock.item(item.id).acked_at is None and not world.live()
            # The queue hands it out again after the reclaim window (2 x the handoff timeout).
            await h.until(lambda: h.noted("paused", s.id), timeout=20)
            assert mock.item(item.id).claims == 2

    run(go())


def test_a_bad_ant_download_fails_the_handoff_deletes_its_sandbox_and_gives_up_in_the_end(mock, world, ant_dist):
    bad = {"amd64": "0" * 64, "arm64": "0" * 64}

    async def go():
        async with harness(
            mock, world, ant_dist, ant_sha256=bad, handoff_timeout_s=2, reconcile_every_s=0.3, max_handoff_attempts=2
        ) as h:
            s = mock.new_session()
            item = mock.run(s.id, "true")
            await h.until(lambda: h.noted("gave up", s.id), timeout=20)
            await h.until(lambda: mock.item(item.id).state == "stopped")
            failures = h.noted("handoff failed", s.id)
            assert "sha256" in failures[0].detail
            assert len(h.noted("deleted", s.id)) == 2 and not world.live()

    run(go())


def test_a_worker_that_dies_at_once_fails_the_handoff(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, max_handoff_attempts=1) as h:
            s = mock.new_session(metadata={"stub_crash": "1"})
            item = mock.run(s.id, "true")
            await h.until(lambda: h.noted("gave up", s.id))
            assert "exited at once (exit 3)" in h.noted("gave up", s.id)[0].detail
            assert "crashing as asked" in h.noted("gave up", s.id)[0].detail
            assert mock.item(item.id).state == "stopped" and mock.item(item.id).stopped_by == "environment_key"
            assert not world.live()

    run(go())


def test_healthcheck_items_and_items_without_a_secret_are_ended_without_a_sandbox(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            hc = mock.healthcheck()
            s = mock.new_session()
            item = mock.run(s.id, "true", webhook=False)
            mock.item(item.id).no_secret = True
            mock.emit("session.status_run_started", s.id)
            await h.until(lambda: mock.item(hc.id).state == "stopped" and mock.item(item.id).state == "stopped")
            assert not world.created
            assert any("no secret" in n.detail for n in h.noted("refused"))

    run(go())


def test_session_metadata_picks_the_template(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session(metadata={"prized_template": "prized-agent-v1"})
            mock.run(s.id, "true")
            await h.until(lambda: h.noted("paused", s.id))
            assert world.created[0]["template"] == "prized-agent-v1"

    run(go())


def test_sessions_fork_a_warm_parent_and_only_a_warm_parent(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, max_handoff_attempts=1) as h:
            parent = await h.orch.boxes.make_parent(lambda sb: w.install_ant(sb, h.settings))
            Path(parent.home, "work", "deps.txt").write_text("installed\n")
            s = mock.new_session(metadata={"prized_fork_from": parent.id})
            mock.run(s.id, "cat deps.txt > seen.txt")
            await h.until(lambda: h.noted("paused", s.id))
            child = h.sandbox_of(s.id)
            assert child is not parent and ("fork", child.id) in world.calls
            assert work_file(child, "seen.txt").read_text() == "installed\n"
            assert parent.state == "paused"
            # Not any sandbox: one without the warm-parent label is refused.
            other = mock.new_session(metadata={"prized_fork_from": child.id})
            mock.run(other.id, "true")
            await h.until(lambda: h.noted("gave up", other.id))
            assert "not a warm parent" in h.noted("gave up", other.id)[0].detail
            # Nor another environment's warm parent in the same Prized workspace.
            role = {"prized-managed-agents-role": "warm-parent"}
            foreign = await world.api().create(labels={**h.orch.boxes.base, ENVIRONMENT: "env_other", **role})
            third = mock.new_session(metadata={"prized_fork_from": foreign.id})
            mock.run(third.id, "true")
            await h.until(lambda: h.noted("gave up", third.id))
            detail = h.noted("gave up", third.id)[0].detail
            assert "not a warm parent of this environment" in detail and f"anthropic-environment={ENV_ID}" in detail
            assert ("fork", foreign.id) not in world.calls and not world.live(**{"anthropic-session": third.id})

    run(go())


def test_warm_parent_setting_forks_and_skips_the_ant_download(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            parent = await h.orch.boxes.make_parent(lambda sb: w.install_ant(sb, h.settings))
            os.rename(ant_dist["dir"], ant_dist["dir"] + ".gone")  # a second download would fail
            try:
                h.orch.s = h.orch.boxes.s = _replace(h.settings, warm_parent=parent.id)
                s = mock.new_session()
                mock.run(s.id, "true")
                await h.until(lambda: h.noted("paused", s.id))
                assert h.noted("forked", s.id)
            finally:
                os.rename(ant_dist["dir"] + ".gone", ant_dist["dir"])

    run(go())


def test_a_run_longer_than_auto_pause_stays_awake_and_a_dead_orchestrator_lets_it_pause(mock, world, ant_dist):
    async def go():
        world.start_auto_pause(0.8)
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            mock.run(s.id, "sleep 2.5; echo done > long.txt")
            await h.until(lambda: h.noted("paused", s.id), timeout=20)
            sb = h.sandbox_of(s.id)
            assert work_file(sb, "long.txt").read_text() == "done\n"
            assert ("auto-pause", sb.id) not in world.calls
            # Now a run whose orchestrator goes away: its sandbox pauses itself, freezing the worker.
            mock.run(s.id, "sleep 30")
            await h.until(lambda: h.noted("acked") and len(h.noted("acked", s.id)) == 2)
        await asyncio.sleep(1.5)
        assert ("auto-pause", sb.id) in world.calls and sb.state == "paused"

    run(go())


def test_a_restarted_orchestrator_adopts_the_runs_it_finds(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as first:
            s = mock.new_session()
            mock.run(s.id, "sleep 2; echo finished > out.txt")
            await first.until(lambda: first.noted("acked", s.id))
        async with harness(mock, world, ant_dist) as second:
            await second.until(lambda: second.noted("adopted", s.id))
            await second.until(lambda: second.noted("paused", s.id), timeout=20)
            sb = second.sandbox_of(s.id)
            assert work_file(sb, "out.txt").read_text() == "finished\n"
            assert len([c for c in world.calls if c[0] == "create"]) == 1
            assert not second.noted("worker started")

    run(go())


def test_the_sweep_deletes_sandboxes_of_sessions_that_ended_while_nobody_watched(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            mock.run(s.id, "true")
            await h.until(lambda: h.noted("paused", s.id))
        mock.delete(s.id, webhook=False)
        async with harness(mock, world, ant_dist) as h2:
            await h2.until(lambda: h2.noted("swept", s.id))
            assert h2.sandbox_of(s.id) is None

    run(go())


def test_a_run_past_its_timeout_is_stopped_and_the_sandbox_paused(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, run_timeout_s=1) as h:
            s = mock.new_session()
            item = mock.run(s.id, "sleep 60")
            await h.until(lambda: h.noted("run timeout", s.id))
            await h.until(lambda: h.noted("paused", s.id), timeout=20)
            assert mock.item(item.id).state == "stopped"

    run(go())


def test_reused_work_ids_get_runs_of_their_own(mock, world, ant_dist):
    mock.reuse_work_ids = True

    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            mock.run(s.id, "echo a > a.txt")
            await h.until(lambda: len(h.noted("paused", s.id)) == 1)
            mock.run(s.id, "echo b > b.txt")
            await h.until(lambda: len(h.noted("paused", s.id)) == 2)
            runs = list(Path(h.sandbox_of(s.id).home, w.RUNS).iterdir())
            assert len(runs) == 2 and all(r.name.startswith(s.id) for r in runs)

    run(go())


def test_concurrent_sessions_respect_the_handoff_limit(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, max_handoffs=1) as h:
            peak = 0

            async def watch():
                nonlocal peak
                while True:
                    peak = max(peak, h.orch._handoffs)
                    await asyncio.sleep(0.01)

            watcher = asyncio.create_task(watch())
            sessions = [mock.new_session() for _ in range(3)]
            for s in sessions:
                mock.run(s.id, f"echo {s.id} > id.txt")
            await h.until(lambda: all(h.noted("paused", s.id) for s in sessions), timeout=30)
            watcher.cancel()
            assert peak == 1
            assert len({h.sandbox_of(s.id).id for s in sessions}) == 3

    run(go())


def test_while_shutting_down_deliveries_get_503_so_they_are_retried(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            h.orch._stopping.set()
            event = await delivered(mock, "session.status_run_started", "sesn_x")
            assert [st for e, _, st in mock.deliveries if e == event] == [503, 503, 503]

    run(go())


def _replace(settings, **kw):
    import dataclasses

    return dataclasses.replace(settings, **kw)
