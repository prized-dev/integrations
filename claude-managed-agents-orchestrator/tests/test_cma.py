"""The API client against the mock, over real HTTP on 127.0.0.1."""

from __future__ import annotations

import pytest
from conftest import run
from mock_cma import ENV_ID, ENV_KEY

from prized_managed_agents.cma import CMA, CMAError


async def nosleep(_: float) -> None:
    return None


def client(mock, key: str = ENV_KEY) -> CMA:
    return CMA(mock.url, ENV_ID, key, sleep=nosleep, worker_id="test-worker")


def test_calls_carry_the_environment_key_as_bearer_and_the_beta_header(mock):
    async def go():
        c = client(mock)
        assert (await c.stats())["type"] == "work_queue_stats"
        assert await c.poll(block_ms=None, reclaim_older_than_ms=5000) is None
        await c.close()

    run(go())
    assert [(m, p, k) for m, p, k in mock.requests] == [
        ("GET", f"/v1/environments/{ENV_ID}/work/stats", "environment_key"),
        ("GET", f"/v1/environments/{ENV_ID}/work/poll", "environment_key"),
    ]
    assert "test-worker" in mock.pollers


@pytest.mark.parametrize("empty", [200, 204])
def test_an_empty_poll_is_none_whether_null_or_no_content(mock, empty):
    mock.empty_poll_status = empty

    async def go():
        c = client(mock)
        try:
            return await c.poll(block_ms=50, reclaim_older_than_ms=5000)
        finally:
            await c.close()

    assert run(go()) is None


def test_a_claimed_item_carries_its_session_and_secret_and_acks_once(mock):
    s = mock.new_session()
    item = mock.run(s.id, "true", webhook=False)

    async def go():
        c = client(mock)
        try:
            got = await c.poll(block_ms=None, reclaim_older_than_ms=5000)
            assert got is not None and got.id == item.id and got.session_id == s.id and got.kind == "session"
            assert got.secret and got.secret not in repr(got)
            assert await c.ack(got.id) is True
            assert mock.item(got.id).state == "starting"
            assert await c.ack(got.id) is True  # a 409: past queued, nothing left to ack
            await c.stop(got.id)
            await c.stop(got.id)  # a 409: stopped already
            assert mock.item(got.id).state == "stopped"
        finally:
            await c.close()

    run(go())


def test_an_unacked_item_comes_back_after_the_reclaim_window(mock):
    s = mock.new_session()
    mock.run(s.id, "true", webhook=False)

    async def go():
        c = client(mock)
        try:
            first = await c.poll(block_ms=None, reclaim_older_than_ms=200)
            assert await c.poll(block_ms=None, reclaim_older_than_ms=200) is None
            again = await c.poll(block_ms=500, reclaim_older_than_ms=200)
            assert again is not None and again.id == first.id and again.secret != first.secret
        finally:
            await c.close()

    run(go())


def test_transient_failures_are_retried_and_others_are_not(mock):
    s = mock.new_session()
    mock.run(s.id, "true", webhook=False)
    mock.fail_next["poll"] = [503]
    mock.fail_next["ack"] = [500, 529]

    async def go():
        c = client(mock)
        try:
            got = await c.poll(block_ms=None, reclaim_older_than_ms=5000)
            assert got is not None
            assert await c.ack(got.id)
            mock.fail_next["stop"] = [400]
            with pytest.raises(CMAError) as info:
                await c.stop(got.id)
            assert info.value.status == 400 and not info.value.transient
        finally:
            await c.close()

    run(go())


def test_a_wrong_key_is_an_auth_error_that_never_shows_the_key(mock):
    async def go():
        c = client(mock, key="sk-ant-oat01-WRONG-KEY-123")
        try:
            with pytest.raises(CMAError) as info:
                await c.stats()
            return info.value
        finally:
            await c.close()

    err = run(go())
    assert err.auth and err.status == 401
    assert "WRONG-KEY" not in str(err) and ENV_KEY not in str(err)


def test_a_session_is_gone_only_on_the_apis_own_not_found(mock):
    s = mock.new_session(metadata={"prized_template": "prized-agent-v1"})

    async def go():
        c = client(mock)
        try:
            got = await c.session(s.id)
            assert got is not None and got.metadata == {"prized_template": "prized-agent-v1"} and got.status == "idle"
            mock.delete(s.id, webhook=False)
            assert await c.session(s.id) is None
            # A 404 that is not the API's "Session not found: <id>" (a proxy, a wrong path) deletes nothing.
            other = CMA(mock.url + "/proxy", ENV_ID, ENV_KEY, sleep=nosleep)
            with pytest.raises(CMAError):
                await other.session(s.id)
            await other.close()
        finally:
            await c.close()

    run(go())
