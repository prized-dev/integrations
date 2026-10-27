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

"""An offline stand-in for the Prized API behind prized.AsyncSandbox.

A ``Remote`` is a sandbox on the server: its state, and a private temporary directory
standing in for its disk. Commands run in a real local ``bash`` with every
``/home/prized`` rewritten into that directory, so the shell scripts the tool builds
are tested for real. Every ``create`` or ``connect`` returns a new ``FakeAsyncSandbox``
handle onto a Remote, like the real SDK. A deleted sandbox answers ``not_found``, also
to ``connect``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import os
import posixpath
import shlex
import shutil
import signal
import tempfile
from typing import Any, Callable
from unittest import mock

import prized
from prized import CommandTimeoutError, ExecResult, FileInfo, PrizedError, WriteInfo, _runs

_ids = itertools.count(1)
HOME = "/home/prized"
BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ARG_LIMIT = 128 * 1024
STAGED = "/home/prized/.prized/maf"  # where the tool keeps shell state and staged commands


class Remote:
    def __init__(self, root: str | None = None, **create_kwargs: Any) -> None:
        self.id = f"sbx_fake{next(_ids):04d}"
        self.state = "running"
        self.create_kwargs = create_kwargs
        self.root = root or tempfile.mkdtemp(prefix="prized-fake-")
        os.makedirs(self.local(f"{HOME}/work"), exist_ok=True)
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.handler: Callable[[Any, dict[str, Any]], Any] | None = None
        self.handles: list[FakeAsyncSandbox] = []
        self.kids: list[Remote] = []
        self.fail_delete = 0  # how many delete() calls fail before one succeeds
        self.pause_kwargs: list[dict[str, Any]] = []
        # Like sandboxes from some template builds: once /home/prized/work is gone,
        # every exec and file call fails with a 502.
        self.needs_work_dir = True
        self.running = 0  # commands running right now
        self.max_running = 0

    @property
    def deleted(self) -> bool:
        return self.state == "deleted"

    def check_alive(self) -> None:
        if self.state == "deleted":
            raise PrizedError("not_found", status=404, sandbox_id=self.id)
        if self.needs_work_dir and not os.path.isdir(self.root + HOME + "/work"):
            raise PrizedError("worker_failed", status=502, sandbox_id=self.id)

    def local(self, path: str) -> str:
        """The local path standing in for a path in the sandbox."""
        if path == "/":
            return self.root
        if path == HOME or path.startswith(HOME + "/"):
            return self.root + path
        raise AssertionError(f"the fake only has {HOME}: {path}")

    def to_local(self, text: str) -> str:
        return text.replace(HOME, self.root + HOME)

    def from_local(self, text: str) -> str:
        return text.replace(self.root, "")

    def has_symlink(self, path: str) -> bool:
        """Whether any component of ``path`` is a symlink (the file API refuses those)."""
        local = self.local(path)
        while len(local) > len(self.root):
            if os.path.islink(local):
                return True
            local = os.path.dirname(local)
        return False

    def read(self, path: str) -> str:
        with open(self.local(path)) as f:
            return f.read()

    def exists(self, path: str) -> bool:
        return os.path.exists(self.local(path))

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class FakeFiles:
    def __init__(self, handle: FakeAsyncSandbox) -> None:
        self._h = handle
        self._r = handle.remote

    async def write(self, path: str, data: Any) -> WriteInfo:
        await asyncio.sleep(0)
        self._h.check_usable()
        self._r.check_alive()
        if not path.startswith(HOME + "/"):
            raise PrizedError("path_outside_root", status=403)
        local = self._r.local(path)
        if self._r.has_symlink(path):
            raise PrizedError("symlink_not_allowed", status=400)
        if os.path.isdir(local):
            raise PrizedError("not_regular_file", status=400)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        raw = data.encode() if isinstance(data, str) else bytes(data)
        if path.startswith(STAGED):
            # A command staged to run: its paths are rewritten like an inline command's.
            raw = self._r.to_local(raw.decode()).encode()
        with open(local, "wb") as f:
            f.write(raw)
        os.chmod(local, 0o600)  # the real file API's mode for a written file
        return WriteInfo(path, len(raw))

    async def read_bytes(self, path: str) -> bytes:
        await asyncio.sleep(0)
        self._h.check_usable()
        self._r.check_alive()
        if not path.startswith(HOME + "/"):
            raise PrizedError("path_outside_root", status=403)
        local = self._r.local(path)
        if self._r.has_symlink(path):
            raise PrizedError("symlink_not_allowed", status=400)
        if os.path.isdir(local):
            raise PrizedError("not_regular_file", status=400)
        parent = os.path.dirname(local)
        while len(parent) > len(self._r.root):
            if os.path.isfile(parent):  # as the server answers a path under a file
                raise PrizedError("not_directory", status=400)
            parent = os.path.dirname(parent)
        if not os.path.exists(local):
            raise PrizedError("not_found", status=404)
        with open(local, "rb") as f:
            return f.read()

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        await asyncio.sleep(0)
        self._h.check_usable()
        self._r.check_alive()
        local = self._r.local(path)
        if os.path.isdir(local) and recursive:
            shutil.rmtree(local)
        elif os.path.lexists(local):
            os.remove(local)

    async def list(self, path: str) -> list[FileInfo]:
        await asyncio.sleep(0)
        self._h.check_usable()
        self._r.check_alive()
        if not (path == HOME or path.startswith(HOME + "/")):
            raise PrizedError("path_outside_root", status=403)
        local = self._r.local(path)
        if os.path.islink(local):
            raise PrizedError("symlink_not_allowed", status=400)
        if os.path.isfile(local):
            raise PrizedError("not_directory")
        if not os.path.isdir(local):
            raise PrizedError("not_found", status=404)
        out = []
        for name in sorted(os.listdir(local)):
            full = os.path.join(local, name)
            is_dir = os.path.isdir(full) and not os.path.islink(full)
            size = 4096 if is_dir else os.lstat(full).st_size
            out.append(FileInfo(name, posixpath.join(path, name), size, is_dir, os.path.islink(full), "0644", None))
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
        _check_detached_size(cmd, kwargs)
        r.running += 1
        r.max_running = max(r.max_running, r.running)
        try:
            if r.handler is not None:
                out = r.handler(cmd, kwargs)
                if inspect.isawaitable(out):
                    out = await out
                if out is not None:
                    return out
            return await self._bash(cmd, **kwargs)
        finally:
            r.running -= 1

    async def _bash(
        self,
        cmd: Any,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        on_stdout: Callable[[str], Any] | None = None,
        on_stderr: Callable[[str], Any] | None = None,
        full_output: bool = False,
    ) -> ExecResult:
        r = self.remote
        script = r.to_local(cmd if isinstance(cmd, str) else shlex.join(cmd))
        if len(script.encode()) >= ARG_LIMIT:
            raise PrizedError("spawn_failed", "fork/exec /bin/bash: argument list too long", status=400)
        full_env = {"HOME": r.root + HOME, "PATH": BASE_PATH, "LANG": "C.UTF-8", "USER": "prized"}
        full_env.update({k: r.to_local(v) for k, v in (r.create_kwargs.get("env_vars") or {}).items()})
        full_env.update({k: r.to_local(v) for k, v in (env or {}).items()})
        workdir = r.local(cwd or f"{HOME}/work")
        if not os.path.isdir(workdir):  # as the command server answers a cwd it can't enter
            raise PrizedError("spawn_failed", f"chdir {cwd}: no such file or directory", status=400)
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            script,
            cwd=workdir,
            env=full_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def pump(stream: asyncio.StreamReader, sink: list[str], callback: Callable[[str], Any] | None) -> None:
            while chunk := await stream.read(4096):
                text = r.from_local(chunk.decode("utf-8", "replace"))
                sink.append(text)
                if callback is not None:
                    outcome = callback(text)
                    if inspect.isawaitable(outcome):
                        await outcome

        out: list[str] = []
        err: list[str] = []
        assert proc.stdout is not None and proc.stderr is not None
        work = asyncio.gather(pump(proc.stdout, out, on_stdout), pump(proc.stderr, err, on_stderr), proc.wait())
        limit = None if not timeout else timeout
        try:
            await asyncio.wait_for(asyncio.shield(work), limit)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(work, 5)
            result = ExecResult("".join(out), "".join(err), -1, timed_out=True)
            raise CommandTimeoutError(result, limit, r.id) from None
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            raise
        return ExecResult("".join(out), "".join(err), proc.returncode or 0)

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
            root = tempfile.mkdtemp(prefix="prized-fake-")
            shutil.copytree(self.remote.root, root, dirs_exist_ok=True, symlinks=True)
            kid = Remote(root=root, **self.remote.create_kwargs)
            # The shell state saved paths in the parent's directory: point them at the copy's.
            state_dir = kid.local(STAGED)
            for name in os.listdir(state_dir) if os.path.isdir(state_dir) else []:
                path = os.path.join(state_dir, name)
                with open(path) as f:
                    text = f.read()
                with open(path, "w") as f:
                    f.write(text.replace(self.remote.root, kid.root))
            self.remote.kids.append(kid)
            kids.append(FakeAsyncSandbox(kid))
        return kids

    async def close(self) -> None:
        self.closed = True


def cut_result(stdout: str, reason: str) -> ExecResult:
    """A result whose output the SDK cut, saying why (``truncated_reason``, prized 0.1.1+;
    set by hand on 0.1.0, which has no such field)."""
    try:
        return ExecResult(stdout, "", 0, truncated=True, truncated_reason=reason)  # type: ignore[call-arg]
    except TypeError:  # prized 0.1.0
        result = ExecResult(stdout, "", 0, truncated=True)
        object.__setattr__(result, "truncated_reason", reason)
        return result


def output_read_error(exit_code: int, reason: str = "network_error") -> PrizedError:
    """What prized 0.1.1+ raises when a command ran but its output could not be read back
    (``OutputReadError``); built by hand on 0.1.0, which has no such class."""
    out, err = "/home/prized/.prized/exec/x/stdout", "/home/prized/.prized/exec/x/stderr"
    message = f"The command ran (exit {exit_code}), but its output could not be read back."
    cls = getattr(prized, "OutputReadError", None)
    if cls is not None:
        return cls(message, exit_code=exit_code, stdout_path=out, stderr_path=err, reason=reason)  # type: ignore[no-any-return]
    error = PrizedError("output_read_failed", message, reason=reason)
    error.exit_code, error.stdout_path, error.stderr_path = exit_code, out, err  # type: ignore[attr-defined]
    return error


def _check_detached_size(cmd: Any, kwargs: dict[str, Any]) -> None:
    """As the SDK does: a streamed command, or one with no timeout or over 240 s, runs
    detached, inside a start script that quotes it twice and that the server runs as one
    `bash -c` argument, which Linux caps at 128 KiB."""
    timeout = kwargs.get("timeout")
    limit = 60.0 if timeout is None else float(timeout)
    streamed = kwargs.get("on_stdout") is not None or kwargs.get("on_stderr") is not None
    if streamed or limit == 0 or limit > 240:
        text = cmd if isinstance(cmd, str) else shlex.join(cmd)
        wire = _runs.start_script(_runs.new_run_id(), text, None if limit == 0 else limit)
        if len(wire.encode()) + 1 > ARG_LIMIT:
            raise PrizedError("spawn_failed", "fork/exec /bin/bash: argument list too long", status=400)


class FakeSdk:
    """Patches prized.AsyncSandbox.create/connect with fakes it records."""

    def __init__(self) -> None:
        self.created: list[Remote] = []
        self.connects: list[str] = []
        self.remotes: dict[str, Remote] = {}
        self.fail_creates = 0  # how many create() calls fail before one succeeds
        self.create_delay = 0.0  # seconds; lets threads overlap in a create

    def alive(self) -> list[Remote]:
        return [r for r in self.all() if not r.deleted]

    def all(self) -> list[Remote]:
        return self.created + [k for r in self.created for k in r.kids]

    async def create(self, template: str = "prized-base-v1", **kwargs: Any) -> FakeAsyncSandbox:
        await asyncio.sleep(self.create_delay)
        if self.fail_creates:
            self.fail_creates -= 1
            raise PrizedError("sandbox_capacity", status=503)
        for unknown in set(kwargs) - _CREATE_KWARGS:
            raise TypeError(f"create() got an unexpected keyword argument {unknown!r}")
        remote = Remote(template=template, **kwargs)
        self.created.append(remote)
        self.remotes[remote.id] = remote
        return FakeAsyncSandbox(remote)

    def find(self, sandbox_id: str) -> Remote | None:
        return next((r for r in self.all() if r.id == sandbox_id), None)

    async def connect(self, sandbox_id: str, **_: Any) -> FakeAsyncSandbox:
        await asyncio.sleep(0)
        self.connects.append(sandbox_id)
        remote = self.find(sandbox_id)
        if remote is None or remote.deleted:
            raise PrizedError("not_found", status=404, sandbox_id=sandbox_id)
        return FakeAsyncSandbox(remote)

    def add(self, **kwargs: Any) -> Remote:
        """A sandbox that already exists (made by someone else), to attach to."""
        remote = Remote(**kwargs)
        self.created.append(remote)
        return remote

    def patch(self) -> contextlib.ExitStack:
        # Not mock.patch.multiple: its own `create` parameter would swallow that attribute.
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(prized.AsyncSandbox, "create", new=mock.AsyncMock(side_effect=self.create))
        )
        stack.enter_context(
            mock.patch.object(prized.AsyncSandbox, "connect", new=mock.AsyncMock(side_effect=self.connect))
        )
        stack.callback(self.cleanup)
        return stack

    def cleanup(self) -> None:
        for remote in self.all():
            remote.cleanup()


_CREATE_KWARGS = set(inspect.signature(prized.AsyncSandbox.create).parameters) - {"cls", "template"}
