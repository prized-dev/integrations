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

"""An in-memory stand-in for the Prized API behind prized.AsyncSandbox.

A ``Remote`` is a sandbox on the server (its state and files); every ``create`` or
``connect`` returns a new ``FakeAsyncSandbox`` handle onto one, like the real SDK. A
deleted sandbox answers ``not_found``, also to ``connect``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import itertools
import posixpath
import shlex
from typing import Any, Callable
from unittest import mock

import prized
from prized import ExecResult, FileInfo, PrizedError, WriteInfo

_ids = itertools.count(1)
FALLBACK_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"


class Remote:
    def __init__(self, **create_kwargs: Any) -> None:
        self.id = f"sbx_fake{next(_ids):04d}"
        self.state = "running"
        self.create_kwargs = create_kwargs
        self.fs: dict[str, bytes] = {}
        self.dirs: set[str] = {"/home/prized/work"}
        self.symlinks: dict[str, str] = {}
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.handler: Callable[[Any, dict[str, Any]], Any] | None = None
        self.handles: list[FakeAsyncSandbox] = []
        self.kids: list[Remote] = []
        self.cancelled_execs = 0
        self.fail_delete = 0  # how many delete() calls fail before one succeeds
        self.pause_kwargs: list[dict[str, Any]] = []

    @property
    def deleted(self) -> bool:
        return self.state == "deleted"

    def check_alive(self) -> None:
        if self.state == "deleted":
            raise PrizedError("not_found", status=404, sandbox_id=self.id)


class FakeFiles:
    def __init__(self, handle: FakeAsyncSandbox) -> None:
        self._h = handle
        self._r = handle.remote

    async def write(self, path: str, data: Any) -> WriteInfo:
        await asyncio.sleep(0)
        self._h.check_usable()
        self._r.check_alive()
        if not path.startswith("/home/prized/"):
            raise PrizedError("path_outside_root", status=403)
        if path in self._r.symlinks:
            raise PrizedError("symlink_not_allowed", status=400)
        self._r.fs[path] = data.encode() if isinstance(data, str) else bytes(data)
        return WriteInfo(path, len(self._r.fs[path]))

    async def read_bytes(self, path: str) -> bytes:
        await asyncio.sleep(0)
        self._h.check_usable()
        self._r.check_alive()
        if not path.startswith("/home/prized/"):
            raise PrizedError("path_outside_root", status=403)
        if path in self._r.symlinks:
            raise PrizedError("symlink_not_allowed", status=400)
        if path in self._r.dirs:
            raise PrizedError("not_regular_file", status=400)
        if path not in self._r.fs:
            raise PrizedError("not_found", status=404)
        return self._r.fs[path]

    async def list(self, path: str) -> list[FileInfo]:
        await asyncio.sleep(0)
        self._h.check_usable()
        self._r.check_alive()
        if path in self._r.fs:
            raise PrizedError("not_directory")
        if path not in self._r.dirs:
            raise PrizedError("not_found", status=404)
        out = []
        for p, data in sorted(self._r.fs.items()):
            if posixpath.dirname(p) == path:
                out.append(FileInfo(posixpath.basename(p), p, len(data), False, False, 0o644, None))
        for d in sorted(self._r.dirs):
            if posixpath.dirname(d) == path:
                out.append(FileInfo(posixpath.basename(d), d, 4096, True, False, 0o755, None))
        return out


class FakeAsyncSandbox:
    """One handle onto a Remote. Like the SDK's, it belongs to the event loop that made
    it, and a closed handle can't be used."""

    def __init__(self, remote: Remote) -> None:
        self.remote = remote
        self.id = remote.id
        self.state = remote.state
        self.closed = False
        self.loop = asyncio.get_running_loop()
        self.files = FakeFiles(self)
        remote.handles.append(self)

    def check_usable(self) -> None:
        if self.closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("handle used on another event loop")

    async def exec(self, cmd: Any, **kwargs: Any) -> ExecResult:
        await asyncio.sleep(0)
        self.check_usable()
        r = self.remote
        r.check_alive()
        r.calls.append((cmd, kwargs))
        try:
            if isinstance(cmd, list) and cmd == ["printenv", "PATH"]:
                return ExecResult(FALLBACK_PATH + "\n", "", 0)
            if r.handler is not None:
                out = r.handler(cmd, kwargs)
                if inspect.isawaitable(out):
                    out = await out
                if out is not None:
                    return out
            return _shell(r, cmd)
        except asyncio.CancelledError:
            r.cancelled_execs += 1
            raise

    async def delete(self, **_: Any) -> None:
        await asyncio.sleep(0)
        self.check_usable()
        if self.remote.fail_delete:
            self.remote.fail_delete -= 1
            raise PrizedError("network_error")
        self.remote.state = self.state = "deleted"

    async def pause(self, **kwargs: Any) -> None:
        self.check_usable()
        self.remote.check_alive()
        self.remote.pause_kwargs.append(kwargs)
        self.remote.state = self.state = "paused"

    async def refresh(self) -> FakeAsyncSandbox:
        await asyncio.sleep(0)
        self.check_usable()
        self.remote.check_alive()
        self.state = self.remote.state
        return self

    async def fork(self, count: int = 1, **_: Any) -> list[FakeAsyncSandbox]:
        self.check_usable()
        kids = []
        for _i in range(count):
            kid = Remote()
            kid.fs = dict(self.remote.fs)
            self.remote.kids.append(kid)
            kids.append(FakeAsyncSandbox(kid))
        return kids

    async def close(self) -> None:
        self.closed = True


