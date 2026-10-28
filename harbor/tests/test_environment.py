import base64
import os
from pathlib import Path

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from prized import CommandTimeoutError, ExecResult

from harbor_prized import PrizedEnvironment
from harbor_prized import _environment as _module


class _Files:
    def __init__(self) -> None:
        self.written: list[tuple[str, bytes, int | None]] = []

    async def write(self, path, data, mode=None):
        self.written.append((path, data, mode))

    async def read_bytes(self, path):
        return b""


class _Sandbox:
    """Stands in for prized.AsyncSandbox: records each command it is given."""

    id = "sbx_test"

    def __init__(self, exit_code: int = 0, raises: Exception | None = None) -> None:
        self.files = _Files()
        self.execs: list[dict] = []
        self.deleted = self.closed = False
        self._exit_code, self._raises = exit_code, raises

    async def exec(self, cmd, timeout=None, full_output=False):
        self.execs.append({"cmd": cmd, "timeout": timeout, "full_output": full_output})
        if self._raises is not None:
            raise self._raises
        return ExecResult(stdout="out", stderr="err", exit_code=self._exit_code)

    async def delete(self):
        self.deleted = True

    async def close(self):
        self.closed = True


def _environment(tmp_path, **config) -> PrizedEnvironment:
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
    )


def _script(command: str) -> list[str]:
    assert command.startswith("/home/prized/enter.sh - ")
    return base64.b64decode(command.split(" ", 2)[2]).decode().splitlines()


def test_requires_prebuilt_image(tmp_path):
    with pytest.raises(ValueError, match="docker_image"):
        _environment(tmp_path)


def test_labels_from_key_value_string(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    assert environment._labels == {"harbor": "1", "team": "evals", "run": "42"}


@pytest.mark.asyncio
async def test_exec_runs_in_image_with_env_and_cwd(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._image_env = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    environment._image_workdir = "/app"
    environment._sandbox = sandbox = _Sandbox(exit_code=3)

    result = await environment.exec("echo hi", env={"KEY": "va lue"}, timeout_sec=30)

    assert (result.stdout, result.stderr, result.return_code) == ("out", "err", 3)
    assert sandbox.execs[0]["timeout"] == 30
    assert sandbox.execs[0]["full_output"] is True
    assert _script(sandbox.execs[0]["cmd"]) == [
        'rm -f -- "$0"',
        "export PATH=/usr/local/bin:/usr/bin:/bin",
        "export KEY='va lue'",
        "cd /app || exit 1",
        "echo hi",
    ]


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
async def test_long_scripts_are_uploaded(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()

    await environment.exec("x" * 70_000)

    path = sandbox.files.written[0][0]
    assert path.startswith("/home/prized/rootfs/.runtime-xfer/cmd-")
    assert sandbox.execs[0]["cmd"] == (
        f"/home/prized/enter.sh {path.removeprefix('/home/prized/rootfs')}"
    )


@pytest.mark.asyncio
async def test_upload_keeps_mode_without_setuid_bits(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()
    source = tmp_path / "tool"
    source.write_text("#!/bin/sh\n")
    source.chmod(0o755)
    real_stat = Path.stat

    def setuid_stat(self, *args, **kwargs):  # some sandboxes refuse chmod u+s
        st = real_stat(self, *args, **kwargs)
        if self != source:
            return st
        return os.stat_result((st.st_mode | 0o4000, *tuple(st)[1:]))

    monkeypatch.setattr(Path, "stat", setuid_stat)
    await environment.upload_file(source, "/usr/local/bin/tool")

    assert _script(sandbox.execs[0]["cmd"])[-1].endswith(
        "&& chmod 755 /usr/local/bin/tool"
    )


@pytest.mark.asyncio
async def test_stop_deletes_the_sandbox(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._sandbox = sandbox = _Sandbox()

    await environment.stop(delete=True)

    assert (sandbox.deleted, sandbox.closed, environment._sandbox) == (True, True, None)


def test_dpkg_hook_opens_up_files_left_at_mode_0():
    hook = _module._DPKG_HOOK
    assert hook.startswith("post-invoke=find /usr ")
    assert "-perm 0000 -exec chmod 0755 {} +" in hook


def test_refuses_compose_tasks(tmp_path):
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "docker-compose.yaml").write_text("services: {}\n")
    with pytest.raises(ValueError, match="Docker Compose"):
        _environment(tmp_path, docker_image="python:3.12")


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
    await environment.upload_file(source, "/root/secret")

    assert _script(sandbox.execs[0]["cmd"])[-1].endswith("&& chmod 0 /root/secret")


def test_holder_failure_is_reported_and_the_script_removed():
    script = _module._ENTER_SCRIPT
    assert "could not start the sandbox's namespace holder" in script
    assert 'rm -f -- "$f" "$t"; exit 1' in script
    assert f"mkdir -p {_module._ROOT}{_module._XFER}" in script
