# SPDX-License-Identifier: BSD-3-Clause

"""Unit tests for PrizedProvider.

Provider-behavior tests inject a duck-typed fake adapter (``_adapter=``), so
they run without ``pip install prized`` or a Prized account. A loopback FastAPI
app stands in for a sandbox port behind Prized's edge (it requires the
``Prized-Key`` header), so the default private-port path is exercised end to
end through the provider's local auth proxy, including a real WebSocket. The
generated install script runs under ``sh`` with stub ``git`` and ``uv``. The
real ``_DefaultPrizedAdapter`` is pinned against a fake ``prized`` module, and
against the installed SDK's signatures when ``prized`` is installed.
"""

from __future__ import annotations

import inspect
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import types

import pytest
import requests
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from openenv_prized import provider as prized_provider
from openenv.core.containers.runtime.hf_sandbox_provider import _find_available_port
from openenv_prized.provider import (
    _DefaultPrizedAdapter,
    _parse_source,
    _tar_directory,
    _uvicorn_app,
    PrizedProvider,
)
from openenv.core.containers.runtime.providers import ContainerProvider

_KEY = "pz-test-key"


# ---------------------------------------------------------------------------
# A sandbox port behind Prized's edge: needs Prized-Key, which the app never sees
# ---------------------------------------------------------------------------
class _FakeEdgePort:
    def __init__(self):
        self.port = _find_available_port()
        self.app_headers: list[dict] = []
        self.healthy = True
        app = FastAPI()

        def allowed(headers) -> bool:
            return headers.get("prized-key") == _KEY

        async def health(request: Request):
            if not allowed(request.headers):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            self.app_headers.append(dict(request.headers))
            if not self.healthy:
                return JSONResponse({"status": "starting"}, status_code=502)
            return {"status": "healthy", "path": request.url.path}

        async def ws(websocket: WebSocket):
            if not allowed(websocket.headers):
                await websocket.close(code=1008)
                return
            await websocket.accept()
            while True:
                message = await websocket.receive_text()
                await websocket.send_text(f"echo:{message}")

        # /health and /ws for a provider's sandbox; /s/<id>/... for fork copies.
        app.get("/health")(health)
        app.get("/s/{sid}/health")(health)
        app.websocket("/ws")(ws)

        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self._thread.start()
        while not self._server.started:
            time.sleep(0.02)

    def stop(self):
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture(scope="module")
def edge():
    server = _FakeEdgePort()
    server.start()
    yield server
    server.stop()


# ---------------------------------------------------------------------------
# Fake adapter (duck-typed to _DefaultPrizedAdapter)
# ---------------------------------------------------------------------------
class _FakeSandbox:
    _next = 0

    def __init__(self):
        _FakeSandbox._next += 1
        self.id = f"sbx_{_FakeSandbox._next:016x}"


