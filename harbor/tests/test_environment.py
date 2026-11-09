import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from harbor.environments.capabilities import EnvironmentResourceCapabilities
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from prized import CommandTimeoutError, ExecResult, PrizedError

from harbor_prized import PrizedEnvironment
from harbor_prized import _environment as _module

PASSWD = (
    "root:x:0:0:root:/root:/bin/bash\n"
    "+::::::\n"  # a NIS entry: no numeric ids, skipped
    "agent:x:1001:1002:Agent:/home/agent:/bin/bash\n"
    "nohome:x:1003:1003::\n"
)
GROUP = "root:x:0:\nagent:x:1002:\nstaff:x:50:agent\n"
RUNTIME = "python3 /home/prized/runtime.py "


class _Files:
    """Stands in for the SDK's files API, with a small per-request size limit."""

    limit = 1 << 30

    def __init__(self) -> None:
        self.written: list[tuple[str, bytes, int | None]] = []
        self.content = {
            "/home/prized/rootfs/etc/passwd": PASSWD.encode(),
            "/home/prized/rootfs/etc/group": GROUP.encode(),
        }

    async def write(self, path, data, mode=None):
        self.written.append((path, data, mode))
        self.content[path] = data

    async def read_bytes(self, path):
        data = self.content.get(path, b"")
        if len(data) > self.limit:
            raise PrizedError("file_too_large")
        return data


class _Sandbox:
    """Stands in for prized.AsyncSandbox: records each command it is given.

    ``answer(cmd)`` may return an ExecResult (or raise) for a command; by
    default every command succeeds with "out"/"err".
    """

    id = "sbx_test"

    def __init__(self, exit_code: int = 0, raises: Exception | None = None) -> None:
        self.files = _Files()
        self.execs: list[dict] = []
        self.deleted = self.closed = self.paused = False
        self.beats, self.beat_raises = 0, None
        self._exit_code, self._raises = exit_code, raises
        self.answer = None

    async def exec(self, cmd, timeout=None, full_output=False):
        self.execs.append({"cmd": cmd, "timeout": timeout, "full_output": full_output})
        if self.answer is not None:
            result = self.answer(cmd)
            if result is not None:
                return result
        if self._raises is not None:
            raise self._raises
        return ExecResult(stdout="out", stderr="err", exit_code=self._exit_code)

    async def delete(self):
        self.deleted = True

    async def pause(self):
        self.paused = True

    async def keep_alive(self):
        self.beats += 1
        if self.beat_raises is not None:
            raise self.beat_raises

    async def close(self):
        self.closed = True


def _environment(
    tmp_path,
    network_policy=None,
    phase_network_policies=None,
    docker=None,
    registry_auth=None,
    **config,
) -> PrizedEnvironment:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(exist_ok=True)
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return PrizedEnvironment(
        environment_dir=environment_dir,
        environment_name="prized-test",
        session_id="prized-test",
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(**config),
        labels="team=evals,run=42",
        network_policy=network_policy,
        phase_network_policies=phase_network_policies,
        docker=docker,
        registry_auth=registry_auth,
    )


@pytest.fixture(autouse=True)
def _no_network_refusal_remembered(monkeypatch):
    monkeypatch.setattr(_module, "_network_refused_at", None)


def _script(command: str) -> list[str]:
    assert command.startswith(RUNTIME)
    return base64.b64decode(command.split(" - ", 1)[1]).decode().splitlines()


def _runtime_calls(sandbox: _Sandbox) -> list[str]:
    return [e["cmd"] for e in sandbox.execs if e["cmd"].startswith(RUNTIME)]


# -- definition and shape ---------------------------------------------------


def test_a_task_without_an_image_is_docker_modes_or_refused(tmp_path):
    # No definition at all: Harbor's own error.
    with pytest.raises(FileNotFoundError, match="docker_image"):
        _environment(tmp_path)
    (tmp_path / "environment").mkdir(exist_ok=True)
    (tmp_path / "environment" / "Dockerfile").write_text("FROM python:3.12\n")
    # A Dockerfile: Docker mode by itself (test_docker.py), or the old refusal.
    assert _environment(tmp_path)._docker
    with pytest.raises(ValueError, match="docker_image") as info:
        _environment(tmp_path, docker="false")
    assert "Building from a Dockerfile is not supported" in str(info.value)


def test_compose_tasks_are_docker_modes_or_refused(tmp_path):
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "docker-compose.yaml").write_text("services: {}\n")
    assert _environment(tmp_path, docker_image="python:3.12")._docker
    with pytest.raises(ValueError, match="Docker Compose"):
        _environment(tmp_path, docker_image="python:3.12", docker="false")


def test_refuses_storage_past_the_largest_disk(tmp_path):
    with pytest.raises(ValueError, match="at most 128 GB"):
        _environment(tmp_path, docker_image="python:3.12", storage_mb=200_000)


@pytest.mark.parametrize("hosts", [["pypi.org"], ["10.0.0.0/8"], ["*.example.com"]])
def test_refuses_allowlists_that_name_hosts(tmp_path, hosts):
    policy = NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=hosts)
    with pytest.raises(ValueError, match="is not supported by"):
        _environment(tmp_path, docker_image="python:3.12", network_policy=policy)


