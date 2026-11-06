# Copyright 2026 Prized
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A stand-in for ``prized.Sandbox`` whose commands really run, with bash, in a temporary directory
that plays ``/home/prized``. The kernel this package uploads is a real process on a real unix socket,
so the offline tests exercise the whole protocol (start, run, persist state, final answer, errors,
shutdown) without touching the Prized API.
"""

from __future__ import annotations

import itertools
import os
import pathlib
import subprocess
from collections.abc import Callable
from typing import Any

from prized import CommandTimeoutError, ExecResult, PrizedError, WriteInfo

_ids = itertools.count(1)
HOME = "/home/prized"


class FakeFiles:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sb = sandbox

    def write(self, path: str, data: Any, *, mode: int | None = None) -> WriteInfo:
        self._sb.check()
        assert path.startswith(HOME + "/"), path
        self._sb.writes.append(path)
        local = self._sb.local(path)
        local.parent.mkdir(parents=True, exist_ok=True)
        content = data.encode() if isinstance(data, str) else bytes(data)
        local.write_bytes(content)
        return WriteInfo(path, len(content))

    def read(self, path: str, *, encoding: str = "utf-8") -> str:
        self._sb.check()
        local = self._sb.local(path)
        if not local.exists():
            raise PrizedError("not_found", status=404, sandbox_id=self._sb.id)
        return local.read_text(encoding=encoding)

    def exists(self, path: str) -> bool:
        self._sb.check()
        return self._sb.local(path).exists()

    def remove(self, path: str, *, recursive: bool = False) -> None:
        self._sb.check()
        local = self._sb.local(path)
        if local.exists():
            local.unlink()


class FakeSandbox:
    def __init__(self, root: pathlib.Path, kwargs: dict[str, Any]) -> None:
        self.id = f"sbx_fake{next(_ids):04d}"
        self.kwargs = kwargs
        self.state = "running"
        self.root = root / self.id
        (self.root / "home/prized/work").mkdir(parents=True)
        self.files = FakeFiles(self)
        self.commands: list[str] = []
        self.writes: list[str] = []
        self.deletes = 0
        self.closed = False
        # exec_hook(command) may return an ExecResult or raise; None runs the command for real.
        self.exec_hook: Callable[[str], ExecResult | None] | None = None

    def local(self, path: str) -> pathlib.Path:
        return self.root / path.lstrip("/")

    def check(self) -> None:
        if self.closed:
            raise PrizedError("handle_closed")
        if self.state == "deleted":
            raise PrizedError("not_found", status=404, sandbox_id=self.id)

    def exec(self, cmd: str, *, timeout: float | None = None, **kwargs: Any) -> ExecResult:
        assert isinstance(cmd, str)
        self.check()
        self.commands.append(cmd)
        if self.exec_hook is not None:
            hooked = self.exec_hook(cmd)
            if hooked is not None:
                return hooked
        local_home = str(self.local(HOME))
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": local_home,
            "LANG": "C.UTF-8",
            **(self.kwargs.get("env_vars") or {}),
        }
        try:
            proc = subprocess.run(
                ["bash", "-c", cmd.replace(HOME, local_home)],
                cwd=local_home + "/work",
                env=env,
                capture_output=True,
                text=True,
                timeout=min(timeout or 60.0, 60.0),
            )
        except subprocess.TimeoutExpired as err:
            partial = err.stdout.decode() if isinstance(err.stdout, bytes) else (err.stdout or "")
            raise CommandTimeoutError(ExecResult(partial, "", -1, timed_out=True), timeout) from err
        return ExecResult(proc.stdout, proc.stderr, proc.returncode)

    def refresh(self) -> FakeSandbox:
        self.check()
        return self

    def delete(self, **_: Any) -> None:
        self.check()
        self.deletes += 1
        self.state = "deleted"

    def close(self) -> None:
        self.closed = True


class FakeCloud:
    """Patches ``prized.Sandbox.create`` and ``prized.Sandbox.connect``."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.created: list[FakeSandbox] = []
        self.by_id: dict[str, FakeSandbox] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.connect_calls: list[dict[str, Any]] = []

    def create(self, template: str = "prized-base-v1", **kwargs: Any) -> FakeSandbox:
        self.create_calls.append({"template": template, **kwargs})
        sandbox = FakeSandbox(self.root, kwargs)
        self.created.append(sandbox)
        self.by_id[sandbox.id] = sandbox
        return sandbox

    def connect(self, sandbox_id: str, **kwargs: Any) -> FakeSandbox:
        self.connect_calls.append({"sandbox_id": sandbox_id, **kwargs})
        sandbox = self.by_id.get(sandbox_id)
        if sandbox is None or sandbox.state == "deleted":
            raise PrizedError("not_found", status=404, sandbox_id=sandbox_id)
        sandbox.closed = False
        return sandbox

    def add_existing(self) -> FakeSandbox:
        sandbox = FakeSandbox(self.root, {})
        self.by_id[sandbox.id] = sandbox
        return sandbox
