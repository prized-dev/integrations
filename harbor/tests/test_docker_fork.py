"""Docker mode with fork mode: build once per environment, fork per trial.

``_Box`` is a Prized sandbox with the simulated dockerd of ``test_docker``:
it keeps the images ``compose build`` and ``compose pull`` made, and a fork
copies its parent's files, images and networks (a parent never has a
container). ``compose up --no-build`` fails, as Compose does, when the image
isn't there; a plain ``up`` builds again (Harbor's ``pull_policy: build``),
which the tests count as a build.
"""

import asyncio
import base64
import json
import shlex
from pathlib import Path

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from prized import ExecResult, PrizedError
from test_docker import DOCKER, HOME, NO_NETWORK, OK, PUBLIC, FakeDocker
from test_docker import _Files as _DockerFiles

from harbor_prized import PrizedEnvironment, _fork
from harbor_prized import _docker as _docker_module
from harbor_prized import _environment as _module
from harbor_prized._registry import docker_cli_config

PULL_AUTH = f"{HOME}/.pull-auth"
FAKE_TOKEN = "dckr_pat_000000fake"  # an obvious fake, never a real token
UBUNTU = "FROM ubuntu:24.04\nRUN apt-get update\n"
ALPINE = "FROM alpine:3.20\n"


class _Box:
    def __init__(self, cloud, labels, *, parent=None):
        cloud.count += 1
        self.id = f"sbx_d{cloud.count}"
        self.cloud, self.labels, self.parent = cloud, dict(labels), parent
        self.state = "running"
        self.files = _DockerFiles()
        self.docker = FakeDocker(self.files, None)
        self.images: set[str] = set()
        self.execs: list[dict] = []
        self.closed = False
        self.files_at_pause: dict[str, bytes] | None = None
        if parent is not None:
            assert not parent.docker.containers, "a parent never has a container"
            self.files.content = dict(parent.files.content)
            self.images = set(parent.images)
            self.docker.networks = dict(parent.docker.networks)
        cloud.boxes.append(self)

    def check_running(self):
        if self.state != "running":
            raise PrizedError("sandbox_not_running", f"{self.id} is {self.state}")

    def has_credentials(self) -> bool:
        return f"{PULL_AUTH}/config.json" in self.files.content

    async def exec(self, cmd, timeout=None, full_output=False, env=None):
        self.check_running()
        self.execs.append({"cmd": cmd, "env": dict(env or {})})
        cloud = self.cloud
        if cmd.startswith(f"rm -rf {PULL_AUTH}"):
            if cloud.keep_credentials:
                return ExecResult(stdout="", stderr="rm: busy", exit_code=1)
            for path in [p for p in self.files.content if p.startswith(PULL_AUTH)]:
                del self.files.content[path]
            return OK
        if (
            cmd.startswith(f"{DOCKER} info")
            and self.parent is not None
            and cloud.dockerd_dead_in_copies
        ):
            return ExecResult(
                stdout="", stderr="Cannot connect to the Docker daemon", exit_code=1
            )
        if cmd.startswith(f"{DOCKER} compose "):
            argv = shlex.split(cmd)
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
            sub = argv[argv.index(files[-1]) + 1 :]
            built = any(f.endswith("docker-compose-build.yaml") for f in files)
            config = (env or {}).get("DOCKER_CONFIG")
            seen = (self.id, config, self.has_credentials())
            if sub == ["build"]:
                cloud.builds.append(seen)
                if built:
                    self.images.add("harbor-main")
            elif sub[:1] == ["pull"]:
                cloud.pulls.append(seen)
                if not built:
                    self.images.add(env["PREBUILT_IMAGE_NAME"])
            elif sub[:2] == ["up", "-d"]:
                cloud.ups.append((*seen, sub))
                need = "harbor-main" if built else env["PREBUILT_IMAGE_NAME"]
                if "--no-build" in sub and need not in self.images:
                    return ExecResult(
                        stdout="",
                        stderr=f"Error: pull access denied for {need}",
                        exit_code=1,
                    )
                if "--no-build" not in sub and built:
                    cloud.builds.append(seen)  # pull_policy: build
                    self.images.add("harbor-main")
        return self.docker.run(cmd, env) or OK

    async def pause(self):
        self.check_running()
        self.state = "paused"
        self.files_at_pause = dict(self.files.content)

    async def fork(self, count=1, *, labels=None):
        assert count == 1 and self.state == "paused"
        self.cloud.fork_calls.append(self.id)
        if self.cloud.fork_errors:
            raise self.cloud.fork_errors.pop(0)
        return [_Box(self.cloud, labels or self.labels, parent=self)]

    async def delete(self, wait=False):
        self.state = "deleted"

    async def keep_alive(self):
        pass

    async def close(self):
        self.closed = True