class _FakeAdapter:
    def __init__(self, port_url="https://8000-sbx-0001.p.prized.dev"):
        self.created: list[dict] = []
        self.commands: list[str] = []
        self.command_env: list[dict | None] = []
        self.background: list[str] = []
        self.writes: list[dict] = []
        self.deleted: list[str] = []
        self.public_calls: list[dict] = []
        self.forks: list[dict] = []
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.port_url_value = port_url
        self.per_sandbox_urls = False
        self.fail_create = False
        self.fail_delete = 0
        self.install_exit = 0
        self.interrupt_install = False
        self.has_docker = True
        self.docker_run_exit = 0
        self.server_state = "RUNNING"
        self.fail_liveness = False
        self.yaml = "spec_version: 1\nname: echo_env\napp: server.app:app\nport: 8000\n"
        self.dockerfiles: dict[str, str] = {}
        self.log = "Traceback: boom, token=supersecret"

    def create_sandbox(self, **kwargs):
        if self.fail_create:
            raise RuntimeError("create failed")
        sandbox = _FakeSandbox()
        self.created.append({**kwargs, "sandbox": sandbox})
        return sandbox

    def exec(self, sandbox, command, *, timeout=60, env=None, background=False):
        self.commands.append(command)
        self.command_env.append(env)
        if background:
            self.background.append(command)
            return 0, ""
        if "OPENENV_ROOT=" in command:
            if self.interrupt_install:
                raise KeyboardInterrupt
            if self.install_exit:
                return self.install_exit, ""
            return 0, "OPENENV_ROOT=/home/prized/openenv/env\n"
        if command.startswith("cat ") and "openenv.yaml" in command:
            return (0, self.yaml) if self.yaml is not None else (1, "")
        if command.startswith("cat ") and "Dockerfile" in command:
            for name, content in self.dockerfiles.items():
                if command.split()[1].endswith("/" + name):
                    return 0, content
            return 1, ""
        if command.startswith("docker version"):
            return (0, "") if self.has_docker else (127, "")
        if "docker pull" in command:
            return self.docker_run_exit, ""
        if self.fail_liveness and ("kill -0" in command or "docker inspect" in command):
            raise RuntimeError("edge hiccup")
        if "docker inspect" in command:
            return 0, "false\n" if self.server_state == "DEAD" else "true\n"
        if "kill -0" in command:
            return 0, self.server_state + "\n"
        if command.startswith("tail -c") or command.startswith("docker logs"):
            return 0, self.log
        return 0, ""

    def write(self, sandbox, path, data, *, mode):
        self.writes.append({"path": path, "data": data, "mode": mode})

    def port_url(self, sandbox, port):
        if self.per_sandbox_urls:
            return f"{self.port_url_value}/s/{sandbox.id}"
        return self.port_url_value

    def port_headers(self, sandbox):
        return {"Prized-Key": _KEY}

    def set_port_public(self, sandbox, port, *, expires_in):
        self.public_calls.append({"port": port, "expires_in": expires_in})
        return f"https://{port}-{sandbox.id.replace('_', '-')}.p.prized.dev/"

    def pause(self, sandbox):
        self.paused.append(sandbox.id)

    def resume(self, sandbox):
        self.resumed.append(sandbox.id)

    def fork(self, sandbox, *, count, labels):
        copies = [_FakeSandbox() for _ in range(count)]
        self.forks.append({"parent": sandbox.id, "count": count, "labels": labels})
        return copies

    def sandbox_id(self, sandbox):
        return sandbox.id

    def delete(self, sandbox):
        if self.fail_delete:
            self.fail_delete -= 1
            raise RuntimeError("delete failed")
        self.deleted.append(sandbox.id)


@pytest.fixture()
def adapter(edge):
    return _FakeAdapter(port_url=edge.url)


@pytest.fixture(autouse=True)
def _no_poll_sleep(monkeypatch):
    # Only the provider's own readiness pause; threads keep their real sleep.
    monkeypatch.setattr(prized_provider, "_sleep", lambda seconds: None)


def _started(adapter, **kwargs) -> PrizedProvider:
    provider = PrizedProvider(_adapter=adapter, **kwargs)
    provider.start_container(kwargs.get("image") or "space:openenv/echo_env")
    return provider


def _install(adapter) -> str:
    return next(c for c in adapter.commands if "OPENENV_ROOT=" in c)


# ---------------------------------------------------------------------------
# Contract and sources
# ---------------------------------------------------------------------------
def test_is_a_container_provider():
    assert issubclass(PrizedProvider, ContainerProvider)


@pytest.mark.parametrize(
    ("image", "kind", "location", "ref", "subdirectory"),
    [
        (
            "space:openenv/echo_env",
            "git",
            "https://huggingface.co/spaces/openenv/echo_env",
            None,
            None,
        ),
        (
            "space:openenv/echo_env@abc123",
            "git",
            "https://huggingface.co/spaces/openenv/echo_env",
            "abc123",
            None,
        ),
        (
            "git+https://github.com/org/env.git@v1.2",
            "git",
            "https://github.com/org/env.git",
            "v1.2",
            None,
        ),
        (
            "git+https://github.com/org/env@feature/x",
            "git",
            "https://github.com/org/env",
            "feature/x",
            None,
        ),
        (
            "git+https://github.com/org/env",
            "git",
            "https://github.com/org/env",
            None,
            None,
        ),
        (
            "git+https://github.com/openenv/OpenEnv@main#subdirectory=envs/echo_env",
            "git",
            "https://github.com/openenv/OpenEnv",
            "main",
            "envs/echo_env",
        ),
        (
            "registry.hf.space/openenv-echo-env:latest",
            "oci",
            "registry.hf.space/openenv-echo-env:latest",
            None,
            None,
        ),
    ],
)
def test_parse_source(image, kind, location, ref, subdirectory):
    assert _parse_source(image) == {
        "kind": kind,
        "location": location,
        "ref": ref,
        "subdirectory": subdirectory,
    }


