"""A stand-in for the Prized SDK's ``Sandbox``, with no network and no key.

Each fake sandbox is a directory standing in for ``/home/prized``. Commands really run, with bash,
with ``HOME`` pointed at that directory, so the orchestrator's in-sandbox scripts are tested as
they are. ``pause()`` freezes every process the sandbox started (SIGSTOP) and ``resume()`` thaws
them, as a real pause keeps memory and processes; a command sent to a paused sandbox resumes it
first, as the real edge does. ``fork()`` copies the files (the real one copies memory too).
"""

from __future__ import annotations

import itertools
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Any

from prized import PrizedError

HOME = "/home/prized"


@dataclass
class Result:
    stdout: str
    stderr: str
    exit_code: int


class World:
    """Every fake sandbox, and a log of the lifecycle calls made on them."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="fake-prized-")
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.calls: list[tuple[str, str]] = []
        self.ids = itertools.count(1)
        self.lock = threading.RLock()
        self.fail_create: str | None = None

    def close(self) -> None:
        for sb in list(self.sandboxes.values()):
            sb._kill_all()
        shutil.rmtree(self.root, ignore_errors=True)

    def api(self) -> type:
        world = self

        class API:
            @staticmethod
            def create(template: str = "prized-base-v1", **kw: Any) -> FakeSandbox:
                if world.fail_create:
                    raise PrizedError(world.fail_create)
                return world.new(template, kw)

            @staticmethod
            def list(labels: dict[str, str] | None = None, state: Any = None, **_: Any) -> list[FakeSandbox]:
                with world.lock:
                    out = [
                        sb
                        for sb in world.sandboxes.values()
                        if sb.state != "deleted"
                        and all(sb.labels.get(k) == v for k, v in (labels or {}).items())
                    ]
                return sorted(out, key=lambda sb: -sb.n)

            @staticmethod
            def connect(sandbox_id: str, **_: Any) -> FakeSandbox:
                sb = world.sandboxes.get(sandbox_id)
                if sb is None or sb.state == "deleted":
                    raise PrizedError("not_found", sandbox_id=sandbox_id)
                return sb

            @staticmethod
            def delete_by_labels(labels: dict[str, str], **_: Any) -> list[str]:
                ids = []
                for sb in API.list(labels=labels):
                    sb.delete()
                    ids.append(sb.id)
                return ids

        return API

    def new(self, template: str, kw: dict[str, Any], files_from: str | None = None) -> FakeSandbox:
        with self.lock:
            n = next(self.ids)
            sb = FakeSandbox(self, f"sbx_fake{n:04d}", n, template, dict(kw.get("labels") or {}), kw)
            if files_from:
                shutil.copytree(files_from, sb.home, symlinks=True, dirs_exist_ok=True)
            self.sandboxes[sb.id] = sb
            self.calls.append(("create" if not files_from else "fork", sb.id))
            return sb


class Files:
    def __init__(self, sb: FakeSandbox) -> None:
        self.sb = sb

    def upload(self, local_path: str, path: str, mode: int | None = None) -> None:
        dest = self.sb.local(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(local_path, dest)
        if mode is not None:
            os.chmod(dest, mode)
        self.sb.world.calls.append(("upload", self.sb.id))

    def read(self, path: str) -> str:
        with open(self.sb.local(path)) as fh:
            return fh.read()


class FakeSandbox:
    def __init__(
        self, world: World, sid: str, n: int, template: str, labels: dict[str, str], options: dict[str, Any]
    ) -> None:
        self.world = world
        self.id = sid
        self.n = n
        self.template = template
        self.labels = labels
        self.options = options
        self.state = "running"
        self.home = os.path.join(world.root, sid)
        os.makedirs(os.path.join(self.home, "work"), exist_ok=True)
        self.groups: list[int] = []
        self.files = Files(self)

    def __repr__(self) -> str:
        return f"FakeSandbox({self.id}, {self.state})"

    def local(self, path: str) -> str:
        if path.startswith(HOME):
            return self.home + path[len(HOME) :]
        return os.path.join(self.home, "work", path)

    def _gone(self) -> None:
        if self.state == "deleted":
            raise PrizedError("sandbox_deleted", sandbox_id=self.id)

    def _env(self, env: dict[str, str] | None) -> dict[str, str]:
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
            "USER": "prized",
            "LANG": "C.UTF-8",
            "PRIZED_SANDBOX_ID": self.id,
        }
        for k, v in (env or {}).items():
            base[k] = v.replace(HOME, self.home)
        return base

    def exec(
        self,
        cmd: str,
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        background: bool = False,
        full_output: bool = False,
        cwd: str | None = None,
    ) -> Any:
        self._gone()
        if self.state == "paused":
            self.resume()
        workdir = self.local(cwd) if cwd else os.path.join(self.home, "work")
        if background:
            proc = subprocess.Popen(
                ["bash", "-c", cmd],
                cwd=workdir,
                env=self._env(env),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.groups.append(proc.pid)
            self.world.calls.append(("exec-background", self.id))
            return proc
        done = subprocess.run(
            ["bash", "-c", cmd],
            cwd=workdir,
            env=self._env(env),
            capture_output=True,
            text=True,
            timeout=timeout or 60,
        )
        return Result(done.stdout, done.stderr, done.returncode)

    def _signal(self, sig: int) -> None:
        for pgid in self.groups:
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass

    def pause(self, **_: Any) -> None:
        self._gone()
        if self.state != "paused":
            self._signal(signal.SIGSTOP)
            self.state = "paused"
            self.world.calls.append(("pause", self.id))

    def resume(self, **_: Any) -> None:
        self._gone()
        if self.state == "paused":
            self._signal(signal.SIGCONT)
            self.state = "running"
            self.world.calls.append(("resume", self.id))

    def refresh(self) -> None:
        return None

    def fork(self, count: int = 1, *, labels: dict[str, str] | None = None, **_: Any) -> list[FakeSandbox]:
        self._gone()
        return [
            self.world.new(
                self.template, {"labels": labels if labels is not None else self.labels}, files_from=self.home
            )
            for _ in range(count)
        ]

    def _kill_all(self) -> None:
        self._signal(signal.SIGCONT)
        self._signal(signal.SIGKILL)

    def delete(self, **_: Any) -> None:
        if self.state != "deleted":
            self._kill_all()
            self.state = "deleted"
            self.world.calls.append(("delete", self.id))