class _Cloud:
    def __init__(self):
        self.count = 0
        self.boxes: list[_Box] = []
        self.creates: list[dict] = []
        self.fork_errors: list[Exception] = []
        self.fork_calls: list[str] = []
        self.builds: list[tuple] = []
        self.pulls: list[tuple] = []
        self.ups: list[tuple] = []
        self.dockerd_dead_in_copies = False
        self.keep_credentials = False

    async def create(self, template, **kwargs):
        self.creates.append({"template": template, **kwargs})
        return _Box(self, kwargs["labels"])

    def parents(self):
        return [b for b in self.boxes if _fork.PARENT_LABEL in b.labels]

    def copies(self):
        return [b for b in self.boxes if b.parent is not None]

    def fresh(self):
        return [
            b
            for b in self.boxes
            if b.parent is None and _fork.PARENT_LABEL not in b.labels
        ]

    def alive(self):
        return [b for b in self.boxes if b.state != "deleted"]


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.delenv("PRIZED_API_KEY", raising=False)
    monkeypatch.setattr(_fork, "_CACHE", None)
    monkeypatch.setattr(_fork, "WAIT_FULL_SEC", 0)
    monkeypatch.setattr(_fork, "WAIT_SETTLE_SEC", 0)
    monkeypatch.setattr(_fork, "note_build", lambda: None)
    monkeypatch.setattr(_module, "_docker_refusal", None)
    monkeypatch.setattr(_module, "_network_refused_at", None)
    monkeypatch.setattr(_module, "_WARNED_NO_CREDENTIALS", set())

    async def no_put(sandbox, network):
        raise AssertionError("Docker mode never asks Prized to switch the network")

    monkeypatch.setattr(_module, "put_network", no_put)
    the_cloud = _Cloud()
    monkeypatch.setattr(_module.AsyncSandbox, "create", the_cloud.create)
    return the_cloud


class _Job:
    """A job dir with Harbor's plan (lock.json), and a task's environment
    folder holding its Dockerfile: tasks given the same Dockerfile have the
    same ``environment_id``."""

    def __init__(self, root: Path, plan: list[str] | None):
        self.dir = root / "job"
        self.dir.mkdir()
        if plan is not None:
            trials = [{"task": {"name": t, "digest": f"sha256:{t}"}} for t in plan]
            (self.dir / "lock.json").write_text(json.dumps({"trials": trials}))
        self.n = 0

    def environment(
        self, task: str, dockerfile: str = UBUNTU, **kwargs
    ) -> PrizedEnvironment:
        self.n += 1
        trial_dir = self.dir / f"{task}__{self.n}"
        paths = TrialPaths(trial_dir=trial_dir)
        paths.mkdir()
        (trial_dir / "lock.json").write_text(
            json.dumps({"task": {"name": task, "digest": f"sha256:{task}"}})
        )
        environment_dir = self.dir.parent / f"environment-{task}"
        environment_dir.mkdir(exist_ok=True)
        (environment_dir / "Dockerfile").write_text(dockerfile)
        kwargs.setdefault("fork_mode", True)
        config = kwargs.pop("config", {"cpus": 1, "memory_mb": 2048})
        return PrizedEnvironment(
            environment_dir=environment_dir,
            environment_name=task,
            session_id=f"{task}__{self.n}__env",
            trial_paths=paths,
            task_env_config=EnvironmentConfig(**config),
            labels="run=t",
            **kwargs,
        )