@pytest.mark.parametrize(
    "image",
    [
        "",
        "  ",
        "space:echo_env",
        "space:a/b/c",
        "git+http://github.com/org/env",
        "git+https://github.com/org/env?x=1",
        "git+https://github.com/org/env#egg=env",
        "git+https://github.com/org/env#subdirectory=../etc",
        "git+https://github.com/org/env#subdirectory=/abs",
        "dir:",
    ],
)
def test_parse_source_rejects_bad_specs(image):
    with pytest.raises(ValueError):
        _parse_source(image)


@pytest.mark.parametrize(
    "image",
    [
        "git+https://ghp_secrettoken@github.com/org/env",
        "git+https://u:ghp_secrettoken@github.com/o/r",
    ],
)
def test_git_credentials_are_refused_without_echoing_them(image):
    with pytest.raises(ValueError, match="credentials") as info:
        _parse_source(image)
    assert "ghp_secrettoken" not in str(info.value)


def test_space_source_installs_with_uv_and_proxies_with_key(adapter, edge):
    provider = PrizedProvider(
        _adapter=adapter, env_vars={"API_TOKEN": "supersecret"}, labels={"run": "r1"}
    )
    base_url = provider.start_container("space:openenv/echo_env")

    created = adapter.created[0]
    assert created["template"] == "prized-base-v1"
    assert created["labels"] == {"openenv": "server", "run": "r1"}
    assert created["max_runtime_min"] == 60
    # Auto-pause off (port traffic is not activity), paused copies deleted.
    assert created["auto_pause_min"] == 0
    assert created["auto_delete_min"] == 60

    install = _install(adapter)
    assert (
        "git clone --quiet --depth 1 https://huggingface.co/spaces/openenv/echo_env"
        in install
    )
    assert '"$UV" sync' in install

    launch = adapter.background[0]
    assert "uvicorn server.app:app --host 0.0.0.0 --port 8000" in launch
    assert "/home/prized/openenv/server.pid" in launch
    # Secrets travel in the exec request's env, never on a command line.
    assert all("supersecret" not in command for command in adapter.commands)
    assert adapter.command_env[adapter.commands.index(launch)] == {
        "API_TOKEN": "supersecret"
    }

    # Default: private port behind a loopback proxy that adds Prized-Key.
    assert base_url.startswith("http://127.0.0.1:")
    assert adapter.public_calls == []
    provider.wait_for_ready(base_url, timeout_s=5)
    assert edge.app_headers[-1]["prized-key"] == _KEY
    provider.stop_container()


def test_websocket_round_trip_through_the_proxy(adapter):
    """A real WebSocket through the proxy, as EnvClient opens it."""
    from websockets.sync.client import connect

    provider = _started(adapter)
    ws_url = provider.base_url.replace("http://", "ws://") + "/ws"
    with connect(ws_url) as ws:
        ws.send("hello")
        assert ws.recv(timeout=5) == "echo:hello"
    provider.stop_container()


def test_git_ref_and_subdirectory(adapter):
    provider = _started(
        adapter,
        image="git+https://github.com/org/mono@feature/x#subdirectory=envs/echo_env",
    )
    install = _install(adapter)
    assert (
        "git clone --quiet https://github.com/org/mono /home/prized/openenv/env"
        in install
    )
    assert "checkout --quiet feature/x" in install
    assert "BASE=/home/prized/openenv/env/envs/echo_env;" in install
    provider.stop_container()


def test_dir_source_uploads_a_clean_tarball(adapter, tmp_path):
    env = tmp_path / "my_env"
    (env / "server").mkdir(parents=True)
    (env / "openenv.yaml").write_text("app: server.app:app\n")
    (env / "server" / "app.py").write_text("app = None\n")
    (env / ".venv").mkdir()
    (env / ".venv" / "big").write_text("x")
    (env / "server" / "__pycache__").mkdir()
    (env / "server" / "__pycache__" / "app.pyc").write_bytes(b"\0")
    (env / ".env").write_text("TOKEN=supersecret\n")
    os.symlink("/etc/passwd", env / "link")
    os.link(env / "server" / "app.py", env / "server" / "hard.py")
    alias = tmp_path / "alias"
    os.symlink(env, alias)

    # A symlink to the directory is followed: the archive is not empty.
    provider = _started(adapter, image=f"dir:{alias}")

    upload = adapter.writes[0]
    assert upload["path"] == "/home/prized/openenv/src.tgz"
    assert upload["mode"] == 0o600
    names = tarfile.open(fileobj=io.BytesIO(upload["data"])).getnames()
    assert {"./openenv.yaml", "./server/app.py", "./server/hard.py"} <= set(names)
    assert not any(
        ".venv" in n or "__pycache__" in n or n.endswith(("link", ".env"))
        for n in names
    )
    assert "tar -xzf /home/prized/openenv/src.tgz" in _install(adapter)
    provider.stop_container()


