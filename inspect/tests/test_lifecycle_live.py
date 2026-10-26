"""Live lifecycle checks: create per sample, delete or pause on cleanup, task and CLI cleanup."""

import os

import pytest
from prized import AsyncSandbox, PrizedError

from inspect_prized import PrizedSandboxConfig, PrizedSandboxEnvironment

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("PRIZED_LIVE") != "1", reason="PRIZED_LIVE=1 not set"),
]
LABELS = {"test": "inspect-prized-lifecycle"}
if os.environ.get("PRIZED_TEST_TASK"):
    LABELS["task"] = os.environ["PRIZED_TEST_TASK"]


async def _state(sandbox_id: str) -> str:
    try:
        sb = await AsyncSandbox.connect(sandbox_id)
    except PrizedError as err:
        assert err.code in ("not_found", "sandbox_deleted")
        return "gone"
    try:
        return sb.state
    finally:
        await sb.close()


async def test_sample_lifecycle_keep_and_cli_cleanup(capsys):
    cfg = PrizedSandboxConfig(vcpu=1, memory_gb=2, labels=LABELS, env={"GREETING": "hi"}, max_runtime_min=15)
    envs = await PrizedSandboxEnvironment.sample_init("my task/1", cfg, {})
    env = envs["default"].as_type(PrizedSandboxEnvironment)
    sid = env.sandbox.id
    assert env.sandbox.labels["created-by"] == "inspect-prized"
    assert env.sandbox.labels["inspect-task"] == "my-task-1"
    assert (await env.exec(["sh", "-c", "echo $GREETING"])).stdout == "hi\n"
    conn = await env.connection()
    assert conn.command == f"prized sandbox ssh {sid}"
    await PrizedSandboxEnvironment.sample_cleanup("my task/1", cfg, envs, False)
    assert await _state(sid) in ("deleting", "deleted", "gone")

    kept_cfg = cfg.model_copy(update={"keep": True})
    envs = await PrizedSandboxEnvironment.sample_init("t", kept_cfg, {})
    kept = envs["default"].as_type(PrizedSandboxEnvironment).sandbox.id
    await PrizedSandboxEnvironment.sample_cleanup("t", kept_cfg, envs, True)
    await PrizedSandboxEnvironment.task_cleanup("t", kept_cfg, True)  # keep=True: task cleanup leaves it
    assert await _state(kept) in ("pausing", "paused")

    other = await AsyncSandbox.create(vcpu=1, memory_gb=2, labels=LABELS)
    try:
        await PrizedSandboxEnvironment.cli_cleanup(other.id)
        assert "left alone" in capsys.readouterr().out
        assert await _state(other.id) == "running"
    finally:
        await other.delete()
        await other.close()
    await PrizedSandboxEnvironment.cli_cleanup(kept)
    assert "Deleted" in capsys.readouterr().out
    assert await _state(kept) in ("deleting", "deleted", "gone")


async def test_task_cleanup_deletes_what_sample_cleanup_missed():
    cfg = PrizedSandboxConfig(vcpu=1, memory_gb=2, labels=LABELS, max_runtime_min=15)
    envs = await PrizedSandboxEnvironment.sample_init("t2", cfg, {})
    sid = envs["default"].as_type(PrizedSandboxEnvironment).sandbox.id
    assert sid in PrizedSandboxEnvironment._live["t2"]
    await envs["default"].as_type(PrizedSandboxEnvironment).sandbox.close()  # sample cleanup never ran
    await PrizedSandboxEnvironment.task_cleanup("t2", cfg, True)
    assert sid not in PrizedSandboxEnvironment._live["t2"]
    assert await _state(sid) in ("deleting", "deleted", "gone")


async def test_deleted_sandbox_is_unavailable_and_task_cleanup_is_per_task():
    from inspect_ai.util import SandboxUnavailableError

    cfg = PrizedSandboxConfig(vcpu=1, memory_gb=2, labels=LABELS, max_runtime_min=15)
    a = await PrizedSandboxEnvironment.sample_init("task-a", cfg, {})
    b = await PrizedSandboxEnvironment.sample_init("task-b", cfg, {})
    env_a = a["default"].as_type(PrizedSandboxEnvironment)
    env_b = b["default"].as_type(PrizedSandboxEnvironment)
    try:
        await env_a.sandbox.delete(wait=True)
        with pytest.raises(SandboxUnavailableError):
            await env_a.exec(["true"])
        await PrizedSandboxEnvironment.task_cleanup("task-a", cfg, True)  # must not touch task-b
        assert (await env_b.exec(["echo", "still here"])).stdout == "still here\n"
    finally:
        await PrizedSandboxEnvironment.sample_cleanup("task-a", cfg, a, False)
        await PrizedSandboxEnvironment.sample_cleanup("task-b", cfg, b, False)
    assert await _state(env_b.sandbox.id) in ("deleting", "deleted", "gone")