async def _trial(environment: PrizedEnvironment) -> PrizedEnvironment:
    await asyncio.wait_for(environment.start(force_build=False), 10)
    assert environment._docker
    return environment


async def _run(job: _Job, tasks: list[str], **kwargs) -> list[PrizedEnvironment]:
    """Each task's trial in turn, stopped (deleted) as Harbor's finalize does."""
    done = []
    for task in tasks:
        environment = await _trial(job.environment(task, **kwargs))
        await environment.stop(delete=True)
        done.append(environment)
    return done


# -- one build, a fork per trial --------------------------------------------------


async def test_three_trials_of_a_task_build_once_and_fork_three_times(
    tmp_path, cloud, caplog
):
    caplog.set_level("DEBUG")
    job = _Job(tmp_path, ["a", "a", "a"])
    await _run(job, ["a", "a", "a"])
    (parent,) = cloud.parents()
    assert cloud.creates[0]["template"] == "prized-docker-v1"
    # One build and one set of pulls, in the parent; nothing ran there.
    assert [b[0] for b in cloud.builds] == [parent.id]
    assert [p[0] for p in cloud.pulls] == [parent.id]
    assert not parent.docker.containers
    copies = cloud.copies()
    assert len(copies) == 3 and not cloud.fresh()
    for copy in copies:
        assert copy.labels[_fork.COPY_LABEL] == parent.id
        assert _fork.PARENT_LABEL not in copy.labels
        # Up without a build: the parent's image, not a second build.
        (up,) = [u for u in cloud.ups if u[0] == copy.id]
        assert up[3] == ["up", "-d", "--no-build"]
        assert copy.docker.containers  # main is up in the copy
    assert parent.state == "deleted"  # the last planned trial has its copy
    assert cloud.alive() == []
    assert "is the paused parent of environment" in caplog.text
    assert f"forked from {parent.id}" in caplog.text


async def test_tasks_sharing_a_dockerfile_share_one_build(tmp_path, cloud):
    # Three tasks, one Dockerfile (78% of the Hub's Dockerfile tasks share
    # one of 100), one trial each: the second task makes the key shared.
    job = _Job(tmp_path, ["a", "b", "c"])
    await _run(job, ["a", "b", "c"])
    (parent,) = cloud.parents()
    assert parent.labels["harbor-task"] == "b"
    # a was planned once and nothing said the key would be shared: fresh.
    assert [f.labels["harbor-session"] for f in cloud.fresh()] == ["a__1__env"]
    (fresh,) = cloud.fresh()
    # The fresh trial builds (and its up rebuilds from cache); the parent once.
    assert [b[0] for b in cloud.builds if b[0] != fresh.id] == [parent.id]
    assert len(cloud.copies()) == 2
    # Shared keys are kept for the next task; the job's end deletes them.
    assert parent.state == "paused"
    await _fork.close_parents()
    assert cloud.alive() == []


async def test_attempts_of_tasks_sharing_a_dockerfile_all_fork_one_parent(
    tmp_path, cloud
):
    job = _Job(tmp_path, ["a", "b", "a", "b"])
    await _run(job, ["a", "b", "a", "b"])
    assert len(cloud.parents()) == 1
    assert len(cloud.builds) == 1 and len(cloud.pulls) == 1
    assert len(cloud.copies()) == 4 and not cloud.fresh()
    await _fork.close_parents()
    assert cloud.alive() == []


