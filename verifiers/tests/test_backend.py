"""The backend contract, as verifiers' own tests/unit/test_runtimes.py states it, run against
PrizedBackend on the fake Prized; plus the parts that are Prized's own: building the environment
once, forking it per rollout, and leaving nothing behind."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from collections.abc import AsyncIterator
from typing import Any

import fake_prized
import pytest
from fake_prized import Cloud
from prized import PrizedError
from pydantic import ValidationError
from verifiers.v1.errors import SandboxError
from verifiers.v1.runtimes import Runtime, load_config, make_backend, provision_runtime

from verifiers_prized import PrizedBackend, PrizedConfig, pool
from verifiers_prized.config import DEFAULT_WORKDIR

PYTHON = ["sh", "-c", 'exec "$(command -v python3 || command -v python)" "$@"', "py"]


def config(**fields: Any) -> PrizedConfig:
    loaded = load_config({"type": "custom", "import_path": "verifiers_prized.PrizedBackend", **fields})
    assert isinstance(loaded, PrizedConfig)
    return loaded


@contextlib.asynccontextmanager
async def provisioned(**fields: Any) -> AsyncIterator[Runtime]:
    async with asyncio.timeout(30), provision_runtime(config(**fields)) as view:
        yield view


async def wait_for(predicate: Any, timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.05)


# --- the contract (verifiers' test_runtimes.py, test for test) -----------------------------------


async def test_run(cloud: Cloud) -> None:
    async with provisioned() as view:
        result = await view.run(["sh", "-c", 'echo "$GREETING"; exit 3'], {"GREETING": "hi"})
        assert result.exit_code == 3, result
        assert result.stdout.strip() == "hi", result
        stdin = await view.run(["cat"], {})
        assert stdin.exit_code == 0 and stdin.stdout == "", "run must close stdin"


async def test_exec(cloud: Cloud) -> None:
    async with provisioned() as view:
        process = await view.exec(["sh", "-c", 'read line; echo "got $line"; echo oops >&2; exit 4'], {})
        await process.write(b"ping\n")
        stdout = b"".join([chunk async for chunk in process.stdout])
        stderr = b"".join([chunk async for chunk in process.stderr])
        assert await process.wait() == 4
        assert stdout.strip() == b"got ping", stdout
        assert stderr.strip() == b"oops", stderr
        assert await process.poll() == 4
        sleeper = await view.exec(["sleep", "600"], {})
        assert await sleeper.poll() is None
        await sleeper.kill()
        async with asyncio.timeout(30):
            assert await sleeper.wait() != 0


async def test_exec_keeps_stdin_open_between_writes(cloud: Cloud) -> None:
    async with provisioned() as view:
        process = await view.exec(
            ["sh", "-c", 'while read line; do echo "got $line"; [ "$line" = bye ] && exit 0; done; exit 9'],
            {},
        )
        first = process.stdout.__anext__()
        await process.write(b"one\n")
        assert (await first).strip() == b"got one"
        await process.write(b"two\nbye\n")
        rest = b"".join([chunk async for chunk in process.stdout])
        assert rest.split() == [b"got", b"two", b"got", b"bye"]
        assert await process.wait() == 0
        with pytest.raises(SandboxError, match="exited"):
            await process.write(b"late\n")


async def test_exec_stdin_carries_large_and_binary_writes(cloud: Cloud) -> None:
    async with provisioned() as view:
        data = bytes(range(256)) * 1024  # 256 KiB, every byte value: several chunks
        process = await view.exec(["sh", "-c", f"head -c {len(data)} | od -An -v -tx1 | tr -d ' \\n'"], {})
        await process.write(data)
        out = b"".join([chunk async for chunk in process.stdout])
        assert await process.wait() == 0
        assert out.decode() == data.hex()


async def test_run_background(cloud: Cloud) -> None:
    async with provisioned() as view:
        await view.run_background(["sh", "-c", "echo up; sleep 600"], {}, "bg.log")

        async def started() -> bool:
            with contextlib.suppress(SandboxError, OSError):
                return (await view.read("bg.log")).strip() == b"up"
            return False

        await wait_for(started)


async def test_files(cloud: Cloud) -> None:
    async with provisioned() as view:
        data = bytes(range(256)) * 64
        await view.write("vf-check/nested/data.bin", data)
        assert await view.read("vf-check/nested/data.bin") == data
        assert await view.read("vf-check/nested/data.bin", max_bytes=len(data)) == data
        with pytest.raises(SandboxError):
            await view.read("vf-check/nested/data.bin", max_bytes=10)
        listed = await view.run(["sh", "-c", "wc -c < vf-check/nested/data.bin"], {})
        assert listed.stdout.strip() == str(len(data)), "write must land in the workdir"
        with pytest.raises(SandboxError):
            await view.read("no/such/file")


async def test_files_outside_home_go_through_a_command(cloud: Cloud, tmp_path: Any) -> None:
    # Uploads reach /home/prized only; anything else (here a host temp dir, which the fake leaves
    # as it is) is staged in the home and moved into place.
    target = f"/tmp/vfp-test-{uuid.uuid4().hex}/deep/file.bin"
    try:
        async with provisioned() as view:
            await view.write(target, b"\x00\xffoutside")
            assert await view.read(target) == b"\x00\xffoutside"
            assert await view.read(target, max_bytes=9) == b"\x00\xffoutside"
            with pytest.raises(SandboxError, match="byte limit"):
                await view.read(target, max_bytes=3)
            staged = await view.run(["sh", "-c", "ls -A /home/prized/.cache/verifiers-prized | wc -l"], {})
            assert staged.stdout.strip() == "0", "the staged copy must be moved, not left"
    finally:
        shutil.rmtree(target.split("/deep/")[0], ignore_errors=True)


def test_declares_only_what_prized_has() -> None:
    assert PrizedBackend.config_class is PrizedConfig
    assert not PrizedBackend.SUPPORTS_TASK_IMAGES
    assert not PrizedBackend.SUPPORTS_USER_SWITCHING
    assert not PrizedBackend.SUPPORTS_PORT_EXPOSURE
    assert not PrizedBackend.SUPPORTS_COMPOSE
    assert PrizedBackend.is_local is False


async def test_user_switching_is_refused(cloud: Cloud) -> None:
    async with provisioned() as view:
        with pytest.raises(ValueError, match="the custom runtime cannot run as another user"):
            view.with_user("nobody")


async def test_expose_is_refused(cloud: Cloud) -> None:
    async with provisioned() as view:
        with pytest.raises(SandboxError, match="cannot expose a port"):
            await view.backend.expose(8000)


# --- config ---------------------------------------------------------------------------------------


def test_config_validates_its_fields() -> None:
    assert config().resolved_workdir == DEFAULT_WORKDIR
    with pytest.raises(ValidationError, match="setpu"):
        config(setpu=["x"])
    with pytest.raises(ValidationError, match="whole number"):
        config(cpu=1.5)
    with pytest.raises(ValidationError, match="disk"):
        config(disk=5)
    with pytest.raises(ValidationError, match="set by verifiers-prized"):
        config(labels={"vfp-run": "x"})
    with pytest.raises(ValidationError, match="needs fork = true"):
        config(parent="sbx_1", fork=False)
    with pytest.raises(ValidationError, match="absolute"):
        config(workdir="relative")


def test_task_resources_are_clamped_not_refused() -> None:
    # verifiers applies a task's resources with model_copy, which skips validation.
    creds = pool.Credentials("k")
    task = config().model_copy(update={"cpu": 0.5, "memory": 0.25, "disk": 5})
    recipe = pool.Recipe.of(task, creds)
    assert (recipe.vcpu, recipe.memory_mb, recipe.disk_gb) == (1, 1024, 10)
    big = pool.Recipe.of(config().model_copy(update={"cpu": 64, "memory": 512, "disk": 1000}), creds)
    assert (big.vcpu, big.memory_mb, big.disk_gb) == (32, 128 * 1024, 128)


async def test_missing_api_key_names_the_variable(cloud: Cloud, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_PRIZED_KEY", raising=False)
    with pytest.raises(SandboxError, match=r"\$MY_PRIZED_KEY"):
        async with provisioned(api_key_env="MY_PRIZED_KEY"):
            pass
    assert cloud.calls == []


async def test_api_key_comes_from_the_named_variable(cloud: Cloud, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PRIZED_KEY", "dcp_1111")
    async with provisioned(api_key_env="MY_PRIZED_KEY") as view:
        assert cloud.boxes[view.info.id].api_key == "dcp_1111"


# --- the environment: built once, forked per rollout ----------------------------------------------


SETUP = [
    "echo built > env.txt",
    "mkdir -p ~/.venv/bin && printf '#!/bin/sh\\necho tool-ok\\n' > ~/.venv/bin/tool",
    "chmod +x ~/.venv/bin/tool",
    "echo $((1 + $(cat ~/builds 2>/dev/null || echo 0))) > ~/builds",
]


async def test_setup_runs_once_and_every_rollout_gets_a_copy(cloud: Cloud) -> None:
    cfg = config(setup=SETUP, labels={"team": "rl"})
    seen: list[str] = []

    async def rollout(i: int) -> None:
        async with provision_runtime(cfg) as view:
            assert (await view.read("env.txt")).strip() == b"built"
            assert (await view.run(["sh", "-c", "~/.venv/bin/tool"], {})).stdout.strip() == "tool-ok"
            assert (await view.read("/home/prized/builds")).strip() == b"1"
            # Each copy is its own: a write in one is not seen by another.
            await view.write("mine.txt", str(i).encode())
            assert (await view.read("mine.txt")).decode() == str(i)
            seen.append(view.info.id)

    await asyncio.gather(*(rollout(i) for i in range(6)))
    parents = cloud.live("parent")
    assert len(parents) == 1 and parents[0].state == "paused"
    assert cloud.count("create") == 1, "setup must run once"
    assert cloud.count("fork") == 1, "rollouts that start together are forked together"
    assert len(set(seen)) == 6 and all(cloud.boxes[s].parent == parents[0].id for s in seen)
    assert all(cloud.boxes[s].state == "deleted" for s in seen), "each copy is deleted at teardown"
    assert cloud.wakes == [], "nothing may run in the paused parent"
    assert parents[0].labels == {"team": "rl", "verifiers-prized": "parent", "vfp-run": pool.RUN_ID}
    assert {cloud.boxes[s].labels["verifiers-prized"] for s in seen} == {"rollout"}
    assert cloud.boxes[seen[0]].labels["team"] == "rl"
    assert parents[0].shape == {"vcpu": 1, "memory_mb": 2048, "disk_gb": 10, "max_runtime_min": None}
    assert list(pool._tracked) == [parents[0].id], "only the parent stays, until exit"


async def test_later_rollouts_reuse_the_parent(cloud: Cloud) -> None:
    cfg = config(setup=SETUP)
    for _ in range(3):
        async with provision_runtime(cfg) as view:
            assert (await view.read("env.txt")).strip() == b"built"
    assert cloud.count("create") == 1 and cloud.count("fork") == 3


async def test_shape_and_recipe_choose_the_parent(cloud: Cloud) -> None:
    async with provisioned(setup=SETUP):
        pass
    async with provisioned(setup=SETUP, workdir="/home/prized/elsewhere") as view:
        assert view.backend.workdir == "/home/prized/elsewhere"
        assert (await view.run(["pwd"], {})).stdout.strip().endswith("/home/prized/elsewhere")
    assert cloud.count("create") == 1, "a task's workdir does not need its own parent"
    async with provisioned(setup=SETUP, cpu=2):
        pass
    assert cloud.count("create") == 2, "copies keep their parent's shape: another shape, another parent"
    async with provisioned(setup=[*SETUP, "true"]):
        pass
    assert cloud.count("create") == 3


async def test_workdir_that_cannot_be_made_fails_the_rollout(cloud: Cloud) -> None:
    with pytest.raises(SandboxError, match="cannot use workdir.*without root"):
        async with provisioned(setup=["touch /home/prized/afile"], workdir="/home/prized/afile/sub"):
            pass
    assert [b.id for b in cloud.live()] == [cloud.live("parent")[0].id], "the copy is deleted"


async def test_env_reaches_commands_and_setup_env_reaches_only_setup(
    cloud: Cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    cfg = config(setup=['printf %s "$HF_TOKEN" > token.txt'], setup_env=["HF_TOKEN"])
    async with provision_runtime(cfg, env={"TASK_VAR": "t"}) as view:
        assert (await view.read("token.txt")) == b"hf_secret"
        result = await view.run(
            ["sh", "-c", 'echo "$TASK_VAR/$CALL_VAR/${HF_TOKEN:-unset}"'], {"CALL_VAR": "c"}
        )
        assert result.stdout.strip() == "t/c/unset"


async def test_setup_env_that_is_not_set_fails_before_anything_is_made(cloud: Cloud) -> None:
    with pytest.raises(SandboxError, match="NOT_SET_ANYWHERE"):
        async with provisioned(setup=["true"], setup_env=["NOT_SET_ANYWHERE"]):
            pass
    assert cloud.count("create") == 0


async def test_failed_setup_fails_fast_and_leaves_nothing(cloud: Cloud) -> None:
    cfg = config(setup=["echo ok", "echo broken >&2; exit 7"])
    with pytest.raises(SandboxError, match=r"setup command 2 exited 7(.|\n)*broken"):
        async with provision_runtime(cfg):
            pass
    assert cloud.live() == [] and pool._tracked == {}
    with pytest.raises(SandboxError, match="setup command 2"):
        async with provision_runtime(cfg):
            pass
    assert cloud.count("create") == 1, "a failed setup is not run again for every rollout"


async def test_setup_timeout(cloud: Cloud) -> None:
    with pytest.raises(SandboxError, match="setup_timeout"):
        async with provisioned(setup=["sleep 5"], setup_timeout=0.2):
            pass
    assert cloud.live() == []


async def test_unpaused_parent_keeps_running(cloud: Cloud) -> None:
    async with provisioned(setup=SETUP, pause_parent=False):
        pass
    assert cloud.live("parent")[0].state == "running"
    assert cloud.count("pause") == 0


async def test_fork_false_gives_each_rollout_its_own_sandbox(cloud: Cloud) -> None:
    cfg = config(setup=SETUP, fork=False)
    ids = []
    for _ in range(2):
        async with provision_runtime(cfg) as view:
            assert (await view.read("env.txt")).strip() == b"built"
            ids.append(view.info.id)
    assert cloud.count("create") == 2 and cloud.count("fork") == 0 and cloud.count("pause") == 0
    assert cloud.live() == [] and pool._tracked == {}
    assert {cloud.boxes[i].labels["verifiers-prized"] for i in ids} == {"rollout"}


async def test_your_own_parent_is_forked_and_never_touched(cloud: Cloud) -> None:
    mine = cloud.new_box("prized-base-v1", {}, {"owner": "me"}, "k")
    (mine.root / "home/prized/work/env.txt").write_text("mine\n")
    mine.state = "paused"
    for _ in range(2):
        async with provisioned(parent=mine.id, setup=["exit 1"]) as view:
            assert (await view.read("env.txt")) == b"mine\n"
    assert mine.state == "paused" and cloud.count("pause") == 0 and cloud.count("create") == 0
    assert mine.id not in pool._tracked
    await pool.close_pools()
    assert mine.state == "paused", "close_pools deletes only parents it built"


async def test_a_parent_that_is_gone_fails_clearly(cloud: Cloud) -> None:
    with pytest.raises(SandboxError, match="sbx_9999 not found"):
        async with provisioned(parent="sbx_9999"):
            pass


# --- capacity: full machines, quotas, refusals ----------------------------------------------------


async def rollouts(cfg: PrizedConfig, n: int, hold: float = 0.0) -> list[str]:
    ids: list[str] = []

    async def one() -> None:
        async with provision_runtime(cfg) as view:
            ids.append(view.info.id)
            await asyncio.sleep(hold)

    async with asyncio.timeout(30):
        await asyncio.gather(*(one() for _ in range(n)))
    return ids


async def test_fork_capacity_forks_what_fits_then_the_rest(cloud: Cloud) -> None:
    cloud.capacity = 3
    ids = await rollouts(config(setup=SETUP, max_parents=1), 5, hold=0.2)
    assert len(set(ids)) == 5
    forks = [int(c[2]) for c in cloud.calls if c[0] == "fork"]
    assert forks[:2] == [5, 3], forks  # refused (fits 3), then 3; the last 2 once copies are deleted
    assert sum(forks[1:]) >= 5
    assert len(cloud.live("parent")) == 1


async def test_full_machines_get_another_parent(cloud: Cloud) -> None:
    cloud.capacity = 2
    ids = await rollouts(config(setup=SETUP, max_parents=3), 6, hold=1.0)
    parents = {cloud.boxes[i].parent for i in ids}
    assert len(ids) == 6 and len(parents) == 3, parents
    assert cloud.count("create") == 3


async def test_workspace_quota_is_waited_out(cloud: Cloud) -> None:
    cloud.fork_refusals = [
        PrizedError("sandbox_quota_exceeded", status=429, retry_after=0.1, body={"admitted": 0}),
        PrizedError("sandbox_capacity", status=503),
    ]
    ids = await rollouts(config(setup=SETUP), 2)
    assert len(ids) == 2 and cloud.count("fork") == 3


async def test_capacity_wait_runs_out(cloud: Cloud) -> None:
    cloud.capacity = 0
    with pytest.raises(SandboxError, match="capacity_wait"):
        await rollouts(config(setup=SETUP, max_parents=1, capacity_wait=1), 1)
    assert [b.labels["verifiers-prized"] for b in cloud.live()] == ["parent"]


async def test_a_refusal_that_will_not_pass_fails_the_rollout(cloud: Cloud) -> None:
    cloud.fork_refusals = [PrizedError("insufficient_balance", status=402)]
    with pytest.raises(SandboxError, match="insufficient_balance"):
        await rollouts(config(setup=SETUP), 1)
    assert len(await rollouts(config(setup=SETUP), 1)) == 1, "the next rollout tries again"


async def test_free_tier_run_limit_is_not_waited_out(cloud: Cloud) -> None:
    cloud.fork_refusals = [PrizedError("sandbox_quota_exceeded", status=429, reason="max_runtime")]
    with pytest.raises(SandboxError, match="sandbox_quota_exceeded"):
        await rollouts(config(setup=SETUP), 1)


async def test_a_parent_that_disappears_is_rebuilt(cloud: Cloud) -> None:
    await rollouts(config(setup=SETUP), 1)
    first = cloud.live("parent")[0]
    cloud.delete(first)  # its run time ended, say
    ids = await rollouts(config(setup=SETUP), 2)
    second = cloud.live("parent")[0]
    assert second.id != first.id and {cloud.boxes[i].parent for i in ids} == {second.id}


async def test_your_parent_disappearing_fails_every_waiting_rollout(cloud: Cloud) -> None:
    mine = cloud.new_box("prized-base-v1", {}, {}, "k")
    await rollouts(config(parent=mine.id), 1)
    (handle,) = [p.sandbox for p in next(iter(pool._pools.values())).parents]
    cloud.delete(mine)
    with pytest.raises(SandboxError, match="can no longer fork"):
        await rollouts(config(parent=mine.id), 3)
    await asyncio.sleep(0)
    assert handle.closed, "the handle to a parent that is gone is released"


# --- cancellation and cleanup ---------------------------------------------------------------------


async def test_cancelled_start_leaves_no_copy(cloud: Cloud) -> None:
    cfg = config(setup=SETUP)
    await rollouts(cfg, 1)  # the parent exists
    cloud.fork_delay = 0.3
    backends = [make_backend(cfg) for _ in range(3)]
    starts = [asyncio.ensure_future(b.start()) for b in backends]
    await asyncio.sleep(0.15)  # the fork is in flight
    starts[0].cancel()
    starts[1].cancel()
    await asyncio.gather(*starts, return_exceptions=True)
    assert starts[2].result() is None
    await asyncio.gather(*(b.stop() for b in backends))
    assert [b.labels["verifiers-prized"] for b in cloud.live()] == ["parent"]
    assert set(pool._tracked) == {cloud.live("parent")[0].id}


async def test_a_copy_freed_by_a_cancel_goes_to_the_next_rollout(cloud: Cloud) -> None:
    cfg = config(setup=SETUP)
    await rollouts(cfg, 1)
    cloud.fork_delay = 0.3
    first, second = make_backend(cfg), make_backend(cfg)
    start_first = asyncio.ensure_future(first.start())
    await asyncio.sleep(0.15)  # a fork of one is in flight for `first`
    start_second = asyncio.ensure_future(second.start())
    await asyncio.sleep(0.01)
    start_first.cancel()
    await asyncio.gather(start_first, return_exceptions=True)
    await asyncio.wait_for(start_second, 5)
    assert cloud.count("fork") == 2, "the second rollout took the first one's copy"
    await second.stop()
    await first.stop()
    assert [b.labels["verifiers-prized"] for b in cloud.live()] == ["parent"]


async def test_stop_is_idempotent_and_abandons_processes(cloud: Cloud) -> None:
    backend = make_backend(config())
    await backend.start()
    view = backend.bind()
    sleeper = await view.exec(["sleep", "600"], {})
    await backend.stop()
    await backend.stop()
    # Gone with its sandbox: -1, as the SDK reports a run that is gone (never a hang or a raise,
    # which ACP's stop path, catching only TimeoutError, would let replace the rollout's error).
    assert await sleeper.wait() == -1 and await sleeper.poll() == -1
    assert cloud.count("delete") == 1


async def test_cleanup_deletes_synchronously(cloud: Cloud) -> None:
    backend = make_backend(config())
    await backend.start()
    sandbox_id = backend.info.id
    await asyncio.to_thread(backend.cleanup)
    assert cloud.boxes[sandbox_id].state == "deleted" and sandbox_id not in pool._tracked
    await backend.stop()


async def test_leftovers_are_deleted_at_exit_by_label(cloud: Cloud) -> None:
    backends = [make_backend(config(setup=SETUP)) for _ in range(2)]
    await asyncio.gather(*(b.start() for b in backends))
    assert len(cloud.live()) == 3
    await asyncio.to_thread(pool.delete_leftovers)
    assert cloud.live() == [] and pool._tracked == {}
    assert cloud.count("delete_by_labels") == 1 and cloud.count("sync_delete") == 0
    for backend in backends:
        await backend.stop()


async def test_close_pools_deletes_the_parents(cloud: Cloud) -> None:
    await rollouts(config(setup=SETUP), 2)
    assert len(cloud.live("parent")) == 1
    await pool.close_pools()
    assert cloud.live() == [] and pool._tracked == {}


async def test_a_copy_that_arrives_as_its_rollout_is_cancelled_is_deleted(
    cloud: Cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The copy is handed over and the rollout cancelled before it resumes: the copy has no owner.
    import fake_prized

    the_pool = pool.ParentPool(pool.Recipe.of(config(), pool.Credentials("k")), pool.Credentials("k"))
    monkeypatch.setattr(the_pool, "_poke", lambda: None)  # no dispatcher: this test hands it out
    task = asyncio.ensure_future(the_pool.acquire())
    await asyncio.sleep(0)
    (waiter,) = the_pool._waiters
    child = fake_prized.AsyncSandbox(cloud.new_box("prized-base-v1", {}, {}, "k"))
    waiter.future.set_result((child, pool.Parent(child, owned=True)))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cloud.boxes[child.id].state == "deleted"


# --- review fixes (11-02): waiting for setup, failed builds, the exit sweep, the key ---------------


async def test_a_long_setup_does_not_count_against_capacity_wait(cloud: Cloud) -> None:
    # capacity_wait is time waiting for room; the first rollouts also wait for setup, which can take
    # far longer (pip installs, builds): they must get their copies, not a "no room" error, and a
    # refusal right after the build gets the whole capacity_wait.
    cloud.fork_refusals = [PrizedError("sandbox_capacity", status=503, retry_after=0.1)]
    ids = await rollouts(config(setup=["sleep 1.2", *SETUP], capacity_wait=0.8), 2)
    assert len(set(ids)) == 2 and cloud.count("create") == 1 and cloud.count("fork") == 2


async def test_capacity_wait_zero_forks_a_ready_parent(cloud: Cloud) -> None:
    # 0 means: fail at once when there is no room, not: always fail.
    cfg = config(setup=SETUP, capacity_wait=0)
    assert len(await rollouts(cfg, 1)) == 1
    assert len(await rollouts(cfg, 2)) == 2
    cloud.capacity = 0
    with pytest.raises(SandboxError, match="capacity_wait"):
        await rollouts(cfg, 1)


async def test_a_parent_that_cannot_be_built_is_retried_after_a_pause(cloud: Cloud) -> None:
    # Every machine full, and the workspace refuses another parent: no create storm while the
    # parents rest, one try per backoff.
    cfg = config(setup=SETUP, max_parents=3, capacity_wait=5)
    await rollouts(cfg, 1)  # the first parent
    cloud.capacity = 1
    cloud.create_refusals = [
        PrizedError("sandbox_quota_exceeded", status=429, reason="sandbox_limit") for _ in range(10_000)
    ]
    ids = await rollouts(cfg, 2, hold=1.2)  # the second waits for the first one's copy
    assert len(set(ids)) == 2
    assert cloud.count("create") <= 4, cloud.count("create")
    assert len(cloud.live("parent")) == 1


async def test_a_refused_first_parent_is_waited_out(cloud: Cloud) -> None:
    # The SDK never retries sandbox_quota_exceeded, and a busy workspace (other env-server workers'
    # copies) refuses the first parent too: wait it out within capacity_wait, as for a fork.
    cloud.create_refusals = [
        PrizedError("sandbox_quota_exceeded", status=429, reason="sandbox_limit"),
        PrizedError("sandbox_capacity", status=503, retry_after=0.1),
    ]
    ids = await rollouts(config(setup=SETUP), 2)
    assert len(set(ids)) == 2 and cloud.count("create") == 3
    # A refusal that will not pass still fails at once.
    cloud.create_refusals = [PrizedError("insufficient_balance", status=409)]
    with pytest.raises(SandboxError, match="insufficient_balance"):
        await rollouts(config(setup=[*SETUP, "true"]), 1)


async def test_the_exit_sweep_runs_with_nothing_tracked(cloud: Cloud) -> None:
    # Your own parent is never tracked, and each copy is untracked once deleted; a copy made by a
    # fork whose answer never came (a second Ctrl-C) is not tracked either. The run's label finds it.
    mine = cloud.new_box("prized-base-v1", {}, {"owner": "me"}, "k")
    await rollouts(config(parent=mine.id), 1)
    assert pool._tracked == {}
    lost = cloud.new_box("prized-base-v1", {}, {"verifiers-prized": "rollout", "vfp-run": pool.RUN_ID}, "k")
    await asyncio.to_thread(pool.delete_leftovers)
    assert lost.state == "deleted" and mine.state == "running"


def test_setup_env_may_not_carry_the_prized_key() -> None:
    with pytest.raises(ValidationError, match="Prized API key"):
        config(setup_env=["PRIZED_API_KEY"])
    with pytest.raises(ValidationError, match="Prized API key"):
        config(api_key_env="MY_PRIZED_KEY", setup_env=["HF_TOKEN", "MY_PRIZED_KEY"])
    assert config(setup_env=["HF_TOKEN"]).setup_env == ["HF_TOKEN"]


async def test_exec_streams_bytes_that_are_not_utf8(cloud: Cloud) -> None:
    # verifiers' ACP harnesses (claude_code, codex, opencode, pi, rlm...) frame each packet with an
    # 8-byte big-endian length: any byte >= 0x80 there must arrive as it was sent.
    payload = b'{"jsonrpc":"2.0","result":"' + "é".encode() * 100 + b'"}'
    frame = len(payload).to_bytes(8, "big") + payload
    assert any(b >= 0x80 for b in frame[:8])
    octal = "".join(f"\\{b:03o}" for b in frame)
    async with provisioned() as view:
        process = await view.exec(["sh", "-c", f"printf '{octal}'; printf '\\377\\000' >&2"], {})
        stdout = b"".join([chunk async for chunk in process.stdout])
        stderr = b"".join([chunk async for chunk in process.stderr])
        assert await process.wait() == 0
        assert stdout == frame and stderr == b"\xff\x00"
        missing = await view.exec(["no-such-command-here"], {})
        err = b"".join([chunk async for chunk in missing.stderr])
        assert await missing.wait() == 127 and b"not found" in err


async def test_terminate_keeps_what_the_command_prints_on_its_way_out(cloud: Cloud) -> None:
    async with provisioned() as view:
        process = await view.exec(
            ["sh", "-c", "trap 'echo bye; exit 3' TERM; echo up; while :; do sleep 0.05; done"], {}
        )
        first = process.stdout.__anext__()
        assert (await asyncio.wait_for(first, 10)).strip() == b"up"
        await process.terminate()
        rest = b"".join([chunk async for chunk in process.stdout])
        assert rest.strip() == b"bye"
        assert await process.wait() == 3


async def test_a_failed_copy_or_a_busy_parent_is_not_a_parent_gone(cloud: Cloud) -> None:
    # The SDK raises sandbox_deleted with a copy's id when a copy fails and the fork is undone, and
    # parent_not_running is a parent between states: neither may retire (or delete) the parent.
    mine = cloud.new_box("prized-base-v1", {}, {}, "k")
    cloud.fork_refusals = [PrizedError("sandbox_deleted", sandbox_id="sbx_a_copy")]
    assert len(await rollouts(config(parent=mine.id), 1)) == 1
    assert len(await rollouts(config(parent=mine.id), 1)) == 1, "the parent is still usable"
    cfg = config(setup=SETUP, max_parents=1)
    await rollouts(cfg, 1)
    (parent,) = cloud.live("parent")
    cloud.fork_refusals = [PrizedError("parent_not_running", status=409)]
    assert len(await rollouts(cfg, 1)) == 1
    assert cloud.count("create") == 1 and cloud.live("parent") == [parent]


async def test_close_pools_fails_a_rollout_whose_fork_is_in_flight(cloud: Cloud) -> None:
    cfg = config(setup=SETUP)
    await rollouts(cfg, 1)
    cloud.fork_delay = 0.5
    backend = make_backend(cfg)
    start = asyncio.ensure_future(backend.start())
    await asyncio.sleep(0.2)  # the fork is in flight
    await pool.close_pools()
    with pytest.raises(SandboxError, match="closed"):
        await asyncio.wait_for(start, 5)
    await backend.stop()
    assert cloud.live() == []


async def test_a_parent_that_cannot_fork_is_replaced(cloud: Cloud) -> None:
    cfg = config(setup=SETUP)
    cloud.fork_refusals = [PrizedError("fork_unsupported_host", status=409, reason="host_retiring")]
    assert len(await rollouts(cfg, 1)) == 1
    first, second = sorted(b.id for b in cloud.boxes.values() if b.labels.get("verifiers-prized") == "parent")
    assert cloud.boxes[first].state == "deleted" and [b.id for b in cloud.live("parent")] == [second]
    # Too big to fork: another parent of the same shape would be too, so the rollout fails.
    cloud.fork_refusals = [PrizedError("parent_not_forkable", status=409, reason="parent_too_big")]
    with pytest.raises(SandboxError, match="parent_not_forkable"):
        await rollouts(cfg, 1)
    # A parent after parent that cannot fork: the one that forked before is replaced without
    # counting, then a few new ones are tried, then the rollouts fail, and setup is not run again
    # for every new wave of rollouts.
    cloud.fork_refusals = [PrizedError("parent_not_forkable", status=409, reason="memory_discarded")] * 10
    with pytest.raises(SandboxError, match="in a row could not fork"):
        await rollouts(cfg, 1)
    assert cloud.count("create") == 3 + pool.MAX_REPLACEMENTS
    with pytest.raises(SandboxError, match="in a row could not fork"):
        await rollouts(cfg, 2)
    assert cloud.count("create") == 3 + pool.MAX_REPLACEMENTS


async def test_config_and_info_name_the_workdir_in_use(cloud: Cloud) -> None:
    backend = make_backend(config())
    assert backend.config.workdir == DEFAULT_WORKDIR and backend.info.workdir == DEFAULT_WORKDIR
    task = make_backend(config().model_copy(update={"workdir": "sub"}))  # a task's, unvalidated
    assert task.config.workdir == task.info.workdir == f"{DEFAULT_WORKDIR}/sub"


def test_setup_timeout_may_not_be_negative() -> None:
    with pytest.raises(ValidationError, match="setup_timeout"):
        config(setup_timeout=-1)
    assert config(setup_timeout=0).setup_timeout == 0


async def test_run_refuses_output_the_sdk_cut_short(cloud: Cloud) -> None:
    async with provisioned() as view:
        cloud.truncate_output = True
        with pytest.raises(SandboxError, match="more than the SDK keeps"):
            await view.run(["echo", "hi"], {})
        with pytest.raises(SandboxError, match="more than the SDK keeps"):
            await view.read("/etc/hostname")  # outside the home: read through `run`


def test_a_forked_child_gets_a_run_of_its_own(cloud: Cloud) -> None:
    # A child forked from a process that has made sandboxes must not sweep that process's
    # sandboxes (its label, its tracked ones) at its own exit.
    import os

    pool.track("sbx_parents", pool.Credentials("k"))
    before = pool.RUN_ID
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child
        ok = pool.RUN_ID != before and pool._tracked == {} and pool._used == {}
        os.write(w, b"1" if ok else b"0")
        os._exit(0)
    os.close(w)
    os.waitpid(pid, 0)
    assert os.read(r, 1) == b"1"
    assert pool.RUN_ID == before and "sbx_parents" in pool._tracked


async def test_waiting_in_line_behind_a_fork_is_not_waiting_for_room(cloud: Cloud) -> None:
    # capacity_wait counts from a refusal, not from a wait for another rollout's fork to finish.
    cfg = config(setup=SETUP, capacity_wait=0, max_parents=1)
    await rollouts(cfg, 1)
    cloud.fork_delay = 0.4
    first = asyncio.ensure_future(rollouts(cfg, 1))
    await asyncio.sleep(0.1)  # its fork is in flight
    assert len(await rollouts(cfg, 1)) == 1
    assert len(await first) == 1


async def test_a_build_that_fails_after_a_long_setup_is_retried(cloud: Cloud) -> None:
    # The setup before a transient failure is not time spent waiting for room either.
    cloud.pause_refusals = [PrizedError("sandbox_unreachable", status=503)]
    ids = await rollouts(config(setup=["sleep 1.2", *SETUP], capacity_wait=0.8), 1)
    assert len(ids) == 1 and cloud.count("create") == 2


async def test_a_child_left_running_keeps_its_stdout(cloud: Cloud, tmp_path: Any) -> None:
    # A command that exits while a child it started still writes: the child must not be killed by
    # SIGPIPE once the wrapper stops waiting (2 s) for the encoders.
    marker = tmp_path / "done"
    script = f"(sleep 2.3; echo two; sleep 0.2; echo three; touch {marker}) & echo one"
    async with provisioned() as view:
        process = await view.exec(["sh", "-c", script], {})
        assert (await asyncio.wait_for(process.stdout.__anext__(), 10)).strip() == b"one"
        assert await asyncio.wait_for(process.wait(), 10) == 0

        async def touched() -> bool:
            return marker.exists()

        await wait_for(touched, 10)


async def test_exec_env_names_like_the_wrappers_own_reach_the_command(cloud: Cloud) -> None:
    env = {"f": "F", "o": "O", "e": "E", "h": "H", "k": "K", "c": "C", "rc": "R"}
    async with provisioned() as view:
        process = await view.exec(["sh", "-c", 'printf %s "$f$o$e$h$k$c$rc"'], env)
        assert b"".join([chunk async for chunk in process.stdout]) == b"FOEHKCR"
        assert await process.wait() == 0


async def test_a_line_cut_off_by_a_kill_is_never_passed_on_as_text() -> None:
    from verifiers_prized.backend import _Lines

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    lines = _Lines(queue)
    lines(":aGk=\n:aGVsbG8gd2")  # "hi", then "hello w..." cut off mid-group
    lines.close()
    got = [queue.get_nowait() for _ in range(queue.qsize())]
    assert got == [b"hi", b"hello ", None]


async def test_a_refused_rollout_waits_out_another_parents_build(cloud: Cloud) -> None:
    # B is refused room (parent 1 is full) and a second parent is built: the build's setup does not
    # count against B's capacity_wait, during it or after it (a refusal right after the build).
    cfg = config(setup=["sleep 1.2", *SETUP], capacity_wait=0.8, max_parents=2)
    await rollouts(cfg, 1)  # parent 1
    cloud.capacity = 1
    first = asyncio.ensure_future(rollouts(cfg, 1, hold=2.5))
    await asyncio.sleep(0.3)  # A holds parent 1's only copy
    second = asyncio.ensure_future(rollouts(cfg, 1))  # B: refused, so parent 2 is built
    # B is refused by parent 1 again at +0.5 s (it then rests 1 s); the next fork is B's on
    # parent 2 once it is built (+1.2 s), and that one is refused too.
    await asyncio.sleep(0.7)
    cloud.fork_refusals = [PrizedError("sandbox_capacity", status=503)]
    assert len(await second) == 1
    assert len(await first) == 1
    assert cloud.count("create") == 2 and cloud.fork_refusals == []


async def test_a_first_parent_never_admitted_fails_within_capacity_wait(cloud: Cloud) -> None:
    cloud.create_refusals = [
        PrizedError("sandbox_quota_exceeded", status=429, reason="sandbox_limit") for _ in range(100)
    ]
    began = asyncio.get_running_loop().time()
    with pytest.raises(SandboxError, match="capacity_wait"):
        await rollouts(config(setup=SETUP, capacity_wait=1), 1)
    assert asyncio.get_running_loop().time() - began < 5
    assert cloud.count("create") <= 4


def test_build_prints_a_paused_parent_that_runs_fork(
    cloud: Cloud, capsys: pytest.CaptureFixture[str]
) -> None:
    from verifiers_prized.__main__ import main

    code = main(
        ["build", "--setup", "echo built > env.txt", "--label", "team=evals", "--setup-timeout", "60"]
    )
    assert code == 0
    sandbox_id = capsys.readouterr().out.strip()
    box = cloud.boxes[sandbox_id]
    assert box.state == "paused"
    assert box.labels["team"] == "evals" and box.labels["verifiers-prized"] == "parent"
    assert box.labels["vfp-run"].startswith("build-"), "never swept by a run's label"
    # It outlives the process: never tracked, and not under this run's label, which the exit sweep
    # deletes (the warm-up copy carries that label).
    assert sandbox_id not in pool._tracked and box.labels["vfp-run"] != f"{pool.RUN_ID}"

    async def fork_it() -> bytes:
        async with provisioned(parent=sandbox_id) as view:
            data: bytes = await view.read("env.txt")
        await pool.close_pools()
        return data

    assert asyncio.run(fork_it()) == b"built\n"
    assert cloud.boxes[sandbox_id].state == "paused" and cloud.count("create") == 1


def test_build_refuses_a_bad_label_and_a_missing_key(
    cloud: Cloud, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from verifiers_prized.__main__ import main

    with pytest.raises(SystemExit, match="key=value"):
        main(["build", "--label", "novalue"])
    monkeypatch.delenv("PRIZED_API_KEY")
    assert main(["build"]) == 1
    assert "PRIZED_API_KEY" in capsys.readouterr().err
    assert cloud.count("create") == 0


def test_build_logs_its_progress_not_every_request(
    cloud: Cloud, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    from verifiers_prized.__main__ import main

    monkeypatch.setattr(pool.logger, "handlers", [])
    monkeypatch.setattr(pool.logger, "level", logging.NOTSET)
    root = logging.getLogger()  # as in a fresh interpreter: no handlers, WARNING
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    assert main(["build"]) == 0
    err = capsys.readouterr().err
    assert "verifiers-prized: parent" in err and "ready in" in err
    logging.getLogger("httpx").info("HTTP Request: POST https://api.prized.dev/ ...")
    assert "HTTP Request" not in capsys.readouterr().err


# --- the warm-up fork (a paused parent's first fork writes its memory to disk) --------------------


def _ops(cloud: Cloud, *names: str) -> list[tuple[str, ...]]:
    return [c for c in cloud.calls if c[0] in names]


def _record_delete_waits(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    waits: dict[str, bool] = {}
    real_delete = fake_prized.AsyncSandbox.delete

    async def delete(self: fake_prized.AsyncSandbox, *, wait: bool = False, timeout: float = 120.0) -> None:
        waits[self.id] = wait
        await real_delete(self, wait=wait, timeout=timeout)

    monkeypatch.setattr(fake_prized.AsyncSandbox, "delete", delete)
    return waits


def test_build_forks_the_paused_parent_once_and_deletes_the_copy(
    cloud: Cloud, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from verifiers_prized.__main__ import main

    waits = _record_delete_waits(monkeypatch)
    assert main(["build", "--setup", "echo built > env.txt", "--label", "team=evals"]) == 0
    out = capsys.readouterr()
    sandbox_id = out.out.strip()
    parent = cloud.boxes[sandbox_id]
    [warm] = [b for b in cloud.boxes.values() if b.labels.get("verifiers-prized") == "warmup"]
    # After the pause, so the fork writes the pause point the run's forks then copy.
    assert _ops(cloud, "pause", "fork", "delete") == [
        ("pause", sandbox_id),
        ("fork", sandbox_id, "1"),
        ("delete", warm.id),
    ]
    assert warm.parent == sandbox_id and warm.state == "deleted"
    assert waits == {warm.id: True}, "until it is gone: a deleting copy still counts toward the quota"
    assert warm.labels["team"] == "evals" and warm.labels["vfp-run"] == pool.RUN_ID
    assert parent.state == "paused" and (parent.root / "home/prized/work/env.txt").read_text() == "built\n"
    assert "ready to fork" in out.err and sandbox_id in out.err
    # Told before the warm-up that the setup is done, so a Ctrl-C there is not a blind one.
    assert out.err.index("is set up and paused") < out.err.index("ready to fork")
    assert pool._tracked == {}, "the copy is deleted, the parent never tracked"
    # The exit sweep (this run's label) takes copies, never the parent it printed.
    pool.delete_leftovers()
    assert parent.state == "paused" and parent.labels["vfp-run"] != pool.RUN_ID


@pytest.mark.parametrize("flag", ["--no-warm", "--no-pause"])
def test_build_without_a_warm_up(cloud: Cloud, capsys: pytest.CaptureFixture[str], flag: str) -> None:
    from verifiers_prized.__main__ import main

    assert main(["build", flag]) == 0
    sandbox_id = capsys.readouterr().out.strip()
    assert cloud.count("fork") == 0 and pool._used == {}
    # A running parent is copied as it is at each fork, so a warm-up would not carry over.
    assert cloud.boxes[sandbox_id].state == ("paused" if flag == "--no-warm" else "running")


@pytest.mark.parametrize(
    "error",
    [
        PrizedError("sandbox_quota_exceeded", status=429, reason="sandbox_limit"),
        RuntimeError("not the SDK's"),  # best effort: no error of the warm-up loses the built parent
    ],
)
def test_a_failed_warm_up_keeps_the_built_parent(
    cloud: Cloud, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    from verifiers_prized.__main__ import main

    cloud.fork_refusals = [error]  # type: ignore[list-item]
    assert main(["build", "--setup", "echo built > env.txt"]) == 0
    out = capsys.readouterr()
    sandbox_id = out.out.strip()
    assert cloud.boxes[sandbox_id].state == "paused" and len(cloud.live()) == 1
    assert "warm-up fork failed" in out.err and str(error) in out.err


def _build_task() -> asyncio.Future[tuple[str, bool]]:
    from verifiers_prized.__main__ import _build

    cfg = config(setup=["echo built > env.txt"])
    return asyncio.ensure_future(_build(cfg, pool.credentials(cfg), pause=True, warm=True))


async def test_a_build_interrupted_during_its_warm_up_keeps_the_parent(cloud: Cloud) -> None:
    # A Ctrl-C (asyncio.run cancels the task) while the warm-up forks: the setup is done, so the parent
    # is kept, paused, and its id returned; the fork is abandoned and leaves no copy.
    cloud.fork_delay = 5
    build = _build_task()

    async def forking() -> bool:
        return cloud.count("fork") == 1

    await wait_for(forking)
    build.cancel()
    sandbox_id, interrupted = await build
    assert interrupted and cloud.boxes[sandbox_id].state == "paused"
    assert [b.id for b in cloud.live()] == [sandbox_id] and pool._tracked == {}


async def test_a_warm_up_fork_that_lands_after_a_ctrl_c_is_swept_never_the_parent(
    cloud: Cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fork made its copy, but the answer came after the Ctrl-C: the parent is kept, and the exit
    # sweep finds the copy by this run's label, never the parent (labelled build-<run>).
    real_fork = fake_prized.AsyncSandbox.fork

    async def fork(self: fake_prized.AsyncSandbox, count: int = 1, **kw: Any) -> list[Any]:
        copies = await real_fork(self, count, **kw)
        await asyncio.sleep(5)  # the answer is late
        return copies

    monkeypatch.setattr(fake_prized.AsyncSandbox, "fork", fork)
    build = _build_task()

    async def landed() -> bool:
        return bool(cloud.live("warmup"))

    await wait_for(landed)
    build.cancel()
    sandbox_id, interrupted = await build
    [copy] = cloud.live("warmup")
    assert interrupted and not pool.is_tracked(copy.id)
    pool.delete_leftovers()
    assert [b.id for b in cloud.live()] == [sandbox_id] and cloud.boxes[sandbox_id].state == "paused"
    assert ("delete_by_labels", f"vfp-run={pool.RUN_ID}") in cloud.calls


async def test_a_ctrl_c_while_the_warm_up_copy_is_deleted_still_deletes_it(
    cloud: Cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_delete = fake_prized.AsyncSandbox.delete
    started: list[str] = []

    async def slow_delete(
        self: fake_prized.AsyncSandbox, *, wait: bool = False, timeout: float = 120.0
    ) -> None:
        started.append(self.id)
        await asyncio.sleep(0.3)
        await real_delete(self, wait=wait, timeout=timeout)

    monkeypatch.setattr(fake_prized.AsyncSandbox, "delete", slow_delete)
    build = _build_task()

    async def deleting() -> bool:
        return bool(started)

    await wait_for(deleting)
    build.cancel()
    sandbox_id, interrupted = await build
    # The copy's delete runs to the end (it is shielded); the parent is kept.
    assert interrupted and started != [sandbox_id] and cloud.live("warmup") == []
    assert [b.id for b in cloud.live()] == [sandbox_id] and pool._tracked == {}


def test_the_cli_prints_the_kept_parent_after_a_ctrl_c_in_its_warm_up(
    cloud: Cloud, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from verifiers_prized import __main__ as cli

    async def interrupted(*args: Any) -> bool:
        raise asyncio.CancelledError  # what asyncio.run's Ctrl-C handler delivers

    monkeypatch.setattr(cli, "warm_fork", interrupted)
    assert cli.main(["build", "--setup", "echo built > env.txt"]) == 130
    out = capsys.readouterr()
    sandbox_id = out.out.strip()
    assert cloud.boxes[sandbox_id].state == "paused" and "is kept" in out.err


async def test_a_warm_up_copy_whose_delete_fails_is_logged_as_such_and_swept_at_exit(
    cloud: Cloud, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_delete = fake_prized.AsyncSandbox.delete

    async def delete(self: fake_prized.AsyncSandbox, *, wait: bool = False, timeout: float = 120.0) -> None:
        if cloud.boxes[self.id].labels.get("verifiers-prized") == "warmup":
            raise PrizedError("internal_error", status=500, sandbox_id=self.id)
        await real_delete(self, wait=wait, timeout=timeout)

    monkeypatch.setattr(fake_prized.AsyncSandbox, "delete", delete)
    sandbox_id, interrupted = await _build_task()
    [copy] = cloud.live("warmup")
    assert not interrupted and pool.is_tracked(copy.id)
    assert "delete did not finish" in caplog.text and "made and deleted" not in caplog.text
    pool.delete_leftovers()
    assert [b.id for b in cloud.live()] == [sandbox_id]


async def test_the_pool_forks_its_own_parents_without_a_warm_up(cloud: Cloud) -> None:
    # Rollouts are waiting while the pool builds, so a warm-up there would only add to their wait:
    # the first fork is theirs.
    assert len(await rollouts(config(setup=SETUP), 3)) == 3
    assert not [b for b in cloud.boxes.values() if b.labels.get("verifiers-prized") == "warmup"]
    [parent] = cloud.live("parent")
    assert sum(int(c[2]) for c in _ops(cloud, "fork")) == 3
    assert parent.state == "paused"


# --- the 11-03 review ----------------------------------------------------------------------------


async def test_parents_that_forked_before_are_replaced_without_counting(cloud: Cloud) -> None:
    # Four parents that have all forked lose their machine together (a retiring host): each is
    # replaced, and the rollouts get copies of the new ones instead of the pool shutting down.
    cfg = config(setup=SETUP)
    cloud.capacity = 1
    assert len(await rollouts(cfg, 4, hold=0.3)) == 4
    assert len(cloud.live("parent")) == 4
    cloud.fork_refusals = [PrizedError("fork_unsupported_host", status=409, reason="host_retiring")] * 4
    assert len(await rollouts(cfg, 4, hold=0.3)) == 4
    assert len(await rollouts(cfg, 4)) == 4
    assert cloud.fork_refusals == [] and len(cloud.live("parent")) == 4


async def test_a_sticky_error_is_raised_fresh_each_time(cloud: Cloud) -> None:
    cfg = config(setup=["exit 3"])
    errors = []
    for _ in range(3):
        with pytest.raises(SandboxError, match="setup command 1 exited 3") as caught:
            await rollouts(cfg, 2)
        errors.append(caught.value)
    assert len({id(e) for e in errors}) == 3
    depth = [len(__import__("traceback").extract_tb(e.__traceback__)) for e in errors]
    assert depth[2] <= depth[1], depth


async def test_a_create_that_keeps_failing_counts_against_capacity_wait(
    cloud: Cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each refused create takes 0.5 s (the SDK retrying a full workspace): that is waiting for room.
    monkeypatch.setattr(pool, "BACKOFF_MIN", 0.05)
    monkeypatch.setattr(pool, "BACKOFF_MAX", 0.1)
    cloud.create_delay = 0.5
    cloud.create_refusals = [PrizedError("sandbox_capacity", status=503) for _ in range(100)]
    began = asyncio.get_running_loop().time()
    with pytest.raises(SandboxError, match="capacity_wait"):
        await rollouts(config(setup=SETUP, capacity_wait=1.2), 1)
    assert asyncio.get_running_loop().time() - began < 3.5
    assert cloud.count("create") <= 4


async def test_your_parent_between_states_is_waited_out(cloud: Cloud) -> None:
    mine = cloud.new_box("prized-base-v1", {}, {"owner": "me"}, "k")
    (mine.root / "home/prized/work/env.txt").write_text("mine\n")
    mine.state = "pausing"

    async def settle() -> None:
        await asyncio.sleep(0.4)
        mine.state = "paused"

    settling = asyncio.ensure_future(settle())
    async with provisioned(parent=mine.id) as view:
        assert (await view.read("env.txt")) == b"mine\n"
    await settling
    assert cloud.count("connect") >= 2 and mine.state == "paused"
    await pool.close_pools()


async def test_terminate_adds_nothing_to_stderr(cloud: Cloud) -> None:
    async with provisioned() as view:
        # Not a shell (a shell running `sleep` prints its own "Terminated", on the command's stderr).
        code = "import time; print('started', flush=True); time.sleep(30)"
        process = await view.exec([*PYTHON, "-c", code], {})
        stdout = process.stdout.__aiter__()
        assert (await stdout.__anext__()).strip() == b"started"
        await process.terminate()
        stderr = b"".join([chunk async for chunk in process.stderr])
        assert await process.wait() != 0
        assert stderr == b"", stderr


async def test_a_stdin_write_after_the_command_ended_fails_at_once(cloud: Cloud) -> None:
    async with provisioned() as view:
        # The command exits while a child it started keeps its stdout (the 2 s grace).
        process = await view.exec(["sh", "-c", "(sleep 1.5; echo late) & exit 0"], {})
        await asyncio.sleep(0.5)
        began = asyncio.get_running_loop().time()
        with pytest.raises(SandboxError, match="exited"):
            await process.write(b"x\n")
        assert asyncio.get_running_loop().time() - began < 3
        assert await process.wait() == 0
        killed = await view.exec(["sleep", "30"], {})
        await killed.kill()
        began = asyncio.get_running_loop().time()
        with pytest.raises(SandboxError, match="exited"):
            await killed.write(b"x\n")
        assert asyncio.get_running_loop().time() - began < 1


async def test_close_finishes_deleting_a_replaced_parent(cloud: Cloud) -> None:
    cfg = config(setup=SETUP)
    assert len(await rollouts(cfg, 1)) == 1
    (first,) = cloud.live("parent")
    cloud.fork_refusals = [PrizedError("fork_unsupported_host", status=409, reason="host_retiring")]
    real_delete = fake_prized.AsyncSandbox.delete
    slow: list[str] = []

    async def slow_delete(self: Any, **kwargs: Any) -> None:
        if self.id == first.id:
            slow.append(self.id)
            await asyncio.sleep(0.3)
        await real_delete(self, **kwargs)

    fake_prized.AsyncSandbox.delete = slow_delete  # type: ignore[method-assign]
    try:
        backend = make_backend(cfg)
        start = asyncio.ensure_future(backend.start())
        while not slow:
            await asyncio.sleep(0.01)
        await pool.close_pools()  # while the replaced parent's delete is under way
        with contextlib.suppress(SandboxError):
            await start
        await backend.stop()
    finally:
        fake_prized.AsyncSandbox.delete = real_delete  # type: ignore[method-assign]
    assert first.state == "deleted"
    assert cloud.live() == []


def test_a_key_is_named_by_an_opaque_id_never_by_itself() -> None:
    first, again, other = (pool.Credentials(k) for k in ("dcp_0000aaaa", "dcp_0000aaaa", "dcp_0000bbbb"))
    assert first.key_id == again.key_id != other.key_id
    recipe = pool.Recipe.of(config(), first)
    assert "dcp_0000aaaa" not in repr(recipe) and "dcp_0000aaaa" not in repr(first)


# --- the second 11-03 review ---------------------------------------------------------------------


async def test_a_process_lost_mid_rollout_is_a_sandbox_error(cloud: Cloud) -> None:
    # Its sandbox goes away under it (its run time over, its machine lost): verifiers gets a
    # SandboxError with the cause, which its retry rules for sandbox errors match.
    async with provisioned() as view:
        process = await view.exec(["sleep", "30"], {})
        cloud.delete(cloud.boxes[view.backend.info.id])
        with pytest.raises(SandboxError, match="lost the process: .*sandbox_deleted"):
            await process.wait()
        assert await process.poll() == -1


async def test_your_parent_stuck_between_states_names_that_when_it_fails(
    cloud: Cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pool, "BACKOFF_MIN", 0.05)
    monkeypatch.setattr(pool, "BACKOFF_MAX", 0.1)
    mine = cloud.new_box("prized-base-v1", {}, {"owner": "me"}, "k")
    mine.state = "resizing"
    with pytest.raises(SandboxError, match="capacity_wait .*is resizing"):
        async with asyncio.timeout(10), provisioned(parent=mine.id, capacity_wait=0.5):
            pass
    assert cloud.count("connect") >= 2


def test_warnings_reach_stderr_when_nothing_else_shows_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging

    library = logging.getLogger("verifiers.v1")
    root = logging.getLogger()
    # A script that never set up logging: verifiers.v1's NullHandler and nothing else.
    monkeypatch.setattr(library, "handlers", [logging.NullHandler()])
    monkeypatch.setattr(library, "propagate", True)
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(
        pool.logger, "handlers", [h for h in pool.logger.handlers if type(h).__name__ == "_LastResort"]
    )
    pool.logger.warning("verifiers-prized: could not delete sandbox sbx_x")
    assert "could not delete sandbox sbx_x" in capsys.readouterr().err
    # Once something real shows them (verifiers' CLIs, the user's own setup), not twice.
    seen: list[str] = []

    class Seen(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    monkeypatch.setattr(library, "handlers", [Seen()])
    pool.logger.warning("verifiers-prized: once")
    assert seen == ["verifiers-prized: once"] and "once" not in capsys.readouterr().err