def test_labels_from_key_value_string(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    assert environment._labels == {
        "harbor": "1",
        "harbor-session": "prized-test",
        "team": "evals",
        "run": "42",
    }


@pytest.mark.parametrize(
    ("config", "kwargs", "shape"),
    [
        ({}, {}, (2, 4096, 30)),  # no cpus in the task: 2
        ({"cpus": 1, "memory_mb": 2048, "storage_mb": 10240}, {}, (1, 2048, 30)),
        ({"cpus": 4, "memory_mb": 8192}, {}, (4, 8192, 30)),
        ({"cpus": 1}, {"min_vcpu": 4}, (4, 4096, 30)),
        ({"memory_mb": 512, "storage_mb": 120_000}, {}, (2, 1024, 128)),
        ({"cpus": 1}, {"override_cpus": 8, "image_headroom_gb": 5}, (8, 4096, 15)),
    ],
)
def test_sandbox_shape(tmp_path, config, kwargs, shape):
    environment = _environment(tmp_path, docker_image="python:3.12", **config)
    for key, value in kwargs.items():
        if key == "override_cpus":
            environment._override_cpus = value
            environment._maybe_override_task_env_config()
        else:
            setattr(environment, f"_{key}", value)
    assert environment._shape() == shape


@pytest.mark.skipif(
    "fractional_cpus" not in EnvironmentResourceCapabilities.model_fields,
    reason="Harbor before 0.25 takes whole cpus only",
)
def test_fractional_cpus_round_up_to_whole_vcpus(tmp_path):
    # Harbor 0.25 refuses cpus = 0.5 in the constructor unless the provider
    # says it takes fractional cpus.
    assert PrizedEnvironment.resource_capabilities().fractional_cpus
    environment = _environment(
        tmp_path, docker_image="python:3.12", cpus=0.5, memory_mb=1024
    )
    assert environment._shape() == (1, 1024, 30)
    environment._override_cpus = 2.5
    environment._maybe_override_task_env_config()
    assert environment._shape() == (3, 1024, 30)


# -- start and stop ---------------------------------------------------------


@pytest.mark.asyncio
async def test_start_creates_a_labelled_sandbox_and_unpacks_the_image(
    tmp_path, monkeypatch
):
    environment = _environment(tmp_path, docker_image="python:3.12", cpus=1)
    sandbox = _Sandbox()
    sandbox.answer = lambda cmd: (
        ExecResult(
            stdout='{"config": {"Env": ["A=1"], "WorkingDir": "/w", "User": "app"}}',
            stderr="",
            exit_code=0,
        )
        if "crane config" in cmd
        else None
    )
    created = {}

    async def create(template, **kwargs):
        created.update(template=template, **kwargs)
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    monkeypatch.setattr(_module.PrizedEnvironment, "_user_identity", _no_user)

    await environment.start(force_build=False)

    assert created["template"] == "prized-base-v1"
    assert created["labels"]["harbor-session"] == "prized-test"
    assert (created["max_runtime_min"], created["auto_delete_min"]) == (1440, 1440)
    setup = sandbox.execs[0]["cmd"]
    assert "set -e; cd ~; umask 022; " in setup
    assert '"${PIPESTATUS[0]} ${PIPESTATUS[1]}"' in setup  # crane checked on its own
    assert "--preserve-permissions" in setup
    assert (environment._image_env, environment._image_workdir) == ({"A": "1"}, "/w")
    assert environment._image_user == "app"
    assert sandbox.files.written[0][0] == "/home/prized/runtime.py"
    await environment.stop(delete=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        PrizedError("invalid_api_key"),  # the SDK's message names auth.md already
        PrizedError("unauthorized", "Unauthorized.", status=401),
    ],
)
async def test_a_refused_key_points_agents_at_sign_up_once(
    tmp_path, monkeypatch, error
):
    environment = _environment(tmp_path, docker_image="python:3.12")

    async def create(template, **kwargs):
        raise error

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)

    with pytest.raises(RuntimeError, match="auth.md") as info:
        await environment.start(force_build=False)
    assert str(info.value).count("auth.md") == 1
    assert ".." not in str(info.value)


@pytest.mark.asyncio
async def test_a_heartbeat_keeps_the_sandbox_from_pausing_until_stop(
    tmp_path, monkeypatch
):
    environment = _environment(tmp_path, docker_image="python:3.12")
    sandbox = _Sandbox()
    sandbox.beat_raises = PrizedError("network_error")  # a failed beat is retried
    created = {}

    async def create(template, **kwargs):
        created.update(kwargs)
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    monkeypatch.setattr(_module.PrizedEnvironment, "_prepare_image", _no_image)
    monkeypatch.setattr(_module, "_HEARTBEAT_SEC", 0.01)

    await environment.start(force_build=False)
    assert created["auto_pause_min"] == 30

    async def beats(n):
        while sandbox.beats < n:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(beats(3), 5)
    heartbeat = environment._heartbeat
    await environment.stop(delete=True)
    await asyncio.sleep(0)
    assert heartbeat.cancelled()
    beats = sandbox.beats
    await asyncio.sleep(0.05)
    assert sandbox.beats == beats and sandbox.deleted


