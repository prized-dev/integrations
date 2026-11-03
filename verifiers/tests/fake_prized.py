"""An in-memory Prized for the tests: the parts of the `prized` SDK (0.1.2) that verifiers-prized
calls, with the same signatures (test_contract.py checks them against the real SDK).

Each fake sandbox is a directory. Commands run with the local `sh`, with the sandbox's
/home/prized mapped onto its directory (the path is rewritten in commands, working directories and
file calls), so setup, forks (a copy of the directory), stdin FIFOs and background processes are
real. Memory is not: a fork copies the disk only. Refusals are scripted per call.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import itertools
import os
import shlex
import shutil
import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prized import CommandTimeoutError, ExecResult, PrizedError

HOME = "/home/prized"


@dataclass
class Box:
    id: str
    root: Path
    template: str
    shape: dict[str, Any]
    labels: dict[str, str]
    parent: str | None = None
    state: str = "running"
    api_key: str = ""

    def path(self, path: str) -> Path:
        if path != HOME and not path.startswith(HOME + "/"):
            raise PrizedError("path_outside_root", status=400, sandbox_id=self.id)
        return Path(str(self.root) + path)

    def text(self, value: str) -> str:
        return value.replace(HOME, str(self.root) + HOME)


@dataclass
class Cloud:
    root: Path
    boxes: dict[str, Box] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    fork_refusals: list[PrizedError] = field(default_factory=list)
    """Raised by the next forks, one each, before the capacity check."""
    create_refusals: list[PrizedError] = field(default_factory=list)
    pause_refusals: list[PrizedError] = field(default_factory=list)
    capacity: int | None = None
    """Copies of one parent that may be alive at once (fork_capacity past it)."""
    fork_delay: float = 0.01
    create_delay: float = 0.0
    """Seconds a create takes before it answers (a refusal included, as after the SDK's retries)."""
    truncate_output: bool = False
    """Blocking and detached commands answer as if their output passed the SDK's cap."""
    procs: dict[str, list[asyncio.subprocess.Process]] = field(default_factory=dict)
    """Processes started in each sandbox; deleting it kills them, as deleting a VM would."""
    wakes: list[str] = field(default_factory=list)
    """Paused sandboxes a command woke (the edge resumes a paused sandbox for a command)."""
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def new_box(self, template: str, shape: dict[str, Any], labels: Mapping[str, str], api_key: str) -> Box:
        box_id = f"sbx_{next(self._ids):04d}"
        box = Box(box_id, self.root / box_id, template, dict(shape), dict(labels), api_key=api_key)
        (box.root / HOME.lstrip("/") / "work").mkdir(parents=True)
        self.boxes[box_id] = box
        return box

    def live(self, role: str | None = None) -> list[Box]:
        return [
            b
            for b in self.boxes.values()
            if b.state not in ("deleted",) and (role is None or b.labels.get("verifiers-prized") == role)
        ]

    def count(self, call: str) -> int:
        return sum(1 for c in self.calls if c[0] == call)

    def delete(self, box: Box) -> list[asyncio.subprocess.Process]:
        box.state = "deleted"
        procs = self.procs.pop(box.id, [])
        for proc in procs:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
        shutil.rmtree(box.root, ignore_errors=True)
        return procs


CLOUD: Cloud | None = None


def cloud() -> Cloud:
    assert CLOUD is not None, "install the fake first (the `cloud` fixture)"
    return CLOUD


def _box(sandbox_id: str) -> Box:
    box = cloud().boxes.get(sandbox_id)
    if box is None or box.state == "deleted":
        raise PrizedError("not_found", status=404, sandbox_id=sandbox_id)
    return box


def _command(cmd: str | Sequence[str]) -> str:
    return cmd if isinstance(cmd, str) else " ".join(shlex.quote(p) for p in cmd)


def _env(box: Box, env: Mapping[str, str] | None) -> dict[str, str]:
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(box.root) + HOME}
    return {**base, **{k: box.text(v) for k, v in (env or {}).items()}}


