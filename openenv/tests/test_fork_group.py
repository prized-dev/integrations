# SPDX-License-Identifier: Apache-2.0

"""Unit tests for fork_group: a fake Prized API, real OpenEnv servers.

Each "sandbox" runs a real OpenEnv ``HTTPEnvServer`` (openenv's own session,
``/ws?session_id=`` and WebSocket code) for a small dice environment, on
loopback. The fake Prized API forks a sandbox the way a microVM copy does at
the session level: the copy's server holds deep copies of the parent's
sessions (RNG state included) and its record of which sessions have a
WebSocket attached. A fake edge in front of every port requires
``Prized-Key`` and, like Prized's, admits a limited number of first opens in
flight; the rest get ``429`` with ``Retry-After: 1``.

The transplant reads private attributes of ``HTTPEnvServer`` (openenv is
pinned to 0.12.x); ``test_the_server_internals_the_fake_fork_copies`` fails
first if they move.
"""

from __future__ import annotations

import asyncio
import copy
import random
import re
import threading
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from typing import Optional

import pytest
import uvicorn
from fastapi import FastAPI
from openenv.core.client_types import StepResult
from openenv.core.containers.runtime.hf_sandbox_provider import _find_available_port
from openenv.core.env_client import EnvClient
from openenv.core.env_server.http_server import HTTPEnvServer
from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import Action, Observation, State

from openenv_prized import (
    PrizedProvider,
    fork_group,
    forkable,
    provider as prized_provider,
)
from openenv_prized import forking as fg

_KEY = "pz-test-key"


# ---------------------------------------------------------------------------
# A dice environment: an RNG that a copy carries over, reseedable by code
# ---------------------------------------------------------------------------
class DiceAction(Action):
    code: str


class DiceObservation(Observation):
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class DiceState(State):
    rolls: list = []


_SEED = re.compile(r"import random\nrandom\.seed\((-?\d+)\)\n")


class DiceEnvironment(Environment):
    """``roll`` draws from the env's RNG; the coding_env reseed line seeds it."""

    def __init__(self):
        super().__init__()
        self._rng = random.Random()
        self._state = DiceState(episode_id=None, step_count=0)

    def reset(self, seed: Optional[int] = None, episode_id: Optional[str] = None, **kw):
        self._rng = random.Random(seed)
        self._state = DiceState(
            episode_id=episode_id or str(uuid.uuid4()), step_count=0
        )
        return DiceObservation()

    def step(self, action: DiceAction, timeout_s=None, **kw):
        self._state.step_count += 1
        seed = _SEED.fullmatch(action.code)
        if seed:
            self._rng.seed(int(seed.group(1)))
            return DiceObservation()
        if action.code == "roll":
            value = self._rng.random()
            self._state.rolls = [*self._state.rolls, value]
            return DiceObservation(stdout=repr(value))
        return DiceObservation(stderr="NameError: boom", exit_code=1)

    @property
    def state(self) -> DiceState:
        return self._state


class DiceEnv(EnvClient[DiceAction, DiceObservation, DiceState]):
    def _step_payload(self, action: DiceAction) -> dict:
        return {"code": action.code}

    def _parse_result(self, payload: dict) -> StepResult[DiceObservation]:
        return StepResult(
            observation=DiceObservation(**payload["observation"]),
            reward=payload.get("reward"),
            done=bool(payload.get("done", False)),
        )

    def _parse_state(self, payload: dict) -> DiceState:
        return DiceState(**payload)


class CodingEnv(DiceEnv):
    """Named like OpenEnv's coding_env client, so the default reseeder applies."""


async def roll(client) -> float:
    return float((await client.step(DiceAction(code="roll"))).observation.stdout)


# ---------------------------------------------------------------------------
# The fake edge and the env servers behind it
# ---------------------------------------------------------------------------
class _Edge:
    """Shared by every port: first opens in flight are capped like Prized's."""

    def __init__(self, limit: int = 8, open_delay_s: float = 0.05):
        self.limit = limit
        self.open_delay_s = open_delay_s
        self.lock = threading.Lock()
        self.opened: set = set()
        self.inflight = 0
        self.max_inflight = 0
        self.refused = 0
        self.ws_before_open = 0