def test_dir_source_must_exist(adapter, tmp_path):
    with pytest.raises(ValueError, match="dir: source"):
        PrizedProvider(_adapter=adapter).start_container(f"dir:{tmp_path / 'missing'}")
    assert adapter.created == []


def test_dir_source_size_is_capped_while_packing(monkeypatch, tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 2048)
    monkeypatch.setattr(prized_provider, "_MAX_UNPACKED_BYTES", 1024)
    with pytest.raises(ValueError, match="unpacked"):
        _tar_directory(str(tmp_path))


def test_explicit_cmd_and_install_cmd(adapter):
    provider = _started(
        adapter, cmd="python -m server.app", install_cmd="pip install -e ."
    )
    install = _install(adapter)
    assert "pip install -e ." in install and '"$UV" sync' not in install
    assert "python -m server.app" in adapter.background[0]
    assert not any(c.startswith("cat ") for c in adapter.commands)
    provider.stop_container()


def test_missing_app_field_uses_the_dockerfile_cmd(adapter):
    adapter.yaml = "name: coding_env\naction: CodeAction\n"
    adapter.dockerfiles = {
        "Dockerfile": 'FROM python:3.11\nCMD ["uvicorn", "coding_env.server.app:app", '
        '"--host", "0.0.0.0", "--port", "8000"]\n'
    }
    provider = _started(adapter)
    assert "uvicorn coding_env.server.app:app --host 0.0.0.0" in adapter.background[0]
    provider.stop_container()


def test_missing_app_field_and_dockerfile_falls_back_to_server_app(adapter):
    adapter.yaml = None
    adapter.dockerfiles = {"server/Dockerfile": "FROM python:3.11\nCMD python run.py\n"}
    provider = _started(adapter)
    assert "uvicorn server.app:app --host 0.0.0.0 --port 8000" in adapter.background[0]
    provider.stop_container()


@pytest.mark.parametrize(
    ("cmd", "app"),
    [
        ("uvicorn coding_env.server.app:app --port 8000", "coding_env.server.app:app"),
        ("/app/.venv/bin/uvicorn server.app:app", "server.app:app"),
        (
            "python -m uvicorn envs.x.server.app:create_app --factory",
            "envs.x.server.app:create_app",
        ),
        ("python run.py", None),
        ("uvicorn 'unbalanced", None),
    ],
)
def test_uvicorn_app_from_cmd(cmd, app):
    assert _uvicorn_app(cmd) == app


