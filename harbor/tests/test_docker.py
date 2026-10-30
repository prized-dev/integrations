"""Docker mode (_docker.py): compose inside a prized-docker-v1 sandbox."""

import io
import json
import shlex
import tarfile

import pytest
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from prized import ExecResult, PrizedError

from harbor_prized import PrizedEnvironment
from harbor_prized import _docker as _docker_module
from harbor_prized import _environment as _module

HOME = "/home/prized"
COMPOSE = f"{HOME}/harbor/compose"
PROJECT = f"{HOME}/harbor/environment"
STAGING = f"{HOME}/harbor/staging/"
UNTAR = 'mkdir -p "$1" && tar --no-same-owner -xzf - -C "$1"'


class _Files:
    def __init__(self) -> None:
        self.content: dict[str, bytes] = {}

    async def write(self, path, data, mode=None):
        # The files API reaches only the sandbox user's home.
        assert path.startswith(HOME + "/"), path
        self.content[path] = data

    async def read_bytes(self, path):
        assert path.startswith(HOME + "/"), path
        if path not in self.content:
            raise PrizedError("not_found")
        return self.content[path]


class _Sandbox:
    id = "sbx_docker"

    def __init__(self) -> None:
        self.files = _Files()
        self.execs: list[dict] = []
        self.answer = None
        self.deleted = self.closed = False

    async def exec(self, cmd, timeout=None, full_output=False, env=None):
        self.execs.append({"cmd": cmd, "timeout": timeout, "env": env})
        if self.answer is not None:
            result = self.answer(cmd)
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
        return ExecResult(stdout="", stderr="", exit_code=0)

    async def delete(self):
        self.deleted = True

    async def close(self):
        self.closed = True

    def compose_calls(self) -> list[list[str]]:
        return [
            shlex.split(e["cmd"])
            for e in self.execs
            if e["cmd"].startswith("/usr/bin/docker compose")
        ]


def _environment(tmp_path, *, files=None, network_policy=None, ek=None, **config):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(exist_ok=True)
    for name, text in (files or {}).items():
        (environment_dir / name).write_text(text)
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return PrizedEnvironment(
        environment_dir=environment_dir,
        environment_name="docker-test",
        session_id="docker-test",
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(**config),
        network_policy=network_policy,
        **({"docker": "true"} if ek is None else ek),
    )


def _fake_create(monkeypatch, sandbox, created=None):
    async def create(template, **kwargs):
        if created is not None:
            created.update(template=template, **kwargs)
        return sandbox

    monkeypatch.setattr(_module.AsyncSandbox, "create", create)


async def _started(tmp_path, monkeypatch, force_build=False, **kwargs):
    environment = _environment(tmp_path, **kwargs)
    sandbox = _Sandbox()
    created = {}
    _fake_create(monkeypatch, sandbox, created)
    await environment.start(force_build=force_build)
    sandbox.execs_at_start = len(sandbox.execs)
    return environment, sandbox, created


def _files_flags(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]


def _staged_after(argv: list[str], op: str) -> str:
    path = argv[argv.index(op) + 1]
    assert path.startswith(STAGING), path
    return path


NO_NETWORK = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
DOCKERFILE = {"Dockerfile": "FROM ubuntu:24.04\n"}


# -- mode and definition --------------------------------------------------------


def test_docker_mode_picks_the_docker_template_and_capabilities(tmp_path):
    environment = _environment(tmp_path, docker_image="ubuntu:24.04")
    assert environment._template == "prized-docker-v1"
    assert environment.capabilities.docker_compose
    assert environment.capabilities.disable_internet
    assert not environment.capabilities.network_allowlist


def test_a_docker_template_implies_docker_mode_unless_turned_off(tmp_path):
    implied = _environment(
        tmp_path, docker_image="ubuntu:24.04", ek={"template": "prized-docker-v1"}
    )
    assert implied._docker
    off = _environment(
        tmp_path,
        docker_image="ubuntu:24.04",
        ek={"template": "prized-docker-v1", "docker": "false"},
    )
    assert not off._docker and off._template == "prized-docker-v1"
    plain = _environment(tmp_path, docker_image="ubuntu:24.04", ek={})
    assert not plain._docker and plain._template == "prized-base-v1"


def test_docker_mode_adds_memory_for_the_daemons(tmp_path):
    environment = _environment(
        tmp_path, docker_image="ubuntu:24.04", cpus=1, memory_mb=2048
    )
    assert environment._shape() == (1, 2560, 30)


def test_bad_docker_flag_is_refused(tmp_path):
    with pytest.raises(ValueError, match="true or false"):
        _environment(tmp_path, docker_image="ubuntu:24.04", ek={"docker": "maybe"})