class _Gate:
    """ASGI middleware: Prized-Key required; a port's first open is admitted
    only under the edge's in-flight limit, otherwise 429 Retry-After: 1."""

    def __init__(self, app, edge: _Edge, name: str):
        self.app, self.edge, self.name = app, edge, name

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if dict(scope["headers"]).get(b"prized-key") != _KEY.encode():
            if scope["type"] == "websocket":
                await receive()
                await send({"type": "websocket.close", "code": 1008})
            else:
                await _json(send, 401, b'{"error":"unauthorized"}')
            return
        edge = self.edge
        if self.name not in edge.opened:
            if scope["type"] == "websocket":
                edge.ws_before_open += 1
            else:
                with edge.lock:
                    refuse = edge.inflight >= edge.limit
                    if refuse:
                        edge.refused += 1
                    else:
                        edge.inflight += 1
                        edge.max_inflight = max(edge.max_inflight, edge.inflight)
                if refuse:
                    await _json(
                        send, 429, b'{"error":"rate_limited"}', retry_after=b"1"
                    )
                    return
                try:
                    await asyncio.sleep(edge.open_delay_s)
                finally:
                    with edge.lock:
                        edge.inflight -= 1
                        edge.opened.add(self.name)
        await self.app(scope, receive, send)


async def _json(send, status: int, body: bytes, retry_after: Optional[bytes] = None):
    headers = [(b"content-type", b"application/json")]
    if retry_after:
        headers.append((b"retry-after", retry_after))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _new_env_server() -> HTTPEnvServer:
    return HTTPEnvServer(
        DiceEnvironment, DiceAction, DiceObservation, state_cls=DiceState
    )


def _copy_env_server(parent: HTTPEnvServer) -> HTTPEnvServer:
    """What a microVM copy holds: the parent's sessions, RNGs and attachments."""
    child = _new_env_server()
    for sid, env in parent._sessions.items():
        if env is None:
            continue
        child._sessions[sid] = copy.deepcopy(env)
        child._session_executors[sid] = ThreadPoolExecutor(max_workers=1)
        child._session_stacks[sid] = AsyncExitStack()
        if sid in parent._session_info:
            child._session_info[sid] = copy.copy(parent._session_info[sid])
        if sid in parent._session_modes:
            child._session_modes[sid] = parent._session_modes[sid]
    # A socket attached at fork time stays attached in the copy, forever.
    child._session_websocket_attachments = set(parent._session_websocket_attachments)
    return child