class FakeCommandHandle:
    def __init__(self, sandbox: AsyncSandbox, process: asyncio.subprocess.Process) -> None:
        self._sb = sandbox
        self._process = process
        self.id = f"run_{process.pid}"
        self.pid = process.pid
        self.sandbox_id = sandbox.id

    async def wait(
        self,
        *,
        timeout: float | None = None,
        on_stdout: Callable[[str], Any] | None = None,
        on_stderr: Callable[[str], Any] | None = None,
        poll_outage: float | None = None,
    ) -> ExecResult:
        async def pump(stream: asyncio.StreamReader | None, callback: Callable[[str], Any] | None) -> str:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            parts: list[str] = []
            assert stream is not None
            while chunk := await stream.read(4096):
                text = decoder.decode(chunk)
                if text:
                    parts.append(text)
                    if callback is not None:
                        callback(text)
            tail = decoder.decode(b"", final=True)
            if tail:
                parts.append(tail)
                if callback is not None:
                    callback(tail)
            return "".join(parts)

        out, err, code = await asyncio.gather(
            pump(self._process.stdout, on_stdout), pump(self._process.stderr, on_stderr), self._process.wait()
        )
        box = cloud().boxes.get(self.sandbox_id)
        if box is None or box.state == "deleted":
            # As the SDK's polls do once the sandbox is gone (deleted, or its run time over).
            raise PrizedError("sandbox_deleted", status=410, sandbox_id=self.sandbox_id)
        return ExecResult(stdout=out, stderr=err, exit_code=code if code >= 0 else 128 - code)

    async def kill(self, signal: str = "SIGTERM") -> bool:
        cloud().calls.append(("kill", self.sandbox_id, signal))
        if self._process.returncode is not None:
            return False
        try:
            os.killpg(self._process.pid, _SIGNALS[signal])
        except ProcessLookupError:
            return False
        return True

    async def cleanup(self) -> None:
        cloud().calls.append(("cleanup", self.sandbox_id))


_SIGNALS = {"SIGTERM": signal.SIGTERM, "SIGKILL": signal.SIGKILL}


class AsyncFiles:
    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sb = sandbox

    async def write(
        self, path: str, data: str | bytes | bytearray | memoryview, *, mode: int | None = None
    ) -> Any:
        box = _box(self._sb.id)
        cloud().calls.append(("write", box.id, path))
        target = box.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else bytes(data))

    async def read_bytes(self, path: str) -> bytes:
        box = _box(self._sb.id)
        cloud().calls.append(("read", box.id, path))
        target = box.path(path)
        if not target.is_file():
            raise PrizedError("not_found", status=404, sandbox_id=box.id)
        return target.read_bytes()