async def test_different_environments_get_their_own_parents(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "b", "a", "b"])
    for task in ["a", "b", "a", "b"]:
        dockerfile = UBUNTU if task == "a" else ALPINE
        environment = await _trial(job.environment(task, dockerfile=dockerfile))
        await environment.stop(delete=True)
    assert len(cloud.parents()) == 2 and len(cloud.builds) == 2
    assert len(cloud.copies()) == 4
    assert cloud.alive() == []


async def test_the_key_is_the_environment_not_the_task(tmp_path, cloud):
    job = _Job(tmp_path, None)
    a, b = job.environment("a"), job.environment("b")
    c = job.environment("c", dockerfile=ALPINE)
    assert a._fork_key(None, "sha256:a") == b._fork_key(None, "sha256:b")
    assert a._fork_key(None, "sha256:a") != c._fork_key(None, "sha256:c")
    # A forced build, another shape, another task env: another key.
    assert a._fork_key(None, "x", True) != a._fork_key(None, "x")
    bigger = job.environment("a", config={"cpus": 4, "memory_mb": 2048})
    assert bigger._fork_key(None, "x") != a._fork_key(None, "x")
    with_env = job.environment(
        "a", config={"cpus": 1, "memory_mb": 2048, "env": {"V": "2"}}
    )
    assert with_env._fork_key(None, "x") != a._fork_key(None, "x")


async def test_without_fork_mode_every_trial_builds(tmp_path, cloud):
    job = _Job(tmp_path, ["a", "a"])
    await _run(job, ["a", "a"], fork_mode=False)
    assert len(cloud.fresh()) == 2 and not cloud.parents() and not cloud.fork_calls
    # The fresh path: build, pull, then up (which builds again from cache).
    assert [u[3] for u in cloud.ups] == [["up", "-d"], ["up", "-d"]]


# -- each copy is its own trial -------------------------------------------------


async def test_a_copy_writes_its_own_trials_compose_files_and_network(tmp_path, cloud):
    # The parent was built for a public trial; the copy's trial closes the
    # network for the agent: it writes the closed networks and the egress
    # network itself, and its containers start under its policy.
    job = _Job(tmp_path, ["a", "a"])
    first = await _trial(job.environment("a"))
    await first.stop(delete=True)
    restricted = await _trial(
        job.environment("a", network_policy=PUBLIC, phase_network_policies=[NO_NETWORK])
    )
    copy = restricted._sandbox
    assert copy.parent is not None
    network = f"{HOME}/harbor/compose/docker-compose-network.json"
    assert network not in copy.parent.files.content
    assert network in copy.files.content
    assert copy.docker.can_reach_internet("main")  # public setup
    await restricted.set_network_policy(NO_NETWORK)
    assert not copy.docker.can_reach_internet("main")
    await restricted.stop(delete=True)
    assert cloud.alive() == []


# -- fail closed ----------------------------------------------------------------


async def test_a_refused_fork_builds_that_trial_fresh(tmp_path, cloud, caplog):
    job = _Job(tmp_path, ["a", "a"])
    cloud.fork_errors = [PrizedError("fork_unsupported_host", status=409)]
    first = await _trial(job.environment("a"))
    assert first._sandbox in cloud.fresh()
    assert "could not fork" in caplog.text and "building it fresh" in caplog.text
    await first.stop(delete=True)
    # The parent is dropped (its host can't fork); the next trial builds a new one.
    second = await _trial(job.environment("a"))
    await second.stop(delete=True)
    assert cloud.alive() == []


async def test_a_copy_that_doesnt_come_up_builds_fresh_then_stops_forking(
    tmp_path, cloud, caplog
):
    job = _Job(tmp_path, ["a", "a", "a", "a"])
    cloud.dockerd_dead_in_copies = True
    for n in range(4):
        environment = await _trial(job.environment("a", dockerd_wait_sec=0))
        # Every trial ends up on a fresh build that works.
        assert environment._sandbox.parent is None
        assert environment._sandbox in cloud.fresh()
        await environment.stop(delete=True)
    assert caplog.text.count("building it fresh for this trial") == 2
    # Two failed copies: the parent is deleted and the key builds fresh.
    assert len(cloud.copies()) == 2 and len(cloud.fork_calls) == 2
    assert all(p.state == "deleted" for p in cloud.parents())
    assert cloud.alive() == []