# ---------------------------------------------------------------------------
# The generated install script, run under sh with stub git and uv
# ---------------------------------------------------------------------------
_needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def _stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_install(tmp_path, image, *, git="", uv="exit 0\n", with_uv=True):
    """Run the provider's install script for *image* with stubs; return (code, out)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The clone destination is git's last argument; the stub fills it.
    _stub(
        bin_dir,
        "git",
        'for last; do :; done\n[ "$1" = clone ] || exit 0\nmkdir -p "$last"\n' + git,
    )
    if with_uv:
        _stub(bin_dir, "uv", uv)
    _stub(bin_dir, "python3", "exit 1\n")
    provider = PrizedProvider(_adapter=_FakeAdapter())
    provider._root = str(tmp_path / "root")
    script = provider._install_script(_parse_source(image), None)
    result = subprocess.run(
        ["sh", "-c", script],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout


@_needs_sh
def test_install_script_picks_the_shallowest_env_root(tmp_path):
    code, out = _run_install(
        tmp_path,
        "git+https://example.com/mono",
        # Alphabetically envs/a comes first; by depth, z does.
        git='mkdir -p "$last/envs/a" "$last/z"\n'
        'touch "$last/envs/a/openenv.yaml" "$last/z/openenv.yaml"\n',
    )
    assert code == 0
    assert out.strip() == f"OPENENV_ROOT={tmp_path}/root/env/z"


@_needs_sh
def test_install_script_uses_the_subdirectory(tmp_path):
    code, out = _run_install(
        tmp_path,
        "git+https://example.com/mono#subdirectory=envs/b",
        git='mkdir -p "$last/envs/a" "$last/envs/b"\ntouch "$last/envs/a/openenv.yaml" "$last/envs/b/openenv.yaml"\n',
    )
    assert (code, out.strip()) == (0, f"OPENENV_ROOT={tmp_path}/root/env/envs/b")


@_needs_sh
@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"git": "exit 1\n"}, 4),
        ({"git": 'touch "$last/openenv.yaml"\n', "uv": "exit 1\n"}, 5),
        ({"git": 'touch "$last/openenv.yaml"\n'}, 0),
    ],
)
def test_install_script_exit_codes(tmp_path, kwargs, code):
    assert _run_install(tmp_path, "space:o/e", **kwargs)[0] == code


@_needs_sh
def test_install_script_exits_6_for_a_missing_subdirectory(tmp_path):
    assert _run_install(tmp_path, "git+https://example.com/r#subdirectory=nope")[0] == 6


@_needs_sh
@pytest.mark.skipif(
    shutil.which("uv", path="/usr/bin:/bin") is not None,
    reason="a system uv is on PATH",
)
def test_install_script_exits_3_when_uv_cannot_be_installed(tmp_path):
    code, _ = _run_install(
        tmp_path, "space:o/e", git='touch "$last/openenv.yaml"\n', with_uv=False
    )
    assert code == 3


# ---------------------------------------------------------------------------
# Registry images (Docker template)
# ---------------------------------------------------------------------------
def test_oci_image_runs_with_docker_and_env_file(adapter):
    provider = PrizedProvider(_adapter=adapter, env_vars={"HF_TOKEN": "supersecret"})
    provider.start_container("registry.hf.space/openenv-echo-env:latest")

    assert adapter.created[0]["template"] == "prized-docker-v1"
    env_file = adapter.writes[0]
    assert env_file["path"] == "/home/prized/openenv/server.env"
    assert env_file["mode"] == 0o600
    assert env_file["data"] == b"HF_TOKEN=supersecret\n"
    run = next(c for c in adapter.commands if "docker run" in c)
    assert "docker pull --quiet registry.hf.space/openenv-echo-env:latest" in run
    assert "-p 8000:8000 --env-file /home/prized/openenv/server.env" in run
    assert "rm -f /home/prized/openenv/server.env" in run
    assert all("supersecret" not in command for command in adapter.commands)
    provider.wait_for_ready(provider.base_url, timeout_s=5)
    provider.stop_container()


def test_oci_image_with_cmd_override(adapter):
    provider = _started(
        adapter, image="ghcr.io/org/env:1", cmd="uvicorn app:app --port 8000"
    )
    run = next(c for c in adapter.commands if "docker run" in c)
    assert "ghcr.io/org/env:1 sh -c 'uvicorn app:app --port 8000' >> " in run
    provider.stop_container()


@pytest.mark.parametrize(
    "env_vars", [{"BAD KEY": "v"}, {"#X": "v"}, {"": "v"}, {"K": "a\nb"}, {"K": "a\r"}]
)
def test_oci_env_vars_docker_cannot_carry_are_refused_before_create(adapter, env_vars):
    with pytest.raises(ValueError, match="docker"):
        PrizedProvider(_adapter=adapter, env_vars=env_vars).start_container(
            "ghcr.io/o/e:1"
        )
    assert adapter.created == []


def test_oci_image_without_docker_names_the_template(adapter):
    adapter.has_docker = False
    with pytest.raises(RuntimeError, match="prized-docker-v1"):
        _started(adapter, image="ghcr.io/org/env:1", template="prized-base-v1")
    assert len(adapter.deleted) == 1


def test_docker_container_exit_is_reported(adapter):
    provider = _started(adapter, image="ghcr.io/org/env:1")
    adapter.server_state = "DEAD"
    with pytest.raises(RuntimeError, match="docker logs openenv-server"):
        provider.wait_for_ready("http://127.0.0.1:1", timeout_s=5)
    provider.stop_container()


# ---------------------------------------------------------------------------
# Start-time validation and cleanup
# ---------------------------------------------------------------------------
def test_port_other_than_8000_is_rejected(adapter):
    with pytest.raises(ValueError, match="8000"):
        PrizedProvider(_adapter=adapter).start_container("space:a/b", port=9000)
    assert adapter.created == []


def test_unknown_option_is_rejected_and_wait_timeout_is_accepted(adapter):
    with pytest.raises(ValueError, match="bogus"):
        PrizedProvider(_adapter=adapter).start_container("space:a/b", bogus=1)
    provider = PrizedProvider(_adapter=adapter)
    provider.start_container("space:a/b", wait_timeout=30.0)
    provider.stop_container()


def test_image_is_required(adapter):
    with pytest.raises(ValueError, match="image"):
        PrizedProvider(_adapter=adapter).start_container()


def test_constructor_image_supports_provider_owned_start(adapter):
    provider = PrizedProvider(_adapter=adapter, image="space:openenv/echo_env")
    assert provider.start_container().startswith("http://127.0.0.1:")
    provider.stop_container()


def test_second_start_is_rejected(adapter):
    provider = _started(adapter)
    with pytest.raises(RuntimeError, match="already has an active sandbox"):
        provider.start_container("space:a/b")
    assert len(adapter.created) == 1
    provider.stop_container()


def test_create_failure_deletes_nothing_and_drops_secrets(adapter):
    adapter.fail_create = True
    provider = PrizedProvider(_adapter=adapter, env_vars={"K": "supersecret"})
    with pytest.raises(RuntimeError, match="create failed"):
        provider.start_container("space:a/b")
    assert adapter.deleted == []
    assert provider._redact_values == set()


@pytest.mark.parametrize(
    ("code", "step"),
    [(3, "installing uv"), (4, "fetching"), (5, "installing it"), (6, "subdirectory")],
)
def test_install_failure_names_the_step_withholds_output_and_deletes(
    adapter, code, step
):
    adapter.install_exit = code
    with pytest.raises(RuntimeError, match=step) as info:
        _started(adapter)
    assert "supersecret" not in str(info.value)
    assert "surface_server_logs=True" in str(info.value)
    assert len(adapter.deleted) == 1


def test_install_failure_surfaces_redacted_log_on_request(adapter):
    adapter.install_exit = 5
    adapter.log = "pip failed for token=supersecret"
    with pytest.raises(RuntimeError) as info:
        _started(adapter, surface_server_logs=True, env_vars={"T": "supersecret"})
    assert "pip failed for token=***" in str(info.value)


def test_redaction_replaces_the_longest_secret_first(adapter):
    provider = PrizedProvider(_adapter=adapter)
    provider._redact_values = {"abc", "abcdef"}
    assert provider._redact("x=abcdef y=abc") == "x=*** y=***"


def test_interrupt_during_install_deletes_the_sandbox(adapter):
    adapter.interrupt_install = True
    with pytest.raises(KeyboardInterrupt):
        _started(adapter)
    assert len(adapter.deleted) == 1


def test_cleanup_failure_does_not_mask_start_error(adapter):
    adapter.install_exit = 4
    adapter.fail_delete = 1
    provider = PrizedProvider(_adapter=adapter)
    with pytest.raises(RuntimeError, match="fetching"):
        provider.start_container("space:a/b")
    # The sandbox is kept for a retry, which deletes it.
    assert provider.sandbox_id is not None
    provider.stop_container()
    assert len(adapter.deleted) == 1


# ---------------------------------------------------------------------------
# Transport (S1) and public ports (S2)
# ---------------------------------------------------------------------------
def test_plaintext_port_url_off_this_machine_is_refused(adapter):
    adapter.port_url_value = "http://8000-sbx-1.p.prized.dev"
    with pytest.raises(RuntimeError, match="non-HTTPS"):
        _started(adapter)
    assert len(adapter.deleted) == 1


def test_public_port_returns_https_url_without_proxy(adapter):
    provider = _started(adapter, public=True, public_expires_in=3600)
    assert adapter.public_calls == [{"port": 8000, "expires_in": 3600}]
    assert provider.base_url.startswith("https://8000-sbx-")
    assert not provider.base_url.endswith("/")
    assert provider._proxy is None
    provider.stop_container()


def test_public_expiry_needs_public(adapter):
    with pytest.raises(ValueError, match="public=True"):
        PrizedProvider(_adapter=adapter, public_expires_in=3600)


# ---------------------------------------------------------------------------
# Readiness (S4)
# ---------------------------------------------------------------------------
def test_dead_server_raises_without_output_by_default(adapter, edge):
    provider = _started(adapter, env_vars={"T": "supersecret"})
    adapter.server_state = "DEAD"
    edge.healthy = False
    try:
        with pytest.raises(RuntimeError, match="died during startup") as info:
            provider.wait_for_ready(provider.base_url, timeout_s=5)
        assert "supersecret" not in str(info.value)
        assert "Traceback" not in str(info.value)
    finally:
        edge.healthy = True
        provider.stop_container()


def test_dead_server_surfaces_redacted_log_on_request(adapter):
    provider = _started(
        adapter, env_vars={"T": "supersecret"}, surface_server_logs=True
    )
    adapter.server_state = "DEAD"
    with pytest.raises(RuntimeError) as info:
        provider.wait_for_ready("http://127.0.0.1:1", timeout_s=5)
    assert "token=***" in str(info.value) and "supersecret" not in str(info.value)
    provider.stop_container()


def test_wait_for_ready_times_out(adapter):
    provider = _started(adapter)
    adapter.server_state = "STARTING"
    with pytest.raises(TimeoutError):
        provider.wait_for_ready("http://127.0.0.1:1", timeout_s=0.3)
    provider.stop_container()


def test_a_failed_liveness_check_keeps_polling(adapter):
    provider = _started(adapter)
    adapter.fail_liveness = True
    with pytest.raises(TimeoutError):
        provider.wait_for_ready("http://127.0.0.1:1", timeout_s=0.3)
    provider.stop_container()


# ---------------------------------------------------------------------------
# Stop, close
# ---------------------------------------------------------------------------
def test_stop_deletes_once_and_closes_the_proxy(adapter):
    provider = _started(adapter)
    base_url = provider.base_url
    provider.stop_container()
    provider.stop_container()
    assert len(adapter.deleted) == 1
    with pytest.raises(requests.RequestException):
        requests.get(f"{base_url}/health", timeout=1)
    with pytest.raises(RuntimeError, match="no active base_url"):
        provider.base_url  # noqa: B018


def test_failed_delete_is_retried_by_the_next_stop(adapter):
    provider = _started(adapter)
    adapter.fail_delete = 1
    with pytest.raises(RuntimeError, match="delete failed"):
        provider.stop_container()
    assert provider.sandbox_id is not None
    provider.stop_container()
    assert len(adapter.deleted) == 1 and provider.sandbox_id is None


def test_context_manager_deletes_sandbox(adapter):
    with PrizedProvider(_adapter=adapter, image="space:a/b") as provider:
        provider.start_container()
    assert len(adapter.deleted) == 1


# ---------------------------------------------------------------------------
# Beyond the contract: pause, resume, fork
# ---------------------------------------------------------------------------
def test_pause_and_resume(adapter):
    provider = _started(adapter)
    provider.pause()
    provider.resume()
    assert adapter.paused == adapter.resumed == [provider.sandbox_id]
    provider.stop_container()
    with pytest.raises(RuntimeError, match="no active sandbox"):
        provider.pause()


def test_fork_returns_started_providers_on_their_own_copies(adapter):
    adapter.per_sandbox_urls = True
    parent = _started(adapter, labels={"run": "r1"}, env_vars={"K": "v"})
    copies = parent.fork(2, labels={"copy": "yes"})

    assert adapter.forks == [
        {
            "parent": parent.sandbox_id,
            "count": 2,
            "labels": {"openenv": "server", "run": "r1", "copy": "yes"},
        }
    ]
    for copy in copies:
        assert copy.sandbox_id != parent.sandbox_id
        # Each copy's proxy targets that copy's own port URL.
        assert copy._proxy.target_url.endswith(f"/s/{copy.sandbox_id}")
        assert (
            copy._env_vars == parent._env_vars
            and copy._env_vars is not parent._env_vars
        )
        copy.wait_for_ready(copy.base_url, timeout_s=5)
    assert parent._proxy.target_url.endswith(f"/s/{parent.sandbox_id}")
    # No install or start ran again for the copies.
    assert len(adapter.background) == 1

    first_id = copies[0].sandbox_id
    copies[0].stop_container()
    assert adapter.deleted == [first_id]
    assert parent.sandbox_id is not None
    copies[1].stop_container()
    parent.stop_container()
    assert len(adapter.deleted) == 3


def test_fork_cleans_up_when_a_copy_cannot_be_reached(adapter):
    parent = _started(adapter)
    adapter.port_url_value = "http://8000-sbx-x.p.prized.dev"
    with pytest.raises(RuntimeError, match="non-HTTPS"):
        parent.fork(3)
    assert len(adapter.deleted) == 3
    parent.stop_container()


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_fork_count_must_be_positive_int(adapter, count):
    provider = _started(adapter)
    with pytest.raises(ValueError):
        provider.fork(count)
    provider.stop_container()


# ---------------------------------------------------------------------------
# The real adapter's SDK shape: a fake `prized` module, then the installed SDK
# ---------------------------------------------------------------------------
def test_default_adapter_sdk_shape(monkeypatch):
    calls: dict = {}

    class _Result:
        exit_code = 0
        stdout = "out"

    class _Files:
        def write(self, path, data, *, mode=None):
            calls["write"] = (path, data, mode)

    class _Sandbox:
        id = "sbx_0000000000000001"
        files = _Files()

        @classmethod
        def create(cls, template, **kwargs):
            calls["create"] = (template, kwargs)
            return cls()

        def exec(self, command, **kwargs):
            calls.setdefault("exec", []).append((command, kwargs))
            return _Result()

        def preview_url(self, port):
            return f"https://{port}-sbx-0000000000000001.p.prized.dev"

        def preview_headers(self):
            return {"Prized-Key": "k"}

        def set_port_public(self, port, public, *, expires_in=None):
            calls["public"] = (port, public, expires_in)
            return types.SimpleNamespace(url="https://public.example")

        def fork(self, count, *, labels=None):
            calls["fork"] = (count, labels)
            return []

        def delete(self):
            calls["order"] = calls.get("order", []) + ["delete"]

        def close(self):
            calls["order"] = calls.get("order", []) + ["close"]

    monkeypatch.setitem(sys.modules, "prized", types.SimpleNamespace(Sandbox=_Sandbox))
    adapter = _DefaultPrizedAdapter(api_key="k", base_url=None, edge_url=None)
    sandbox = adapter.create_sandbox(
        template="prized-base-v1",
        vcpu=2,
        memory_gb=None,
        disk_gb=None,
        labels={"a": "b"},
        max_runtime_min=60,
        auto_pause_min=0,
        auto_delete_min=60,
    )
    assert calls["create"] == (
        "prized-base-v1",
        {
            "vcpu": 2,
            "labels": {"a": "b"},
            "max_runtime_min": 60,
            "auto_pause_min": 0,
            "auto_delete_min": 60,
            "api_key": "k",
        },
    )
    assert adapter.exec(sandbox, "true", timeout=5) == (0, "out")
    assert adapter.exec(sandbox, "sleep 9", env={"A": "1"}, background=True) == (0, "")
    assert calls["exec"][-1] == ("sleep 9", {"env": {"A": "1"}, "background": True})
    adapter.write(sandbox, "/home/prized/x", b"1", mode=0o600)
    assert calls["write"] == ("/home/prized/x", b"1", 0o600)
    assert adapter.port_headers(sandbox) == {"Prized-Key": "k"}
    assert (
        adapter.set_port_public(sandbox, 8000, expires_in=None)
        == "https://public.example"
    )
    assert calls["public"] == (8000, True, None)
    adapter.fork(sandbox, count=2, labels={"x": "y"})
    assert calls["fork"] == (2, {"x": "y"})
    adapter.delete(sandbox)
    assert calls["order"] == ["delete", "close"]


def test_default_adapter_calls_match_the_installed_sdk():
    """The adapter's calls bind to the real SDK's signatures (when installed)."""
    prized = pytest.importorskip("prized")
    sandbox_cls = prized.Sandbox
    origin = {
        "api_key": "k",
        "base_url": "http://127.0.0.1:1",
        "edge_url": "http://127.0.0.1:1",
    }
    inspect.signature(sandbox_cls.create).bind(
        "prized-base-v1",
        vcpu=1,
        memory_gb=1.0,
        disk_gb=10,
        labels={},
        max_runtime_min=60,
        auto_pause_min=0,
        auto_delete_min=60,
        **origin,
    )
    me = object()
    inspect.signature(sandbox_cls.exec).bind(me, "true", env=None, timeout=5)
    inspect.signature(sandbox_cls.exec).bind(me, "true", env=None, background=True)
    inspect.signature(prized.Files.write).bind(me, "/p", b"", mode=0o600)
    inspect.signature(sandbox_cls.preview_url).bind(me, 8000)
    inspect.signature(sandbox_cls.preview_headers).bind(me)
    inspect.signature(sandbox_cls.set_port_public).bind(me, 8000, True, expires_in=None)
    inspect.signature(sandbox_cls.fork).bind(me, 2, labels={})
    for name in ("pause", "resume", "delete", "close"):
        inspect.signature(getattr(sandbox_cls, name)).bind(me)


def test_missing_sdk_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "prized", None)
    with pytest.raises(RuntimeError, match="openenv-prized"):
        PrizedProvider()
