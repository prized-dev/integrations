"""Cases where fork mode must do no worse than fresh mode (from an independent review)."""

import asyncio

from prized import PrizedError
from test_fork import _Job, _Sandbox, _trial, cloud  # noqa: F401

from harbor_prized import _environment


def _finished(job, task, n):
    """Trials a run before this one finished, as Harbor leaves them."""
    import json

    for i in range(n):
        trial_dir = job.dir / f"{task}__done{i}"
        trial_dir.mkdir()
        task_lock = {"task": {"name": task, "digest": f"sha256:{task}"}}
        (trial_dir / "lock.json").write_text(json.dumps(task_lock))
        (trial_dir / "result.json").write_text("{}")


def _disk_quota(cloud, monkeypatch, limit_gb):  # noqa: F811
    """The workspace's retained-disk quota: every sandbox not deleted counts,
    paused parents and running trials alike."""
    create, fork = cloud.create, _Sandbox.fork

    def used():
        return sum(getattr(s, "disk", 0) for s in cloud.alive())

    def refuse():
        return PrizedError("sandbox_quota_exceeded", status=429, reason="retained_disk")

    async def create_(template, **kwargs):
        if used() + kwargs["disk_gb"] > limit_gb:
            raise refuse()
        sandbox = await create(template, **kwargs)
        sandbox.disk = kwargs["disk_gb"]
        return sandbox

    async def fork_(self, count=1, *, labels=None):
        if used() + self.disk > limit_gb:
            self.cloud.fork_calls.append(self.id)
            raise refuse()
        (copy,) = await fork(self, count, labels=labels)
        copy.disk = self.disk
        return [copy]

    monkeypatch.setattr(_environment.AsyncSandbox, "create", create_)
    monkeypatch.setattr(_Sandbox, "fork", fork_)


async def _run_concurrently(job, n, **kwargs):
    environments = [job.environment("a", **kwargs) for _ in range(n)]
    return await asyncio.gather(
        *(_trial(e) for e in environments), return_exceptions=True
    )


async def test_fresh_mode_fits_three_trials_in_a_100_gb_quota(
    tmp_path,
    cloud,  # noqa: F811
    monkeypatch,
):
    # Baseline: 3 concurrent 30 GB trials fit a 100 GB disk quota (free tier).
    _disk_quota(cloud, monkeypatch, 100)
    job = _Job(tmp_path, ["a"] * 4)
    results = await _run_concurrently(job, 3, fork_mode=False)
    assert not [r for r in results if isinstance(r, BaseException)]


async def test_fork_mode_runs_every_trial_fresh_mode_runs_under_a_disk_quota(
    tmp_path,
    cloud,  # noqa: F811
    monkeypatch,
):
    # Same job in fork mode: the paused parent's 30 GB pushes the third copy
    # over the quota. The parent must make way so the fresh start fits.
    _disk_quota(cloud, monkeypatch, 100)
    job = _Job(tmp_path, ["a"] * 4)
    results = await _run_concurrently(job, 3)
    failed = [r for r in results if isinstance(r, BaseException)]
    assert not failed, f"fork mode failed {len(failed)} trial(s): {failed!r}"


async def test_a_resumed_job_builds_no_parent_for_a_task_with_one_trial_left(
    tmp_path,
    cloud,  # noqa: F811
):
    # `harbor job resume`: the job's lock.json lists every trial, the two that
    # already finished too. Only one trial of a is left to run, so a parent
    # can only cost time (and disk until the job ends).
    job = _Job(tmp_path, ["a", "a", "a"])
    _finished(job, "a", 2)
    environment = await _trial(job.environment("a"))
    await environment.stop(delete=True)
    assert not cloud.parents() or all(p.state == "deleted" for p in cloud.parents()), [
        (p.id, p.state) for p in cloud.parents()
    ]


async def test_a_resumed_job_keeps_its_budget_for_tasks_that_need_it(
    tmp_path,
    cloud,  # noqa: F811
):
    # Resume of a TaskMajor job whose a1, a2 and b1 finished: a3, b2 and b3
    # are left. Room for one parent: b's (needed twice) should get it.
    job = _Job(tmp_path, ["a", "a", "a", "b", "b", "b"])
    _finished(job, "a", 2)
    _finished(job, "b", 1)
    for task in ["a", "b", "b"]:
        environment = job.environment(task, fork_disk_budget_gb=30)
        await (await _trial(environment)).stop(delete=True)
    built = [p.labels["harbor-task"] for p in cloud.parents()]
    assert built == ["b"], f"parents built for {built}; {len(cloud.fresh())} fresh"


async def test_a_retried_trial_does_not_cost_a_later_trial_its_fork(
    tmp_path,
    cloud,  # noqa: F811
):
    # Harbor's --max-retries: a failed trial runs again in the same trial dir,
    # with a new environment. Three trials planned, the first retried once.
    job = _Job(tmp_path, ["a", "a", "a"])
    first = await _trial(job.environment("a"))
    await first.stop(delete=True)
    retry = job.environment("a")
    retry.trial_paths = first.trial_paths  # the same trial, its second attempt
    await (await _trial(retry)).stop(delete=True)
    for _ in range(2):
        await (await _trial(job.environment("a"))).stop(delete=True)
    assert not cloud.fresh(), f"{len(cloud.fresh())} of 4 starts were fresh"
