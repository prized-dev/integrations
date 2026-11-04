"""Fork mode (``--ek fork_mode=true``) against a stand-in for the Prized API.

``_Cloud`` keeps every sandbox it made. A fork copies its parent's command
history and files as they were at the pause, so a test can see what each
copy started from and what each trial did in it.
"""

import asyncio
import json
from pathlib import Path

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from prized import ExecResult, PrizedError

from harbor_prized import PrizedEnvironment, TaskMajor, _fork
from harbor_prized import _environment as _module

CRANE_CONFIG = json.dumps(
    {"config": {"Env": ["A=1", "PATH=/usr/bin"], "WorkingDir": "/w", "User": ""}}
)
IDENTITY = "set -e; " + _module._IDENTITY


class _Files:
    def __init__(self, sandbox, content=None):
        self._sandbox = sandbox
        self.content = dict(content or {})

    async def write(self, path, data, mode=None):
        self._sandbox.check_running()
        self.content[path] = data

    async def read_bytes(self, path):
        return self.content.get(path, b"")


class _Sandbox:
    def __init__(self, cloud, labels, *, history=(), files=None, parent=None):
        cloud.count += 1
        self.id = f"sbx_{cloud.count}"
        self.cloud, self.labels, self.parent = cloud, dict(labels), parent
        self.state = "running"
        self.history = list(history)
        self.files = _Files(self, files)
        self.closed = False
        self.delete_waits: list[bool] = []
        cloud.sandboxes.append(self)

    def check_running(self):
        if self.state != "running":
            raise PrizedError("sandbox_not_running", f"{self.id} is {self.state}")

    async def exec(self, cmd, timeout=None, full_output=False):
        self.check_running()
        self.history.append(cmd)
        answer = self.cloud.answer(self, cmd)
        if answer is not None:
            return answer
        if "crane config" in cmd:
            return ExecResult(stdout=CRANE_CONFIG, stderr="", exit_code=0)
        return ExecResult(stdout="/w\n", stderr="", exit_code=0)

    async def pause(self):
        if self.cloud.pause_error is not None:
            raise self.cloud.pause_error
        self.check_running()
        self.state = "paused"

    async def fork(self, count=1, *, labels=None):
        assert count == 1 and self.state == "paused"
        self.cloud.fork_calls.append(self.id)
        gate = self.cloud.fork_gate
        if gate is not None:
            await gate.wait()
        if self.cloud.fork_errors:
            raise self.cloud.fork_errors.pop(0)
        if self.state != "paused":
            raise PrizedError("not_found", status=404)
        self.cloud.admit()
        copy = _Sandbox(
            self.cloud,
            labels if labels is not None else self.labels,
            history=self.history,
            files=self.files.content,
            parent=self,
        )
        return [copy]

    async def delete(self, wait=False):
        self.delete_waits.append(wait)
        self.state = "deleted"

    async def keep_alive(self):
        pass

    async def close(self):
        self.closed = True


class _Cloud:
    def __init__(self):
        self.count = 0
        self.sandboxes: list[_Sandbox] = []
        self.create_errors: list[Exception] = []
        self.fork_errors: list[Exception] = []
        self.fork_calls: list[str] = []
        self.fork_gate: asyncio.Event | None = None
        self.pause_error: Exception | None = None
        self.answers = []  # (predicate, ExecResult or exception)
        self.quota_gb: int | None = (
            None  # disk of every sandbox not deleted, 30 GB each
        )

    def admit(self):
        alive = [s for s in self.sandboxes if s.state != "deleted"]
        if self.quota_gb is not None and 30 * (len(alive) + 1) > self.quota_gb:
            raise PrizedError(
                "sandbox_quota_exceeded", status=429, reason="retained_disk"
            )

    def answer(self, sandbox, cmd):
        for predicate, result in self.answers:
            if predicate(sandbox, cmd):
                if isinstance(result, BaseException):
                    raise result
                return result
        return None

    async def create(self, template, **kwargs):
        if self.create_errors:
            raise self.create_errors.pop(0)
        self.admit()
        return _Sandbox(self, kwargs["labels"])

    def parents(self):
        return [s for s in self.sandboxes if _fork.PARENT_LABEL in s.labels]

    def copies(self):
        return [s for s in self.sandboxes if s.parent is not None]

    def fresh(self):
        return [
            s
            for s in self.sandboxes
            if s.parent is None and _fork.PARENT_LABEL not in s.labels
        ]

    def alive(self):
        return [s for s in self.sandboxes if s.state != "deleted"]


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.delenv("PRIZED_API_KEY", raising=False)
    monkeypatch.setattr(_fork, "_CACHE", None)
    monkeypatch.setattr(_fork, "WAIT_FULL_SEC", 0)
    monkeypatch.setattr(_fork, "WAIT_SETTLE_SEC", 0)
    builds = []
    monkeypatch.setattr(_fork, "note_build", lambda: builds.append(1))
    the_cloud = _Cloud()
    monkeypatch.setattr(_module.AsyncSandbox, "create", the_cloud.create)
    return the_cloud