def test_docker_mode_takes_dockerfile_and_compose_tasks(tmp_path):
    _environment(tmp_path, files=DOCKERFILE)
    _environment(
        tmp_path,
        files={"docker-compose.yaml": "services:\n  main:\n    image: ubuntu:24.04\n"},
    )


def test_docker_mode_still_needs_a_definition_and_a_disk_that_fits(tmp_path):
    with pytest.raises(FileNotFoundError):
        _environment(tmp_path)
    with pytest.raises(ValueError, match="at most 128 GB"):
        _environment(tmp_path, docker_image="ubuntu:24.04", storage_mb=200 * 1024)


# -- start ------------------------------------------------------------------------


async def test_start_runs_compose_from_the_prebuilt_image(tmp_path, monkeypatch):
    _, sandbox, created = await _started(
        tmp_path,
        monkeypatch,
        docker_image="alexgshaw/mailman:20251031",
        cpus=1,
        memory_mb=2048,
        workdir="/app",
    )
    assert created["template"] == "prized-docker-v1"
    assert sandbox.execs[0]["cmd"].startswith("/usr/bin/docker info")
    calls = sandbox.compose_calls()
    build, up = calls[0], calls[1]
    assert build[-1] == "build" and up[-2:] == ["up", "-d"]
    assert up[up.index("-p") + 1] == "harbor"
    assert up[up.index("--project-directory") + 1] == PROJECT
    assert _files_flags(up) == [
        f"{COMPOSE}/docker-compose-resources.json",
        f"{COMPOSE}/docker-compose-prebuilt.yaml",
        f"{COMPOSE}/docker-compose-environment.json",
        f"{COMPOSE}/docker-compose-mounts.json",
    ]
    compose_env = next(e["env"] for e in sandbox.execs if e["cmd"].endswith(" up -d"))
    assert compose_env["PREBUILT_IMAGE_NAME"] == "alexgshaw/mailman:20251031"
    assert compose_env["CONTEXT_DIR"] == PROJECT
    # The CLI keeps its own config dir whatever HOME the task's env sets.
    assert compose_env["DOCKER_CONFIG"] == f"{HOME}/.docker"
    content = sandbox.files.content
    for name in (
        "docker-compose-build.yaml",
        "docker-compose-prebuilt.yaml",
        "docker-compose-mounts.json",
        "docker-compose-environment.json",
    ):
        assert f"{COMPOSE}/{name}" in content
    assert f"{COMPOSE}/docker-compose-no-network.json" not in content
    # As Harbor's Docker provider: the task's cpus and memory cap main.
    resources = json.loads(content[f"{COMPOSE}/docker-compose-resources.json"])
    main = resources["services"]["main"]
    assert (main["cpus"], main["mem_limit"]) == (1.0, "2048m")
    # The task's workdir is made in main, as root.
    assert any(
        c[-6:] == ["-u", "root", "main", "bash", "-c", "mkdir -p /app"] for c in calls
    )


async def test_a_dockerfile_task_is_built_in_the_sandbox(tmp_path, monkeypatch):
    _, sandbox, _ = await _started(tmp_path, monkeypatch, files=DOCKERFILE)
    up = sandbox.compose_calls()[1]
    assert f"{COMPOSE}/docker-compose-build.yaml" in _files_flags(up)
    # The project folder holds the task's environment, unpacked from one archive.
    unpack = [e["cmd"] for e in sandbox.execs if f"-C {PROJECT}" in e["cmd"]]
    assert unpack and unpack[0].startswith(f"mkdir -p {PROJECT} && tar -xzf {STAGING}")


async def test_no_network_closes_every_project_network(tmp_path, monkeypatch):
    compose = (
        "services:\n  main:\n    networks: [back]\n  db:\n    image: redis:7-alpine\n"
        "    networks: [back]\nnetworks:\n  back: {}\n"
    )
    _, sandbox, _ = await _started(
        tmp_path,
        monkeypatch,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=NO_NETWORK,
    )
    flags = _files_flags(sandbox.compose_calls()[1])
    assert flags.index(f"{PROJECT}/docker-compose.yaml") == 2
    assert flags[-2:] == [
        f"{COMPOSE}/docker-compose-no-network.json",
        f"{COMPOSE}/docker-compose-mounts.json",
    ]
    # Internal networks: no route out, but main still reaches its sidecars.
    override = json.loads(
        sandbox.files.content[f"{COMPOSE}/docker-compose-no-network.json"]
    )
    assert override == {
        "networks": {"default": {"internal": True}, "back": {"internal": True}}
    }


