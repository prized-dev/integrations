"""Offline fixtures: a mock Claude Managed Agents API on 127.0.0.1 (mock_cma), fake Prized sandboxes
whose commands really run (fake_prized), and a stub ``ant`` (bin/ant) packed into a tarball the
orchestrator installs into each sandbox the way it installs the real one, by URL and sha256.

No test reaches the network: httpx refuses any host but 127.0.0.1, the Prized key is fake and its
API URL a dead local port, and scripts/test.sh runs the suite with the network cut off.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import io
import os
import socket
import sys
import tarfile
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import mock_cma  # noqa: E402
from fake_prized import World  # noqa: E402

from prized_managed_agents import worker  # noqa: E402
from prized_managed_agents.cma import CMA  # noqa: E402
from prized_managed_agents.config import Settings  # noqa: E402
from prized_managed_agents.orchestrator import Orchestrator  # noqa: E402
from prized_managed_agents.sandboxes import SessionSandboxes  # noqa: E402
from prized_managed_agents.server import WebhookApp  # noqa: E402
from prized_managed_agents.webhooks import Verifier  # noqa: E402

STUB_ANT = Path(__file__).parent / "bin" / "ant"
# Substrings that pick out one in-sandbox script, for World.fail_exec and fail_after_exec.
STATUS_CMD = 'ls -1t "$PMA_RUNS"'
LAUNCH_CMD = "setsid -f sh -c"
COLLECT_CMD = 'touch "$r/collected"'
BLOCKED: list[str] = []
_SEND = httpx.AsyncClient.send
_SEND_SYNC = httpx.Client.send


def _local(request: httpx.Request) -> bool:
    return request.url.host in ("127.0.0.1", "localhost")


async def _guard_async(self: httpx.AsyncClient, request: httpx.Request, *a: Any, **kw: Any) -> httpx.Response:
    if not _local(request):
        BLOCKED.append(str(request.url))
        raise httpx.ConnectError(f"tests do not leave 127.0.0.1: {request.url}", request=request)
    return await _SEND(self, request, *a, **kw)


def _guard_sync(self: httpx.Client, request: httpx.Request, *a: Any, **kw: Any) -> httpx.Response:
    if not _local(request):
        BLOCKED.append(str(request.url))
        raise httpx.ConnectError(f"tests do not leave 127.0.0.1: {request.url}", request=request)
    return _SEND_SYNC(self, request, *a, **kw)


@pytest.fixture(autouse=True)
def _offline(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live") is not None:
        return
    monkeypatch.setenv("PRIZED_API_KEY", "dcp_000000000000offline_unit_test_key")
    monkeypatch.setattr("prized._core.DEFAULT_BASE_URL", "http://127.0.0.1:9", raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "send", _guard_async)
    monkeypatch.setattr(httpx.Client, "send", _guard_sync)
    monkeypatch.setattr("prized_managed_agents.orchestrator.SETTLE_S", 0.5)
    monkeypatch.setattr("prized_managed_agents.orchestrator.RETIRE_WAIT_S", 3.0)


@pytest.fixture(scope="session")
def ant_dist() -> Iterator[dict[str, Any]]:
    """The stub ant as a release tarball: ant_<version>_linux_<arch>.tar.gz, and its sha256."""
    d = tempfile.mkdtemp(prefix="ant-dist-")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tar.gettarinfo(str(STUB_ANT), arcname="ant")
        info.mode = 0o755
        with open(STUB_ANT, "rb") as fh:
            tar.addfile(info, fh)
    data = buf.getvalue()
    for arch in ("amd64", "arm64"):
        Path(d, f"ant_2.7.1_linux_{arch}.tar.gz").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    yield {"url": f"file://{d}/ant_{{version}}_linux_{{arch}}.tar.gz", "sha": {"amd64": sha, "arm64": sha}, "dir": d}


@pytest.fixture
def mock() -> Iterator[mock_cma.MockCMA]:
    m = mock_cma.MockCMA(lease_ttl=1.5)
    yield m
    m.close()


@pytest.fixture
def world() -> Iterator[World]:
    w = World()
    yield w
    w.close()


def settings_for(mock: mock_cma.MockCMA, ant_dist: dict[str, Any], **over: Any) -> Settings:
    base: dict[str, Any] = dict(
        environment_id=mock_cma.ENV_ID,
        environment_key=mock_cma.ENV_KEY,
        base_url=mock.url,
        webhook_key=mock_cma.WEBHOOK_KEY,
        ant_url=ant_dist["url"],
        ant_sha256=ant_dist["sha"],
        max_idle_s=1,
        handoff_timeout_s=3,
        probe_every_s=0.2,
        sweep_every_s=3600,
        reconcile_every_s=3600,
        stop_grace_s=3,
        labels={"test": "cma"},
    )
    base.update(over)
    return Settings(**base)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclasses.dataclass
class Harness:
    orch: Orchestrator
    app: WebhookApp | None
    mock: mock_cma.MockCMA
    world: World
    settings: Settings

    async def until(self, what: Callable[[], Any], timeout: float = 20.0, *, why: str = "") -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = what()
            if got:
                return got
            await asyncio.sleep(0.05)
        notes = [(n.session, n.what, n.detail) for n in self.orch.notes[-15:]]
        raise AssertionError(f"timed out waiting for {why or what}; notes: {notes}")

    def noted(self, what: str, session: str | None = None) -> list[Any]:
        return [n for n in self.orch.notes if n.what == what and (session is None or n.session == session)]

    def sandbox_of(self, sid: str) -> Any:
        found = self.world.live(**{"anthropic-session": sid})
        return found[0] if found else None


@contextlib.asynccontextmanager
async def harness(
    mock: mock_cma.MockCMA, world: World, ant_dist: dict[str, Any], *, serve: bool = True, **over: Any
) -> AsyncIterator[Harness]:
    """An orchestrator running against the mock, its webhook receiver on a loopback port that the
    mock delivers to (unless ``serve=False`` or webhooks are off)."""
    import uvicorn

    s = settings_for(mock, ant_dist, **over)
    cma = CMA(s.base_url, s.environment_id, s.environment_key)
    boxes = SessionSandboxes(s, api=world.api(), sleep=lambda _: asyncio.sleep(0))
    orch = Orchestrator(s, cma, boxes)
    app = None
    server = None
    serving = None
    if serve and s.webhook_key:
        app = WebhookApp(orch, Verifier(s.webhook_key))
        port = free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                interface="asgi3",
                lifespan="off",
                log_level="warning",
                access_log=False,
            )
        )
        server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        mock.endpoint = f"http://127.0.0.1:{port}/webhook"
    task = asyncio.create_task(orch.run())
    h = Harness(orch, app, mock, world, s)
    try:
        while not orch._started and not task.done():
            await asyncio.sleep(0.01)
        if task.done():
            task.result()
        yield h
    finally:
        orch.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 30)
        if server is not None and serving is not None:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serving, 10)
        mock.endpoint = None
        await cma.close()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def sandbox_text(world: World) -> str:
    """Every byte in every fake sandbox's files, for checks that a key never landed there."""
    out = []
    for root, _dirs, files in os.walk(world.root):
        for f in files:
            with contextlib.suppress(OSError):
                out.append(Path(root, f).read_bytes().decode(errors="replace"))
    return "\n".join(out)


def craft_run(
    sb: Any, session_id: str, work_id: str, *, now: float | None = None, key: str = mock_cma.ENV_KEY, **files: str
) -> str:
    """Write a run directory into a fake sandbox by hand, signed with ``key``, as an orchestrator of
    that environment would have; ``files`` are its other files (done, pid, collected...)."""
    rid = worker.run_id(worker.run_key(key), session_id, work_id, now=now)
    d = Path(sb.home, worker.RUNS, rid)
    d.mkdir(parents=True)
    for name, text in {"session": session_id, "work": work_id, **files}.items():
        (d / name).write_text(text)
    return rid


__all__ = ["Harness", "craft_run", "harness", "run", "sandbox_text", "settings_for", "worker"]