class _Job:
    """A job dir with Harbor's plan (lock.json) and one trial dir per environment."""

    def __init__(self, root: Path, plan: list[str] | None):
        self.dir = root / "job"
        self.dir.mkdir()
        if plan is not None:
            trials = [{"task": {"name": t, "digest": f"sha256:{t}"}} for t in plan]
            (self.dir / "lock.json").write_text(json.dumps({"trials": trials}))
        self.n = 0

    def environment(
        self, task: str, name: str | None = None, **kwargs
    ) -> PrizedEnvironment:
        self.n += 1
        trial_dir = self.dir / (name or f"{task}__{self.n}")
        paths = TrialPaths(trial_dir=trial_dir)
        paths.mkdir()  # a retried trial gets its old name
        (trial_dir / "lock.json").write_text(
            json.dumps({"task": {"name": task, "digest": f"sha256:{task}"}})
        )
        environment_dir = self.dir.parent / f"environment-{task}"
        if not environment_dir.exists():
            environment_dir.mkdir()
            (environment_dir / "data.txt").write_text(task)
        kwargs.setdefault("fork_mode", True)
        kwargs.setdefault("fork_disk_budget_gb", 120)
        config = kwargs.pop("config", {"cpus": 1, "memory_mb": 2048})
        return PrizedEnvironment(
            environment_dir=environment_dir,
            environment_name=task,
            session_id=f"{task}__{self.n}__env",
            trial_paths=paths,
            task_env_config=EnvironmentConfig(docker_image=f"img/{task}:1", **config),
            labels="run=t",
            **kwargs,
        )


async def _trial(environment: PrizedEnvironment, command: str | None = None):
    # Bounded, so a regression fails the test instead of hanging it.
    await asyncio.wait_for(environment.start(force_build=False), 10)
    if command is not None:
        await environment.exec(command)
    return environment


async def _until(condition) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), 10)


# -- options --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        ("true", True),
        (1, True),
        ("yes", True),
        (False, False),
        ("off", False),
        (0, False),
    ],
)
def test_fork_mode_takes_yes_or_no(tmp_path, value, expected):
    environment = _Job(tmp_path, None).environment("a", fork_mode=value)
    assert environment._fork_mode is expected


@pytest.mark.parametrize("value", ["maybe", 2, 1.5, ["x"]])
def test_fork_mode_refuses_anything_else(tmp_path, value):
    with pytest.raises(TypeError, match="fork_mode takes true or false"):
        _Job(tmp_path, None).environment("a", fork_mode=value)


