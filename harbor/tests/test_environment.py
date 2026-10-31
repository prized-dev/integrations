import asyncio
import base64
import io
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

    async def close(self):
        self.closed = True


def _environment(tmp_path, network_policy=None, **config) -> PrizedEnvironment:
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
    )


def _script(command: str) -> list[str]:
    assert command.startswith(RUNTIME)
    return base64.b64decode(command.split(" - ", 1)[1]).decode().splitlines()


def _runtime_calls(sandbox: _Sandbox) -> list[str]:
    return [e["cmd"] for e in sandbox.execs if e["cmd"].startswith(RUNTIME)]


# -- definition and shape ---------------------------------------------------


def test_requires_prebuilt_image(tmp_path):
    with pytest.raises(ValueError, match="docker_image"):
        _environment(tmp_path)


def test_refuses_compose_tasks(tmp_path):
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "docker-compose.yaml").write_text("services: {}\n")
    with pytest.raises(ValueError, match="Docker Compose"):
        _environment(tmp_path, docker_image="python:3.12")


def test_refuses_storage_past_the_largest_disk(tmp_path):
    with pytest.raises(ValueError, match="at most 128 GB"):
        _environment(tmp_path, docker_image="python:3.12", storage_mb=200_000)


@pytest.mark.parametrize("mode", [NetworkMode.NO_NETWORK, NetworkMode.ALLOWLIST])
def test_refuses_network_policies(tmp_path, mode):
    hosts = ["pypi.org"] if mode == NetworkMode.ALLOWLIST else []
    policy = NetworkPolicy(network_mode=mode, allowed_hosts=hosts)
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
    assert (created["max_runtime_min"], created["auto_delete_min"]) == (1440, 10080)
    setup = sandbox.execs[0]["cmd"]
    assert "set -e; cd ~; umask 022; " in setup
    assert '"${PIPESTATUS[0]} ${PIPESTATUS[1]}"' in setup  # crane checked on its own
    assert "--preserve-permissions" in setup
    assert (environment._image_env, environment._image_workdir) == ({"A": "1"}, "/w")
    assert environment._image_user == "app"
    assert sandbox.files.written[0][0] == "/home/prized/runtime.py"


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

    sandboxes.clear()
    monkeypatch.setattr(
        _module.AsyncSandbox, "create", _always(PrizedError("network_error"), sandboxes)
    )
    with pytest.raises(PrizedError):
        await environment.start(force_build=False)
    assert [s.deleted for s in sandboxes] == [True, True]  # twice at most


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
async def test_exec_as_a_user_runs_with_its_ids_and_home(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._image_env = {"PATH": "/usr/bin:/bin"}
    environment._sandbox = sandbox = _Sandbox()

    await environment.exec("id", user="agent")
    with environment.with_default_user(1003):
        await environment.exec("id")
    await environment.exec("id", user="agent:staff")
    await environment.exec("id", user="4242")

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
