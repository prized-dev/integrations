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

"""A stand-in for prized.Sandbox: commands really run, with bash, in a temporary directory that
plays /home/prized, so the package's own command lines and runner script are exercised.

``FakeCloud`` replaces ``prized.Sandbox.create`` and ``prized.Sandbox.connect``; each sandbox it
makes is a ``FakeSandbox`` that records every call.
"""

from __future__ import annotations

import itertools
import os
import pathlib
import subprocess
from collections.abc import Callable
from typing import Any

from prized import CommandTimeoutError, ExecResult, PrizedError, Resources, WriteInfo

_ids = itertools.count(1)
HOME = "/home/prized"


class FakeFiles:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sb = sandbox

    def write(self, path: str, data: Any, *, mode: int | None = None) -> WriteInfo:
        self._sb.check()
        self._sb.writes.append(path)
        assert path.startswith(HOME + "/"), path
        local = self._sb.local(path)
        local.parent.mkdir(parents=True, exist_ok=True)
        content = data.encode() if isinstance(data, str) else bytes(data)
        local.write_bytes(content)
        return WriteInfo(path, len(content))


class FakeSandbox:
    def __init__(self, root: pathlib.Path, kwargs: dict[str, Any]) -> None:
        self.id = f"sbx_fake{next(_ids):04d}"
        self.kwargs = kwargs
        self.state = "running"
        self.resources = Resources(vcpu=2, memory_mb=4096, disk_gb=10)
        self.root = root / self.id
        (self.root / "home/prized/work").mkdir(parents=True)
        self.files = FakeFiles(self)
        self.commands: list[str] = []
        self.timeouts: list[Any] = []
        self.writes: list[str] = []
        self.deletes = 0
        self.closed = False
        # exec_hook(command) may return an ExecResult or raise; None runs the command for real.
        self.exec_hook: Callable[[str], ExecResult | None] | None = None
        self.refresh_hook: Callable[[], None] | None = None

    def local(self, path: str) -> pathlib.Path:
        return self.root / path.lstrip("/")

    def check(self) -> None:
        if self.closed:
            raise PrizedError("handle_closed")
        if self.state == "deleted":
            raise PrizedError("not_found", status=404, sandbox_id=self.id)

    def exec(self, cmd: str, *, timeout: float | None = None, **kwargs: Any) -> ExecResult:
        assert not kwargs, kwargs
        assert isinstance(cmd, str)
        self.check()
        self.commands.append(cmd)
        self.timeouts.append(timeout)
        if self.exec_hook is not None:
            hooked = self.exec_hook(cmd)
            if hooked is not None:
                return hooked
        local_home = str(self.local(HOME))
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": local_home,
            "LANG": "C.UTF-8",
            **self.kwargs.get("env_vars", {}),
        }
        proc = subprocess.run(
            ["bash", "-c", cmd.replace(HOME, local_home)],
            cwd=local_home + "/work",
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return ExecResult(proc.stdout, proc.stderr, proc.returncode)

    def refresh(self) -> FakeSandbox:
        if self.refresh_hook is not None:
            self.refresh_hook()
        self.check()
        return self

    def delete(self, **_: Any) -> None:
        self.check()
        self.deletes += 1
        self.state = "deleted"

    def close(self) -> None:
        self.closed = True


class FakeCloud:
    """Patches prized.Sandbox.create and prized.Sandbox.connect."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.created: list[FakeSandbox] = []
        self.by_id: dict[str, FakeSandbox] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.connect_calls: list[dict[str, Any]] = []
        self.fail_create: PrizedError | None = None

    def create(self, template: str = "prized-base-v1", **kwargs: Any) -> FakeSandbox:
        self.create_calls.append({"template": template, **kwargs})
        if self.fail_create is not None:
            raise self.fail_create
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


def timeout_error(stdout: str, timeout: float) -> CommandTimeoutError:
    """What prized raises when a command runs past its timeout."""
    result = ExecResult(stdout, "", -1, timed_out=True)
    return CommandTimeoutError(result, timeout)
