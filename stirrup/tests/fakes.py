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

"""An offline stand-in for prized.AsyncSandbox.

A ``Remote`` is a sandbox on the server: its state and a private temporary directory
standing in for its disk. Commands run in a real local ``bash``, with every ``/home/prized``
and ``/srv`` (a directory outside the home, for paths the files API doesn't serve) rewritten
into that directory, so the shell the backend builds is tested for real. The files API keeps
the real one's rules: only under /home/prized, no symlinks, regular files only.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import signal
import tempfile
from typing import Any

from prized import CommandTimeoutError, ExecResult, PrizedError, WriteInfo

_ids = itertools.count(1)
HOME = "/home/prized"
MAPPED = (HOME, "/srv")  # sandbox paths that exist in the fake's directory
BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class Remote:
    def __init__(self, world: World, **create_kwargs: Any) -> None:
        self.world = world
        self.id = f"sbx_fake{next(_ids):04d}"
        self.state = "running"
        self.create_kwargs = create_kwargs
        self.root = tempfile.mkdtemp(prefix="stirrup-prized-fake-")
        for top in MAPPED:
            os.makedirs(self.root + top, exist_ok=True)
        os.makedirs(self.root + HOME + "/work", exist_ok=True)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.deletes = 0

    def local(self, path: str) -> str:
        if path == "/":
            return self.root
        if any(path == top or path.startswith(top + "/") for top in MAPPED):
            return self.root + path
        raise AssertionError(f"the fake only has {MAPPED}: {path}")

    def to_local(self, text: str) -> str:
        for top in MAPPED:
            text = text.replace(top, self.root + top)
        return text

    def from_local(self, text: str) -> str:
        return text.replace(self.root, "")

    def check_alive(self) -> None:
        if self.state == "deleted":
            raise PrizedError("not_found", status=404, sandbox_id=self.id)

    def has_symlink(self, path: str) -> bool:
        local = self.local(path)
        while len(local) > len(self.root):
            if os.path.islink(local):
                return True
            local = os.path.dirname(local)
        return False

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class World:
    """Every fake sandbox of one test, and the failures it should meet."""

    def __init__(self) -> None:
        self.remotes: dict[str, Remote] = {}
        self.handles: list[FakeAsyncSandbox] = []
        # op name -> errors to raise, first one first ("create", "fork", "exec", "write", "read", "delete").
        self.failures: dict[str, list[BaseException]] = {}
        self.events: list[str] = []

    def fail(self, op: str, *errors: BaseException) -> None:
        self.failures.setdefault(op, []).extend(errors)

    def maybe_fail(self, op: str) -> None:
        queue = self.failures.get(op)
        if queue:
            raise queue.pop(0)

    def add(self, **create_kwargs: Any) -> Remote:
        remote = Remote(self, **create_kwargs)
        self.remotes[remote.id] = remote
        return remote

    def only(self) -> Remote:
        assert len(self.remotes) == 1, list(self.remotes)
        return next(iter(self.remotes.values()))

    def cleanup(self) -> None:
        for remote in self.remotes.values():
            remote.cleanup()


class FakeFiles:
    def __init__(self, handle: FakeAsyncSandbox) -> None:
        self._h = handle
        self._r = handle.remote

    def _checked(self, path: str) -> str:
        self._h.check_usable()
        self._r.check_alive()
        if not path.startswith("/"):
            raise AssertionError(f"the backend should send absolute paths: {path}")
        if not path.startswith(HOME + "/"):
            raise PrizedError("path_outside_root", status=403, sandbox_id=self._r.id)
        if ".." in path.split("/"):
            raise PrizedError("invalid_path", status=400, sandbox_id=self._r.id)
        if self._r.has_symlink(path):
            raise PrizedError("symlink_not_allowed", status=400, sandbox_id=self._r.id)
        return self._r.local(path)

    async def write(self, path: str, data: Any) -> WriteInfo:
        await asyncio.sleep(0)
        self._r.calls.append(("write", {"path": path}))
        self._r.world.maybe_fail("write")
        local = self._checked(path)
        if os.path.isdir(local):
            raise PrizedError("not_regular_file", status=400, sandbox_id=self._r.id)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        raw = data.encode() if isinstance(data, str) else bytes(data)
        with open(local, "wb") as f:
            f.write(raw)
        os.chmod(local, 0o600)  # the real files API's mode for a new file
        return WriteInfo(path, len(raw))

    async def read_bytes(self, path: str) -> bytes:
        await asyncio.sleep(0)
        self._r.calls.append(("read", {"path": path}))
        self._r.world.maybe_fail("read")
        local = self._checked(path)
        if os.path.isdir(local):
            raise PrizedError("not_regular_file", status=400, sandbox_id=self._r.id)
        if not os.path.exists(local):
            raise PrizedError("not_found", status=404, sandbox_id=self._r.id)
        with open(local, "rb") as f:
            return f.read()

    async def exists(self, path: str) -> bool:
        await asyncio.sleep(0)
        self._r.calls.append(("exists", {"path": path}))
        self._h.check_usable()
        self._r.check_alive()
        return os.path.lexists(self._r.local(path))

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        await asyncio.sleep(0)
        self._r.calls.append(("remove", {"path": path}))
        local = self._checked(path)
        if os.path.isdir(local) and not os.path.islink(local):
            if recursive:
                shutil.rmtree(local)
            else:
                os.rmdir(local)
        elif os.path.lexists(local):
            os.remove(local)


class FakeAsyncSandbox:
    """One handle onto a Remote, with the prized.AsyncSandbox calls the backend makes."""

    world: World  # set per test by the `world` fixture

    def __init__(self, remote: Remote) -> None:
        self.remote = remote
        self.id = remote.id
        self.closed = False
        self.files = FakeFiles(self)
        remote.world.handles.append(self)

    @property
    def state(self) -> str:
        return self.remote.state

    def check_usable(self) -> None:
        if self.closed:
            raise AssertionError("a closed handle was used")

    @classmethod
    async def create(cls, template: str = "prized-base-v1", **kwargs: Any) -> FakeAsyncSandbox:
        await asyncio.sleep(0)
        cls.world.events.append("create")
        cls.world.maybe_fail("create")
        if template not in ("prized-base-v1", "prized-agent-v1"):
            raise PrizedError("template_not_found", status=404)
        return cls(cls.world.add(template=template, **kwargs))

    @classmethod
    async def connect(cls, sandbox_id: str, **kwargs: Any) -> FakeAsyncSandbox:
        await asyncio.sleep(0)
        cls.world.events.append(f"connect {sandbox_id}")
        remote = cls.world.remotes.get(sandbox_id)
        if remote is None or remote.state == "deleted":
            raise PrizedError("not_found", status=404, sandbox_id=sandbox_id)
        return cls(remote)

    async def fork(
        self,
        count: int = 1,
        *,
        labels: Any = None,
        wait: bool = True,
        timeout: float = 180,
        idempotency_key: Any = None,
    ) -> list[FakeAsyncSandbox]:
        await asyncio.sleep(0)
        self.check_usable()
        self.remote.check_alive()
        self.world.events.append(f"fork {self.id}")
        self.world.maybe_fail("fork")
        copies = []
        for _ in range(count):
            kid = self.world.add(forked_from=self.id, labels=labels, timeout=timeout, idempotency_key=idempotency_key)
            shutil.rmtree(kid.root)
            shutil.copytree(self.remote.root, kid.root, symlinks=True)
            copies.append(FakeAsyncSandbox(kid))
        return copies

    async def delete(self, *, wait: bool = False, timeout: float = 120) -> None:
        await asyncio.sleep(0)
        self.check_usable()
        self.world.events.append(f"delete {self.id}")
        self.remote.deletes += 1
        self.world.maybe_fail("delete")
        self.remote.state = "deleted"

    async def close(self) -> None:
        self.closed = True

    async def exec(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ExecResult:
        await asyncio.sleep(0)
        self.check_usable()
        r = self.remote
        r.calls.append(("exec", {"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout, **kwargs}))
        r.world.maybe_fail("exec")
        r.check_alive()
        if not isinstance(cmd, str) or cmd.strip() == "":
            raise ValueError("cmd is empty")
        workdir = r.local(cwd if cwd is not None else HOME + "/work")
        full_env = {"PATH": BASE_PATH, "HOME": r.local(HOME), "LANG": "C.UTF-8"}
        full_env.update(r.create_kwargs.get("env_vars") or {})
        full_env.update(env or {})
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            r.to_local(cmd),
            cwd=workdir,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        limit = 60.0 if timeout is None else timeout
        out_buf, err_buf = bytearray(), bytearray()

        async def pump(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
            assert stream is not None
            while chunk := await stream.read(65536):
                buf.extend(chunk)

        running = asyncio.gather(pump(proc.stdout, out_buf), pump(proc.stderr, err_buf), proc.wait())
        try:
            await asyncio.wait_for(asyncio.shield(running), timeout=limit or None)
        except TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await running
            result = ExecResult(
                stdout=r.from_local(out_buf.decode(errors="replace")),
                stderr=r.from_local(err_buf.decode(errors="replace")),
                exit_code=-1,
                timed_out=True,
            )
            raise CommandTimeoutError(result, limit, r.id) from None
        out, err = bytes(out_buf), bytes(err_buf)
        return ExecResult(
            stdout=r.from_local(out.decode(errors="replace")),
            stderr=r.from_local(err.decode(errors="replace")),
            exit_code=proc.returncode if proc.returncode is not None else -1,
        )