class AsyncSandbox:
    def __init__(self, box: Box) -> None:
        self._box = box
        self.files = AsyncFiles(self)
        self.closed = False

    @property
    def id(self) -> str:
        return self._box.id

    @property
    def state(self) -> str:
        return self._box.state

    @classmethod
    async def create(
        cls,
        template: str = "prized-base-v1",
        *,
        vcpu: int | None = None,
        memory_gb: float | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        env_vars: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
        max_runtime_min: int | None = None,
        timeout: float = 180.0,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> AsyncSandbox:
        c = cloud()
        c.calls.append(("create", template))
        await asyncio.sleep(c.create_delay)
        if c.create_refusals:
            raise c.create_refusals.pop(0)
        shape = {"vcpu": vcpu, "memory_mb": memory_mb, "disk_gb": disk_gb, "max_runtime_min": max_runtime_min}
        return cls(c.new_box(template, shape, labels or {}, api_key or ""))

    @classmethod
    async def connect(
        cls, sandbox_id: str, *, api_key: str | None = None, base_url: str | None = None
    ) -> AsyncSandbox:
        cloud().calls.append(("connect", sandbox_id))
        return cls(_box(sandbox_id))

    async def exec(
        self,
        cmd: str | Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        background: bool = False,
        full_output: bool = False,
    ) -> Any:
        box = _box(self.id)
        if box.state == "paused":
            cloud().wakes.append(box.id)
            box.state = "running"
        text = box.text(_command(cmd))
        cloud().calls.append(("exec", box.id, _command(cmd)))
        workdir = box.text(cwd or f"{HOME}/work")
        process = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            text,
            cwd=workdir,
            env=_env(box, env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        cloud().procs.setdefault(box.id, []).append(process)
        handle = FakeCommandHandle(self, process)
        if background:
            return handle
        try:
            if timeout:
                async with asyncio.timeout(timeout):
                    result = await handle.wait()
            else:
                result = await handle.wait()
        except TimeoutError:
            await handle.kill("SIGKILL")
            raise CommandTimeoutError(ExecResult("", "", -1, timed_out=True), timeout, box.id) from None
        except BaseException:
            await handle.kill("SIGKILL")
            raise
        if cloud().truncate_output:
            return ExecResult(result.stdout, result.stderr, result.exit_code, truncated=True)
        return result

    async def refresh(self) -> AsyncSandbox:
        cloud().calls.append(("refresh", self.id))
        _box(self.id)  # 404 once deleted
        return self

    async def pause(self, *, wait: bool = True, timeout: float = 120.0) -> None:
        box = _box(self.id)
        cloud().calls.append(("pause", box.id))
        if cloud().pause_refusals:
            raise cloud().pause_refusals.pop(0)
        box.state = "paused"

    async def fork(
        self,
        count: int = 1,
        *,
        labels: Mapping[str, str] | None = None,
        wait: bool = True,
        timeout: float = 180.0,
    ) -> list[AsyncSandbox]:
        c = cloud()
        box = _box(self.id)
        c.calls.append(("fork", box.id, str(count)))
        await asyncio.sleep(c.fork_delay)
        if c.fork_refusals:
            raise c.fork_refusals.pop(0)
        if box.state not in ("running", "paused"):
            raise PrizedError("parent_not_running", status=409, sandbox_id=box.id)
        if c.capacity is not None:
            alive = sum(1 for b in c.live() if b.parent == box.id)
            fits = max(0, c.capacity - alive)
            if count > fits:
                raise PrizedError(
                    "fork_capacity",
                    status=409,
                    sandbox_id=box.id,
                    body={"error": "fork_capacity", "fits": fits},
                )
        children = []
        for _ in range(count):
            child = c.new_box(
                box.template, box.shape, labels if labels is not None else box.labels, box.api_key
            )
            shutil.rmtree(child.root)
            shutil.copytree(box.root, child.root, symlinks=True)
            child.parent = box.id
            children.append(AsyncSandbox(child))
        return children

    async def delete(self, *, wait: bool = False, timeout: float = 120.0) -> None:
        c = cloud()
        c.calls.append(("delete", self.id))
        box = c.boxes.get(self.id)
        if box is not None and box.state != "deleted":
            for proc in c.delete(box):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 5)

    async def close(self) -> None:
        self.closed = True


class Sandbox:
    """The synchronous SDK, for the atexit backstop."""

    def __init__(self, box: Box) -> None:
        self._box = box

    @property
    def id(self) -> str:
        return self._box.id

    @classmethod
    def connect(cls, sandbox_id: str, *, api_key: str | None = None, base_url: str | None = None) -> Sandbox:
        cloud().calls.append(("sync_connect", sandbox_id))
        return cls(_box(sandbox_id))

    def delete(self, *, wait: bool = False, timeout: float = 120.0) -> None:
        c = cloud()
        c.calls.append(("sync_delete", self.id))
        if self._box.state != "deleted":
            c.delete(self._box)

    def close(self) -> None:
        pass

    @classmethod
    def delete_by_labels(
        cls, labels: Mapping[str, str], *, api_key: str | None = None, base_url: str | None = None
    ) -> list[str]:
        c = cloud()
        c.calls.append(("delete_by_labels", ",".join(f"{k}={v}" for k, v in labels.items())))
        gone = []
        for box in c.live():
            if all(box.labels.get(k) == v for k, v in labels.items()):
                c.delete(box)
                gone.append(box.id)
        return gone
