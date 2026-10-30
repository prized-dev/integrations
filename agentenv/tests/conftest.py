from __future__ import annotations

import pytest
from agent_env.config import reset_config

from agentenv_prized.provider import PrizedSandboxProvider
from agentenv_prized.sandbox import ROOT_PROBE

from .fakes import FakeCloud, Result

FAKE_KEY = "dcp_0000000000000000000000000000000000000000"


@pytest.fixture(autouse=True)
def _agentenv_config(tmp_path_factory, monkeypatch):
    """An empty agent-env config of the test's own, so no config file on this machine is read."""
    directory = tmp_path_factory.mktemp("agentenv-config")
    (directory / "config.toml").write_text("")
    monkeypatch.setenv("AGENT_ENV_CONFIG", str(directory / "config.toml"))
    reset_config()
    yield
    reset_config()


@pytest.fixture
def cloud(tmp_path):
    return FakeCloud(tmp_path)


@pytest.fixture
def provider(cloud):
    return PrizedSandboxProvider(api_key=FAKE_KEY, sandbox_cls=cloud.cls())


def root_mode(cloud: FakeCloud, mode: str) -> None:
    """Have the sandbox answer the root probe with ``mode``."""
    cloud.answer(lambda command: Result(f"{mode}\n") if "echo none" in command and "docker info" in command else None)


def docker_ready(cloud: FakeCloud, ok: bool = True) -> None:
    """Have the sandbox answer the Docker readiness script."""
    result = (
        Result("Docker Compose version v5.6.1\n")
        if ok
        else Result(
            stderr="this sandbox's template has no Docker; use a template with Docker, such as prized-docker-v1\n",
            exit_code=3,
        )
    )
    cloud.answer(lambda command: result if "docker compose version" in command else None)


__all__ = ["FAKE_KEY", "ROOT_PROBE", "docker_ready", "root_mode"]