async def test_off_by_default_every_trial_starts_fresh(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a"])
    for _ in range(2):
        environment = await _trial(job.environment("a", fork_mode=False))
        await environment.stop(delete=True)
    assert len(cloud.fresh()) == 2 and not cloud.parents() and not cloud.fork_calls


# -- the lifecycle ----------------------------------------------------------


async def test_attempts_fork_one_parent_that_goes_after_the_last(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a", "a"])
    environments = []
    for _ in range(3):
        environments.append(await _trial(job.environment("a")))
        assert len(cloud.parents()) == 1
    (parent,) = cloud.parents()
    assert parent.labels[_fork.RUN_LABEL] == _fork.RUN_ID
    assert parent.labels["harbor"] == "1" and parent.labels["run"] == "t"
    assert parent.labels["harbor-task"] == "a"
    assert parent.state == "deleted"  # the 3rd of 3 planned has its copy
    assert not cloud.fresh() and len(cloud.copies()) == 3
    for environment, copy in zip(environments, cloud.copies()):
        assert environment._sandbox is copy
        assert copy.labels["harbor-session"] == environment.session_id
        assert copy.labels[_fork.COPY_LABEL] == parent.id
        assert _fork.PARENT_LABEL not in copy.labels
        assert copy.history[-1] == IDENTITY  # its own hostname and resolver
        assert environment._image_env == {"A": "1", "PATH": "/usr/bin"}
        assert environment._image_workdir == "/w"
        await environment.stop(delete=True)
    assert cloud.alive() == []


async def test_trials_that_start_together_share_one_build(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a", "a"])
    environments = [job.environment("a") for _ in range(3)]
    await asyncio.gather(*(_trial(e) for e in environments))
    assert len(cloud.parents()) == 1 and len(cloud.copies()) == 3
    assert cloud.parents()[0].state == "deleted"
    for environment in environments:
        await environment.stop(delete=True)
    assert cloud.alive() == []


async def test_a_task_planned_once_starts_fresh(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "b"])
    for task in ("a", "b"):
        await (await _trial(job.environment(task))).stop(delete=True)
    assert len(cloud.fresh()) == 2 and not cloud.parents() and not cloud.fork_calls


async def test_no_state_leaks_between_attempts(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a", "a"])
    first = await _trial(job.environment("a"), "echo one > /tmp/x")
    second = await _trial(job.environment("a"), "cat /tmp/x")
    (parent,) = cloud.parents()
    one, two = first._sandbox, second._sandbox
    setup = parent.history
    # Both copies start from exactly the parent's post-start state...
    assert one.history[: len(setup)] == setup == two.history[: len(setup)]
    assert one.history[len(setup)] == two.history[len(setup)] == IDENTITY
    # ...and each sees only its own trial's commands; the parent ran none.
    assert any("echo one" in _decoded(c) for c in one.history)
    assert not any("echo one" in _decoded(c) for c in two.history)
    assert not any(
        "echo one" in _decoded(c) or "cat /tmp/x" in _decoded(c) for c in setup
    )
    assert one.files.content == two.files.content == parent.files.content


def _decoded(command: str) -> str:
    import base64

    if " - " not in command:
        return command
    try:
        return base64.b64decode(command.rsplit(" ", 1)[1]).decode()
    except ValueError:
        return command


async def test_the_parent_holds_no_pull_credentials(tmp_path, cloud, monkeypatch):
    monkeypatch.setenv("DOCKERHUB_USERNAME", "me")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "secret-token")
    job = _Job(tmp_path, ["a", "a"])
    environment = job.environment("a", registry_auth="env")
    environment.task_env_config.docker_image = "docker.io/me/a:1"
    await _trial(environment)
    (parent,) = cloud.parents()
    auth = _module._PULL_AUTH_DIR
    removal = [c for c in parent.history if c.startswith(f"rm -rf {auth} && ! test -e")]
    assert len(removal) == 1
    assert (
        parent.history.index(removal[0]) == len(parent.history) - 1
    )  # last, then paused
    assert parent.state == "paused"
    # The copy was made after the removal, and never written credentials itself.
    assert removal[0] in environment._sandbox.history


# -- the disk budget --------------------------------------------------------


async def test_attempt_major_keeps_only_the_parents_that_fit(tmp_path, cloud):
    # Harbor's own order, room for one 30 GB parent: a's is kept for its
    # later attempts; b and c, needed later than a, start fresh.
    job = _Job(tmp_path, ["a", "b", "c", "a", "b", "c", "a", "b", "c"])
    for task in ["a", "b", "c"] * 3:
        environment = await _trial(job.environment(task, fork_disk_budget_gb=30))
        await environment.stop(delete=True)
    (parent,) = cloud.parents()
    assert parent.labels["harbor-task"] == "a" and parent.state == "deleted"
    assert len(cloud.copies()) == 3 and len(cloud.fresh()) == 6
    assert cloud.alive() == []


async def test_a_task_needed_sooner_evicts_the_farthest_parent(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "b", "b", "a"])
    for task in ["a", "b", "b", "a"]:
        environment = await _trial(job.environment(task, fork_disk_budget_gb=30))
        await environment.stop(delete=True)
    a, b = cloud.parents()
    assert (a.labels["harbor-task"], b.labels["harbor-task"]) == ("a", "b")
    assert a.state == b.state == "deleted"
    # a's parent went to make room for b (needed at 1 and 2, a only at 3);
    # a's last trial then had no later one to build a parent for.
    assert [c.parent for c in cloud.copies()] == [a, b, b]
    assert a.delete_waits == [True]  # gone before b's parent counts toward the quota
    assert len(cloud.fresh()) == 1 and cloud.alive() == []


async def test_without_a_plan_the_least_recently_used_parent_goes(tmp_path, cloud):
    job = _Job(tmp_path, None)
    for task in ["a", "b", "a"]:
        environment = await _trial(job.environment(task, fork_disk_budget_gb=30))
        await environment.stop(delete=True)
    a1, b, a2 = cloud.parents()
    assert (a1.state, b.state, a2.state) == ("deleted", "deleted", "paused")
    assert not cloud.fresh()
    await _fork.close_parents()  # the job's end (TaskMajor), else the exit sweep
    assert cloud.alive() == []


async def test_a_budget_too_small_for_one_parent_starts_fresh(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a"])
    for _ in range(2):
        await _trial(job.environment("a", fork_disk_budget_gb=29))
    assert len(cloud.fresh()) == 2 and not cloud.parents()


async def test_a_parent_in_use_is_never_evicted(tmp_path, cloud):
    # b is needed sooner than a, but a's parent is mid-fork.
    job = _Job(tmp_path, ["a", "b", "b", "a"])
    cloud.fork_gate = asyncio.Event()
    a1 = asyncio.ensure_future(_trial(job.environment("a", fork_disk_budget_gb=30)))
    await _until(lambda: cloud.fork_calls)
    # a's parent is mid-fork: b gets no room, and a's parent stays.
    await _trial(job.environment("b", fork_disk_budget_gb=30))
    cloud.fork_gate.set()
    await a1
    (parent,) = cloud.parents()
    assert parent.state == "paused" and len(cloud.fresh()) == 1


# -- refusals and fallbacks -------------------------------------------------


@pytest.mark.parametrize(
    ("errors", "forks", "fresh", "parent_after"),
    [
        ([PrizedError("fork_capacity", status=409)], 2, 0, "paused"),
        ([PrizedError("parent_not_running", status=409)], 2, 0, "paused"),
        ([PrizedError("sandbox_error", "copy failed")], 2, 0, "paused"),
        ([PrizedError("fork_capacity", status=409)] * 2, 2, 1, "paused"),
        ([PrizedError("fork_unsupported_host", status=409)], 1, 1, "deleted"),
        (
            [PrizedError("parent_not_forkable", status=409, reason="memory_discarded")],
            1,
            1,
            "deleted",
        ),
        ([PrizedError("not_found", status=404)], 1, 1, "deleted"),
        ([PrizedError("sandbox_capacity", status=503)], 1, 1, "paused"),
        ([PrizedError("timeout")], 1, 1, "paused"),
    ],
)
async def test_a_refused_fork_starts_the_trial_fresh(
    tmp_path, cloud, errors, forks, fresh, parent_after
):
    job = _Job(tmp_path, ["a", "a", "a"])
    cloud.fork_errors = list(errors)
    environment = await _trial(job.environment("a"))
    assert len(cloud.fork_calls) == forks and len(cloud.fresh()) == fresh
    (parent,) = cloud.parents()
    assert parent.state == parent_after
    assert environment._sandbox.state == "running"
    assert environment._image_env["A"] == "1"
    await environment.stop(delete=True)


async def test_a_shape_too_big_to_fork_never_builds_a_parent(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a"])
    big = {"cpus": 8, "memory_mb": 32768}  # past about 26 GB: parent_too_big
    for _ in range(2):
        await (await _trial(job.environment("a", config=big))).stop(delete=True)
    assert len(cloud.fresh()) == 2 and not cloud.parents()
    assert _fork.forkable(1, 26 * 1024) and not _fork.forkable(16, 30 * 1024)
    assert _fork.forkable(32, 100 * 1024) and not _fork.forkable(32, 110 * 1024)


async def test_trials_refused_by_one_parent_count_as_one_failure(tmp_path, cloud):
    job = _Job(tmp_path, ["a"] * 5)
    cloud.fork_gate = asyncio.Event()
    trials = [asyncio.ensure_future(_trial(job.environment("a"))) for _ in range(3)]
    await _until(lambda: len(cloud.fork_calls) == 3)
    cloud.fork_errors = [PrizedError("fork_unsupported_host", status=409)] * 3
    cloud.fork_gate.set()
    for environment in await asyncio.gather(*trials):
        await environment.stop(delete=True)
    # One parent failed, not three: the task's next trial builds another.
    await (await _trial(job.environment("a"))).stop(delete=True)
    assert len(cloud.parents()) == 2 and cloud.parents()[1].labels["harbor-task"] == "a"
    assert len(cloud.copies()) == 1


async def test_the_parent_is_idle_while_a_copy_is_set_up(tmp_path, cloud, monkeypatch):
    job = _Job(tmp_path, ["a", "a", "a"])
    setting_up = asyncio.Event()
    original = _Sandbox.exec

    async def exec_(self, cmd, timeout=None, full_output=False):
        if self.parent is not None and cmd == IDENTITY:
            setting_up.set()
            await asyncio.sleep(0.05)
        return await original(self, cmd, timeout, full_output)

    monkeypatch.setattr(_Sandbox, "exec", exec_)
    trial = asyncio.ensure_future(_trial(job.environment("a")))
    await setting_up.wait()
    (parent,) = _fork._CACHE.parents()
    assert parent.leases == 0  # it could make way now
    await trial


async def test_a_parent_too_big_to_fork_keeps_its_task_fresh(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a", "a"])
    cloud.fork_errors = [
        PrizedError("parent_not_forkable", status=409, reason="parent_too_big")
    ]
    for _ in range(3):
        await (await _trial(job.environment("a"))).stop(delete=True)
    assert len(cloud.parents()) == 1 and len(cloud.fresh()) == 3
    assert cloud.alive() == []


async def test_a_task_whose_fork_fails_twice_stays_fresh(tmp_path, cloud):
    job = _Job(tmp_path, ["a"] * 4)
    cloud.fork_errors = [PrizedError("fork_unsupported_host", status=409)] * 2
    for _ in range(4):
        await (await _trial(job.environment("a"))).stop(delete=True)
    assert len(cloud.parents()) == 2  # rebuilt once after the first refusal
    assert len(cloud.fresh()) == 4 and cloud.alive() == []


async def test_a_quota_refused_fork_evicts_an_idle_parent_first(tmp_path, cloud):
    job = _Job(tmp_path, ["b", "a", "a", "b"])
    await (await _trial(job.environment("b"))).stop(delete=True)
    cloud.fork_errors = [
        PrizedError("sandbox_quota_exceeded", status=429, reason="retained_disk")
    ]
    environment = await _trial(job.environment("a"))
    b, a = cloud.parents()
    assert b.state == "deleted" and b.delete_waits == [True]  # gone before the retry
    assert environment._sandbox.parent is a and not cloud.fresh()
    assert a.state == "deleted"  # parents make way: no more for this run
    await (await _trial(job.environment("a"))).stop(delete=True)
    assert len(cloud.parents()) == 2 and len(cloud.fresh()) == 1


async def test_a_quota_refused_parent_shrinks_the_budget(tmp_path, cloud):
    job = _Job(tmp_path, ["b", "a", "a", "c", "c", "b"])
    await (await _trial(job.environment("b"))).stop(delete=True)
    cloud.create_errors = [
        PrizedError("sandbox_quota_exceeded", status=429, reason="retained_disk")
    ]
    await (await _trial(job.environment("a"))).stop(delete=True)
    (b,) = cloud.parents()
    assert b.state == "deleted"  # evicted, so the fresh start could fit
    assert len(cloud.fresh()) == 1
    for task in ["a", "c", "c"]:
        await (await _trial(job.environment(task))).stop(delete=True)
    assert len(cloud.parents()) == 1 and len(cloud.fresh()) == 4  # budget is now 0


async def test_parents_make_way_when_the_disk_quota_is_reached(tmp_path, cloud):
    # Room for two 30 GB sandboxes, as for two fresh trials at once; in fork
    # mode the parent is a third. No trial may fail for it.
    cloud.quota_gb = 60
    job = _Job(tmp_path, ["a", "a", "a"])
    first = await _trial(job.environment("a"))  # the parent and its copy: 60 GB
    second = await _trial(job.environment("a"))  # its copy is refused
    (parent,) = cloud.parents()
    assert parent.state == "deleted" and parent.delete_waits == [True]
    assert second._sandbox in cloud.fresh() and second._sandbox.state == "running"
    await first.stop(delete=True)
    third = await _trial(job.environment("a"))  # no more parents this run
    assert third._sandbox in cloud.fresh() and len(cloud.parents()) == 1


async def test_a_fresh_start_refused_by_the_quota_deletes_idle_parents(tmp_path, cloud):
    cloud.quota_gb = 60
    job = _Job(tmp_path, ["a", "b", "a"])
    await _trial(job.environment("a"))  # a's parent (idle now) and copy: 60 GB
    b = await _trial(job.environment("b"))  # b is planned once: fresh, refused
    (parent,) = cloud.parents()
    assert parent.state == "deleted" and parent.delete_waits == [True]
    assert b._sandbox in cloud.fresh() and b._sandbox.state == "running"


async def test_a_refused_fresh_start_waits_for_a_parent_in_use(tmp_path, cloud):
    # 30 GB of room: either a or b could run fresh, not both. b is refused
    # while a's parent is mid-fork, and waits for it to go, instead of
    # failing next to a parent that is about to be deleted.
    cloud.quota_gb = 60
    _Sandbox(cloud, {"other": "1"})  # 30 GB some other run holds
    job = _Job(tmp_path, ["a", "a", "b"])
    cloud.fork_gate = asyncio.Event()
    a = asyncio.ensure_future(_trial(job.environment("a")))
    await _until(lambda: cloud.fork_calls)  # a's parent is up, its fork waiting
    b = asyncio.ensure_future(_trial(job.environment("b")))  # fresh, refused
    await asyncio.sleep(0.05)
    assert not b.done()  # waiting for a's parent to go
    cloud.fork_gate.set()  # a's copy is refused too; its parent goes
    results = await asyncio.gather(a, b, return_exceptions=True)
    started = [r for r in results if isinstance(r, PrizedEnvironment)]
    refused = [r for r in results if isinstance(r, RuntimeError)]
    assert len(started) == 1 and len(refused) == 1, results  # as fresh mode
    assert "quota" in str(refused[0])
    assert [p.state for p in cloud.parents()] == ["deleted"]


async def test_a_resumed_job_plans_only_the_trials_left(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a", "b", "b"])
    done = job.environment("a", fork_mode=False)
    (done.trial_paths.trial_dir / "result.json").write_text("{}")
    await (await _trial(job.environment("a"))).stop(delete=True)  # a's last trial
    await (await _trial(job.environment("b"))).stop(delete=True)
    await (await _trial(job.environment("b"))).stop(delete=True)
    (parent,) = cloud.parents()
    assert parent.labels["harbor-task"] == "b" and parent.state == "deleted"
    assert len(cloud.fresh()) == 1 and cloud.alive() == []


async def test_a_retried_trial_counts_once(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a"])
    await (await _trial(job.environment("a", name="a__x"))).stop(delete=True)
    await (await _trial(job.environment("a", name="a__x"))).stop(delete=True)  # retry
    (parent,) = cloud.parents()
    assert parent.state == "paused"  # a's second planned trial is still to come
    await (await _trial(job.environment("a", name="a__y"))).stop(delete=True)
    assert parent.state == "deleted" and cloud.alive() == []


async def test_a_failed_pull_fails_the_trial_once_and_waiters_start_fresh(
    tmp_path, cloud, monkeypatch
):
    job = _Job(tmp_path, ["a", "a"])
    pulling, release = asyncio.Event(), asyncio.Event()
    original = _Sandbox.exec

    async def exec_(self, cmd, timeout=None, full_output=False):
        if "crane export" in cmd and _fork.PARENT_LABEL in self.labels:
            pulling.set()
            await release.wait()
            return ExecResult(stdout="", stderr="MANIFEST_UNKNOWN", exit_code=1)
        return await original(self, cmd, timeout, full_output)

    monkeypatch.setattr(_Sandbox, "exec", exec_)
    builder = asyncio.ensure_future(_trial(job.environment("a")))
    await pulling.wait()
    waiter = asyncio.ensure_future(_trial(job.environment("a")))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RuntimeError, match="MANIFEST_UNKNOWN"):
        await builder
    waiting = await waiter
    (parent,) = cloud.parents()
    assert parent.state == "deleted" and not cloud.fork_calls
    assert waiting._sandbox in cloud.fresh()


async def test_a_parent_that_wont_pause_is_deleted_and_the_trial_starts_fresh(
    tmp_path, cloud
):
    job = _Job(tmp_path, ["a", "a"])
    cloud.pause_error = PrizedError("sandbox_error", "pause failed")
    environment = await _trial(job.environment("a"))
    (parent,) = cloud.parents()
    assert parent.state == "deleted" and environment._sandbox in cloud.fresh()


async def test_a_copy_that_cant_be_set_up_is_deleted(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a"])
    cloud.answers.append(
        (
            lambda s, c: s.parent is not None and c == IDENTITY,
            ExecResult(stdout="", stderr="read-only", exit_code=1),
        )
    )
    environment = await _trial(job.environment("a"))
    (copy,) = cloud.copies()
    assert copy.state == "deleted" and environment._sandbox in cloud.fresh()


# -- cancellation and cleanup -----------------------------------------------


async def test_a_cancelled_build_deletes_the_parent_and_waiters_start_fresh(
    tmp_path, cloud, monkeypatch
):
    job = _Job(tmp_path, ["a", "a"])
    pulling = asyncio.Event()
    original = _Sandbox.exec

    async def exec_(self, cmd, timeout=None, full_output=False):
        if "crane export" in cmd and _fork.PARENT_LABEL in self.labels:
            pulling.set()
            await asyncio.sleep(3600)
        return await original(self, cmd, timeout, full_output)

    monkeypatch.setattr(_Sandbox, "exec", exec_)
    builder = asyncio.ensure_future(_trial(job.environment("a")))
    await pulling.wait()
    waiter = asyncio.ensure_future(_trial(job.environment("a")))
    await asyncio.sleep(0)
    builder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await builder
    monkeypatch.setattr(_Sandbox, "exec", original)
    waiting = await waiter
    (parent,) = cloud.parents()
    assert parent.state == "deleted" and parent.closed
    assert waiting._sandbox in cloud.fresh() and not cloud.fork_calls


async def test_a_waiter_cancelled_while_waiting_leaves_the_build_alone(
    tmp_path, cloud, monkeypatch
):
    job = _Job(tmp_path, ["a", "a", "a"])
    pulling, release = asyncio.Event(), asyncio.Event()
    original = _Sandbox.exec

    async def exec_(self, cmd, timeout=None, full_output=False):
        if "crane export" in cmd and _fork.PARENT_LABEL in self.labels:
            pulling.set()
            await release.wait()
        return await original(self, cmd, timeout, full_output)

    monkeypatch.setattr(_Sandbox, "exec", exec_)
    builder = asyncio.ensure_future(_trial(job.environment("a")))
    await pulling.wait()
    waiter = asyncio.ensure_future(_trial(job.environment("a")))
    (building,) = _fork._CACHE.parents()
    await _until(lambda: building.leases == 2)  # the waiter holds its lease, waiting
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert building.leases == 1 and not building.ready.done()
    release.set()
    await builder
    (parent,) = cloud.parents()
    assert parent.state == "paused" and len(cloud.copies()) == 1
    await (await _trial(job.environment("a"))).stop(delete=True)
    assert parent.state == "deleted"  # the 3rd planned trial had its copy


async def test_a_cancelled_copy_setup_deletes_the_copy(tmp_path, cloud, monkeypatch):
    job = _Job(tmp_path, ["a", "a"])
    started = asyncio.Event()
    original = _Sandbox.exec

    async def exec_(self, cmd, timeout=None, full_output=False):
        if self.parent is not None and cmd == IDENTITY:
            started.set()
            await asyncio.sleep(3600)
        return await original(self, cmd, timeout, full_output)

    monkeypatch.setattr(_Sandbox, "exec", exec_)
    trial = asyncio.ensure_future(_trial(job.environment("a")))
    await started.wait()
    trial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trial
    (copy,) = cloud.copies()
    assert copy.state == "deleted" and copy.closed
    (parent,) = cloud.parents()
    assert parent.state == "paused"  # a later trial is planned; it is swept at exit


async def test_the_job_end_deletes_the_parents_left(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a", "b", "b"])
    await _trial(job.environment("a"))
    await _trial(job.environment("b"))
    assert [p.state for p in cloud.parents()] == ["paused", "paused"]
    await TaskMajor().on_job_end(None)
    assert [p.state for p in cloud.parents()] == ["deleted", "deleted"]
    # A new job in the same process starts with a new cache.
    await _trial(job.environment("a"))
    assert len(cloud.parents()) == 3


async def test_a_parent_finished_after_the_job_end_is_deleted(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a"])
    cache = _fork.cache(120)
    trial = asyncio.ensure_future(_trial(job.environment("a")))
    await _until(lambda: cloud.sandboxes)  # the parent is being built
    cache.close()
    environment = await trial
    (parent,) = cloud.parents()
    assert parent.state == "deleted" and environment._sandbox in cloud.fresh()


def test_the_exit_sweep_deletes_this_process_parents_by_label(monkeypatch):
    seen = []
    monkeypatch.setattr(
        _fork.Sandbox, "delete_by_labels", lambda labels: seen.append(labels) or ["x"]
    )
    _fork.sweep()
    assert seen == [{_fork.PARENT_LABEL: "1", _fork.RUN_LABEL: _fork.RUN_ID}]

    def refuse(labels):
        raise PrizedError("missing_api_key")

    monkeypatch.setattr(_fork.Sandbox, "delete_by_labels", refuse)
    _fork.sweep()  # says so, never raises at exit


def test_the_sweep_is_registered_once(monkeypatch):
    registered = []
    monkeypatch.setattr(_fork.atexit, "register", registered.append)
    monkeypatch.setattr(_fork, "_SWEEP", {"registered": False})
    _fork.note_build()
    _fork.note_build()
    assert registered == [_fork.sweep]


# -- keys, plans and order --------------------------------------------------


def test_the_key_is_the_same_for_a_task_and_differs_across_tasks(tmp_path):
    job = _Job(tmp_path, None)
    a1, a2, b = job.environment("a"), job.environment("a"), job.environment("b")
    key = a1._fork_key("img/a:1", "sha256:a")
    assert a2._fork_key("img/a:1", "sha256:a") == key
    assert b._fork_key("img/b:1", "sha256:b") != key
    assert a1._fork_key("img/a:2", "sha256:a") != key  # environment_id omits the image
    bigger = job.environment("a", config={"cpus": 2, "memory_mb": 2048})
    assert bigger._fork_key("img/a:1", "sha256:a") != key


def test_the_plan_reads_harbor_job_lock(tmp_path):
    job = _Job(tmp_path, ["a", "b", "a"])
    plan = _fork.Plan.load(job.dir)
    assert plan is not None and plan.knows("sha256:a") and not plan.knows("sha256:z")
    assert (plan.use_after("sha256:a", 1), plan.use_after("sha256:a", 2)) == (2, None)
    (job.dir / "lock.json").write_text("{not json")
    assert _fork.Plan.load(job.dir) is None
    assert _fork.Plan.load(tmp_path / "nowhere") is None
    assert _fork.task_of(tmp_path / "nowhere") is None


async def test_a_second_start_of_the_same_trial_counts_once(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a"])
    cloud.create_errors = [PrizedError("network_error")]  # start() retries once
    first = await _trial(job.environment("a"))
    second = await _trial(job.environment("a"))
    (parent,) = cloud.parents()
    assert second._sandbox.parent is parent and parent.state == "deleted"
    assert first._sandbox.parent is parent


class _Task:
    def __init__(self, name):
        self.name = name

    def model_dump_json(self):
        return json.dumps({"name": self.name})


class _TrialConfig:
    def __init__(self, task, attempt):
        self.task, self.attempt = _Task(task), attempt


async def test_task_major_runs_every_attempt_of_a_task_first():
    trials = [_TrialConfig(t, n) for n in range(3) for t in "abc"]  # Harbor's order

    class Job:
        def __init__(self):
            self._trial_configs = trials
            self._remaining_trial_configs = [
                t for t in trials if (t.task.name, t.attempt) != ("b", 0)
            ]

    job = Job()
    await TaskMajor().on_job_start(job)
    assert [(t.task.name, t.attempt) for t in job._trial_configs] == [
        (t, n) for t in "abc" for n in range(3)
    ]
    assert [(t.task.name, t.attempt) for t in job._remaining_trial_configs] == [
        ("a", 0),
        ("a", 1),
        ("a", 2),
        ("b", 1),
        ("b", 2),
        ("c", 0),
        ("c", 1),
        ("c", 2),
    ]


async def test_task_major_leaves_an_unknown_job_alone(caplog):
    class Job:
        pass

    job = Job()
    await TaskMajor().on_job_start(job)
    assert "Harbor's own order" in caplog.text
    with pytest.raises(TypeError, match="takes no options"):
        TaskMajor(order="x")