def _shell(r: Remote, cmd: Any) -> ExecResult:
    """Just enough of the helper commands PrizedSandbox runs."""
    if isinstance(cmd, list) and cmd[:2] == ["base64", "-w0"]:
        path = r.symlinks.get(cmd[-1], cmd[-1])
        if path in r.fs:
            return ExecResult(base64.b64encode(r.fs[path]).decode(), "", 0)
        return ExecResult("", f"base64: {cmd[-1]}: No such file or directory\n", 1)
    if isinstance(cmd, list) and cmd[:2] == ["bash", "-c"]:
        script, staging, target = cmd[2], cmd[-2], cmd[-1]
        if staging not in r.fs:
            return ExecResult("", f"cat: {staging}: No such file or directory\n", 1)
        data = r.fs[staging]
        if "trap 'rm -f -- \"$1\"' EXIT" in script:
            del r.fs[staging]
        if target.startswith("/root/"):
            return ExecResult("", f"bash: {target}: Permission denied\n", 1)
        r.fs[r.symlinks.get(target, target)] = data
        return ExecResult("", "", 0)
    if isinstance(cmd, list) and cmd[:2] == ["mkdir", "-p"]:
        r.dirs.add(cmd[-1])
        return ExecResult("", "", 0)
    if isinstance(cmd, list) and cmd[:2] == ["rm", "--"]:
        if r.fs.pop(cmd[-1], None) is None:
            return ExecResult("", f"rm: cannot remove '{cmd[-1]}': No such file or directory\n", 1)
        return ExecResult("", "", 0)
    if isinstance(cmd, str) and cmd.startswith("trap 'rm -f -- "):
        # _run_script: trap 'rm -f -- SCRIPT' EXIT; LANGUAGE < SCRIPT
        language, lt, script = shlex.split(cmd.split("; ", 1)[1])
        assert lt == "<", cmd
        code = r.fs.pop(script).decode()
        return ExecResult(f"ran {language}: {code}", "", 0)
    return ExecResult("", "", 0)


class FakeSdk:
    """Patches prized.AsyncSandbox.create/connect with fakes it records."""

    def __init__(self) -> None:
        self.created: list[Remote] = []
        self.connects: list[str] = []
        self.remotes: dict[str, Remote] = {}
        self.fail_creates = 0  # how many create() calls fail before one succeeds
        self.create_delay = 0.0  # seconds; lets threads overlap in a create

    def alive(self) -> list[Remote]:
        return [r for r in self.created if not r.deleted]

    async def create(self, template: str = "prized-base-v1", **kwargs: Any) -> FakeAsyncSandbox:
        await asyncio.sleep(self.create_delay)
        if self.fail_creates:
            self.fail_creates -= 1
            raise PrizedError("sandbox_capacity", status=503)
        remote = Remote(template=template, **kwargs)
        self.created.append(remote)
        self.remotes[remote.id] = remote
        return FakeAsyncSandbox(remote)

    def find(self, sandbox_id: str) -> Remote | None:
        if sandbox_id in self.remotes:
            return self.remotes[sandbox_id]
        return next((k for r in self.created for k in r.kids if k.id == sandbox_id), None)

    async def connect(self, sandbox_id: str, **_: Any) -> FakeAsyncSandbox:
        await asyncio.sleep(0)
        self.connects.append(sandbox_id)
        remote = self.find(sandbox_id)
        if remote is None or remote.deleted:
            raise PrizedError("not_found", status=404, sandbox_id=sandbox_id)
        return FakeAsyncSandbox(remote)

    def patch(self) -> contextlib.ExitStack:
        # Not mock.patch.multiple: its own `create` parameter would swallow that attribute.
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(prized.AsyncSandbox, "create", new=mock.AsyncMock(side_effect=self.create))
        )
        stack.enter_context(
            mock.patch.object(prized.AsyncSandbox, "connect", new=mock.AsyncMock(side_effect=self.connect))
        )
        return stack
