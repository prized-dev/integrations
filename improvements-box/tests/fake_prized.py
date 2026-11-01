"""A stand-in for the Prized SDK's ``Sandbox``, with no network and no key.

Each fake sandbox is a directory standing in for ``/home/prized``. Commands really run, with bash,
in its ``work/`` (or the ``cwd`` given), with ``HOME`` pointed at it and ``~/.local/bin`` then
``tests/bin`` first on ``PATH`` (``tests/bin`` holds a fake ``claude``). ``fork()`` copies the
directory, as a real fork copies the disk (and memory). Each exec is recorded with the names,
never the values, of the variables it was given. A command past its timeout is killed and raises
``CommandTimeoutError``, as the SDK does.
"""

from __future__ import annotations

import itertools
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from prized import CommandTimeoutError, ExecResult, PrizedError

BIN = str(Path(__file__).parent / "bin")
HOME = "/home/prized"


class World:
    """Every fake sandbox, and a log of the calls made on them."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="fake-prized-")
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.created: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.execs: list[tuple[str, str, list[str]]] = []  # (sandbox, command, env names)
        self.ids = itertools.count(1)
        self.fail_create: str | None = None
        self.fail_delete: str | None = None
        self.fail_fork: str | None = None
        self.before_create: Any = None  # called inside create(), to race it

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def api(self) -> type:
        world = self

        class API:
            @staticmethod
            def create(template: str = "prized-base-v1", **kw: Any) -> FakeSandbox:
                if world.before_create:
                    world.before_create()
                if world.fail_create:
                    raise PrizedError(world.fail_create, status=409)
                return world.new(template, kw, "create")

            @staticmethod
            def list(labels: dict[str, str] | None = None, **_: Any) -> list[FakeSandbox]:
                return [
                    sb
                    for sb in world.sandboxes.values()
                    if sb.state != "deleted" and all(sb.labels.get(k) == v for k, v in (labels or {}).items())
                ]

        return API

    def new(self, template: str, kw: dict[str, Any], how: str, copy_of: str | None = None) -> FakeSandbox:
        n = next(self.ids)
        sb = FakeSandbox(self, f"sbx_fake{n:04d}", template, kw, copy_of)
        self.sandboxes[sb.id] = sb
        self.created.append({"template": template, "how": how, **kw})
        self.calls.append((how, sb.id))
        return sb

    def live(self) -> list[str]:
        return [sb.id for sb in self.sandboxes.values() if sb.state != "deleted"]


class Files:
    def __init__(self, sb: FakeSandbox) -> None:
        self.sb = sb

    def write(self, path: str, data: str | bytes, *, mode: int | None = None) -> None:
        self.sb._gone()
        dest = self.sb.local(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
        os.chmod(dest, 0o600 if mode is None else mode)

    def upload(self, local_path: str | os.PathLike[str], path: str, *, mode: int | None = None) -> None:
        with open(local_path, "rb") as fh:
            self.write(path, fh.read(), mode=mode)

    def read_bytes(self, path: str) -> bytes:
        self.sb._gone()
        with open(self.sb.local(path), "rb") as fh:
            return fh.read()


class FakeSandbox:
    def __init__(
        self, world: World, sid: str, template: str, options: dict[str, Any], copy_of: str | None = None
    ) -> None:
        self.world = world
        self.id = sid
        self.template = template
        self.options = options
        self.labels = dict(options.get("labels") or {})
        self.state = "running"
        self.home = os.path.join(world.root, sid)
        if copy_of:
            shutil.copytree(copy_of, self.home, symlinks=True)
        else:
            os.makedirs(os.path.join(self.home, "work"))
        self.files = Files(self)
        self.procs: list[subprocess.Popen[str]] = []

    def __repr__(self) -> str:
        return f"FakeSandbox({self.id}, {self.state})"

    def local(self, path: str) -> str:
        if path.startswith(HOME):
            return self.home + path[len(HOME) :]
        return os.path.join(self.home, "work", path)

    def _gone(self) -> None:
        if self.state == "deleted":
            raise PrizedError("sandbox_deleted", sandbox_id=self.id, status=404)
        if self.state == "paused":
            self.state = "running"

    def exec(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        on_stdout: Any = None,
        **_: Any,
    ) -> ExecResult:
        self._gone()
        self.world.execs.append((self.id, cmd, sorted(env or {})))
        full_env = {
            "PATH": os.pathsep.join(
                (os.path.join(self.home, ".local", "bin"), BIN, os.environ.get("PATH", "/usr/bin:/bin"))
            ),
            "HOME": self.home,
            "USER": "prized",
            "LANG": "C.UTF-8",
            "PRIZED_SANDBOX_ID": self.id,
            **(env or {}),
        }
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=self.local(cwd) if cwd else os.path.join(self.home, "work"),
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.procs.append(proc)
        try:
            out, err = proc.communicate(timeout=timeout or 60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            raise CommandTimeoutError(ExecResult(out, err, -1, timed_out=True), timeout, self.id) from None
        if on_stdout is not None:  # in pieces, as a stream arrives
            for i in range(0, len(out), 7):
                on_stdout(out[i : i + 7])
        return ExecResult(out, err, proc.returncode)

    def pause(self, **_: Any) -> None:
        self._gone()
        self.state = "paused"
        self.world.calls.append(("pause", self.id))

    def resume(self, **_: Any) -> None:
        self._gone()
        self.world.calls.append(("resume", self.id))

    def fork(self, count: int = 1, *, labels: dict[str, str] | None = None, **_: Any) -> list[FakeSandbox]:
        self._gone()
        if self.world.fail_fork:
            raise PrizedError(self.world.fail_fork, status=409, sandbox_id=self.id)
        kw = {**self.options, "labels": labels if labels is not None else self.labels}
        return [self.world.new(self.template, kw, "fork", copy_of=self.home) for _ in range(count)]

    def delete(self, *, wait: bool = False, **_: Any) -> None:
        if self.world.fail_delete:
            raise PrizedError(self.world.fail_delete, status=503, sandbox_id=self.id)
        if self.state != "deleted":
            self.state = "deleted"
            self.world.calls.append(("delete", self.id))
            for proc in self.procs:  # a deleted sandbox's commands end with it
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
