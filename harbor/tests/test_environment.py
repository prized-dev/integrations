import base64
import json

import httpx
import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from harbor_prized import PrizedEnvironment


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


def _attach(environment: PrizedEnvironment, handler) -> None:
    environment._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    environment._sandbox_id = "sbx_test"
    environment._toolbox = "https://edge.example/v1/sandbox/sbx_test"


def test_requires_prebuilt_image(tmp_path):
    with pytest.raises(ValueError, match="docker_image"):
        _environment(tmp_path)


def test_labels_from_key_value_string(tmp_path):
    environment = _environment(tmp_path, docker_image="python:3.12")
    assert environment._labels == {"harbor": "1", "team": "evals", "run": "42"}


@pytest.mark.asyncio
async def test_exec_runs_in_image_with_env_and_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIZED_API_KEY", "dcp_test")
    environment = _environment(tmp_path, docker_image="python:3.12")
    environment._image_env = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    environment._image_workdir = "/app"
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"stdout": "out", "stderr": "err", "exitCode": 3}
        )

    _attach(environment, handler)
    result = await environment.exec("echo hi", env={"KEY": "va lue"}, timeout_sec=30)

    assert (result.stdout, result.stderr, result.return_code) == ("out", "err", 3)
    command = sent[0]["command"]
    assert sent[0]["timeout"] == 30
    assert command.startswith("/home/prized/harbor-enter.sh - ")
    script = base64.b64decode(command.split(" ", 2)[2]).decode()
    assert script.splitlines() == [
        "export PATH=/usr/local/bin:/usr/bin:/bin",
        "export KEY='va lue'",
        "cd /app || exit 1",
        "echo hi",
    ]


@pytest.mark.asyncio
async def test_long_scripts_are_uploaded(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIZED_API_KEY", "dcp_test")
    environment = _environment(tmp_path, docker_image="python:3.12")
    paths: list[str] = []
    commands: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files/upload"):
            paths.append(request.url.params["path"])
            return httpx.Response(200, json={"path": "x", "size": 1})
        commands.append(json.loads(request.content)["command"])
        return httpx.Response(200, json={"stdout": "", "stderr": "", "exitCode": 0})

    _attach(environment, handler)
    await environment.exec("x" * 70_000)

    assert paths[0].startswith("/home/prized/rootfs/.harbor-xfer/cmd-")
    assert commands == [
        f"/home/prized/harbor-enter.sh {paths[0].removeprefix('/home/prized/rootfs')}"
    ]
    assert json.dumps(commands)  # the command itself stays small


@pytest.mark.asyncio
async def test_retries_before_the_command_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIZED_API_KEY", "dcp_test")
    monkeypatch.setattr("harbor_prized._environment.asyncio.sleep", _no_sleep)
    environment = _environment(tmp_path, docker_image="python:3.12")
    answers = [httpx.Response(503), httpx.Response(429)]

    def handler(request: httpx.Request) -> httpx.Response:
        if answers:
            return answers.pop(0)
        return httpx.Response(200, json={"stdout": "ok", "stderr": "", "exitCode": 0})

    _attach(environment, handler)
    result = await environment._host_exec("true")
    assert result.stdout == "ok"


async def _no_sleep(_seconds: float) -> None:
    return None
