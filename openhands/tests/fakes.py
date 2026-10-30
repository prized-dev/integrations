"""Stand-ins for prized.Sandbox and prized.AsyncSandbox that run commands with the
local bash, under a temporary HOME, so the in-sandbox scripts (curl, the WebSocket
relay, the start and probe scripts) run for real against a local fake Agent Server."""

from __future__ import annotations

import asyncio
import itertools
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ids = itertools.count(1)


@dataclass
class Result:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


def _env(home: str, extra: dict | None) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "PYTHONUNBUFFERED": "1"}
    env.update(extra or {})
    return env


def run_local(cmd: str, home: str, env: dict | None = None, cwd: str | None = None, timeout: float | None = None):
    p = subprocess.run(
        ["bash", "-c", cmd],
        check=False,
        capture_output=True,
        text=True,
        env=_env(home, env),
        cwd=cwd or home,
        timeout=timeout,
    )
    return Result(p.stdout, p.stderr, p.returncode)


class _Status(SimpleNamespace):
    pass


class SyncHandle:
    def __init__(self, cmd: str, home: str, env: dict | None, cwd: str | None):
        self.id = f"run_{next(_ids)}"
        self._out = Path(home) / f".fake-{self.id}.out"
        self._err = Path(home) / f".fake-{self.id}.err"
        self._proc = subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=self._out.open("wb"),
            stderr=self._err.open("wb"),
            env=_env(home, env),
            cwd=cwd or home,
            start_new_session=True,
        )
        self.pid = self._proc.pid

    def status(self):
        code = self._proc.poll()
        return _Status(running=code is None, exit_code=code)

    def logs(self):
        return _Status(stdout=self._out.read_text(), stderr=self._err.read_text())

    def kill(self, sig: str = "SIGTERM") -> bool:
        try:
            os.killpg(self.pid, getattr(signal, sig))
            return True
        except ProcessLookupError:
            return False


@dataclass
class FakeSandbox:
    """prized.Sandbox, locally. ``log`` records lifecycle calls."""

    home: str
    id: str = field(default_factory=lambda: f"sbx_{next(_ids):016x}")
    raw: dict = field(default_factory=dict)
    log: list = field(default_factory=list)
    install_stdout: str | None = None
    handles: list = field(default_factory=list)
    fail_install: bool = False

    def __post_init__(self):
        self.files = FakeFiles(self)
        self.info = SimpleNamespace(raw=self.raw)

    def exec(self, cmd, *, cwd=None, env=None, timeout=None, background=False, **_):
        self.log.append(("exec", cmd if isinstance(cmd, str) else " ".join(cmd)))
        if "uv tool install" in cmd:
            return self._fake_install()
        if background:
            h = SyncHandle(cmd, self.home, env, cwd)
            self.handles.append(h)
            return h
        r = run_local(cmd, self.home, env, cwd, timeout)
        if timeout is not None and r.exit_code == 124:
            r.timed_out = True
        return r

    def _fake_install(self):
        if self.fail_install:
            return Result("", "resolution failed", 92)
        state = Path(self.home) / ".openhands-prized"
        (state / "bin").mkdir(parents=True, exist_ok=True)
        (state / "server").mkdir(parents=True, exist_ok=True)
        server = Path(__file__).with_name("fake_agent_server.py")
        script = state / "bin" / "agent-server"
        script.write_text(f'#!/bin/sh\nexec {sys.executable} {server} "$@"\n')
        script.chmod(0o755)
        return Result(self.install_stdout or f"HOME={self.home}\nPY={sys.executable}\nINSTALLED=1\n", "", 0)

    def keep_alive(self):
        self.log.append(("keep_alive",))

    def pause(self, **_):
        self.log.append(("pause",))

    def resume(self, **_):
        self.log.append(("resume",))

    def delete(self, **_):
        self.log.append(("delete",))
        self.stop_all()

    def close(self):
        self.log.append(("close",))

    def fork(self, count=1, labels=None, **_):
        self.log.append(("fork", count, labels))
        child = FakeSandbox(home=self.home, raw=self.raw)
        child.parent = self
        return [child]

    def stop_all(self):
        for h in self.handles:
            h.kill()
            try:
                h._proc.wait(5)
            except Exception:
                pass


class FakeFiles:
    def __init__(self, sb):
        self.sb = sb

    def write(self, path: str, data, mode: int | None = None):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else bytes(data))
        if mode is not None:
            p.chmod(mode)
        self.sb.log.append(("write", path))
        return SimpleNamespace(path=path, bytes=p.stat().st_size)

    def read_bytes(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def download(self, remote: str, local: str) -> int:
        data = Path(remote).read_bytes()
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(data)
        return len(data)


class AsyncHandle:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self.id = f"run_{next(_ids)}"
        self.pid = proc.pid
        self.cleaned = False

    async def wait(self, on_stdout=None, **_):
        assert self.proc.stdout is not None
        while True:
            chunk = await self.proc.stdout.read(4096)
            if not chunk:
                break
            if on_stdout:
                on_stdout(chunk.decode())
        code = await self.proc.wait()
        return Result("", "", code)

    async def kill(self, sig: str = "SIGTERM") -> bool:
        try:
            os.killpg(self.pid, getattr(signal, sig))
            return True
        except ProcessLookupError:
            return False

    async def cleanup(self):
        self.cleaned = True


class FakeAsyncSandbox:
    """prized.AsyncSandbox, locally."""

    def __init__(self, home: str):
        self.home = home
        self.execs: list[str] = []
        self.handles: list[AsyncHandle] = []
        self.files = FakeAsyncFiles()
        self.closed = False

    async def exec(self, cmd, *, timeout=None, background=False, env=None, cwd=None, **_):
        self.execs.append(cmd)
        if background:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_env(self.home, env),
                cwd=cwd or self.home,
                start_new_session=True,
            )
            h = AsyncHandle(proc)
            self.handles.append(h)
            return h
        return await asyncio.to_thread(run_local, cmd, self.home, env, cwd, timeout)

    async def close(self):
        self.closed = True


class FakeAsyncFiles:
    def __init__(self):
        self.writes: list[str] = []
        self.reads: list[str] = []

    async def write(self, path: str, data, mode: Any = None):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bytes(data))
        self.writes.append(path)

    async def read_bytes(self, path: str) -> bytes:
        self.reads.append(path)
        return Path(path).read_bytes()
