"""Three things a stranger hit (gap check, 11-07): a warm parent that deleted itself after
KEEP_PAUSED, an environment key sent to whatever ANTHROPIC_BASE_URL said, and the free tier's limits
breaking the defaults. Each test here fails on the code before the fix."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from conftest import harness, run, settings_for
from mock_cma import ENV_ID, ENV_KEY
from prized import PrizedError

from prized_managed_agents import worker as w
from prized_managed_agents.config import DEFAULT_BASE_URL, ConfigError, Settings
from prized_managed_agents.sandboxes import SessionSandboxes

BASE = {"ANTHROPIC_ENVIRONMENT_ID": ENV_ID, "ANTHROPIC_ENVIRONMENT_KEY": ENV_KEY}
DAY = 86400


def _disk_full() -> PrizedError:
    return PrizedError(
        "sandbox_quota_exceeded",
        "The free tier's 100 GB of disk is in use.",
        status=429,
        reason="retained_disk",
        body={"error": "sandbox_quota_exceeded", "reason": "retained_disk", "tier": "free", "limit": 100},
    )


# -- the environment key's host ----------------------------------------------------------------


def test_anthropic_base_url_is_ignored_and_only_its_own_variable_moves_the_key():
    s = Settings.from_env({**BASE, "ANTHROPIC_BASE_URL": "https://gateway.example/v1/"})
    assert s.base_url == DEFAULT_BASE_URL
    assert s.ignored_base_url == "https://gateway.example/v1"
    moved = Settings.from_env({**BASE, "ORCH_ANTHROPIC_BASE_URL": "https://proxy.example/"})
    assert moved.base_url == "https://proxy.example" and moved.ignored_base_url is None
    same = Settings.from_env({**BASE, "ANTHROPIC_BASE_URL": DEFAULT_BASE_URL + "/"})
    assert same.ignored_base_url is None


@pytest.mark.parametrize(
    "url",
    ["http://gateway.example", "ftp://api.anthropic.com", "https://user:pw@api.anthropic.com", "api.anthropic.com"],
)
def test_the_key_goes_only_to_https_or_this_machine(url):
    with pytest.raises(ConfigError, match="ORCH_ANTHROPIC_BASE_URL"):
        Settings.from_env({**BASE, "ORCH_ANTHROPIC_BASE_URL": url})
    with pytest.raises(ConfigError):
        Settings(environment_id=ENV_ID, environment_key=ENV_KEY, base_url=url)
    assert Settings.from_env({**BASE, "ORCH_ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"}).base_url


def test_start_says_where_the_key_goes_and_warns_about_an_ignored_base_url(mock, world, ant_dist, caplog):
    async def go():
        with caplog.at_level(logging.INFO, logger="prized_managed_agents"):
            async with harness(mock, world, ant_dist, ignored_base_url="https://gateway.example"):
                pass

    run(go())
    said = caplog.text
    assert f"the environment key goes to {mock.url}" in said
    assert "ANTHROPIC_BASE_URL=https://gateway.example is ignored" in said


# -- warm parents ------------------------------------------------------------------------------


def test_a_warm_parent_is_kept_until_it_is_retired(mock, world, ant_dist):
    async def go():
        s = settings_for(mock, ant_dist, keep_paused_s=DAY)
        boxes = SessionSandboxes(s, api=world.api())
        parent = await boxes.make_parent(lambda sb: _noop())
        assert world.created[-1]["auto_delete_min"] == 0  # not KEEP_PAUSED's 1440
        assert [p.id for p in await boxes.parents()] == [parent.id]
        session_box = await world.api().create(labels=boxes.labels("sesn_other"))
        with pytest.raises(PermissionError):
            await boxes.retire_parent(session_box.id)
        assert session_box.state != "deleted"
        await boxes.retire_parent(parent.id)
        assert parent.state == "deleted" and await boxes.parents() == []

        kept = SessionSandboxes(replace(s, warm_parent_keep_s=28 * DAY), api=world.api())
        await kept.make_parent(lambda sb: _noop())
        assert world.created[-1]["auto_delete_min"] == 28 * 1440

    run(go())


def test_warm_parent_keep_reads_from_the_environment():
    assert Settings.from_env(BASE).warm_parent_keep_s == 0
    assert Settings.from_env({**BASE, "WARM_PARENT_KEEP": "28d"}).warm_parent_keep_s == 28 * DAY


def test_copies_of_a_kept_parent_are_swept_once_paused_past_keep_paused(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, keep_paused_s=DAY) as h:
            parent = await h.orch.boxes.make_parent(lambda sb: w.install_ant(sb, h.settings))
            h.orch.s = h.orch.boxes.s = replace(h.settings, warm_parent=parent.id)
            old, fresh = mock.new_session(), mock.new_session()
            for s in (old, fresh):
                mock.run(s.id, "true")
                await h.until(lambda s=s: h.noted("paused", s.id))
            copy_old, copy_fresh = h.sandbox_of(old.id), h.sandbox_of(fresh.id)
            assert h.noted("forked", old.id) and copy_old.options.get("auto_delete_min") == 0
            copy_old.last_active_at = datetime.now(timezone.utc) - timedelta(days=2)
            swept = await h.orch.sweep()
            assert swept == [copy_old.id] and copy_old.state == "deleted"
            assert copy_fresh.state == "paused" and parent.state == "paused"
            assert any("past KEEP_PAUSED" in n.detail for n in h.noted("swept", old.id))

    run(go())


# -- the free tier -----------------------------------------------------------------------------


def test_free_tier_defaults_keep_what_was_named_and_drop_what_it_refuses():
    s = Settings.from_env({**BASE, "MAX_IDLE": "30s", "MAX_RUNTIME": "2h", "MAX_SESSIONS": "40"})
    free, notes = s.on_free_tier()
    assert free.free and free.max_sessions == 10  # 10 sandboxes at once, even if named
    assert free.max_idle_s == 30  # named, so kept
    assert free.auto_pause_s == 120 and free.max_runtime_s is None  # over 60 minutes is refused
    assert free.keep_paused_s == s.keep_paused_s and any("MAX_SESSIONS" in n for n in notes)
    plain, _ = Settings.from_env(BASE).on_free_tier()
    assert plain.max_idle_s == 10 and plain.max_sessions == 10
    slow_probes, _ = Settings.from_env({**BASE, "PROBE_EVERY": "60s"}).on_free_tier()
    assert slow_probes.auto_pause_s == 15 * 60  # 2m would let a served sandbox pause between probes


def _account(routes: dict[str, httpx.Response]) -> Any:
    from prized_managed_agents.account import PrizedAccount  # new with the fix: imported here, so the
    # other tests in this file still run (and fail) against the code before it

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer dcp_test"
        return routes.get(request.url.raw_path.decode(), httpx.Response(404))

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    return PrizedAccount(http_client=client, api_key="dcp_test", base_url="http://127.0.0.1:9")


def test_the_free_tier_is_read_from_prized():
    ended = {
        "ended": [{"id": "sbx_a", "reason": "free_tier_run_limit", "limitMin": 60, "message": "Top up to lift it."}]
    }

    from prized_managed_agents.cli import free_tier_settings

    async def go():
        trial = _account({"/api/v1/me": httpx.Response(200, json={"billing": {"trial": True}})})
        assert (await trial.free_tier())[0] is True
        paid = _account({"/api/v1/me": httpx.Response(200, json={"billing": {"trial": False}})})
        assert (await paid.free_tier())[0] is False
        promo = _account(
            {
                "/api/v1/me": httpx.Response(200, json={"billing": {"trial": False}}),
                "/api/v1/sandboxes?ended=1": httpx.Response(200, json={"sandboxes": [], **ended}),
            }
        )
        assert (await promo.free_tier())[0] is True  # credit that isn't the trial's: still the free tier
        listed = _account(
            {
                "/api/v1/me": httpx.Response(200, json={"billing": None}),
                "/api/v1/sandboxes?ended=1": httpx.Response(200, json={"sandboxes": [], **ended}),
            }
        )
        assert (await listed.free_tier())[0] is True
        why = await listed.gone_reason("sbx_a")
        assert why is not None and "60-minute run limit" in why and "Top up" in why
        assert await listed.gone_reason("sbx_b") is None
        blind = _account(
            {
                "/api/v1/me": httpx.Response(200, json={"billing": None}),
                "/api/v1/sandboxes?ended=1": httpx.Response(200, json={"sandboxes": []}),
            }
        )
        assert (await blind.free_tier())[0] is None

        s = Settings.from_env(BASE)
        assert (await free_tier_settings(s, trial)).free
        assert not (await free_tier_settings(s, paid)).free
        assert not (await free_tier_settings(replace(s, free_tier="0"), trial)).free
        assert (await free_tier_settings(replace(s, free_tier="1"), paid)).max_sessions == 10

    run(go())


def test_on_the_free_tier_a_full_disk_deletes_the_session_paused_longest(mock, world, ant_dist):
    # Not told it is on the free tier (FREE_TIER auto, a key that can't read billing): the refusal's
    # "tier": "free" is enough.
    async def go():
        async with harness(mock, world, ant_dist) as h:
            older, newer, third = mock.new_session(), mock.new_session(), mock.new_session()
            for s in (older, newer):
                mock.run(s.id, "echo kept > notes.txt")
                await h.until(lambda s=s: h.noted("paused", s.id))
            box_older, box_newer = h.sandbox_of(older.id), h.sandbox_of(newer.id)
            box_older.last_active_at = datetime.now(timezone.utc) - timedelta(hours=3)
            # The disk holds two; a deleted sandbox counts until it is gone, as on Prized, so an
            # eviction that doesn't wait for that would be refused again and evict the next one.
            world.disk_cap, world.delete_delay = 2, 2.0
            mock.run(third.id, "true")
            await h.until(lambda: h.noted("paused", third.id), why="the new session to be served")
            assert box_older.state == "deleted" and box_newer.state == "paused"
            evicted = h.noted("evicted", older.id)
            assert evicted and box_older.id in evicted[0].detail and third.id in evicted[0].detail
            assert not h.noted("handoff failed", third.id) and len(h.noted("evicted")) == 1

    run(go())


def test_a_refusal_without_the_free_tiers_tag_evicts_nothing_even_when_told_free(mock, world, ant_dist):
    # A workspace that topped up after the start: FREE_TIER said free, Prized's refusal says paid.
    async def go():
        async with harness(mock, world, ant_dist, free=True) as h:
            first, second = mock.new_session(), mock.new_session()
            mock.run(first.id, "true")
            await h.until(lambda: h.noted("paused", first.id))
            world.fail["create"] = PrizedError(
                "sandbox_quota_exceeded", status=429, reason="retained_disk", body={"reason": "retained_disk"}
            )
            mock.run(second.id, "true")
            await h.until(lambda: h.noted("handoff failed", second.id))
            assert h.sandbox_of(first.id).state == "paused" and not h.noted("evicted")

    run(go())


def test_eviction_skips_sessions_with_a_handoff_or_a_run(mock, world, ant_dist):
    async def go():
        boxes = SessionSandboxes(settings_for(mock, ant_dist), api=world.api())
        api = world.api()
        old = await api.create(labels=boxes.labels("sesn_busy"))
        new = await api.create(labels=boxes.labels("sesn_idle"))
        for sb in (old, new):
            await sb.pause()
        old.last_active_at = datetime.now(timezone.utc) - timedelta(days=1)
        boxes.busy = lambda: {"sesn_busy"}
        victim = await boxes.evict_one("sesn_new")
        assert victim is not None and victim[0] == new.id and old.state == "paused" and new.state == "deleted"

    run(go())


def test_off_the_free_tier_a_quota_deletes_nothing(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            first, second = mock.new_session(), mock.new_session()
            mock.run(first.id, "true")
            await h.until(lambda: h.noted("paused", first.id))
            world.fail["create"] = PrizedError(
                "sandbox_quota_exceeded", status=429, reason="retained_disk", body={"reason": "retained_disk"}
            )
            mock.run(second.id, "true")
            await h.until(lambda: h.noted("handoff failed", second.id))
            assert h.sandbox_of(first.id).state == "paused" and not h.noted("evicted")

    run(go())


def test_a_sandbox_the_free_tier_deleted_is_named_with_the_reason(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist) as h:
            s = mock.new_session()
            mock.run(s.id, "echo one > notes.txt; sleep 30")
            await h.until(lambda: h.noted("acked", s.id))
            first = h.sandbox_of(s.id)
            world.end_at_run_limit(first)
            gone = await h.until(lambda: h.noted("sandbox gone", s.id), why="the probe to notice")
            assert "60-minute run limit" in gone[0].detail
            mock.run(s.id, "cat notes.txt || echo missing")
            await h.until(lambda: h.noted("created", s.id) and len(h.noted("created", s.id)) == 2)
            again = h.noted("sandbox gone", s.id)[-1]
            assert first.id in again.detail and "60-minute run limit" in again.detail
            assert "starts without the session's files" in again.detail

    run(go())


def test_on_the_free_tier_a_sandbox_nearly_out_of_run_time_is_named(mock, world, ant_dist):
    async def go():
        async with harness(mock, world, ant_dist, free=True) as h:
            s = mock.new_session()
            mock.run(s.id, "true")
            await h.until(lambda: h.noted("paused", s.id))
            h.sandbox_of(s.id).expires_at = datetime.now(timezone.utc) + timedelta(minutes=4)
            mock.run(s.id, "true")
            low = await h.until(lambda: h.noted("run time low", s.id))
            assert "3 minutes" in low[0].detail or "4 minutes" in low[0].detail

    run(go())


async def _noop() -> None:
    return None


def test_a_slow_answer_about_a_lost_sandbox_never_fails_the_handoff(mock, world, ant_dist, monkeypatch):
    monkeypatch.setattr("prized_managed_agents.orchestrator.REASON_WAIT_S", 0.2)

    async def go():
        async with harness(mock, world, ant_dist, handoff_timeout_s=2) as h:
            import asyncio

            async def slow(_sandbox_id: str) -> str:
                await asyncio.sleep(30)
                return "never read"

            h.orch.account.gone_reason = slow
            s = mock.new_session()
            mock.run(s.id, "true")
            await h.until(lambda: h.noted("paused", s.id))
            world.end_at_run_limit(h.sandbox_of(s.id) or world.live()[0])
            mock.run(s.id, "true")
            await h.until(lambda: len(h.noted("paused", s.id)) == 2, why="the next message to be served")
            gone = h.noted("sandbox gone", s.id)
            assert gone and "it was deleted" in gone[-1].detail and not h.noted("handoff failed", s.id)

    run(go())
