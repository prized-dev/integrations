"""Cancel a trial at every await point of its start: nothing may leak (from an independent review)."""

import asyncio
import shutil

import pytest
from prized import PrizedError
from test_fork import _Cloud, _Job, _Sandbox, cloud  # noqa: F401

from harbor_prized import _environment, _fork


def _yielding(cloud, monkeypatch):  # noqa: F811
    """Every API call suspends before and after, so a cancel can land there."""
    for name in ("exec", "pause", "delete", "keep_alive"):
        original = getattr(_Sandbox, name)

        def wrap(original):
            async def call(self, *args, **kwargs):
                await asyncio.sleep(0)
                try:
                    return await original(self, *args, **kwargs)
                finally:
                    await asyncio.sleep(0)

            return call

        monkeypatch.setattr(_Sandbox, name, wrap(original))

    # As the SDK does: a create or fork cancelled after it made sandboxes
    # deletes them before the cancel goes on.
    async def made(sandboxes):
        try:
            await asyncio.sleep(0)
        except BaseException:
            for sandbox in sandboxes:
                sandbox.state, sandbox.closed = "deleted", True
            raise

    fork = _Sandbox.fork

    async def fork_(self, count=1, *, labels=None):
        await asyncio.sleep(0)
        copies = await fork(self, count, labels=labels)
        await made(copies)
        return copies

    monkeypatch.setattr(_Sandbox, "fork", fork_)
    create = cloud.create

    async def create_(template, **kwargs):
        await asyncio.sleep(0)
        sandbox = await create(template, **kwargs)
        await made([sandbox])
        return sandbox

    monkeypatch.setattr(_environment.AsyncSandbox, "create", create_)


async def _scenario(job, cloud, steps, victim):  # noqa: F811
    environments = [job.environment("a") for _ in range(2)]
    tasks = [asyncio.ensure_future(e.start(force_build=False)) for e in environments]
    for _ in range(steps):
        await asyncio.sleep(0)
    tasks[victim].cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for environment in environments:
        await environment.stop(delete=True)  # Harbor's finalize
        shutil.rmtree(environment.trial_paths.trial_dir)  # keep the tmp dir small
    leases = [p.leases for p in _fork._CACHE.parents()] if _fork._CACHE else []
    await _fork.close_parents()  # the job's end
    return results, leases


@pytest.mark.parametrize(
    "errors",
    [
        [],
        ["fork_capacity"],
        ["fork_unsupported_host"],
        ["fork_capacity", "fork_capacity"],
        ["sandbox_error"],
    ],
)
@pytest.mark.parametrize("victim", [0, 1])
@pytest.mark.parametrize("plan", [["a", "a"], ["a", "a", "a"]])
async def test_a_cancel_at_any_await_leaks_nothing(
    tmp_path,
    cloud,  # noqa: F811
    monkeypatch,
    victim,
    plan,
    errors,
):
    _yielding(cloud, monkeypatch)
    job = _Job(tmp_path, plan)  # trial names are new each round
    problems, outcomes = [], []
    for steps in range(3000):
        cloud.sandboxes.clear()
        cloud.fork_errors = [PrizedError(code, status=409) for code in errors]
        monkeypatch.setattr(_fork, "_CACHE", None)  # a new job each round
        results, leases = await _scenario(job, cloud, steps, victim)
        alive = [(s.id, s.state, sorted(s.labels)) for s in cloud.alive()]
        unclosed = [s.id for s in cloud.sandboxes if not s.closed]
        other = results[1 - victim]
        outcomes.append(type(results[victim]).__name__)
        if results[victim] is None:  # it started before the cancel: all covered
            break
        if alive or any(leases) or unclosed or isinstance(other, BaseException):
            problems.append((steps, alive, leases, unclosed, repr(other)))
    print("STEPS", victim, plan, len(outcomes))
    assert not problems, problems[:5]
    # The steps reach past the end of the start: every await point was hit.
    assert outcomes[0] == "CancelledError" and outcomes[-1] == "NoneType", outcomes