class _EnvServer:
    def __init__(self, edge: _Edge, env_server: Optional[HTTPEnvServer] = None):
        self.env_server = env_server or _new_env_server()
        app = FastAPI()
        self.env_server.register_routes(app)
        self.port = _find_available_port()
        config = uvicorn.Config(
            _Gate(app, edge, f"port-{self.port}"),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        while not self._server.started:
            time.sleep(0.01)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self._server.should_exit = True
        self._server.force_exit = True
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# The fake Prized API (duck-typed to the provider's adapter)
# ---------------------------------------------------------------------------
class _Sandbox:
    def __init__(self, server: _EnvServer):
        self.id = f"sbx_{uuid.uuid4().hex[:16]}"
        self.server = server


class _ForkingAdapter:
    def __init__(self, edge: _Edge):
        self.edge = edge
        self.live: dict = {}
        self.forks: list = []
        self.deleted: list = []
        self.fail_delete = 0

    def create_sandbox(self, **kwargs):
        sandbox = _Sandbox(_EnvServer(self.edge))
        self.live[sandbox.id] = sandbox
        return sandbox

    def exec(self, sandbox, command, *, timeout=60, env=None, background=False):
        if "OPENENV_ROOT=" in command:
            return 0, "OPENENV_ROOT=/home/prized/openenv/env\n"
        if command.startswith("cat ") and "openenv.yaml" in command:
            return 0, "spec_version: 1\nname: dice\napp: server.app:app\nport: 8000\n"
        if "kill -0" in command:
            return 0, "RUNNING\n"
        return 0, ""

    def write(self, sandbox, path, data, *, mode):
        pass

    def port_url(self, sandbox, port):
        return sandbox.server.url

    def port_headers(self, sandbox):
        return {"Prized-Key": _KEY}

    def fork(self, sandbox, *, count, labels):
        parent = sandbox.server.env_server
        self.forks.append(
            {
                "parent": sandbox.id,
                "count": count,
                "labels": labels,
                "attached": set(parent._session_websocket_attachments),
            }
        )
        copies = [
            _Sandbox(_EnvServer(self.edge, _copy_env_server(parent)))
            for _ in range(count)
        ]
        for c in copies:
            self.live[c.id] = c
        return copies

    def sandbox_id(self, sandbox):
        return sandbox.id

    def delete(self, sandbox):
        if self.fail_delete:
            self.fail_delete -= 1
            raise RuntimeError("delete failed")
        sandbox.server.stop()
        self.live.pop(sandbox.id, None)
        self.deleted.append(sandbox.id)

    def pause(self, sandbox):
        pass

    def resume(self, sandbox):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fast_waits(monkeypatch):
    """Record the helper's waits and keep them short; no provider poll sleep."""
    waits: list = []

    async def sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(min(seconds, 0.02))

    monkeypatch.setattr(fg, "_sleep", sleep)
    monkeypatch.setattr(prized_provider, "_sleep", lambda seconds: None)
    fg._warned.clear()
    return waits


@pytest.fixture()
def edge():
    return _Edge()


@pytest.fixture()
def adapter(edge):
    adapter = _ForkingAdapter(edge)
    yield adapter
    for sandbox in list(adapter.live.values()):
        sandbox.server.stop()


async def _parent(adapter, cls=DiceEnv, *, prefix=3, seed=1, make_forkable=True):
    """A live parent episode: reset(seed) and `prefix` rolls."""
    provider = PrizedProvider(_adapter=adapter, image="space:test/dice")
    url = await asyncio.to_thread(provider.start_container)
    await asyncio.to_thread(provider.wait_for_ready, url, 10)
    env = cls(base_url=url, provider=provider)
    await env.connect()
    if make_forkable:
        await forkable(env)
    await env.reset(seed=seed)
    rolls = [await roll(env) for _ in range(prefix)]
    return env, rolls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_the_server_internals_the_fake_fork_copies():
    server = _new_env_server()
    for name in (
        "_sessions",
        "_session_executors",
        "_session_stacks",
        "_session_info",
        "_session_modes",
        "_session_websocket_attachments",
    ):
        assert hasattr(server, name), name


@pytest.mark.asyncio
async def test_one_fork_call_gives_n_clients_on_the_parents_episode(adapter):
    env, rolls = await _parent(adapter)
    async with fork_group(env, 4, reseed=False, labels={"run": "t"}) as group:
        assert len(adapter.forks) == 1
        call = adapter.forks[0]
        assert call["count"] == 4
        assert call["labels"]["fork_group"] == group.group_id
        assert call["labels"]["run"] == "t"
        # The parent's socket was released before the copy was taken.
        assert call["attached"] == set()
        assert len(group) == 4 and len({c.base_url for c in group}) == 4
        for client in group:
            state = await client.state()
            assert state.rolls == rolls and state.step_count == 3
        assert group.branch_step == 3
        sandboxes = group.sandbox_ids
    assert sorted(adapter.deleted) == sorted(sandboxes)
    # The parent carries on from its own step 3.
    await roll(env)
    assert (await env.state()).step_count == 4
    await env.close()


@pytest.mark.asyncio
async def test_without_reseed_every_copy_repeats_the_parents_next_draw(adapter):
    env, _ = await _parent(adapter)
    async with fork_group(env, 3, reseed=False) as group:
        draws = [await roll(client) for client in group]
        assert {b.reseed for b in group.branches} == {"off"}
    parent_next = await roll(env)
    assert draws == [parent_next] * 3
    await env.close()


@pytest.mark.asyncio
async def test_reseed_gives_each_copy_its_own_reproducible_draws(adapter):
    env, _ = await _parent(adapter, cls=CodingEnv)  # the default reseeder applies
    async with fork_group(env, 3, seeds=100) as group:
        draws = [await roll(client) for client in group]
        assert [b.seed for b in group.branches] == [100, 101, 102]
        assert {b.reseed for b in group.branches} == {"reseeded"}
        # The reseed is one env step on each copy.
        assert [(await c.state()).step_count for c in group] == [5, 5, 5]
    assert draws == [random.Random(100 + i).random() for i in range(3)]
    await env.close()


@pytest.mark.asyncio
async def test_relabel_gives_each_copy_its_own_episode_id(adapter):
    env, _ = await _parent(adapter)
    parent_id = (await env.state()).episode_id
    async with fork_group(env, 3, reseed=False) as group:
        states = [await c.state() for c in group]
        ids = [s.episode_id for s in states]
        assert ids == group.episode_ids
        assert ids == [f"{parent_id}/{group.group_id}-b{i}" for i in range(3)]
        assert [s.branch_index for s in states] == [0, 1, 2]
        assert {s.parent_episode_id for s in states} == {parent_id}
        assert {s.server_episode_id for s in states} == {parent_id}
        assert {s.branch_step for s in states} == {3}
    async with fork_group(env, 2, reseed=False, relabel=False) as group:
        assert {(await c.state()).episode_id for c in group} == {parent_id}
    await env.close()


@pytest.mark.asyncio
async def test_reseed_entries_report_what_happened(adapter):
    env, _ = await _parent(adapter)
    with pytest.warns(RuntimeWarning, match="no reseeder is registered for DiceEnv"):
        async with fork_group(env, 2) as group:
            assert {b.reseed for b in group.branches} == {"unknown"}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with fork_group(env, 1) as group:
            assert group.branches[0].reseed == "unknown"
    assert not [w for w in caught if "no reseeder" in str(w.message)]  # once per env

    calls = []

    async def mine(client, seed):
        calls.append(seed)

    async with fork_group(env, 2, reseed=mine, seeds=[7, 9]) as group:
        assert {b.reseed for b in group.branches} == {"reseeded"}
    assert sorted(calls) == [7, 9]

    try:
        fg.register_reseeder(DiceEnv, fg.NO_RNG)
        async with fork_group(env, 1) as group:
            assert group.branches[0].reseed == "no-rng"
        fg.register_reseeder("DiceEnv", fg.unsupported("no way in"))
        async with fork_group(env, 1) as group:
            assert group.branches[0].reseed == "unsupported"
    finally:
        fg.RESEEDERS.pop("DiceEnv", None)
    await env.close()


@pytest.mark.asyncio
async def test_opens_stay_under_the_edges_limit(adapter, edge):
    edge.limit = 3
    edge.open_delay_s = 0.2
    env, _ = await _parent(adapter)
    async with fork_group(env, 8, reseed=False, max_concurrent_opens=3) as group:
        assert group.rate_limited == 0
    assert edge.refused == 0 and edge.max_inflight <= 3
    assert edge.ws_before_open == 0
    # Control: without the cap the same edge refuses some opens.
    async with fork_group(env, 8, reseed=False, max_concurrent_opens=16) as group:
        assert group.rate_limited == edge.refused > 0
    await env.close()


@pytest.mark.asyncio
async def test_rate_limited_opens_back_off_as_the_edge_asks(adapter, edge, _fast_waits):
    edge.limit = 2
    edge.open_delay_s = 0.2
    env, _ = await _parent(adapter)
    async with fork_group(env, 6, reseed=False) as group:
        assert group.rate_limited == edge.refused > 0
        for client in group:
            assert (await client.state()).step_count == 3
    # Every wait after a 429 honoured Retry-After: 1.
    assert len([w for w in _fast_waits if w >= 1.0]) >= group.rate_limited
    await env.close()


@pytest.mark.asyncio
async def test_a_failing_copy_deletes_every_copy_and_spares_the_parent(adapter):
    env, _ = await _parent(adapter)

    async def flaky(client, seed):
        if seed == 2:
            raise RuntimeError("reseed exploded")

    with pytest.raises(RuntimeError, match="reseed exploded"):
        await fork_group(env, 4, reseed=flaky, seeds=0)
    assert len(adapter.deleted) == 4
    assert (await env.state()).step_count == 3
    await env.close()


@pytest.mark.asyncio
async def test_the_python_reseeder_runs_one_seed_line_and_checks_it():
    class Probe(CodingEnv):
        sent: list = []
        exit_code = 0

        async def step(self, action):
            self.sent.append(action)
            return StepResult(
                observation=DiceObservation(
                    stderr="NameError", exit_code=self.exit_code
                )
            )

    probe = Probe(base_url="http://127.0.0.1:9")
    await fg.reseed_python_random(probe, 42)
    assert isinstance(probe.sent[0], DiceAction)
    assert probe.sent[0].code == "import random\nrandom.seed(42)\n"
    probe.exit_code = 1
    with pytest.raises(RuntimeError, match="reseed step failed"):
        await fg.reseed_python_random(probe, 1)


@pytest.mark.asyncio
async def test_an_episode_on_the_clients_own_socket_is_refused(adapter):
    env, _ = await _parent(adapter, make_forkable=False)
    with pytest.raises(ValueError, match="forkable"):
        await fork_group(env, 2)
    with pytest.raises(RuntimeError, match="before reset"):
        await forkable(env)
    assert adapter.forks == []
    await env.close()


@pytest.mark.asyncio
async def test_forkable_frees_the_sockets_session_first(adapter):
    # The dice server allows one session, like most OpenEnv environments; the
    # connected client already holds it.
    env, _ = await _parent(adapter, prefix=0, make_forkable=False)
    env2 = DiceEnv(base_url=env.base_url)
    await env.disconnect()
    await env2.connect()
    await forkable(env2)
    assert fg.session_id_of(env2)
    assert await forkable(env2) is env2  # idempotent
    await env2.reset(seed=3)
    await roll(env2)
    await env2.close()
    await env.close()


@pytest.mark.asyncio
async def test_group_can_be_awaited_and_closed_twice(adapter):
    env, _ = await _parent(adapter)
    group = await fork_group(env, 2, reseed=False)
    assert group.timings.keys() >= {"detach_s", "fork_s", "open_s", "total_s"}
    stats = group.stats()
    assert stats["n"] == 2 and len(stats["branches"]) == 2
    await group.aclose()
    await group.aclose()
    assert len(adapter.deleted) == 2
    async with await fork_group(env, 1, reseed=False) as group:
        assert len(group) == 1
    with pytest.raises(RuntimeError, match="already opened"):
        await group._open()
    await env.close()


@pytest.mark.asyncio
async def test_a_failed_delete_is_retried_then_reported(adapter):
    env, _ = await _parent(adapter)
    adapter.fail_delete = 1
    async with fork_group(env, 2, reseed=False):
        pass
    assert len(adapter.deleted) == 2
    adapter.fail_delete = 10
    group = await fork_group(env, 1, reseed=False)
    with pytest.raises(RuntimeError, match=f"fork_group={group.group_id}"):
        await group.aclose()
    adapter.fail_delete = 0
    await group.aclose()  # a second aclose retries
    assert len(adapter.deleted) == 3
    await env.close()


@pytest.mark.asyncio
async def test_release_waits_for_a_socket_still_attached(adapter, _fast_waits):
    env, _ = await _parent(adapter)
    other = DiceEnv(base_url=env.base_url)
    other._ws_url = env._ws_url
    await env.disconnect()
    await other.state()  # another socket holds the session now

    async def let_go():
        await asyncio.sleep(0.3)
        await other.disconnect()

    release = asyncio.ensure_future(let_go())
    state = await fg._release_session(env, 10)
    await release
    assert state["step_count"] == 3
    assert 0.05 in _fast_waits  # it retried while the other socket was attached
    await env.close()


def test_arguments_are_checked(adapter):
    env = DiceEnv(base_url="http://127.0.0.1:9")
    with pytest.raises(TypeError, match="PrizedProvider"):
        fork_group(env, 2)
    provider = PrizedProvider(_adapter=adapter)
    for n in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            fork_group(env, n, provider=provider)
    with pytest.raises(ValueError, match="seeds has 1"):
        fork_group(env, 2, provider=provider, seeds=[1])
    assert fork_group(env, 2, provider=provider, seeds=5)._seeds == [5, 6]
    assert fork_group(env, 2, provider=provider, reseed=False)._seeds == [None, None]


def test_the_coding_env_reseeder_is_registered():
    assert fg.RESEEDERS["CodingEnv"] is fg.reseed_python_random
    assert fg.RESEEDERS["EchoEnv"] is fg.NO_RNG
    assert fg.RESEEDERS["BrowserGymEnv"].status == "unsupported"


def test_session_urls():
    url = fg._with_session("ws://127.0.0.1:1/ws", "abc")
    assert url == "ws://127.0.0.1:1/ws?session_id=abc"
    assert fg._with_session(url, "def").endswith("?session_id=def")

    class Client:
        _ws_url = url

    assert fg.session_id_of(Client()) == "abc"
    assert fg._http_url("wss://h.example/x/") == "https://h.example/x"
    assert fg._action_class(DiceEnv(base_url="http://127.0.0.1:9")) is DiceAction


@pytest.mark.asyncio
async def test_cancelling_during_the_fork_call_deletes_its_copies(adapter):
    env, _ = await _parent(adapter)
    entered, release = threading.Event(), threading.Event()
    fork = adapter.fork

    def slow_fork(sandbox, **kwargs):
        entered.set()
        release.wait(5)
        return fork(sandbox, **kwargs)

    adapter.fork = slow_fork
    task = asyncio.ensure_future(fork_group(env, 3, reseed=False)._open())
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()  # the API call returns copies after the caller gave up
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(adapter.forks) == 1 and len(adapter.deleted) == 3
    await env.close()


@pytest.mark.asyncio
async def test_copies_keep_the_parents_client_options(adapter):
    env, _ = await _parent(adapter)
    options = DiceEnv(
        base_url=env.base_url,
        message_timeout_s=7.0,
        websocket_ping_interval_s=None,
    )
    options._ws_url = env._ws_url
    options._provider = env._provider
    await env.disconnect()
    async with fork_group(options, 1, reseed=False) as group:
        copy = group[0]
        assert copy._message_timeout == 7.0
        assert copy._websocket_ping_interval_s is None
        assert copy._websocket_ping_timeout_s == 20.0
        assert copy._provider is None  # the group, not the client, deletes the copy
    await env.close()


class DictStateEnv(DiceEnv):
    """A client whose state() is a dict, like OpenEnv's GenericEnvClient."""

    def _parse_state(self, payload: dict) -> dict:
        return dict(payload)


@pytest.mark.asyncio
async def test_a_client_with_dict_state_is_checked_and_relabelled(adapter):
    env, _ = await _parent(adapter, cls=DictStateEnv)
    async with fork_group(env, 2, reseed=False) as group:
        states = [await c.state() for c in group]
        assert [s["step_count"] for s in states] == [3, 3]
        assert [s["episode_id"] for s in states] == group.episode_ids
        assert {s["server_episode_id"] for s in states} == {group.parent_episode_id}
    await env.close()


@pytest.mark.asyncio
async def test_groups_from_one_parent_take_turns(adapter):
    env, _ = await _parent(adapter)
    first, second = fork_group(env, 2, reseed=False), fork_group(env, 2, reseed=False)
    await asyncio.gather(first._open(), second._open())
    # Neither fork caught the other's probe attached to the parent's session.
    assert [call["attached"] for call in adapter.forks] == [set(), set()]
    for group in (first, second):
        assert [(await c.state()).step_count for c in group] == [3, 3]
        await group.aclose()
    await env.close()


@pytest.mark.asyncio
async def test_a_failed_fork_call_leaves_the_parent_working(adapter):
    env, _ = await _parent(adapter)

    def broken(sandbox, **kwargs):
        raise RuntimeError("fork refused")

    adapter.fork = broken
    with pytest.raises(RuntimeError, match="fork refused"):
        await fork_group(env, 2)
    assert adapter.deleted == []
    assert (await env.state()).step_count == 3
    await env.close()


@pytest.mark.asyncio
async def test_ports_the_edge_refuses_fail_fast(monkeypatch):
    import requests

    calls = []

    class Answer:
        def __init__(self, status):
            self.status_code, self.text, self.headers = status, "no", {}

    def get(url, **kwargs):
        calls.append(url)
        return Answer(403 if len(calls) == 1 else 404)

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(RuntimeError, match="403"):
        await fg._wait_ready("http://127.0.0.1:9", time.monotonic() + 60)
    calls.clear()
    calls.append("skip the 403")
    with pytest.raises(RuntimeError, match="404"):
        await fg._wait_ready("http://127.0.0.1:9", time.monotonic() + 60)
    assert len(calls) == 3  # two 404s, then it gives up


class _StubMCP:
    """Shaped like MCPToolClient: its own HTTP session, which a failed connect
    forgets."""

    use_production_mode = True

    def __init__(self, base_url=None, **kwargs):
        self._production_session_id = None
        self._ws_url = f"{base_url}/ws"
        self.seen = []

    async def connect(self):
        self.seen.append(self._production_session_id)
        if len(self.seen) == 1:
            self._production_session_id = None
            raise ConnectionError("the edge said no")


@pytest.mark.asyncio
async def test_mcp_clients_rejoin_their_own_http_session():
    parent = _StubMCP("http://127.0.0.1:9")
    assert fg.session_id_of(parent) is None
    parent._production_session_id = "s1"
    assert fg.session_id_of(parent) == "s1"
    assert await forkable(parent) is parent  # nothing to move
    client = await fg._attach(
        parent, "http://127.0.0.1:10", "s1", time.monotonic() + 60
    )
    assert client.seen == ["s1", "s1"]  # the retry rejoined the same session
    assert client._ws_url == "http://127.0.0.1:10/ws"  # no query: MCP adds it
