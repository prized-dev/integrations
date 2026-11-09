# SPDX-License-Identifier: Apache-2.0

"""Unit tests for fork_base and PrizedProvider.rebase().

They reuse test_fork_group's fake Prized API: each "sandbox" runs a real
OpenEnv server for a dice environment on loopback, and a fork copies that
server's sessions, RNG state and its record of which sessions have a
WebSocket attached (a session copied while attached refuses every socket in
the copy, as on Prized). The client's traffic goes through the provider's
real loopback proxy, so a rebase moves real WebSockets.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from typing import Optional

import pytest
from openenv.core.env_client import SessionLostError
from openenv.core.env_server.http_server import HTTPEnvServer
from websockets.protocol import State as WSState

import test_fork_group as tfg
from openenv_prized import PrizedProvider, fork_group, forkable
from openenv_prized import forking as fg
from openenv_prized import provider as prized_provider
from test_fork_group import (
    DiceAction,
    DiceEnv,
    DiceEnvironment,
    DiceObservation,
    DiceState,
    roll,
)


class _Recorder(tfg._ForkingAdapter):
    """The fork-aware fake API, recording what ran in which sandbox."""

    def __init__(self, edge):
        super().__init__(edge)
        self.events: list = []
        self.commands: list = []
        self.ready_exit = 0
        self.fail_fork: Optional[BaseException] = None
        self.fork_hook = None  # runs inside the fork call, before the copy
        self.bad_copy_url = False
        self.created: list = []
        self.undeletable: set = set()

    def create_sandbox(self, **kwargs):
        sandbox = super().create_sandbox(**kwargs)
        self.created.append(sandbox)
        self.events.append(("create", sandbox.id))
        return sandbox

    def exec(self, sandbox, command, *, timeout=60, env=None, background=False):
        self.commands.append((sandbox.id, command))
        if background:
            self.events.append(("launch", sandbox.id))
        elif "OPENENV_ROOT=" in command:
            self.events.append(("install", sandbox.id))
        elif "probe()" in command and "/health" in command:
            self.events.append(("ready", sandbox.id))
            return self.ready_exit, ""
        elif command.startswith("docker pull") and "docker run" not in command:
            self.events.append(("pull", sandbox.id))
        elif "docker run" in command:
            self.events.append(("run", sandbox.id))
        return super().exec(
            sandbox, command, timeout=timeout, env=env, background=background
        )

    def fork(self, sandbox, *, count, labels):
        self.events.append(("fork", sandbox.id))
        if self.fork_hook is not None:
            self.fork_hook()
        if self.fail_fork is not None:
            raise self.fail_fork
        copies = super().fork(sandbox, count=count, labels=labels)
        self.forks[-1]["copies"] = [c.id for c in copies]
        return copies

    def port_url(self, sandbox, port):
        self.events.append(("port", sandbox.id))
        if self.bad_copy_url and sandbox is not self.created[0]:
            return "http://8000-sbx-x.p.prized.dev"
        return super().port_url(sandbox, port)

    def set_port_public(self, sandbox, port, *, expires_in):
        self.events.append(("public", sandbox.id))
        return f"https://{port}-{sandbox.id.replace('_', '-')}.p.prized.dev/"

    def delete(self, sandbox):
        if sandbox.id in self.undeletable:
            raise RuntimeError("delete failed")
        super().delete(sandbox)
        self.events.append(("delete", sandbox.id))


@pytest.fixture(autouse=True)
def _fast_waits(monkeypatch):
    async def sleep(seconds):
        await asyncio.sleep(min(seconds, 0.02))

    monkeypatch.setattr(fg, "_sleep", sleep)
    monkeypatch.setattr(prized_provider, "_sleep", lambda seconds: None)


@pytest.fixture()
def adapter():
    adapter = _Recorder(tfg._Edge())
    yield adapter
    for sandbox in list(adapter.live.values()):
        sandbox.server.stop()


def _provider(adapter, **kwargs) -> PrizedProvider:
    return PrizedProvider(_adapter=adapter, image="space:test/dice", **kwargs)


async def _episode(adapter, *, prefix=3, seed=1, make_forkable=True, **kwargs):
    """A live episode on a started provider: reset(seed), then `prefix` rolls."""
    provider = _provider(adapter, **kwargs)
    url = await asyncio.to_thread(provider.start_container)
    await asyncio.to_thread(provider.wait_for_ready, url, 10)
    env = DiceEnv(base_url=url, provider=provider)
    await env.connect()
    if make_forkable:
        await forkable(env)
    await env.reset(seed=seed)
    for _ in range(prefix):
        await roll(env)
    return env, provider


def _draws(seed: int, skip: int, n: int = 1):
    rng = random.Random(seed)
    for _ in range(skip):
        rng.random()
    return [rng.random() for _ in range(n)]


def _session(adapter, sandbox_id, env):
    """The dice environment of env's session inside one fake sandbox."""
    server = adapter.live[sandbox_id].server.env_server
    return server._sessions[fg.session_id_of(env)]