async def test_a_copy_missing_the_image_never_builds_it(tmp_path, cloud, caplog):
    job = _Job(tmp_path, ["a", "a"])
    first = await _trial(job.environment("a"))
    await first.stop(delete=True)
    (parent,) = cloud.parents()
    parent.images.clear()  # as if the parent's build had been lost
    second = await _trial(job.environment("a"))
    assert second._sandbox in cloud.fresh()
    assert "pull access denied for harbor-main" in caplog.text
    builds_in_copies = [
        b for b in cloud.builds if b[0] in {c.id for c in cloud.copies()}
    ]
    assert builds_in_copies == []
    await second.stop(delete=True)


# -- parents are cleaned up ------------------------------------------------------


async def test_parents_carry_the_labels_the_exit_sweep_deletes_by(
    tmp_path, cloud, monkeypatch
):
    job = _Job(tmp_path, ["a", "b"])
    await _run(job, ["a", "b"])
    (parent,) = cloud.parents()
    assert parent.state == "paused"
    assert parent.labels[_fork.PARENT_LABEL] == "1"
    assert parent.labels[_fork.RUN_LABEL] == _fork.RUN_ID
    assert parent.labels["harbor-session"].startswith("fork-parent-docker-")
    swept = []
    monkeypatch.setattr(
        _fork.Sandbox,
        "delete_by_labels",
        staticmethod(lambda labels: swept.append(labels) or ["x"]),
    )
    _fork.sweep()
    assert swept == [{_fork.PARENT_LABEL: "1", _fork.RUN_LABEL: _fork.RUN_ID}]
    await _fork.close_parents()  # TaskMajor's on_job_end
    assert cloud.alive() == []


# -- registry credentials ---------------------------------------------------------


def _hub_env(monkeypatch):
    monkeypatch.setenv("DOCKERHUB_USERNAME", "evals")
    monkeypatch.setenv("DOCKERHUB_TOKEN", FAKE_TOKEN)


def _no_secret_anywhere(cloud, caplog, *secrets):
    for secret in secrets:
        assert secret not in caplog.text
        for box in cloud.boxes:
            for e in box.execs:
                assert secret not in e["cmd"]
                assert all(secret not in v for v in e["env"].values())


async def test_docker_mode_builds_and_pulls_with_registry_auth_only(
    tmp_path, cloud, monkeypatch, caplog
):
    _hub_env(monkeypatch)
    caplog.set_level("DEBUG")
    job = _Job(tmp_path, ["a"])
    environment = await _trial(
        job.environment(
            "a",
            dockerfile="FROM ghcr.io/org/base:1\nFROM ubuntu:24.04\n",
            registry_auth="env",
            fork_mode=False,
        )
    )
    (box,) = cloud.boxes
    # Built and pulled with the credentials in a folder of their own.
    assert cloud.builds[0][1:] == (PULL_AUTH, True)
    assert cloud.pulls[0][1:] == (PULL_AUTH, True)
    # Gone before any container starts; up uses the CLI's own config.
    assert [u[1:3] for u in cloud.ups] == [(f"{HOME}/.docker", False)]
    assert not box.has_credentials()
    basic = base64.b64encode(f"evals:{FAKE_TOKEN}".encode()).decode()
    _no_secret_anywhere(cloud, caplog, FAKE_TOKEN, basic)
    # ghcr.io has no entry in env (Docker Hub only): said once, pulled anonymously.
    assert "no credentials for ghcr.io" in caplog.text
    await environment.stop(delete=True)