@pytest.mark.asyncio
async def test_the_heartbeat_ends_when_the_sandbox_is_gone(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")
    sandbox = _Sandbox()
    sandbox.beat_raises = PrizedError("not_found", status=404)
    monkeypatch.setattr(_module, "_HEARTBEAT_SEC", 0.01)

    await asyncio.wait_for(environment._keep_alive(sandbox), 5)  # returns
    assert sandbox.beats == 1


@pytest.mark.asyncio
async def test_a_quota_refusal_names_leftover_sandboxes(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")

    async def create(template, **kwargs):
        raise PrizedError("sandbox_quota_exceeded", reason="retained_disk", status=429)

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    with pytest.raises(RuntimeError, match="--label harbor=1"):
        await environment.start(force_build=False)


async def _no_image(self, image, credentials=None):
    return None


async def _no_user(self, user):
    return None


@pytest.mark.asyncio
async def test_a_failed_start_deletes_the_sandbox(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")
    sandbox = _Sandbox(exit_code=1)

    async def create(template, **kwargs):
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)

    with pytest.raises(RuntimeError, match="Could not unpack"):
        await environment.start(force_build=False)

    assert (sandbox.deleted, sandbox.closed, environment._sandbox) == (True, True, None)


@pytest.mark.asyncio
async def test_a_start_cut_off_by_the_network_is_retried_once(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")
    sandboxes = []

    async def create(template, **kwargs):
        sandbox = _Sandbox()
        if not sandboxes:  # the first one's setup gets no clear answer
            sandbox._raises = PrizedError("network_error")
        sandbox.answer = lambda cmd: (
            ExecResult(stdout="{}", stderr="", exit_code=0)
            if "crane config" in cmd and sandbox._raises is None
            else None
        )
        sandboxes.append(sandbox)
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    await environment.start(force_build=False)

    assert len(sandboxes) == 2
    assert (sandboxes[0].deleted, sandboxes[1].deleted) == (True, False)
    assert environment._sandbox is sandboxes[1]
    started = sandboxes[1]

    sandboxes.clear()
    monkeypatch.setattr(
        _module.AsyncSandbox, "create", _always(PrizedError("network_error"), sandboxes)
    )
    with pytest.raises(PrizedError):
        await environment.start(force_build=False)
    assert [s.deleted for s in sandboxes] == [True, True]  # twice at most
    assert started.deleted  # a second start replaces the first sandbox
    assert environment._heartbeat is None


def _always(error, made):
    async def create(template, **kwargs):
        sandbox = _Sandbox(raises=error)
        made.append(sandbox)
        return sandbox

    return create


@pytest.mark.asyncio
async def test_stop_deletes_the_sandbox(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()

    await environment.stop(delete=True)

    assert (sandbox.deleted, sandbox.closed, environment._sandbox) == (True, True, None)


@pytest.mark.asyncio
async def test_stop_without_delete_pauses_the_sandbox(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()

    await environment.stop(delete=False)

    assert (sandbox.paused, sandbox.deleted, sandbox.closed) == (True, False, True)


@pytest.mark.asyncio
async def test_a_cancelled_stop_still_finishes_the_delete(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_delete():
        started.set()
        await release.wait()
        sandbox.deleted = True

    sandbox.delete = slow_delete
    stopping = asyncio.ensure_future(environment.stop(delete=True))
    await started.wait()
    stopping.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert (sandbox.deleted, sandbox.closed) == (True, True)


# -- commands ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_runs_in_image_with_env_and_cwd(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._image_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "a.b": "x"}
    environment._image_workdir = "/app"
    environment._sandbox = sandbox = _Sandbox(exit_code=3)

    result = await environment.exec("echo hi", env={"KEY": "va lue"}, timeout_sec=30)

    assert (result.stdout, result.stderr, result.return_code) == ("out", "err", 3)
    assert sandbox.execs[0]["timeout"] == 30
    assert sandbox.execs[0]["full_output"] is True
    assert _script(sandbox.execs[0]["cmd"]) == [
        'rm -f -- "$0"',
        "umask 022",
        "export HOME=/root",
        "export USER=root",
        "export LOGNAME=root",
        "export PATH=/usr/local/bin:/usr/bin:/bin",  # "a.b" can't be exported
        "export KEY='va lue'",
        "cd -- /app >/dev/null || exit 1",
        "echo hi",
    ]


@pytest.mark.asyncio
async def test_task_env_reaches_every_command(tmp_path):
    environment = _environment(
        tmp_path, docker_image="python:3.12", env={"TASK_VAR": "from-task"}
    )
    environment._image_env = {"TASK_VAR": "from-image", "LANG": "C.UTF-8"}
    environment._sandbox = sandbox = _Sandbox()

    await environment.exec("env", env={"EXTRA": "1"})

    lines = _script(sandbox.execs[0]["cmd"])
    assert "export LANG=C.UTF-8" in lines
    assert [line for line in lines if "TASK_VAR" in line] == [
        "export TASK_VAR=from-task"  # the task's env wins over the image's
    ]
    assert "export EXTRA=1" in lines


@pytest.mark.asyncio
async def test_no_timeout_means_no_limit(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()

    await environment.exec("sleep 7200")

    assert sandbox.execs[0]["timeout"] == 0  # the SDK runs it detached, no limit


@pytest.mark.asyncio
async def test_a_command_past_its_timeout_raises(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    killed = ExecResult(stdout="", stderr="", exit_code=-1, timed_out=True)
    environment._sandbox = _Sandbox(raises=CommandTimeoutError(killed, 5.0))

    with pytest.raises(RuntimeError, match="timed out after 5 seconds"):
        await environment.exec("sleep 60", timeout_sec=5)


@pytest.mark.asyncio
async def test_long_scripts_are_uploaded_and_removed_if_they_never_ran(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()

    await environment.exec("x" * 70_000)

    path = sandbox.files.written[0][0]
    assert path.startswith("/home/prized/rootfs/.runtime-xfer/cmd-")
    assert sandbox.execs[0]["cmd"] == RUNTIME + path.removeprefix("/home/prized/rootfs")

    sandbox._raises = PrizedError("sandbox_not_ready")
    with pytest.raises(PrizedError):
        await environment.exec("y" * 70_000)
    staged = sandbox.files.written[1][0]
    assert sandbox.execs[-1]["cmd"] == f"rm -f {staged}"


# -- users ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_as_a_user_runs_with_its_ids_and_home(tmp_path, caplog):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._image_env = {"PATH": "/usr/bin:/bin"}
    environment._sandbox = sandbox = _Sandbox()

    await environment.exec("id")
    assert "User separation is not enforced" not in caplog.text
    await environment.exec("id", user="agent")
    with environment.with_default_user(1003):
        await environment.exec("id")
    await environment.exec("id", user="agent:staff")
    await environment.exec("id", user="4242")
    # Said once per trial, at the first command for a non-root user.
    assert caplog.text.count("User separation is not enforced") == 1
    sandbox.execs = sandbox.execs[1:]

    commands = _runtime_calls(sandbox)
    assert commands[0].startswith(RUNTIME + "--user 1001:1002 - ")
    assert _script(commands[0])[2:5] == [
        "export HOME=/home/agent",
        "export USER=agent",
        "export LOGNAME=agent",
    ]
    assert commands[1].startswith(RUNTIME + "--user 1003:1003 - ")
    assert _script(commands[1])[2] == "export HOME=/"  # empty home field
    assert commands[2].startswith(RUNTIME + "--user 1001:50 - ")
    # A uid with no passwd entry runs as that uid in group 0, as with docker exec.
    assert commands[3].startswith(RUNTIME + "--user 4242:0 - ")
    assert _script(commands[3])[2:4] == ["export HOME=/", "export USER=4242"]


@pytest.mark.asyncio
async def test_the_images_user_is_the_default(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._image_user = "agent"
    environment._sandbox = sandbox = _Sandbox()

    await environment.exec("id")
    await environment.exec("id", user="root")
    await environment.download_file("/etc/hostname", tmp_path / "hostname")

    commands = _runtime_calls(sandbox)
    assert commands[0].startswith(RUNTIME + "--user 1001:1002 - ")
    assert all(c.startswith(RUNTIME + "- ") for c in commands[1:])  # root


@pytest.mark.asyncio
async def test_root_needs_no_passwd_lookup(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()
    sandbox.files.content = {}  # reading would find nothing

    for user in (None, "root", 0, "0", "root:root"):
        await environment.exec("id", user=user)

    assert all(e["cmd"].startswith(RUNTIME + "- ") for e in sandbox.execs)


@pytest.mark.asyncio
async def test_unknown_users_and_groups_are_refused(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()

    with pytest.raises(RuntimeError, match="unable to find user nobody"):
        await environment.exec("id", user="nobody")
    with pytest.raises(RuntimeError, match="unable to find group wheel"):
        await environment.exec("id", user="agent:wheel")
    assert sandbox.execs == []


# -- files ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_keeps_mode_without_setuid_bits(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()
    source = tmp_path / "tool"
    source.write_text("#!/bin/sh\n")
    real_stat = Path.stat

    def setuid_stat(self, *args, **kwargs):  # some sandboxes refuse chmod u+s
        st = real_stat(self, *args, **kwargs)
        if self != source:
            return st
        return os.stat_result(((st.st_mode & ~0o7777) | 0o4755, *tuple(st)[1:]))

    monkeypatch.setattr(Path, "stat", setuid_stat)
    monkeypatch.setattr(_module, "_WINDOWS_HOST", False)
    await environment.upload_file(source, "/usr/local/bin/tool")

    script = _script(sandbox.execs[0]["cmd"])[-1]
    assert script.startswith("if [ -d /usr/local/bin/tool ]; then")  # not into a dir
    assert script.endswith("&& chmod 755 /usr/local/bin/tool")


@pytest.mark.asyncio
async def test_upload_of_a_mode_0_file_still_sets_a_mode(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()
    source = tmp_path / "secret"
    source.write_text("x")
    real_stat = Path.stat

    def mode_0_stat(self, *args, **kwargs):
        st = real_stat(self, *args, **kwargs)
        if self != source:
            return st
        return os.stat_result((st.st_mode & ~0o7777, *tuple(st)[1:]))

    monkeypatch.setattr(Path, "stat", mode_0_stat)
    monkeypatch.setattr(_module, "_WINDOWS_HOST", False)
    await environment.upload_file(source, "/root/secret")

    assert _script(sandbox.execs[0]["cmd"])[-1].endswith("&& chmod 0 /root/secret")


@pytest.mark.asyncio
async def test_upload_from_windows_sets_no_mode(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()
    source = tmp_path / "test.sh"
    source.write_text("echo\n")
    monkeypatch.setattr(_module, "_WINDOWS_HOST", True)

    await environment.upload_file(source, "/tests/test.sh")

    assert "chmod" not in _script(sandbox.execs[0]["cmd"])[-1]


@pytest.mark.asyncio
async def test_a_failed_upload_removes_its_staged_copy(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox(exit_code=1)
    source = tmp_path / "f"
    source.write_text("x")

    with pytest.raises(RuntimeError, match="Failed to upload"):
        await environment.upload_file(source, "/tmp/f")

    staged = sandbox.files.written[0][0]
    assert sandbox.execs[-1]["cmd"] == f"rm -f {staged}"


@pytest.mark.asyncio
async def test_big_files_move_in_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(_module, "_CHUNK", 4)
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()

    await environment._put("/home/prized/big", b"0123456789", mode=0o644)

    assert [(p, d) for p, d, _ in sandbox.files.written] == [
        ("/home/prized/big.part0000", b"0123"),
        ("/home/prized/big.part0001", b"4567"),
        ("/home/prized/big.part0002", b"89"),
    ]
    assembled = sandbox.execs[0]["cmd"]
    assert assembled.startswith("cat /home/prized/big.part0000 ")
    assert "> /home/prized/big && chmod 644 /home/prized/big || {" in assembled
    assert sandbox.execs[-1]["cmd"].startswith("rm -f /home/prized/big.part0000")

    sandbox.files.limit = 4
    sandbox.files.content.update(
        {"/home/prized/out": b"x" * 10, "/home/prized/out.part0000": b"abcd"}
    )
    sandbox.files.content["/home/prized/out.part0001"] = b"ef"
    sandbox.answer = lambda cmd: (
        ExecResult(
            stdout="/home/prized/out.part0000\n/home/prized/out.part0001\n",
            stderr="",
            exit_code=0,
        )
        if cmd.startswith("split ")
        else None
    )
    assert await environment._get("/home/prized/out") == b"abcdef"
    assert sandbox.execs[-1]["cmd"] == "rm -f /home/prized/out.part*"


@pytest.mark.asyncio
async def test_download_dir_accepts_files_changed_while_packing(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()
    changed = ExecResult(
        stdout="",
        stderr="tar: ./agent.log: file changed as we read it\n",
        exit_code=1,
    )
    sandbox.answer = lambda cmd: changed if cmd.startswith(RUNTIME) else None
    packed = _module.pack_dir_to_bytes(Path(__file__).parent, compress=True)

    async def read_bytes(path):
        return packed.getvalue()

    sandbox.files.read_bytes = read_bytes
    await environment.download_dir("/logs/agent", tmp_path / "logs")

    assert (tmp_path / "logs" / Path(__file__).name).exists()
    assert sandbox.execs[-1]["cmd"].startswith(
        "rm -f /home/prized/rootfs/.runtime-xfer/"
    )

    sandbox.answer = lambda cmd: (
        ExecResult(stdout="", stderr="tar: /x: Cannot open", exit_code=2)
        if cmd.startswith(RUNTIME)
        else None
    )
    with pytest.raises(RuntimeError, match="Failed to archive"):
        await environment.download_dir("/x", tmp_path / "x")
    assert sandbox.execs[-1]["cmd"].startswith(
        "rm -f /home/prized/rootfs/.runtime-xfer/"
    )


def test_dpkg_hook_opens_up_files_left_at_mode_0():
    hook = _module._DPKG_HOOK
    assert hook.startswith("post-invoke=find /usr ")
    assert "-perm 0000 -exec chmod 0755 {} +" in hook


def test_image_gets_dockers_hosts_and_hostname():
    assert _module._HOSTS.splitlines() == [
        "127.0.0.1\tlocalhost",
        "::1\tlocalhost ip6-localhost ip6-loopback",
        "127.0.1.1\t$(hostname)",
    ]


def test_benign_tar_errors_are_narrow():
    assert _module._BENIGN_TAR_ERRORS.split("|") == [
        "Cannot mknod",
        "Cannot change mode",
        "Exiting with failure status",
    ]


# -- behaviour that runs for real -------------------------------------------

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bash") is None,
    reason="runs the unpack script with GNU tar under bash",
)


def _run_unpack(tmp_path, archive: bytes | None) -> subprocess.CompletedProcess:
    """Runs the image unpack script with a fake crane that prints ``archive``
    (or fails when it is None)."""
    work = tmp_path / "work"
    root = tmp_path / "root"
    work.mkdir()
    root.mkdir()
    (work / "image.tar").write_bytes(archive or b"")
    crane = work / "crane"
    crane.write_text(
        "#!/bin/sh\n"
        + (
            "cat image.tar\n"
            if archive is not None
            else "echo pull failed >&2; exit 1\n"
        )
    )
    crane.chmod(0o755)
    return subprocess.run(
        [
            "bash",
            "-c",
            "set -e; " + _module.unpack_command("img", str(root)) + "echo ok",
        ],
        cwd=work,
        capture_output=True,
        check=False,
        text=True,
    )


def _tar(*members: tarfile.TarInfo, data: bytes = b"hi\n") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for member in members:
            if member.isreg():
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            else:
                tar.addfile(member)
    return buffer.getvalue()


def _member(
    name: str, kind: bytes = tarfile.REGTYPE, mode: int = 0o644
) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type, member.mode = kind, mode
    return member


@linux_only
def test_unpack_keeps_modes_and_accepts_device_nodes(tmp_path):
    archive = _tar(
        _member("bin/tool", mode=0o755),
        _member("etc/conf", mode=0o640),
        _member("dev/null", kind=tarfile.CHRTYPE, mode=0o666),  # can't mknod
    )
    result = _run_unpack(tmp_path, archive)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert (tmp_path / "root/bin/tool").stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "root/etc/conf").stat().st_mode & 0o777 == 0o640


@linux_only
def test_unpack_fails_when_the_pull_fails(tmp_path):
    result = _run_unpack(tmp_path, None)
    assert result.returncode == 1
    assert "crane export failed" in result.stderr


@linux_only
def test_unpack_fails_on_a_truncated_image(tmp_path):
    archive = _tar(_member("bin/tool", mode=0o755), data=b"x" * 10_000)
    result = _run_unpack(tmp_path, archive[:5_000])
    assert result.returncode == 1
    assert "ok" not in result.stdout


# -- cancellation and failures ------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_failing_while_cancelled_still_raises_the_cancel():
    started = asyncio.Event()

    async def failing_cleanup():
        started.set()
        await asyncio.sleep(0.01)
        raise ValueError("sandbox gone")

    async def caller():
        await _module._despite_cancel(failing_cleanup())

    task = asyncio.ensure_future(caller())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_cancelled_start_deletes_the_sandbox(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")
    sandbox = _Sandbox()
    pulling = asyncio.Event()

    async def slow_exec(cmd, timeout=None, full_output=False):
        pulling.set()
        await asyncio.sleep(3600)

    sandbox.exec = slow_exec

    async def create(template, **kwargs):
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    starting = asyncio.ensure_future(environment.start(force_build=False))
    await pulling.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert (sandbox.deleted, sandbox.closed) == (True, True)


@pytest.mark.asyncio
async def test_a_sandbox_that_wont_pause_is_deleted(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()

    async def refuse_pause():
        raise PrizedError("sandbox_quota_exceeded")

    sandbox.pause = refuse_pause
    await environment.stop(delete=False)

    assert (sandbox.deleted, sandbox.closed) == (True, True)


@pytest.mark.asyncio
async def test_runtime_failures_raise_instead_of_passing_as_exit_codes(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()
    sandbox.answer = lambda cmd: ExecResult(
        stdout="",
        stderr="prized runtime: RuntimeError('could not start the namespace holder')",
        exit_code=125,
    )
    with pytest.raises(RuntimeError, match="could not run the command"):
        await environment.exec("true")

    sandbox.answer = lambda cmd: ExecResult(stdout="", stderr="", exit_code=125)
    assert (await environment.exec("exit 125")).return_code == 125  # the command's own


def test_preflight_points_agents_at_sign_up(monkeypatch):
    monkeypatch.delenv("PRIZED_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="auth.md"):
        PrizedEnvironment.preflight()


def test_preflight_takes_harbors_kwargs(monkeypatch):
    # Harbor after 0.25 calls preflight(kwargs=...); 0.24 and 0.25 call preflight().
    monkeypatch.setenv("PRIZED_API_KEY", "key")
    PrizedEnvironment.preflight()
    PrizedEnvironment.preflight(kwargs={})
    PrizedEnvironment.preflight(kwargs={"template": "prized-base-v1"})


# -- network policies ---------------------------------------------------------

NO_NETWORK = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
EMPTY_ALLOWLIST = NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=[])
PUBLIC = NetworkPolicy(network_mode=NetworkMode.PUBLIC)


class _NetworkApi:
    """Stands in for PUT /api/v1/sandboxes/{id}/network?wait=applied.

    ``answers`` are taken one per call: a dict (the 2xx body's networkChange
    status, "applied" or "pending"), or a PrizedError to raise. When they run
    out, every call is applied. ``calls`` records (network, what the sandbox
    had run by then).
    """

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, sandbox, network):
        self.calls.append((network, len(sandbox.execs)))
        answer = self.answers.pop(0) if self.answers else "applied"
        if isinstance(answer, Exception):
            raise answer
        return {"networkChange": {"status": answer, "network": network}}


def _started_sandbox(monkeypatch, api: _NetworkApi) -> _Sandbox:
    sandbox = _Sandbox()
    sandbox.answer = lambda cmd: (
        ExecResult(stdout="{}", stderr="", exit_code=0)
        if "crane config" in cmd
        else None
    )

    async def create(template, **kwargs):
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    monkeypatch.setattr(_module, "put_network", api)
    return sandbox


def _pulled_at(sandbox: _Sandbox) -> int:
    return next(i for i, e in enumerate(sandbox.execs) if "crane" in e["cmd"])


def test_declares_no_network_empty_allowlists_and_switching(tmp_path):
    caps = _environment(tmp_path, docker_image="python:3.12").capabilities
    assert (
        caps.disable_internet,
        caps.network_allowlist,
        caps.dynamic_network_policy,
    ) == (
        True,
        True,
        True,
    )
    # No allowlist entry type: Harbor refuses any list that names a host.
    assert not any(
        getattr(caps, name)
        for name in type(caps).model_fields
        if name.startswith("network_allowlist_")
    )


def test_maps_harbors_policies_to_prizeds_two_networks():
    assert _module.api_network(PUBLIC) == "default"
    assert _module.api_network(NO_NETWORK) == "none"
    assert _module.api_network(EMPTY_ALLOWLIST) == "none"
    with pytest.raises(ValueError, match="can't limit it to hosts"):
        _module.api_network(
            NetworkPolicy(
                network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["pypi.org"]
            )
        )


@pytest.mark.asyncio
async def test_a_public_task_never_asks_for_a_network(tmp_path, monkeypatch):
    api = _NetworkApi()
    _started_sandbox(monkeypatch, api)
    environment = _environment(tmp_path, docker_image="python:3.12")
    await environment.start(force_build=False)
    assert api.calls == []
    await environment.stop(delete=True)


def _native_environment(tmp_path, **kwargs):
    # Prized's own network switch: without Docker mode (by default, a
    # prebuilt task with a closed phase runs in Docker mode).
    return _environment(tmp_path, docker=False, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [NO_NETWORK, EMPTY_ALLOWLIST])
async def test_a_no_network_task_pulls_with_the_network_then_turns_it_off(
    tmp_path, monkeypatch, policy
):
    api = _NetworkApi()
    sandbox = _started_sandbox(monkeypatch, api)
    environment = _native_environment(
        tmp_path, docker_image="python:3.12", network_policy=policy
    )
    await environment.start(force_build=False)
    # The probe before the pull, "none" after everything start does.
    assert [network for network, _ in api.calls] == ["default", "none"]
    assert api.calls[0][1] <= _pulled_at(sandbox) < api.calls[1][1]
    assert api.calls[1][1] == len(sandbox.execs)
    assert not sandbox.deleted
    await environment.stop(delete=True)


@pytest.mark.asyncio
async def test_harbor_switches_the_network_between_phases(tmp_path, monkeypatch):
    # Setup public, the agent with no network: Harbor's set_network_policy
    # around the agent phase, back to the baseline after it.
    api = _NetworkApi()
    _started_sandbox(monkeypatch, api)
    environment = _native_environment(
        tmp_path,
        docker_image="python:3.12",
        network_policy=PUBLIC,
        phase_network_policies=[EMPTY_ALLOWLIST, NO_NETWORK],
    )
    await environment.start(force_build=False)
    assert [n for n, _ in api.calls] == ["default"]  # the probe; it starts public
    await environment.set_network_policy(EMPTY_ALLOWLIST)
    await environment.set_network_policy(PUBLIC)
    await environment.set_network_policy(NO_NETWORK)
    await environment.set_network_policy(PUBLIC)
    assert [n for n, _ in api.calls] == [
        "default",
        "none",
        "default",
        "none",
        "default",
    ]
    assert environment.network_policy == PUBLIC
    await environment.stop(delete=True)


@pytest.mark.asyncio
async def test_a_pending_change_is_asked_again_until_applied(tmp_path, monkeypatch):
    api = _NetworkApi("applied", "pending", "pending", "applied")
    _started_sandbox(monkeypatch, api)
    environment = _native_environment(
        tmp_path, docker_image="python:3.12", network_policy=NO_NETWORK
    )
    await environment.start(force_build=False)
    assert [n for n, _ in api.calls] == ["default", "none", "none", "none"]
    await environment.stop(delete=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        PrizedError("network_none_unavailable", "cannot", status=409),
        PrizedError("not_found", "Not found.", status=404),  # the route is off
    ],
)
async def test_where_prized_cant_switch_the_task_is_refused_as_before(
    tmp_path, monkeypatch, refusal
):
    api = _NetworkApi(refusal)
    sandbox = _started_sandbox(monkeypatch, api)
    environment = _native_environment(
        tmp_path, docker_image="python:3.12", network_policy=NO_NETWORK
    )
    with pytest.raises(RuntimeError, match="network off here yet") as info:
        await environment.start(force_build=False)
    assert refusal.code in str(info.value)
    # Nothing pulled, nothing left behind.
    assert not any("crane" in e["cmd"] for e in sandbox.execs)
    assert sandbox.deleted
    # The next trial of this run is refused before a sandbox is made...
    with pytest.raises(ValueError, match="network off here yet"):
        _native_environment(
            tmp_path, docker_image="python:3.12", network_policy=NO_NETWORK
        )
    # ...a public one runs as ever...
    _native_environment(tmp_path, docker_image="python:3.12")
    # ...and once the refusal is old, the API is asked again.
    monkeypatch.setattr(_module, "_network_refused_at", -1e9)
    _native_environment(tmp_path, docker_image="python:3.12", network_policy=NO_NETWORK)


@pytest.mark.asyncio
async def test_a_failed_change_is_asked_again_twice_then_fails_the_trial(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(_module, "_NETWORK_RETRY_SEC", 0)
    failed = PrizedError("network_change_failed", "kept", status=503)
    api = _NetworkApi("applied", failed, failed, failed)
    sandbox = _started_sandbox(monkeypatch, api)
    environment = _native_environment(
        tmp_path, docker_image="python:3.12", network_policy=NO_NETWORK
    )
    with pytest.raises(RuntimeError, match='network to "none"'):
        await environment.start(force_build=False)
    assert [n for n, _ in api.calls] == ["default", "none", "none", "none"]
    assert sandbox.deleted


@pytest.mark.asyncio
async def test_a_switch_refused_mid_trial_fails_it_without_remembering(
    tmp_path, monkeypatch
):
    api = _NetworkApi()
    _started_sandbox(monkeypatch, api)
    environment = _native_environment(
        tmp_path,
        docker_image="python:3.12",
        network_policy=PUBLIC,
        phase_network_policies=[NO_NETWORK],
    )
    await environment.start(force_build=False)
    api.answers.append(PrizedError("sandbox_not_running", "paused", status=409))
    with pytest.raises(RuntimeError, match="sandbox_not_running|paused"):
        await environment.set_network_policy(NO_NETWORK)
    assert environment.network_policy == PUBLIC  # Harbor keeps the old one
    assert _module._network_refused_at is None
    await environment.stop(delete=True)


@pytest.mark.asyncio
async def test_put_network_goes_through_the_sdks_transport():
    class _Transport:
        def __init__(self):
            self.seen = None

        def api(self, path):
            return f"https://prized.dev/api/v1{path}"

        def clock(self):
            return 100.0

        async def request(self, method, url, **kwargs):
            self.seen = (method, url, kwargs)

            answer = type("Answer", (), {})()
            answer.document = {"networkChange": {"status": "applied"}}
            return answer

    class _Sbx:
        id = "sbx_1"
        _t = _Transport()

    answer = await _module.put_network(_Sbx(), "none")
    assert answer == {"networkChange": {"status": "applied"}}
    method, url, kwargs = _Sbx._t.seen
    assert (method, url) == ("PUT", "https://prized.dev/api/v1/sandboxes/sbx_1/network")
    assert kwargs["json"] == {"network": "none"}
    assert kwargs["params"] == {"wait": "applied"}
    assert kwargs["sandbox_id"] == "sbx_1"


# -- registry credentials -----------------------------------------------------

FAKE_TOKEN = "dckr_pat_000000fake"  # an obvious fake, never a real token


def _hub_env(monkeypatch):
    monkeypatch.setenv("DOCKERHUB_USERNAME", "evals")
    monkeypatch.setenv("DOCKERHUB_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(_module, "_WARNED_NO_CREDENTIALS", set())


def _starts_on(sandbox, monkeypatch, created=None):
    sandbox.answer = lambda cmd: (
        ExecResult(stdout="{}", stderr="", exit_code=0)
        if "crane config" in cmd
        else None
    )

    async def create(template, **kwargs):
        if created is not None:
            created.append(kwargs)
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)


@pytest.mark.asyncio
async def test_registry_auth_gives_crane_the_token_for_the_pull_only(
    tmp_path, monkeypatch, caplog
):
    _hub_env(monkeypatch)
    caplog.set_level("DEBUG")
    environment = _environment(
        tmp_path, docker_image="alexgshaw/fix-git:20251031", registry_auth="env"
    )
    sandbox = _Sandbox()
    _starts_on(sandbox, monkeypatch)

    await environment.start(force_build=False)

    path, data, mode = sandbox.files.written[0]
    assert (path, mode) == ("/home/prized/.pull-auth/config.json", 0o600)
    basic = base64.b64encode(f"evals:{FAKE_TOKEN}".encode()).decode()
    assert json.loads(data) == {
        "auths": {"https://index.docker.io/v1/": {"auth": basic}}
    }
    setup = sandbox.execs[0]["cmd"]
    # The trap comes first, so nothing can fail before it is set, and it is
    # the setup's only trap.
    assert setup.startswith(
        "export DOCKER_CONFIG=/home/prized/.pull-auth; "
        "trap 'rm -rf /home/prized/.pull-auth' EXIT; set -e; "
    )
    assert setup.count("trap ") == 1
    # The secret goes through the files API only: no command line, no log line.
    for secret in (FAKE_TOKEN, basic):
        assert all(secret not in e["cmd"] for e in sandbox.execs)
        assert secret not in caplog.text
    await environment.stop(delete=True)


@pytest.mark.asyncio
async def test_without_registry_auth_the_pull_is_anonymous(tmp_path, monkeypatch):
    _hub_env(monkeypatch)  # set, but not asked for
    environment = _environment(tmp_path, docker_image="python:3.12")
    sandbox = _Sandbox()
    _starts_on(sandbox, monkeypatch)

    await environment.start(force_build=False)

    assert "DOCKER_CONFIG" not in sandbox.execs[0]["cmd"]
    assert all(".pull-auth" not in path for path, _, _ in sandbox.files.written)
    await environment.stop(delete=True)


@pytest.mark.asyncio
async def test_registry_auth_with_nothing_for_the_registry_warns_once(
    tmp_path, monkeypatch, caplog
):
    _hub_env(monkeypatch)
    for _ in range(2):  # two trials of one run
        environment = _environment(
            tmp_path, docker_image="ghcr.io/org/task:1", registry_auth="env"
        )
        sandbox = _Sandbox()
        _starts_on(sandbox, monkeypatch)
        await environment.start(force_build=False)
        assert "DOCKER_CONFIG" not in sandbox.execs[0]["cmd"]
        await environment.stop(delete=True)
    assert caplog.text.count("has no credentials for ghcr.io") == 1


@pytest.mark.asyncio
async def test_registry_auth_is_read_before_any_sandbox_starts(tmp_path, monkeypatch):
    config = tmp_path / "docker" / "config.json"
    config.parent.mkdir()
    config.write_text(
        '{"auths": {"https://index.docker.io/v1/": {}}, "credsStore": "absent"}'
    )
    monkeypatch.setenv("PATH", str(tmp_path / "no-helpers"))
    environment = _environment(
        tmp_path, docker_image="python:3.12", registry_auth=str(config.parent)
    )
    created = []
    _starts_on(_Sandbox(), monkeypatch, created)

    with pytest.raises(RuntimeError, match="docker-credential-absent"):
        await environment.start(force_build=False)
    assert created == []


@pytest.mark.parametrize("value", [None, False, "", "none", "off", "false"])
def test_registry_auth_can_be_off(tmp_path, value):
    environment = _environment(
        tmp_path, docker_image="python:3.12", registry_auth=value
    )
    assert environment._registry_auth is None


@pytest.mark.parametrize(
    "value", [{"username": "evals", "password": FAKE_TOKEN}, ["evals", FAKE_TOKEN]]
)
def test_registry_auth_refuses_credentials(tmp_path, value):
    with pytest.raises(TypeError, match="never the credentials") as info:
        _environment(tmp_path, docker_image="python:3.12", registry_auth=value)
    assert FAKE_TOKEN not in str(info.value)


@pytest.mark.parametrize("value", [True, 1, 0, 2.5])  # --ek registry_auth=true, =1
def test_registry_auth_refuses_flags_and_numbers(tmp_path, value):
    with pytest.raises(TypeError, match="not true or a number"):
        _environment(tmp_path, docker_image="python:3.12", registry_auth=value)


def test_registry_auth_without_its_credentials_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKERHUB_TOKEN", raising=False)
    monkeypatch.setenv("DOCKERHUB_USERNAME", "evals")
    with pytest.raises(ValueError, match="needs DOCKERHUB_TOKEN"):
        _environment(tmp_path, docker_image="python:3.12", registry_auth="env")
    with pytest.raises(ValueError, match="no Docker config"):
        _environment(
            tmp_path,
            docker_image="python:3.12",
            registry_auth=str(tmp_path / "missing.json"),
        )


@pytest.mark.asyncio
async def test_an_anonymous_rate_limit_names_the_fix(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")
    sandbox = _Sandbox()
    sandbox.answer = lambda cmd: ExecResult(
        stdout="",
        stderr="crane export failed\nTOOMANYREQUESTS: You have reached your "
        "unauthenticated pull rate limit.",
        exit_code=1,
    )

    async def create(template, **kwargs):
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    with pytest.raises(RuntimeError, match="registry_auth=env") as info:
        await environment.start(force_build=False)
    assert "TOOMANYREQUESTS" in str(info.value)
    assert sandbox.deleted


@linux_only
@pytest.mark.parametrize("pull_works", [True, False])
def test_the_credentials_are_removed_when_the_pull_ends(tmp_path, pull_works):
    auth = tmp_path / "pull-auth"
    auth.mkdir()
    (auth / "config.json").write_text('{"auths": {}}')
    work = tmp_path / "work"
    root = tmp_path / "root"
    work.mkdir()
    root.mkdir()
    (work / "image.tar").write_bytes(_tar(_member("bin/tool", mode=0o755)))
    crane = work / "crane"
    crane.write_text(  # proves crane saw the config, then pulls or fails
        '#!/bin/sh\ntest -f "$DOCKER_CONFIG/config.json" && echo seen > seen\n'
        + ("cat image.tar\n" if pull_works else "echo denied >&2; exit 1\n")
    )
    crane.chmod(0o755)
    script = (
        "set -e; "
        + _module.pull_auth_prefix(str(auth))
        + _module.unpack_command("img", str(root))
        + "echo ok"
    )
    result = subprocess.run(
        ["bash", "-c", script], cwd=work, capture_output=True, check=False, text=True
    )

    assert (work / "seen").read_text().strip() == "seen"
    assert result.returncode == (0 if pull_works else 1), result.stderr
    assert not auth.exists()


@pytest.mark.asyncio
async def test_refused_credentials_say_where_they_came_from(tmp_path, monkeypatch):
    _hub_env(monkeypatch)
    environment = _environment(
        tmp_path, docker_image="python:3.12", registry_auth="env"
    )
    sandbox = _Sandbox()
    sandbox.answer = lambda cmd: (
        ExecResult(
            stdout="",
            stderr="Error: GET https://auth.docker.io/token?scope=x: unexpected status "
            'code 401 Unauthorized: {"details":"incorrect username or password"}',
            exit_code=1,
        )
        if "crane" in cmd
        else None
    )

    async def create(template, **kwargs):
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    with pytest.raises(
        RuntimeError, match="DOCKERHUB_USERNAME/DOCKERHUB_TOKEN"
    ) as info:
        await environment.start(force_build=False)
    assert "index.docker.io refused the credentials" in str(info.value)
    assert FAKE_TOKEN not in str(info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image", "registry_auth", "stderr", "hint", "absent"),
    [
        # Docker Hub's limit, with a token: the account's own limit.
        (
            "python:3.12",
            "env",
            "TOOMANYREQUESTS: You have reached your pull rate limit.",
            "pull limit for the account",
            "registry_auth=env with",
        ),
        # Another registry's limit: no Docker Hub advice.
        (
            "ghcr.io/org/task:1",
            None,
            "TOOMANYREQUESTS: retry later",
            None,
            "Docker Hub",
        ),
        # Valid credentials, but an image they can't read (or a typo).
        (
            "evals/private:1",
            "env",
            "UNAUTHORIZED: authentication required",
            "check that they can read evals/private:1",
            "check the token",
        ),
    ],
)
async def test_pull_failures_get_the_hint_that_fits(
    tmp_path, monkeypatch, image, registry_auth, stderr, hint, absent
):
    _hub_env(monkeypatch)
    environment = _environment(
        tmp_path, docker_image=image, registry_auth=registry_auth
    )
    sandbox = _Sandbox()
    sandbox.answer = lambda cmd: (
        ExecResult(stdout="", stderr=stderr, exit_code=1) if "crane" in cmd else None
    )

    async def create(template, **kwargs):
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    with pytest.raises(RuntimeError, match="Could not unpack") as info:
        await environment.start(force_build=False)
    if hint is not None:
        assert hint in str(info.value)
    assert absent not in str(info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("rm_fails", [False, True])
async def test_a_failed_start_removes_the_credentials_before_the_delete(
    tmp_path, monkeypatch, rm_fails
):
    _hub_env(monkeypatch)
    environment = _environment(
        tmp_path, docker_image="python:3.12", registry_auth="env"
    )
    sandbox = _Sandbox()
    events = []

    def answer(cmd):
        if cmd.startswith("rm -rf "):
            events.append(cmd)
            if rm_fails:  # the sandbox no longer answers: the delete still runs
                raise PrizedError("sandbox_unreachable")
            return ExecResult(stdout="", stderr="", exit_code=0)
        # A setup the SDK killed at its timeout: its trap never ran.
        return ExecResult(stdout="", stderr="killed", exit_code=137)

    sandbox.answer = answer

    async def delete():
        events.append("delete")
        sandbox.deleted = True

    sandbox.delete = delete

    async def create(template, **kwargs):
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    with pytest.raises(RuntimeError, match="Could not unpack"):
        await environment.start(force_build=False)
    assert events == ["rm -rf /home/prized/.pull-auth", "delete"]


@pytest.mark.asyncio
async def test_a_failed_start_without_credentials_has_nothing_to_remove(
    tmp_path, monkeypatch
):
    environment = _environment(tmp_path, docker_image="python:3.12")
    sandbox = _Sandbox(exit_code=1)

    async def create(template, **kwargs):
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)
    with pytest.raises(RuntimeError, match="Could not unpack"):
        await environment.start(force_build=False)
    assert all(not e["cmd"].startswith("rm -rf ") for e in sandbox.execs)
    assert sandbox.deleted