async def _rebase(provider):
    return await asyncio.to_thread(provider.rebase)


# ---------------------------------------------------------------------------
# fork_base in start_container
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, mode",
    [
        (None, None),
        (False, None),
        (True, "ready"),
        ("ready", "ready"),
        ("install", "install"),
    ],
)
def test_fork_base_values(value, mode):
    assert PrizedProvider(_adapter=object(), fork_base=value).fork_base == mode


@pytest.mark.parametrize("value", ["yes", "Ready", 1, "start"])
def test_fork_base_rejects_other_values(value):
    with pytest.raises(ValueError, match="fork_base"):
        PrizedProvider(_adapter=object(), fork_base=value)


@pytest.mark.asyncio
async def test_ready_base_forks_the_running_server_and_deletes_the_original(adapter):
    env, provider = await _episode(adapter, fork_base=True)
    first, copy = adapter.created[0].id, provider.sandbox_id
    assert copy != first
    assert adapter.events[:7] == [
        ("create", first),
        ("install", first),
        ("launch", first),
        ("ready", first),  # the server answered, checked inside the sandbox
        ("fork", first),
        ("delete", first),
        ("port", copy),  # the only port ever opened is the copy's
    ]
    assert set(adapter.live) == {copy}
    assert provider.last_rebase["from"] == first and provider.last_rebase["to"] == copy
    assert provider.timings.keys() >= {"create_s", "install_s", "ready_s", "rebase_s"}
    # The episode runs in the copy.
    assert await roll(env) == _draws(1, 3)[0]
    assert _session(adapter, copy, env).state.step_count == 4
    await env.close()
    assert adapter.live == {} and provider.sandbox_id is None


@pytest.mark.asyncio
async def test_install_base_starts_the_server_in_the_copy(adapter):
    provider = _provider(adapter, fork_base="install")
    url = await asyncio.to_thread(provider.start_container)
    first, copy = adapter.created[0].id, provider.sandbox_id
    assert adapter.events == [
        ("create", first),
        ("install", first),
        ("fork", first),
        ("delete", first),
        ("launch", copy),
        ("port", copy),
    ]
    await asyncio.to_thread(provider.wait_for_ready, url, 10)
    await asyncio.to_thread(provider.stop_container)
    assert adapter.live == {}


def test_without_fork_base_nothing_changes(adapter):
    provider = _provider(adapter)
    provider.start_container()
    only = adapter.created[0].id
    assert adapter.events == [
        ("create", only),
        ("install", only),
        ("launch", only),
        ("port", only),
    ]
    assert provider.last_rebase is None
    provider.stop_container()


def test_wait_timeout_bounds_the_ready_wait(adapter):
    provider = _provider(adapter, fork_base="ready")
    provider.start_container(wait_timeout=7)
    script = next(c for _, c in adapter.commands if "probe()" in c)
    assert "+ 7 ))" in script
    provider.stop_container()


def test_a_failed_base_fork_leaves_the_environment_in_the_original(adapter):
    adapter.fail_fork = RuntimeError("fork refused")
    provider = _provider(adapter, fork_base=True)
    with pytest.warns(RuntimeWarning, match="base fork failed.*fork refused"):
        url = provider.start_container()
    assert provider.sandbox_id == adapter.created[0].id
    assert adapter.deleted == []
    provider.wait_for_ready(url, 10)
    provider.stop_container()
    assert adapter.live == {}


