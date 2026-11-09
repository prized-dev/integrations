# SPDX-License-Identifier: Apache-2.0

"""Live test of fork_group on Prized, with OpenEnv's coding_env.

Skipped unless PRIZED_LIVE=1, PRIZED_API_KEY is set and OpenEnv's coding_env
client is importable (``pip install "openenv-coding_env @
git+https://github.com/openenv/OpenEnv#subdirectory=envs/coding_env"``). It
starts coding_env from its Hugging Face Space, builds a short episode, forks
it with ``fork_group`` and checks what the helper promises: one fork call,
every copy on the parent's exact state, its own RNG and episode id, the
parent untouched, and every copy deleted. PRIZED_BASE_URL and PRIZED_EDGE_URL
point the SDK at a local proxy; FORK_N sets the big group's size (default 16).
``test_fork_base_live`` starts coding_env with ``fork_base`` (FORK_BASE:
``ready``, the default, ``install`` or ``none`` for a control run), forks
groups of 8 and rebases mid-episode with the client's socket open.
Prints one JSON line of measurements per check (run with ``-s``).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("PRIZED_LIVE") != "1" or not os.environ.get("PRIZED_API_KEY"),
    reason="live test: set PRIZED_LIVE=1 and PRIZED_API_KEY",
)

coding_env = pytest.importorskip("coding_env")

from openenv_prized import PrizedProvider, fork_group, forkable  # noqa: E402

N = int(os.environ.get("FORK_N", "16"))
PREFIX = [
    "import random\nrandom.seed(1234)\nfirst = random.random()",
    "rows = [(i, (i * 37) % 101) for i in range(500)]\ntotal = sum([b for _, b in rows])",
    "def summary():\n    return str(len(rows)) + ':' + str(total) + ':' + str(first)",
]
DIGEST = "print('state=' + summary())"
DRAW = "print('draw=' + str(random.random()))"


def emit(**record):
    print(json.dumps(record), flush=True)


def provider(**kw) -> PrizedProvider:
    return PrizedProvider(
        base_url=os.environ.get("PRIZED_BASE_URL"),
        edge_url=os.environ.get("PRIZED_EDGE_URL"),
        labels={"test": "fork-group-live"},
        vcpu=2,
        memory_gb=4,
        max_runtime_min=30,
        **kw,
    )


def out(result, key: str) -> str:
    found = re.search(key + r"=(\S+)", result.observation.stdout or "")
    assert found, (result.observation.stdout, result.observation.stderr)
    return found.group(1)


async def run(client, code: str):
    result = await client.step(coding_env.CodeAction(code=code))
    assert result.observation.exit_code == 0, result.observation.stderr
    return result


async def sandbox_exec(p: PrizedProvider, command: str) -> str:
    code, output = await asyncio.to_thread(p._adapter.exec, p._sandbox, command)
    return output.strip()


@pytest.mark.asyncio
async def test_fork_group_live():
    base = provider()
    env = await coding_env.CodingEnv.from_docker_image(
        "space:openenv/coding_env", provider=base
    )
    branches: list = []
    async with env:
        emit(
            check="parent",
            space_commit=await sandbox_exec(
                base, "git -C /home/prized/openenv/env log -1 --format=%H"
            ),
            sandbox=base.sandbox_id,
            # Does anything in the server draw from the `random` module?
            server_getrandbits=await sandbox_exec(
                base,
                "grep -rln 'random.getrandbits' /home/prized/openenv/env/.venv/lib/"
                "python3*/site-packages/websockets /home/prized/openenv/env/.venv/lib/"
                "python3*/site-packages/uvicorn 2>/dev/null | sed 's|.*site-packages/||'",
            ),
        )
        await forkable(env)
        await env.reset()
        for cell in PREFIX:
            await run(env, cell)
        parent_digest = out(await run(env, DIGEST), "state")
        parent = await env.state()
        k = parent.step_count

        # 1. A big group: one fork, every copy at the parent's state, own RNG.
        async with fork_group(env, N, seeds=500) as group:
            branches += group.branches
            assert None not in group.sandbox_ids
            states = [await c.state() for c in group]
            digests = await asyncio.gather(*(run(c, DIGEST) for c in group))
            draws = await asyncio.gather(*(run(c, DRAW) for c in group))
            # Does the guest kernel's RNG differ between copies after the fork?
            urandom = await asyncio.gather(
                *(
                    sandbox_exec(b.provider, "head -c 16 /dev/urandom | od -An -tx1")
                    for b in group.branches
                )
            )
            emit(
                check="group",
                n=N,
                stats=group.stats(),
                digests=sorted({out(d, "state") for d in digests}),
                draws=[out(d, "draw") for d in draws],
                distinct_urandom=len(set(urandom)),
            )
            assert len(group) == N
            assert {out(d, "state") for d in digests} == {parent_digest}
            # One reseed step on each copy, on top of the parent's k steps.
            assert {s.step_count for s in states} == {k + 1}
            assert len({out(d, "draw") for d in draws}) == N
            assert len(set(group.episode_ids)) == N
            assert {s.parent_episode_id for s in states} == {parent.episode_id}
            assert {s.server_episode_id for s in states} == {parent.episode_id}
            assert {b.reseed for b in group.branches} == {"reseeded"}

        # 2. Without reseed, every copy repeats the parent's next draw.
        async with fork_group(env, 4, reseed=False) as crn:
            branches += crn.branches
            draws = await asyncio.gather(*(run(c, DRAW) for c in crn))
            crn_draws = {out(d, "draw") for d in draws}
        parent_next = out(await run(env, DRAW), "draw")
        emit(check="crn", draws=sorted(crn_draws), parent_next=parent_next)
        assert crn_draws == {parent_next}

        # 3. The parent carried on from its own step, untouched by the forks.
        after = await env.state()
        assert after.step_count == k + 1 and after.episode_id == parent.episode_id
        assert out(await run(env, DIGEST), "state") == parent_digest

    # 4. Every copy is gone.
    deleted = [b.provider.sandbox_id is None for b in branches]
    emit(check="cleanup", copies=len(branches), deleted=sum(deleted))
    assert base.sandbox_id is None
    assert len(branches) == N + 4 and all(deleted)


@pytest.mark.asyncio
async def test_fork_group_live_mcp_echo_env():
    """An MCP client (echo_env) keeps its episode in its own HTTP session."""
    echo_env = pytest.importorskip("echo_env")
    base = provider()
    env = await echo_env.EchoEnv.from_docker_image(
        "space:openenv/echo_env", provider=base
    )
    async with env:
        await forkable(env)
        await env.reset()
        for word in ("a", "b", "c"):
            assert await env.call_tool("echo_message", message=word) == word
        parent = await env.state()
        async with fork_group(env, 4) as group:
            states = [await c.state() for c in group]
            echoes = [await c.call_tool("echo_message", message="x") for c in group]
            emit(
                check="mcp",
                stats=group.stats(),
                steps=[s.step_count for s in states],
                parent_step=parent.step_count,
            )
            assert {s.step_count for s in states} == {parent.step_count}
            assert {s.server_episode_id for s in states} == {parent.episode_id}
            assert echoes == ["x"] * 4
            assert {b.reseed for b in group.branches} == {"no-rng"}
            branches = list(group.branches)
        assert (await env.state()).step_count == parent.step_count
    assert base.sandbox_id is None
    assert all(b.provider.sandbox_id is None for b in branches)


@pytest.mark.asyncio
async def test_fork_base_live():
    """fork_base, fork_group(8) on the base, and a rebase mid-episode."""
    mode = os.environ.get("FORK_BASE", "ready")
    base = provider(fork_base=None if mode == "none" else mode)
    t = time.monotonic()
    env = await coding_env.CodingEnv.from_docker_image(
        "space:openenv/coding_env", provider=base
    )
    start_s = round(time.monotonic() - t, 3)
    branches: list = []
    async with env:
        info = await asyncio.to_thread(lambda: base._sandbox.refresh()._data)
        emit(
            check="start",
            fork_base=base.fork_base,
            start_s=start_s,
            timings=base.timings,
            rebase=base.last_rebase,
            sandbox=base.sandbox_id,
            labels=info.labels,
            # The copy's own max-runtime window (30 min asked for).
            runtime_left_min=round(
                (info.expires_at - info.created_at).total_seconds() / 60, 2
            ),
        )
        # The copy keeps the original's labels and gets its own runtime window.
        assert info.labels.get("test") == "fork-group-live"
        assert info.labels.get("openenv") == "server"
        assert info.expires_at - info.created_at >= timedelta(minutes=29.9)
        if mode != "none":
            assert base.last_rebase["to"] == base.sandbox_id
            assert base.last_rebase["from"] != base.sandbox_id
        await forkable(env)
        await env.reset()
        for cell in PREFIX:
            await run(env, cell)
        parent_digest = out(await run(env, DIGEST), "state")

        async def group_of_8(name: str, seeds: int) -> None:
            k = (await env.state()).step_count
            async with fork_group(env, 8, seeds=seeds) as group:
                branches.extend(group.branches)
                digests = await asyncio.gather(*(run(c, DIGEST) for c in group))
                states = [await c.state() for c in group]
                emit(check=name, stats=group.stats())
                assert {out(d, "state") for d in digests} == {parent_digest}
                assert {s.step_count for s in states} == {k + 2}  # reseed + digest
                assert group.stats()["fork_base"] == base.fork_base

        await group_of_8("group", 900)
        await group_of_8("group-again", 910)

        # Rebase mid-episode, with the client's socket open.
        await run(env, "x_mid = 41")
        socket, url, before = env._ws, env.base_url, base.sandbox_id
        step = (await env.state()).step_count
        t = time.monotonic()
        await asyncio.to_thread(base.rebase)
        rebase_s = round(time.monotonic() - t, 3)
        after = out(
            await run(env, "print('state=' + summary() + ':' + str(x_mid + 1))"),
            "state",
        )
        emit(
            check="rebase",
            wall_s=rebase_s,
            record=base.last_rebase,
            same_socket=env._ws is socket,
            same_url=env.base_url == url,
        )
        assert base.sandbox_id != before and env.base_url == url
        assert env._ws is socket
        assert after == parent_digest + ":42"
        assert (await env.state()).step_count == step + 1
        assert base.last_rebase["sockets"] == 1
        assert base.last_rebase["sockets_lost"] == 0
        await group_of_8("group-after-rebase", 920)

    deleted = [b.provider.sandbox_id is None for b in branches]
    emit(check="cleanup", copies=len(branches), deleted=sum(deleted))
    assert base.sandbox_id is None and all(deleted)
