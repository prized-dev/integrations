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

"""Strands sandbox backed by a Prized sandbox (a Firecracker microVM)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import logging
import os
import posixpath
import threading
import uuid
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Literal

from strands.sandbox import ExecutionResult, FileInfo, Sandbox, StreamChunk
from strands.sandbox.constants import LANGUAGE_PATTERN
from strands.sandbox.errors import SandboxPathNotFoundError, SandboxTimeoutError
from strands.vended_tools.file_editor import make_file_editor
from strands.vended_tools.file_editor.file_editor import DEFAULT_FILE_EDITOR_DESCRIPTION
from strands.vended_tools.shell import make_shell
from strands.vended_tools.shell.types import SANDBOX_SHELL_DESCRIPTION

if TYPE_CHECKING:
    from prized import AsyncSandbox, PrizedError
    from prized import ExecResult as PrizedExecResult
    from strands.types.tools import AgentTool

logger = logging.getLogger(__name__)

OnClose = Literal["delete", "pause", "keep"]

_DEFAULT_TEMPLATE = "prized-base-v1"
_HOME = "/home/prized"
_DEFAULT_WORKING_DIR = "/home/prized/work"
# Code to run and file writes go through files here first.
_SCRATCH_DIR = "/home/prized/.prized/strands"
_DEFAULT_AUTO_PAUSE_MIN = 10
# A sandbox this adapter creates and deletes on close is deleted this long after it
# pauses, in case nothing closes it.
_DEFAULT_AUTO_DELETE_MIN = 60
_HELPER_TIMEOUT = 120
_GONE_STATES = frozenset({"deleting", "deleted", "error"})
_MAYBE_GONE_CODES = frozenset({"not_found", "sandbox_not_running", "sandbox_deleted"})
# File API refusals the shell can still serve (it follows symlinks and reports errno).
_SHELL_FALLBACK_CODES = frozenset({"symlink_not_allowed", "permission_denied", "path_outside_root"})
_LABELS = {"strands": "sandbox"}
_LOCAL_BIN = "/home/prized/.local/bin"
_FALLBACK_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
# The templates' Python is "externally managed": without this the model's `pip install`
# fails. With it, pip installs for the user (no root needed) into ~/.local, whose bin
# directory every command also gets on its PATH.
_PIP_ENV = {"PIP_BREAK_SYSTEM_PACKAGES": "1"}
_TRUNCATED_NOTE = "\n[prized: the output was cut off here; redirect large output to a file]\n"
_WRITE_FAILED_NOTE = (
    "\n[prized: the output was cut off here because the sandbox could not save it, most often because its "
    "disk is full; free some space and run the command again]\n"
)
# prized 0.1.1+ (OutputReadError): the command ran, but its output could not be read back.
_OUTPUT_READ_FAILED = "output_read_failed"
_REPLACED_NOTE = (
    "[prized: the previous sandbox was gone (its run time ended or it was deleted after being "
    "paused), so this ran in a fresh, empty sandbox: earlier files and installs are not there]\n"
)
_UNSET: Any = object()


class PrizedSandbox(Sandbox):
    """Run a Strands agent's shell commands, code and file tools in a Prized sandbox.

    A Prized sandbox is a Firecracker microVM (Ubuntu with Python 3.12, Node and git)
    that pauses with its memory and running processes, resumes where it stopped,
    and forks into live copies.

    The sandbox is created on first use (or ``await start()``), or attached by
    ``sandbox_id``. Close it with ``await aclose()`` (or ``close()``, ``with`` or
    ``async with``): a sandbox this object created is deleted, an attached one is kept;
    ``on_close`` changes that. A closed ``PrizedSandbox`` can't be used again. An idle
    sandbox pauses itself after ``auto_pause_min`` minutes and the next command resumes
    it, files, installs and processes intact.

    Example::

        from strands import Agent
        from strands_prized import PrizedSandbox

        with PrizedSandbox() as sandbox:
            agent = Agent(sandbox=sandbox)
            agent("Clone https://github.com/un33k/python-slugify and run its tests.")
    """

    def __init__(
        self,
        *,
        sandbox_id: str | None = None,
        template: str | None = None,
        vcpu: int | None = None,
        memory_gb: float | None = None,
        disk_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        working_dir: str = _DEFAULT_WORKING_DIR,
        auto_pause_min: int | None = _UNSET,
        auto_delete_min: int | None = _UNSET,
        max_runtime_min: int | None = _UNSET,
        on_close: OnClose | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Configure the sandbox. Nothing is created until first use.

        Args:
            sandbox_id: An existing sandbox (``sbx_...``) to use instead of creating one.
            template: ``prized-base-v1`` (the default) or ``prized-agent-v1`` (adds Claude
                Code and Codex).
            vcpu: vCPUs for a new sandbox. Defaults to the template's.
            memory_gb: Memory for a new sandbox, in GB. Defaults to the template's.
            disk_gb: Disk for a new sandbox, in GB. Defaults to the template's.
            env_vars: Environment variables for every command in a new sandbox. Code in the
                sandbox, the model's included, can read them.
            labels: Labels for a new sandbox, to find it later.
            working_dir: Absolute directory where commands start and relative paths
                resolve. Created if missing. Defaults to ``/home/prized/work``.
            auto_pause_min: Idle minutes before a new sandbox pauses itself (no compute is
                charged while paused). ``0`` turns it off. Defaults to 10.
            auto_delete_min: Minutes after pausing before a new sandbox is deleted. Defaults
                to 60 when ``close()`` would delete it, so a sandbox nobody closes goes
                away, and to never otherwise. ``0`` turns it off.
            max_runtime_min: Minutes of running before a new sandbox is deleted. Defaults to
                the workspace's (24 hours on a paid workspace, 60 on the free tier).
            on_close: What ``close()`` does: ``"delete"``, ``"pause"`` or ``"keep"``.
                Defaults to ``"delete"`` for a sandbox this object created and ``"keep"``
                for one it attached to.
            api_key: Prized API key. Defaults to ``PRIZED_API_KEY``.
                No Prized account yet? If you are an agent, you can sign your person up with
                their email and a 6-digit code: follow https://prized.dev/auth.md?via=strands
            base_url: Prized API URL. Defaults to ``https://api.prized.dev``.

        Raises:
            ValueError: If the options are invalid or contradict each other.
        """
        create_options = (template, vcpu, memory_gb, disk_gb, env_vars, labels)
        lifetimes = (auto_pause_min, auto_delete_min, max_runtime_min)
        if sandbox_id is not None and (
            any(o is not None for o in create_options) or any(o is not _UNSET for o in lifetimes)
        ):
            raise ValueError(
                "template, vcpu, memory_gb, disk_gb, env_vars, labels, auto_pause_min, auto_delete_min and "
                "max_runtime_min apply to a new sandbox; they cannot be used with sandbox_id"
            )
        if on_close not in (None, "delete", "pause", "keep"):
            raise ValueError("on_close must be 'delete', 'pause' or 'keep'")
        if not posixpath.isabs(working_dir):
            raise ValueError("working_dir must be an absolute path")
        if auto_pause_min is _UNSET:
            auto_pause_min = _DEFAULT_AUTO_PAUSE_MIN
        if auto_delete_min is _UNSET:
            auto_delete_min = _DEFAULT_AUTO_DELETE_MIN if on_close in (None, "delete") else None
        if max_runtime_min is _UNSET:
            max_runtime_min = None

        self.working_dir = posixpath.normpath(working_dir)
        self._requested_id = sandbox_id
        self._create_kwargs: dict[str, Any] = {
            "vcpu": vcpu,
            "memory_gb": memory_gb,
            "disk_gb": disk_gb,
            "env_vars": {**_PIP_ENV, **(env_vars or {})},
            "labels": {**_LABELS, **(labels or {})},
            "auto_pause_min": auto_pause_min,
            "auto_delete_min": auto_delete_min,
            "max_runtime_min": max_runtime_min,
        }
        self._template = template or _DEFAULT_TEMPLATE
        self._on_close = on_close
        self._api_key = api_key
        self._base_url = base_url
        # Which sandbox is in use is shared by every thread and event loop, under
        # `_state`. Each loop keeps its own handle (the SDK's connections belong to the
        # loop that opened them), valid for one generation: a replacement or a close
        # starts the next.
        self._state = threading.Lock()
        self._current_id: str | None = None  # None: not started yet, or closed
        self._sandbox_id: str | None = sandbox_id
        self._owned = False
        self._closed = False
        self._generation = 0
        self._handles: dict[asyncio.AbstractEventLoop, tuple[int, AsyncSandbox]] = {}
        self._last: AsyncSandbox | None = None
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._path: tuple[str, str] | None = None  # (sandbox id, PATH for commands)
        self._replaced = False

    # ---- lifecycle ----

    @property
    def sandbox_id(self) -> str | None:
        """The sandbox's id (``sbx_...``) once started, also after close.

        Pass it to ``PrizedSandbox(sandbox_id=...)`` to continue in the same sandbox
        later (with ``on_close="pause"`` or ``"keep"``).
        """
        return self._sandbox_id

    @property
    def sandbox(self) -> AsyncSandbox:
        """The started ``prized.AsyncSandbox``, for anything else the Prized SDK offers.
        Use it from the event loop that last ran an operation on this object."""
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._state:
            if self._current_id is None:
                raise RuntimeError("The sandbox is not started; call `await start()` first.")
            cached = self._handles.get(loop) if loop is not None else None
            if cached is not None and cached[0] == self._generation:
                return cached[1]
            if self._last is not None and self._last.id == self._current_id:
                return self._last
        raise RuntimeError("No handle yet: run an operation on this event loop first.")

    async def start(self) -> PrizedSandbox:
        """Create (or attach to) the sandbox now instead of on first use."""
        await self._ensure()
        return self

    async def aclose(self) -> None:
        """Delete, pause or keep the sandbox, per ``on_close``. Safe to call twice; a
        failed delete or pause can be retried."""
        from prized import PrizedError

        with self._state:
            if self._closed:
                return
            sandbox_id, owned = self._current_id, self._owned
            if sandbox_id is None:  # never started
                self._closed = True
                self._generation += 1
                return
        action = self._on_close or ("delete" if owned else "keep")
        try:
            sandbox = await self._acquire(replace=False)
        except RuntimeError:
            if self._closed:
                return  # closed by another thread meanwhile
            raise
        if sandbox is not None:
            try:
                if action == "delete":
                    await sandbox.delete()
                elif action == "pause":
                    await sandbox.pause(wait=False)
            except PrizedError as e:
                if e.code != "not_found":
                    raise  # nothing is released, so the close can be retried
        with self._state:
            self._closed = True
            self._current_id = None
            self._generation += 1  # a call still in flight can't replace it now
            self._handles = {}
        if sandbox is not None:
            await _close_quietly(sandbox)

    def close(self) -> None:
        """Synchronous :meth:`aclose`. Inside a running event loop (a notebook, an async
        app) it runs on a worker thread; from async code prefer ``await aclose()``."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, self.aclose()).result()

    async def __aenter__(self) -> PrizedSandbox:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __enter__(self) -> PrizedSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def pause(self) -> None:
        """Pause now: memory, disk and processes are kept, no compute is charged. The
        next operation resumes it."""
        await (await self._ensure()).pause()

    async def fork(self, count: int = 1, *, on_close: OnClose = "delete") -> list[PrizedSandbox]:
        """Fork into ``count`` live copies with this sandbox's files, memory and running
        processes. Each copy is started; closing it deletes it unless ``on_close`` says
        otherwise."""
        copies = await (await self._ensure()).fork(count)
        result = []
        for copy in copies:
            twin = PrizedSandbox(
                sandbox_id=copy.id,
                working_dir=self.working_dir,
                on_close=on_close,
                api_key=self._api_key,
                base_url=self._base_url,
            )
            # A copy that is ever replaced gets the parent's shape, env and labels.
            twin._template, twin._create_kwargs = self._template, dict(self._create_kwargs)
            twin._publish(asyncio.get_running_loop(), 0, copy, owned=True)
            result.append(twin)
        return result

    # ---- tools ----

    def get_tools(self) -> list[AgentTool]:
        """Strands' standard sandbox tools, bound to this sandbox."""
        where = (
            " Runs in a Prized sandbox (Ubuntu 24.04 microVM, Python 3.12, Node 22, git; no sudo)."
            f" Working directory: {self.working_dir}."
        )
        return [
            make_file_editor(
                sandbox=self,
                name="sandbox_file_editor",
                description=f"{DEFAULT_FILE_EDITOR_DESCRIPTION}{where}",
            ),
            make_shell(
                sandbox=self,
                name="sandbox_shell",
                description=f"{SANDBOX_SHELL_DESCRIPTION}{where}",
            ),
        ]

    # ---- execution ----

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> ExecutionResult:
        """Run a shell command and return its result."""
        result = await self._run(command, timeout=timeout, cwd=cwd, env=env)
        return self._to_result(result)

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Run a shell command, yielding its output as it arrives, then the result."""
        queue: asyncio.Queue[StreamChunk | None] = asyncio.Queue()

        def on_stdout(data: str) -> None:
            queue.put_nowait(StreamChunk(data, "stdout"))

        def on_stderr(data: str) -> None:
            queue.put_nowait(StreamChunk(data, "stderr"))

        async def run() -> PrizedExecResult:
            try:
                return await self._run(
                    command, timeout=timeout, cwd=cwd, env=env, on_stdout=on_stdout, on_stderr=on_stderr
                )
            finally:
                queue.put_nowait(None)

        task = asyncio.ensure_future(run())
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
            result = await task
        finally:
            if not task.done():
                # The consumer stopped early or was cancelled: stop the command.
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        yield self._to_result(result)

    async def execute_code(
        self,
        code: str,
        language: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> ExecutionResult:
        """Run ``code`` with the interpreter ``language`` (e.g. ``python3``, ``node``)."""
        script = await self._stage_code(code, language)
        result = await self._run(_run_script(language, script), timeout=timeout, cwd=cwd, env=env)
        return self._to_result(result)

    async def execute_code_streaming(
        self,
        code: str,
        language: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Run ``code`` with ``language``, yielding output as it arrives, then the result."""
        script = await self._stage_code(code, language)
        async for chunk in self.execute_streaming(_run_script(language, script), timeout=timeout, cwd=cwd, env=env):
            yield chunk

    # ---- files ----

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        """Read a file as bytes. Raises FileNotFoundError if it is missing or can't be read."""
        from prized import PrizedError

        resolved = self._resolve(path)
        sandbox = await self._ensure()
        if _inside_home(resolved):
            try:
                return await sandbox.files.read_bytes(resolved)
            except PrizedError as e:
                if e.code == "not_found":
                    missing = FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), resolved)
                    try:
                        await self._replace_if_gone(sandbox, e, missing_file=True)
                    except PrizedError as check:
                        raise missing from check
                    raise missing from e
                if e.code == "not_regular_file":
                    raise FileNotFoundError(errno.EISDIR, "not a regular file", resolved) from e
                if e.code == "file_too_large":
                    raise OSError(errno.EFBIG, e.message, resolved) from e
                if e.code not in _SHELL_FALLBACK_CODES:
                    raise
        # Outside /home/prized, or a symlink the file API refuses: the shell reads it.
        result = await self._helper(["base64", "-w0", "--", resolved])
        if result.exit_code != 0:
            raise FileNotFoundError(errno.ENOENT, result.stderr.strip() or "cannot read", resolved)
        if result.truncated:
            raise OSError(errno.EFBIG, "too large to read through the shell (about 3 MiB at most)", resolved)
        return base64.b64decode(result.stdout)

    async def write_file(self, path: str, content: bytes, **kwargs: Any) -> None:
        """Write bytes to a file, creating its parent directories. An existing file keeps
        its mode (an executable stays executable), and a symlink is followed."""
        from prized import PrizedError

        resolved = self._resolve(path)
        staging = f"{_SCRATCH_DIR}/{uuid.uuid4().hex}"
        sandbox = await self._ensure()
        try:
            await sandbox.files.write(staging, bytes(content))
        except PrizedError as e:
            sandbox = await self._replace_if_gone(sandbox, e)
            await sandbox.files.write(staging, bytes(content))
        # `cat >` writes into the existing file, so its mode, owner and links stay.
        script = 'trap \'rm -f -- "$1"\' EXIT; mkdir -p -- "$(dirname -- "$2")" && cat -- "$1" > "$2"'
        result = await self._helper(["bash", "-c", script, "write_file", staging, resolved])
        if result.exit_code != 0:
            raise _os_error(result.stderr, resolved)

    async def remove_file(self, path: str, **kwargs: Any) -> None:
        """Remove a file. Raises FileNotFoundError if it does not exist."""
        resolved = self._resolve(path)
        result = await self._helper(["rm", "--", resolved])
        if result.exit_code != 0:
            raise FileNotFoundError(errno.ENOENT, result.stderr.strip() or "cannot remove", resolved)

    async def list_files(self, path: str, **kwargs: Any) -> list[FileInfo]:
        """List a directory's entries with their kind and size."""
        from prized import PrizedError

        resolved = self._resolve(path)
        sandbox = await self._ensure()
        try:
            entries = await sandbox.files.list(resolved)
        except PrizedError as e:
            if e.code == "not_directory":
                raise SandboxPathNotFoundError(path) from e
            if e.code not in _MAYBE_GONE_CODES:
                raise OSError(errno.EIO, e.message, resolved) from e
            # not_found is a missing path, or a missing sandbox: replace a gone one.
            retry = await self._replace_if_gone(sandbox, e, missing_file=True)
            if retry is sandbox:
                raise SandboxPathNotFoundError(path) from e
            try:
                entries = await retry.files.list(resolved)
            except PrizedError as e2:
                if e2.code in ("not_found", "not_directory"):
                    raise SandboxPathNotFoundError(path) from e2
                raise OSError(errno.EIO, e2.message, resolved) from e2
        return [FileInfo(name=e.name, is_dir=e.is_dir, size=None if e.is_dir else e.size) for e in entries]

    # ---- internals ----

    async def _run(
        self,
        command: str,
        *,
        timeout: float | None,
        cwd: str | None,
        env: dict[str, str] | None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> PrizedExecResult:
        from prized import CommandTimeoutError, PrizedError

        directory = self._resolve(cwd) if cwd is not None else self.working_dir
        limit = 0 if timeout is None else timeout
        sandbox = await self._ensure()

        async def once(target: AsyncSandbox) -> PrizedExecResult:
            full_env = {**_PIP_ENV, "PATH": await self._command_path(target), **(env or {})}
            return await target.exec(
                command,
                cwd=directory,
                env=full_env,
                timeout=limit,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                full_output=True,
            )

        try:
            try:
                return await once(sandbox)
            except PrizedError as e:
                if isinstance(e, CommandTimeoutError) or e.code == _OUTPUT_READ_FAILED:
                    raise
                return await once(await self._replace_if_gone(sandbox, e))
        except CommandTimeoutError as e:
            raise SandboxTimeoutError(timeout, e.stdout, e.stderr + _cut_note(e.result)) from e
        except PrizedError as e:
            # The command ran: never run it again, report its exit code and where its output is.
            if e.code != _OUTPUT_READ_FAILED:
                raise
            return _unread_result(e)

    async def _helper(self, argv: list[str]) -> PrizedExecResult:
        """A short helper command, retried once on a replacement sandbox if it was gone."""
        from prized import PrizedError

        sandbox = await self._ensure()
        try:
            return await sandbox.exec(argv, timeout=_HELPER_TIMEOUT)
        except PrizedError as e:
            sandbox = await self._replace_if_gone(sandbox, e)
            return await sandbox.exec(argv, timeout=_HELPER_TIMEOUT)

    async def _command_path(self, sandbox: AsyncSandbox) -> str:
        """The sandbox's PATH with ~/.local/bin first, so pip-installed tools run."""
        if self._path is None or self._path[0] != sandbox.id:
            found = (await sandbox.exec(["printenv", "PATH"], timeout=_HELPER_TIMEOUT)).stdout.strip()
            parts = (found or _FALLBACK_PATH).split(":")
            self._path = (sandbox.id, ":".join([_LOCAL_BIN] + [p for p in parts if p != _LOCAL_BIN]))
        return self._path[1]

    async def _stage_code(self, code: str, language: str) -> str:
        if not LANGUAGE_PATTERN.fullmatch(language):
            raise ValueError(f"language parameter contains invalid characters: {language}")
        script = f"{_SCRATCH_DIR}/{uuid.uuid4().hex}"
        await self.write_file(script, code.encode("utf-8"))
        return script

    def _lock_for(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        """Serializes the tasks of one event loop (threads meet in `_publish`)."""
        with self._state:
            for stale in [lp for lp in self._locks if lp.is_closed()]:
                del self._locks[stale]
                self._handles.pop(stale, None)
            lock = self._locks.get(loop)
            if lock is None:
                lock = self._locks[loop] = asyncio.Lock()
            return lock

    async def _ensure(self) -> AsyncSandbox:
        """This loop's handle on the sandbox, starting (or replacing) it if needed."""
        loop = asyncio.get_running_loop()
        with self._state:
            if self._closed:
                raise RuntimeError("This PrizedSandbox is closed; make a new one.")
            cached = self._handles.get(loop)
            if self._current_id is not None and cached is not None and cached[0] == self._generation:
                return cached[1]
            started = self._current_id is not None
        if not started:
            await self._start(loop)
        sandbox = await self._acquire()
        if sandbox is None:  # pragma: no cover - only without replace
            raise RuntimeError("no sandbox")
        return sandbox

    async def _start(self, loop: asyncio.AbstractEventLoop) -> None:
        async with self._lock_for(loop):
            with self._state:
                if self._closed:
                    raise RuntimeError("This PrizedSandbox is closed; make a new one.")
                if self._current_id is not None:
                    return
                generation = self._generation
            if self._requested_id is not None:
                sandbox, owned, created = await self._attach(self._requested_id), False, False
            else:
                sandbox, owned, created = await self._create(), True, True
            if not self._publish(loop, generation, sandbox, owned):
                # Another thread started it first: use that one.
                if created:
                    await _discard(sandbox)
                else:
                    await _close_quietly(sandbox)

    async def _acquire(self, *, failed: AsyncSandbox | None = None, replace: bool = True) -> AsyncSandbox | None:
        """This loop's handle on the sandbox in use, connecting or replacing it.

        With ``failed`` (a handle an operation just failed on), a handle that is still
        current is checked: if its sandbox is fine it is returned as it is. With
        ``replace=False`` (closing), a gone sandbox gives None.
        """
        from prized import PrizedError

        loop = asyncio.get_running_loop()
        async with self._lock_for(loop):
            for _ in range(5):
                with self._state:
                    generation, sandbox_id, owned = self._generation, self._current_id, self._owned
                    cached = self._handles.get(loop)
                    closed = self._closed
                if sandbox_id is None:
                    # Closed meanwhile (no new sandbox after close), or not started.
                    raise RuntimeError(
                        "This PrizedSandbox is closed; make a new one."
                        if closed
                        else "The sandbox is not started; call `await start()` first."
                    )
                current = cached[1] if cached is not None and cached[0] == generation else None
                if current is not None and current is not failed:
                    return current
                handle: AsyncSandbox | None
                if current is not None:  # the failed handle is still current
                    handle = current
                    try:
                        await current.refresh()
                        gone = current.state in _GONE_STATES
                    except PrizedError as e:
                        if e.code != "not_found":
                            raise
                        gone = True
                    if not gone:
                        return current
                else:
                    handle = await self._connect(sandbox_id)
                    gone = handle is None or handle.state in _GONE_STATES
                    if handle is not None and not gone:
                        with self._state:
                            if self._generation == generation:
                                self._handles[loop] = (generation, handle)
                                self._last = handle
                                return handle
                        await _close_quietly(handle)  # moved on meanwhile: look again
                        continue
                # The sandbox is gone.
                if handle is not None and owned and handle.state == "error":
                    await _discard(handle)  # a failed sandbox still counts toward quota
                    handle = None
                if not replace:
                    if handle is not None and handle is not current:
                        await _close_quietly(handle)
                    return None
                if not owned:
                    raise RuntimeError(f"Sandbox {sandbox_id} is gone.")
                new = await self._create()
                if self._publish(loop, generation, new, owned=True, replaced=True):
                    logger.warning(
                        "sandbox_id=<%s>, new_sandbox_id=<%s> | prized sandbox is gone, created a fresh one; "
                        "its files are lost",
                        sandbox_id,
                        new.id,
                    )
                    return new
                await _discard(new)  # another thread replaced it first: use theirs
            raise RuntimeError("Could not settle on a sandbox; try again.")

    async def _connect(self, sandbox_id: str) -> AsyncSandbox | None:
        """A handle on this loop, or None when the sandbox no longer exists."""
        from prized import AsyncSandbox, PrizedError

        try:
            return await AsyncSandbox.connect(sandbox_id, api_key=self._api_key, base_url=self._base_url)
        except PrizedError as e:
            if e.code == "not_found":
                return None
            raise

    async def _create(self) -> AsyncSandbox:
        from prized import AsyncSandbox

        sandbox = await AsyncSandbox.create(
            self._template, api_key=self._api_key, base_url=self._base_url, **self._create_kwargs
        )
        if self.working_dir != _DEFAULT_WORKING_DIR:
            try:
                await _make_dir(sandbox, self.working_dir)
            except BaseException:
                await _discard(sandbox)
                raise
        return sandbox

    async def _attach(self, sandbox_id: str) -> AsyncSandbox:
        sandbox = await self._connect(sandbox_id)
        if sandbox is None or sandbox.state in _GONE_STATES:
            state = "gone" if sandbox is None else sandbox.state
            if sandbox is not None:
                await _close_quietly(sandbox)
            raise RuntimeError(f"Sandbox {sandbox_id} is {state}.")
        if self.working_dir != _DEFAULT_WORKING_DIR:
            await _make_dir(sandbox, self.working_dir)
        return sandbox

    def _publish(
        self,
        loop: asyncio.AbstractEventLoop,
        generation: int,
        sandbox: AsyncSandbox,
        owned: bool,
        *,
        replaced: bool = False,
    ) -> bool:
        """Makes ``sandbox`` the one in use, unless another thread moved on since
        ``generation`` was read (or it was closed); then the caller still owns it."""
        with self._state:
            if self._generation != generation or self._closed:
                return False
            self._generation += 1
            self._current_id = self._sandbox_id = sandbox.id
            self._owned = owned
            self._handles = {loop: (self._generation, sandbox)}
            self._last = sandbox
            if replaced:
                self._replaced = True
            return True

    async def _replace_if_gone(
        self, failed: AsyncSandbox, error: PrizedError, *, missing_file: bool = False
    ) -> AsyncSandbox:
        """A sandbox to retry on after ``error`` on ``failed``, or ``error`` raised again.

        Only a sandbox this object created is replaced, and only once it is gone. With
        ``missing_file``, a sandbox that is still there is returned as it is, so the
        caller reports the missing file.
        """
        if error.code not in _MAYBE_GONE_CODES:
            raise error
        sandbox = await self._acquire(failed=failed)
        if sandbox is failed:
            if missing_file:
                return failed
            raise error
        assert sandbox is not None
        return sandbox

    def _to_result(self, result: PrizedExecResult) -> ExecutionResult:
        stderr = result.stderr + _cut_note(result)
        with self._state:
            replaced, self._replaced = self._replaced, False
        if replaced:
            stderr = _REPLACED_NOTE + stderr
        return ExecutionResult(exit_code=result.exit_code, stdout=result.stdout, stderr=stderr)

    def _resolve(self, path: str) -> str:
        if not path:
            raise ValueError("path must not be empty")
        return posixpath.normpath(posixpath.join(self.working_dir, path))


def _cut_note(result: PrizedExecResult | None) -> str:
    """The note for output the SDK cut, by why it did (prized 0.1.1+ says: ``truncated_reason``)."""
    if not getattr(result, "truncated", False):
        return ""
    if getattr(result, "truncated_reason", None) == "write_failed":
        return _WRITE_FAILED_NOTE
    return _TRUNCATED_NOTE


def _unread_result(error: PrizedError) -> PrizedExecResult:
    """The result of a command that ran but whose output could not be read back
    (prized 0.1.1+ ``OutputReadError``): its exit code, and a note on where the output is kept."""
    from prized import ExecResult

    paths = " and ".join(p for p in (getattr(error, "stdout_path", None), getattr(error, "stderr_path", None)) if p)
    kept = (
        f" It is kept in the sandbox for an hour: read {paths} instead of running the command again." if paths else ""
    )
    why = f" ({error.reason})" if error.reason else ""
    note = f"[prized: the command ran, but its output could not be read back{why}.{kept}]\n"
    return ExecResult(stdout="", stderr=note, exit_code=getattr(error, "exit_code", 1))


def _inside_home(path: str) -> bool:
    return path == _HOME or path.startswith(_HOME + "/")


def _run_script(language: str, script: str) -> str:
    # The code goes in on stdin, like Strands' shell backends: imports resolve from the
    # working directory. The staged script is removed when the interpreter exits.
    return f"trap 'rm -f -- {script}' EXIT; {language} < {script}"


async def _close_quietly(sandbox: AsyncSandbox) -> None:
    with contextlib.suppress(Exception):
        await sandbox.close()


async def _discard(sandbox: AsyncSandbox) -> None:
    """Deletes a sandbox this object made and no longer uses."""
    with contextlib.suppress(Exception):
        await sandbox.delete()
    await _close_quietly(sandbox)


async def _make_dir(sandbox: AsyncSandbox, path: str) -> None:
    result = await sandbox.exec(["mkdir", "-p", "--", path], timeout=_HELPER_TIMEOUT)
    if result.exit_code != 0:
        raise _os_error(result.stderr, path)


def _os_error(stderr: str, path: str) -> OSError:
    """The OSError a failed shell helper stands for."""
    message = stderr.strip() or "command failed"
    for needle, number, kind in (
        ("No such file or directory", errno.ENOENT, FileNotFoundError),
        ("Is a directory", errno.EISDIR, IsADirectoryError),
        ("Permission denied", errno.EACCES, PermissionError),
        ("Not a directory", errno.ENOTDIR, NotADirectoryError),
    ):
        if needle in message:
            return kind(number, message, path)
    return OSError(errno.EIO, message, path)