@pytest.mark.parametrize(
    "code, error, match",
    [
        (7, RuntimeError, "died"),
        (8, TimeoutError, "ready within"),
        (9, RuntimeError, "curl or python3"),
    ],
)
def test_a_server_that_never_answers_fails_the_start_and_is_deleted(
    adapter, code, error, match
):
    adapter.ready_exit = code
    provider = _provider(adapter, fork_base="ready")
    with pytest.raises(error, match=match):
        provider.start_container()
    assert ("fork", adapter.created[0].id) not in adapter.events
    assert adapter.live == {} and provider.sandbox_id is None


def test_image_ready_base_checks_the_container(adapter):
    provider = PrizedProvider(
        _adapter=adapter, image="registry.example/env:1", fork_base=True
    )
    provider.start_container()
    script = next(c for _, c in adapter.commands if "probe()" in c)
    assert "docker inspect" in script and "kill -0" not in script
    assert ("fork", adapter.created[0].id) in adapter.events
    provider.stop_container()


def test_image_install_base_pulls_then_runs_in_the_copy(adapter):
    provider = PrizedProvider(
        _adapter=adapter, image="registry.example/env:1", fork_base="install"
    )
    provider.start_container()
    first, copy = adapter.created[0].id, provider.sandbox_id
    assert adapter.events == [
        ("create", first),
        ("pull", first),
        ("fork", first),
        ("delete", first),
        ("run", copy),
        ("port", copy),
    ]
    provider.stop_container()


# ---------------------------------------------------------------------------
# rebase(): the copy is adopted, the episode and its open socket survive
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rebase_adopts_the_copy_and_keeps_the_episode_and_socket(adapter):
    env, provider = await _episode(adapter)
    first, url, socket = provider.sandbox_id, env.base_url, env._ws
    assert socket is not None

    assert await _rebase(provider) == url
    copy = provider.sandbox_id
    assert copy != first
    assert first in adapter.deleted and set(adapter.live) == {copy}
    fork = adapter.forks[-1]
    assert fork["parent"] == first and fork["copies"] == [copy]
    assert fork["attached"] == set()  # the session was let go before the copy
    assert fork["labels"] is None  # the copy keeps the original's labels
    # Same URL, same open socket; the episode carries on in the copy.
    assert env.base_url == url and env._ws is socket
    assert await roll(env) == _draws(1, 3)[0]
    assert (await env.state()).step_count == 4
    assert _session(adapter, copy, env).state.step_count == 4
    record = provider.last_rebase
    assert record["from"] == first and record["to"] == copy
    assert record["sockets"] == 1 and record["sockets_lost"] == 0
    assert record.keys() >= {"detach_s", "fork_s", "attach_s", "delete_s", "total_s"}

    # Again: a rebased environment rebases like any other.
    await _rebase(provider)
    assert await roll(env) == _draws(1, 4)[0]
    await env.close()
    assert adapter.live == {}


class _SlowDice(DiceEnvironment):
    def step(self, action, timeout_s=None, **kw):
        if action.code.startswith("sleep:"):
            time.sleep(float(action.code[len("sleep:") :]))
            self._state.step_count += 1
            return DiceObservation(stdout="slept")
        return super().step(action, timeout_s=timeout_s, **kw)


@pytest.mark.asyncio
async def test_a_call_in_flight_is_answered_before_the_socket_moves(
    adapter, monkeypatch
):
    monkeypatch.setattr(
        tfg,
        "_new_env_server",
        lambda: HTTPEnvServer(
            _SlowDice, DiceAction, DiceObservation, state_cls=DiceState
        ),
    )
    env, provider = await _episode(adapter)
    slow = asyncio.ensure_future(env.step(DiceAction(code="sleep:0.6")))
    await asyncio.sleep(0.2)  # the step is running on the server
    rebasing = asyncio.ensure_future(_rebase(provider))
    assert (await slow).observation.stdout == "slept"
    await rebasing
    # The original answered, let go after the step, and the copy has it.
    assert provider.last_rebase["detach_s"] >= 0.2
    assert _session(adapter, provider.sandbox_id, env).state.step_count == 4
    assert await roll(env) == _draws(1, 3)[0]
    await env.close()