async def test_a_parent_pauses_without_credentials_and_copies_never_see_them(
    tmp_path, cloud, monkeypatch, caplog
):
    _hub_env(monkeypatch)
    caplog.set_level("DEBUG")
    job = _Job(tmp_path, ["a", "a"])
    await _run(job, ["a", "a"], registry_auth="env")
    (parent,) = cloud.parents()
    assert cloud.builds[0][0] == parent.id and cloud.builds[0][2] is True
    assert parent.files_at_pause is not None
    assert not any(p.startswith(PULL_AUTH) for p in parent.files_at_pause)
    for copy in cloud.copies():
        assert not any(p.startswith(PULL_AUTH) for p in copy.files.content)
    basic = base64.b64encode(f"evals:{FAKE_TOKEN}".encode()).decode()
    _no_secret_anywhere(cloud, caplog, FAKE_TOKEN, basic)


async def test_credentials_that_wont_go_fail_the_start_closed(
    tmp_path, cloud, monkeypatch
):
    _hub_env(monkeypatch)
    cloud.keep_credentials = True
    job = _Job(tmp_path, ["a"])
    environment = job.environment("a", registry_auth="env", fork_mode=False)
    with pytest.raises(RuntimeError, match="could not remove the registry credentials"):
        await environment.start(force_build=False)
    assert cloud.ups == []  # no container started
    assert cloud.alive() == []


def test_the_registries_a_docker_mode_start_may_pull_from(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "ARG BASE=python:3.12\n"
        "FROM --platform=linux/amd64 ghcr.io/org/builder:1 AS build\n"
        "FROM build AS again\n"
        "FROM ${BASE}\n"
        "from quay.io/x/y:2\n"
        "FROM scratch\n"
    )
    (tmp_path / "docker-compose.yaml").write_text(
        "services:\n  db:\n    image: postgres:16\n  x:\n    image: ${IMG}\n"
    )
    assert _docker_module.image_references(tmp_path, "ubuntu:24.04") == [
        "ubuntu:24.04",
        "ghcr.io/org/builder:1",
        "quay.io/x/y:2",
        "postgres:16",
    ]
    config = json.loads(
        docker_cli_config({"index.docker.io": {"auth": "x"}, "ghcr.io": {"auth": "y"}})
    )
    assert config == {
        "auths": {
            "https://index.docker.io/v1/": {"auth": "x"},
            "ghcr.io": {"auth": "y"},
        }
    }


# -- auto mode ----------------------------------------------------------------------


def test_auto_sends_a_restricted_prebuilt_task_to_docker_mode(tmp_path):
    job = _Job(tmp_path, None)

    def env(**kwargs):
        return job.environment(
            "p", config={"docker_image": "python:3.12"}, fork_mode=False, **kwargs
        )

    assert not env()._docker
    assert not env(network_policy=PUBLIC)._docker
    assert env(network_policy=NO_NETWORK)._docker
    routed = env(network_policy=PUBLIC, phase_network_policies=[NO_NETWORK])
    assert routed._docker and routed._template == "prized-docker-v1"
    assert not env(network_policy=NO_NETWORK, docker="false")._docker


async def test_a_refused_template_says_why_for_a_routed_task(tmp_path, monkeypatch):
    async def refuse(template, **kwargs):
        raise PrizedError("template_inactive", status=409)

    monkeypatch.setattr(_module, "_docker_refusal", None)
    monkeypatch.setattr(_module.AsyncSandbox, "create", refuse)
    job = _Job(tmp_path, None)
    environment = job.environment(
        "p",
        config={"docker_image": "python:3.12"},
        fork_mode=False,
        network_policy=NO_NETWORK,
    )
    with pytest.raises(RuntimeError) as info:
        await environment.start(force_build=False)
    message = str(info.value)
    assert message.startswith(_module._NETWORK_REFUSED)
    assert message.endswith("(409 template_inactive).")