@pytest.mark.parametrize(
    ("compose", "match"),
    [
        (
            "services:\n  db:\n    image: x\n    network_mode: host\n",
            "network_mode 'host'",
        ),
        (
            "services: {}\nnetworks:\n  shared:\n    external: true\n",
            "external network",
        ),
    ],
)
async def test_no_network_refuses_what_it_cannot_close(
    tmp_path, monkeypatch, compose, match
):
    environment = _environment(
        tmp_path,
        files={**DOCKERFILE, "docker-compose.yaml": compose},
        network_policy=NO_NETWORK,
    )
    sandbox = _Sandbox()
    _fake_create(monkeypatch, sandbox)
    with pytest.raises(ValueError, match=match):
        await environment.start(force_build=False)
    assert sandbox.deleted


async def test_bind_mounts_live_under_the_home(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="ubuntu:24.04")
    environment._mounts = [
        {"type": "bind", "source": str(tmp_path / "agent"), "target": "/logs/agent"},
        {
            "type": "bind",
            "source": str(tmp_path / "x"),
            "target": "/data/../data",
            "read_only": True,
        },
    ]
    mounts = f"{HOME}/harbor/mounts"
    assert environment._compose._resolve_volumes() == [
        {"type": "bind", "source": f"{mounts}/logs/agent", "target": "/logs/agent"},
        {
            "type": "bind",
            "source": f"{mounts}/data",
            "target": "/data",
            "read_only": True,
        },
    ]
    sandbox = _Sandbox()
    _fake_create(monkeypatch, sandbox)
    await environment.start(force_build=False)
    both = f"{mounts}/logs/agent {mounts}/data"
    assert any(
        e["cmd"] == f"mkdir -p {both} && chmod 777 {both}" for e in sandbox.execs
    )


async def test_a_daemon_that_never_answers_fails_the_start_and_deletes(
    tmp_path, monkeypatch
):
    environment = _environment(
        tmp_path,
        docker_image="ubuntu:24.04",
        ek={"docker": "true", "dockerd_wait_sec": 0},
    )
    sandbox = _Sandbox()
    missing = ExecResult(
        stdout="",
        stderr="bash: /usr/bin/docker: No such file or directory",
        exit_code=127,
    )
    sandbox.answer = lambda cmd: (
        missing if cmd.startswith("/usr/bin/docker info") else None
    )
    _fake_create(monkeypatch, sandbox)
    with pytest.raises(RuntimeError, match="prized-docker-v1"):
        await environment.start(force_build=False)
    assert sandbox.deleted


async def test_a_hung_docker_info_is_asked_again(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="ubuntu:24.04")
    sandbox = _Sandbox()
    infos = []

    def answer(cmd):
        if cmd.startswith("/usr/bin/docker info"):
            infos.append(cmd)
            if len(infos) == 1:
                return _module.CommandTimeoutError(
                    ExecResult(stdout="", stderr="", exit_code=-1), 30.0
                )
        return None

    async def no_sleep(_):
        return None

    sandbox.answer = answer
    _fake_create(monkeypatch, sandbox)
    monkeypatch.setattr(_docker_module.asyncio, "sleep", no_sleep)
    await environment.start(force_build=False)
    assert len(infos) == 2


async def test_a_failed_up_fails_the_start(tmp_path, monkeypatch):
    environment = _environment(tmp_path, docker_image="ubuntu:24.04")
    sandbox = _Sandbox()
    refused = ExecResult(stdout="", stderr="toomanyrequests", exit_code=1)
    sandbox.answer = lambda cmd: refused if cmd.endswith(" up -d") else None
    _fake_create(monkeypatch, sandbox)
    with pytest.raises(
        RuntimeError, match=r"docker compose up failed: .*toomanyrequests"
    ):
        await environment.start(force_build=False)
    assert sandbox.deleted


# -- commands -----------------------------------------------------------------------


async def test_the_task_env_is_mains_environment(tmp_path, monkeypatch):
    _, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04", env={"TASK_VAR": "t"}
    )
    staged = sandbox.files.content[f"{COMPOSE}/docker-compose-environment.json"]
    assert json.loads(staged)["services"]["main"]["environment"]["TASK_VAR"] == "t"


async def test_exec_runs_a_non_login_bash_in_main(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04", workdir="/app"
    )
    sandbox.execs.clear()
    await environment.exec("echo hi", env={"A": "1"}, user="git", timeout_sec=30)
    argv = sandbox.compose_calls()[0]
    tail = argv[argv.index("exec") :]
    assert tail[:4] == ["exec", "-T", "-w", "/app"]
    assert "A=1" in tail
    # bash -c, not -lc: a login shell would replace the image's PATH (any Harbor).
    assert tail[-6:] == ["-u", "git", "main", "bash", "-c", "echo hi"]
    assert sandbox.execs[0]["timeout"] == 30