@pytest.mark.asyncio
async def test_a_call_longer_than_the_timeout_calls_the_rebase_off(
    adapter, monkeypatch
):
    monkeypatch.setattr(
        tfg,
        "_new_env_server",
        lambda: HTTPEnvServer(
            _SlowDice, DiceAction, DiceObservation, state_cls=DiceState
        ),
    )
    env, provider = await _episode(adapter)
    first, socket = provider.sandbox_id, env._ws
    slow = asyncio.ensure_future(env.step(DiceAction(code="sleep:1.0")))
    await asyncio.sleep(0.2)
    with pytest.raises(TimeoutError, match="still had a call running"):
        await asyncio.to_thread(provider.rebase, timeout_s=0.3)
    assert adapter.forks == [] and provider.sandbox_id == first
    assert (await slow).observation.stdout == "slept"
    # The socket rejoins its session in the original once the call is done.
    assert await roll(env) == _draws(1, 3)[0]
    assert env._ws is socket
    assert _session(adapter, first, env).state.step_count == 5
    await env.close()


@pytest.mark.asyncio
async def test_a_call_made_during_the_move_waits_and_runs_in_the_copy(adapter):
    env, provider = await _episode(adapter)
    original = provider.sandbox_id
    entered, at_fork = threading.Event(), []

    def slow_fork():
        entered.set()
        time.sleep(0.3)  # the roll below is sent meanwhile
        at_fork.append(_session(adapter, original, env).state.step_count)

    adapter.fork_hook = slow_fork
    rebasing = asyncio.ensure_future(_rebase(provider))
    await asyncio.to_thread(entered.wait, 5)
    value = await roll(env)  # sent while the sandbox is being forked
    await rebasing
    assert value == _draws(1, 3)[0]
    assert at_fork == [3]  # it waited: the original never saw it
    assert _session(adapter, provider.sandbox_id, env).state.step_count == 4
    await env.close()


@pytest.mark.asyncio
async def test_a_socket_opened_during_the_move_opens_on_the_copy(adapter):
    env, provider = await _episode(adapter)
    await env.disconnect()  # no socket open; the next call opens one
    entered = threading.Event()
    adapter.fork_hook = lambda: (entered.set(), time.sleep(0.5))
    rebasing = asyncio.ensure_future(_rebase(provider))
    await asyncio.to_thread(entered.wait, 5)
    await env.connect()  # accepted at once, not after the move
    assert not rebasing.done()
    value = await roll(env)
    await rebasing
    assert provider.last_rebase["sockets"] == 0
    assert value == _draws(1, 3)[0]
    assert _session(adapter, provider.sandbox_id, env).state.step_count == 4
    await env.close()


@pytest.mark.asyncio
async def test_fork_group_after_a_rebase_forks_the_copy(adapter):
    env, provider = await _episode(adapter, fork_base=True)
    await _rebase(provider)
    async with fork_group(env, 3, reseed=False) as group:
        assert adapter.forks[-1]["parent"] == provider.sandbox_id
        assert [(await c.state()).step_count for c in group] == [3, 3, 3]
        assert group.stats()["fork_base"] == "ready"
    assert await roll(env) == _draws(1, 3)[0]
    await env.close()
    assert adapter.live == {}


def test_fork_waits_for_a_rebase_in_progress(adapter):
    provider = _provider(adapter)
    provider.start_container()
    first = provider.sandbox_id
    entered = threading.Event()
    adapter.fork_hook = lambda: (entered.set(), time.sleep(0.3))
    rebasing = threading.Thread(target=provider.rebase)
    rebasing.start()
    entered.wait(5)
    adapter.fork_hook = None
    children = provider.fork(1)  # must not copy the sandbox being replaced
    rebasing.join(5)
    assert adapter.forks[-1]["parent"] == provider.sandbox_id != first
    child = children[0]
    assert child._fork_lock is not provider._fork_lock
    assert child._orphans == [] and child._orphans is not provider._orphans
    assert child.fork_base is None and child.last_rebase is None
    child.stop_container()
    provider.stop_container()
    assert adapter.live == {}