async def test_a_long_command_goes_through_a_staged_file(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    command = "echo '" + "x" * 40_000 + "'"
    await environment.exec(command)
    host = sandbox.execs[-1]["cmd"]
    staged = host.split("$(cat ", 1)[1].split(")", 1)[0]
    assert staged.startswith(STAGING)
    assert sandbox.files.content[staged] == command.encode()
    assert host.startswith(
        f'c="$(cat {staged})" && rm -f {staged} && exec /usr/bin/docker'
    )
    assert host.endswith(' main bash -c "$c"') and len(host) < 2000


async def test_sidecar_commands_use_sh(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    await environment.service_exec("redis-cli ping", service="db")
    argv = sandbox.compose_calls()[0]
    assert argv[-4:] == ["db", "sh", "-c", "redis-cli ping"]


# -- files --------------------------------------------------------------------------


async def test_upload_file_streams_a_root_owned_tar_into_main(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    source = tmp_path / "solve.sh"
    source.write_text("echo solved\n")
    source.chmod(0o755)
    seen = {}

    def answer(cmd):
        if UNTAR in cmd:
            argv = shlex.split(cmd)
            seen["argv"] = argv
            seen["tar"] = sandbox.files.content[_staged_after(argv, "<")]

    sandbox.answer = answer
    await environment.upload_file(source, "/solution/solve.sh")
    argv = seen["argv"]
    head = argv[argv.index("exec") :][:8]
    assert head == ["exec", "-T", "-u", "0", "main", "sh", "-c", UNTAR]
    assert argv[argv.index(UNTAR) + 1 : argv.index("<")] == ["sh", "/solution"]
    with tarfile.open(fileobj=io.BytesIO(seen["tar"])) as tar:
        (member,) = tar.getmembers()
        assert (member.name, member.uid, member.gid) == ("solve.sh", 0, 0)
        assert member.mode == 0o755
        assert tar.extractfile(member).read() == b"echo solved\n"
    # The staged archive is removed.
    assert sandbox.execs[-1]["cmd"].startswith(f"rm -f {STAGING}")


async def test_upload_falls_back_to_compose_cp_without_tar(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    no_tar = ExecResult(stdout="", stderr="sh: 1: tar: not found", exit_code=127)
    sandbox.answer = lambda cmd: no_tar if UNTAR in cmd else None
    source = tmp_path / "f.txt"
    source.write_text("x")
    await environment.upload_file(source, "/x/f.txt")
    cp = [c for c in sandbox.compose_calls() if "cp" in c]
    assert cp and cp[-1][-1] == "main:/x/f.txt"


async def test_upload_dir_streams_into_main(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("exit 0\n")
    await environment.upload_dir(tests, "/tests")
    argv = next(c for c in sandbox.compose_calls() if UNTAR in c)
    assert argv[argv.index(UNTAR) + 1 : argv.index("<")] == ["sh", "/tests"]


async def test_download_file_streams_out_of_main_as_root(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()

    def answer(cmd):
        if 'cat -- "$1"' in cmd:
            argv = shlex.split(cmd)
            assert argv[argv.index("exec") :][:5] == [
                "exec",
                "-T",
                "-u",
                "0",
                "main",
            ]
            assert argv[argv.index(">") - 1] == "/logs/verifier/reward.txt"
            sandbox.files.content[_staged_after(argv, ">")] = b"1\n"

    sandbox.answer = answer
    target = tmp_path / "out" / "reward.txt"
    await environment.download_file("/logs/verifier/reward.txt", target)
    assert target.read_bytes() == b"1\n"


async def test_download_dir_streams_a_tar_out_of_main(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        data = b"log line\n"
        info = tarfile.TarInfo("./agent.log")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    changed = ExecResult(
        stdout="", stderr="tar: ./agent.log: file changed as we read it", exit_code=1
    )

    def answer(cmd):
        if 'tar -C "$1" -czf - .' in cmd:
            argv = shlex.split(cmd)
            assert argv[argv.index(">") - 1] == "/logs/agent"
            sandbox.files.content[_staged_after(argv, ">")] = payload.getvalue()
            return changed  # logs still being written: accepted, as GNU tar warns
        return None

    sandbox.answer = answer
    await environment.download_dir("/logs/agent", tmp_path / "agent")
    assert (tmp_path / "agent" / "agent.log").read_bytes() == b"log line\n"


async def test_sidecar_downloads_use_compose_cp(tmp_path, monkeypatch):
    environment, sandbox, _ = await _started(
        tmp_path, monkeypatch, docker_image="ubuntu:24.04"
    )
    sandbox.execs.clear()

    def answer(cmd):
        if cmd.startswith("cp -L /tmp/harbor_"):
            sandbox.files.content[shlex.split(cmd)[3]] = b"state\n"

    sandbox.answer = answer
    await environment.service_download_file(
        "/data/state", tmp_path / "state", service="db"
    )
    cp = next(c for c in sandbox.compose_calls() if "cp" in c)
    assert cp[-2] == "db:/data/state" and cp[-1].startswith("/tmp/harbor_")
    assert (tmp_path / "state").read_bytes() == b"state\n"