# ---------------------------------------------------------------------------
# rebase(): errors clean up
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_failed_fork_changes_nothing(adapter):
    env, provider = await _episode(adapter)
    first, socket = provider.sandbox_id, env._ws
    adapter.fail_fork = RuntimeError("fork refused")
    with pytest.raises(RuntimeError, match="fork refused"):
        await _rebase(provider)
    assert provider.sandbox_id == first and adapter.deleted == []
    # The socket went back to the original's session.
    assert env._ws is socket
    assert await roll(env) == _draws(1, 3)[0]
    assert _session(adapter, first, env).state.step_count == 4
    adapter.fail_fork = None
    await _rebase(provider)  # and a later rebase works
    assert provider.sandbox_id != first
    await env.close()
    assert adapter.live == {}


@pytest.mark.asyncio
async def test_a_copy_that_cannot_be_reached_is_deleted(adapter):
    env, provider = await _episode(adapter)
    first = provider.sandbox_id
    adapter.bad_copy_url = True
    with pytest.raises(RuntimeError, match="non-HTTPS"):
        await _rebase(provider)
    copy = adapter.forks[-1]["copies"][0]
    assert adapter.deleted == [copy]
    assert provider.sandbox_id == first
    assert await roll(env) == _draws(1, 3)[0]
    await env.close()
    assert adapter.live == {}


@pytest.mark.asyncio
async def test_an_original_that_cannot_be_deleted_is_retried_on_stop(adapter):
    env, provider = await _episode(adapter)
    first = provider.sandbox_id
    adapter.undeletable = {first}
    with pytest.warns(
        RuntimeWarning, match=f"could not delete the original sandbox {first}"
    ):
        await _rebase(provider)
    assert provider.sandbox_id != first and first in adapter.live
    assert ("delete", first) not in adapter.events
    assert await roll(env) == _draws(1, 3)[0]
    # stop_container deletes the copy, and retries the original: still failing.
    with pytest.raises(RuntimeError, match=first):
        await asyncio.to_thread(provider.stop_container)
    assert provider.sandbox_id is None and set(adapter.live) == {first}
    adapter.undeletable = set()
    await asyncio.to_thread(provider.stop_container)  # a second stop retries it
    assert adapter.live == {}
    await env.close()


@pytest.mark.asyncio
async def test_an_episode_on_its_own_socket_is_refused_before_forking(adapter):
    env, provider = await _episode(adapter, make_forkable=False)
    with pytest.raises(RuntimeError, match="forkable"):
        await _rebase(provider)
    assert adapter.forks == [] and adapter.deleted == []
    # The episode is untouched, and the proxy lets sockets in again.
    assert await roll(env) == _draws(1, 3)[0]
    assert not provider._proxy._holding
    await env.close()
    assert adapter.live == {}


@pytest.mark.asyncio
async def test_a_socket_that_cannot_rejoin_is_closed_with_a_warning(
    adapter, monkeypatch
):
    env, provider = await _episode(adapter)
    copy_server = tfg._copy_env_server

    def attached_copy(parent):
        child = copy_server(parent)
        child._session_websocket_attachments = set(child._sessions)
        return child

    monkeypatch.setattr(tfg, "_copy_env_server", attached_copy)
    with pytest.warns(RuntimeWarning, match="could not rejoin 1 open WebSocket"):
        await _rebase(provider)
    assert provider.last_rebase["sockets_lost"] == 1
    await asyncio.sleep(0.2)  # the proxy's close reaches the client
    assert env._ws.state is WSState.CLOSED
    with pytest.raises(SessionLostError):
        await roll(env)
    await env.close()
    assert adapter.live == {}


def test_public_rebase_returns_the_copys_url(adapter):
    provider = _provider(adapter, public=True)
    url = provider.start_container()
    first = provider.sandbox_id
    new_url = provider.rebase()
    copy = provider.sandbox_id
    assert new_url == provider.base_url != url
    assert copy.replace("_", "-") in new_url
    assert ("public", copy) in adapter.events and first in adapter.deleted
    provider.stop_container()
