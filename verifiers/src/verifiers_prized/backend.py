"""`PrizedBackend`: a verifiers `RuntimeBackend` on Prized Sandboxes.

    [env.agent.runtime]
    type = "custom"
    import_path = "verifiers_prized.PrizedBackend"
    setup = ["python3 -m venv ~/.venv && ~/.venv/bin/pip install -q numpy"]

`start` takes a running copy of a parent sandbox in which `setup` already ran (pool.py), so a
rollout starts with the environment's disk, memory and processes as they were after setup.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import posixpath
import shlex
import uuid
import weakref
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from prized import CommandTimeoutError, PrizedError
from verifiers.v1.errors import SandboxError
from verifiers.v1.runtimes import (
    CustomRuntimeInfo,
    ProgramResult,
    Runtime,
    RuntimeBackend,
    RuntimeProcess,
)

from . import pool as _pool
from .config import DEFAULT_WORKDIR, HOME, PrizedConfig

STAGING_DIR = f"{HOME}/.cache/verifiers-prized"
"""Uploads land under /home/prized only; a file for elsewhere is staged here, then moved."""
STDIN_CHUNK = 48 * 1024
"""Raw bytes per stdin write request (64 KiB once base64-encoded, well under argv limits)."""

# `exec`: the command reads stdin from a FIFO that a holder process keeps open for writing, so
# the command sees no end of input between writes. Each `write` opens the FIFO, writes, closes.
# Its stdout and stderr go through two more FIFOs to encoders that print each read as one line,
# ":" and its base64, as soon as it arrives: the SDK hands output on as UTF-8 text, which would turn
# bytes that are not UTF-8 (the length prefix of a framed protocol such as verifiers' ACP) into
# U+FFFD. The encoders ignore SIGTERM, so what a terminated command prints on its way out arrives.
# Once the command exits the wrapper waits up to 2 s for them (a USR1 from a timer ends the wait),
# never killing them: a child the command left running keeps its stdout and is never sent SIGPIPE.
# dash prints "Terminated" (or "Killed", ...) for a command killed by a signal, with the command's
# own redirections still in place, or later, once a trapped TERM has cut its wait short. So the
# command runs in a subshell that sets its own `2>` first (a FIFO it cannot open is then reported
# on the command's stderr) and execs it, and once the FIFOs, encoders and holder are set up (their
# errors still reach stderr) the wrapper's own stderr is /dev/null: that line never reaches the
# command's stderr or lands inside an encoder's line. The stdin FIFO goes as soon as the command
# exits, so a later write fails at once. The wrapper's own names start
# with __vfp_, so a caller's variables reach the command unchanged.
_EXEC_WRAPPER = (
    'trap : USR1 USR2; __vfp_f=$1; shift; mkfifo -m 600 "$__vfp_f" "$__vfp_f.1" "$__vfp_f.2" || exit 125; '
    "__vfp_enc() { trap '' TERM USR1 USR2; while __vfp_c=$(dd bs=49152 count=1 2> /dev/null | base64 -w 0) "
    '&& [ -n "$__vfp_c" ]; do printf \':%s\\n\' "$__vfp_c"; done; }; '
    '__vfp_enc < "$__vfp_f.1" & __vfp_o=$!; __vfp_enc < "$__vfp_f.2" >&2 & __vfp_e=$!; '
    'sleep 2147483647 > "$__vfp_f" & __vfp_h=$!; trap : TERM; '
    'exec 2> /dev/null; ( exec "$@" 2> "$__vfp_f.2" < "$__vfp_f" > "$__vfp_f.1" ); __vfp_r=$?; '
    'rm -f "$__vfp_f"; kill "$__vfp_h" 2> /dev/null; '
    '{ sleep 2; kill -USR1 $$; } > /dev/null 2>&1 & __vfp_k=$!; wait "$__vfp_o" "$__vfp_e"; '
    'kill "$__vfp_k" 2> /dev/null; rm -f "$__vfp_f.1" "$__vfp_f.2"; exit "$__vfp_r"'
)
# One stdin write: wait briefly for the FIFO (the command may still be starting), then write
# the decoded bytes. The stdin FIFO gone while the stdout one is there, or gone during the write:
# the command has exited (exit 4). `dd conv=nocreat` never makes a regular file in its place, and
# `timeout` ends an open that waits for a reader that is gone.
_STDIN_WRITE = (
    'i=0; while [ ! -p "$1" ]; do [ -p "$1.1" ] && exit 4; [ "$i" -ge 100 ] && exit 3; '
    "i=$((i+1)); sleep 0.05; done; "
    'printf %s "$2" | timeout 30 sh -c \'base64 -d | dd of="$1" bs=65536 conv=nocreat,notrunc status=none\' '
    'sh "$1"; r=$?; [ "$r" -ne 0 ] && [ ! -p "$1" ] && exit 4; exit "$r"'
)
_MOVE_INTO_PLACE = 'mkdir -p -- "$(dirname -- "$1")" && mv -f -- "$2" "$1"'


def _error(what: str, err: BaseException) -> SandboxError:
    return SandboxError(f"prized {what}: {err}")


class _Lines:
    """One `exec` stream as the SDK hands it on (text, in pieces): each whole line is decoded
    (an encoder's chunk, or a line the wrapper's own shell printed) and queued as bytes."""

    def __init__(self, queue: asyncio.Queue[bytes | None]) -> None:
        self._queue = queue
        self._rest = ""

    def __call__(self, text: str) -> None:
        *lines, self._rest = (self._rest + text).split("\n")
        for line in lines:
            self._queue.put_nowait(self._decoded(line, "\n"))

    def close(self) -> None:
        rest, self._rest = self._rest, ""
        if rest.startswith(":"):
            # An encoder stopped mid-line (a SIGKILL): what its whole base64 groups hold, never
            # the encoding itself as output.
            whole = rest[1 : 1 + (len(rest) - 1) // 4 * 4]
            with contextlib.suppress(binascii.Error, ValueError):
                if data := base64.b64decode(whole, validate=True):
                    self._queue.put_nowait(data)
        elif rest:
            self._queue.put_nowait(rest.encode())
        self._queue.put_nowait(None)

    @staticmethod
    def _decoded(line: str, end: str) -> bytes:
        if line.startswith(":"):
            with contextlib.suppress(binascii.Error, ValueError):
                return base64.b64decode(line[1:], validate=True)
        return (line + end).encode()


class PrizedProcess(RuntimeProcess):
    """A command started with `exec`: stdout and stderr stream as the command prints, byte for
    byte (each read crosses as a base64 line, see `_EXEC_WRAPPER`); stdin writes go through a
    FIFO."""

    def __init__(self, sandbox: Any, handle: Any, stdin_path: str) -> None:
        self._sandbox = sandbox
        self._handle = handle
        self._stdin_path = stdin_path
        self._stdin_lock = asyncio.Lock()
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._err: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._done = asyncio.Event()
        self._returncode: int | None = None
        self._failure: BaseException | None = None
        self._killed = False
        self.stdout: AsyncIterator[bytes] = self._stream(self._out)
        self.stderr: AsyncIterator[bytes] = self._stream(self._err)
        self._pump = asyncio.ensure_future(self._follow())

    @staticmethod
    async def _stream(queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
        while (chunk := await queue.get()) is not None:
            yield chunk

    async def _follow(self) -> None:
        stdout, stderr = _Lines(self._out), _Lines(self._err)
        try:
            result = await self._handle.wait(on_stdout=stdout, on_stderr=stderr)
            self._returncode = result.exit_code
        except asyncio.CancelledError as err:  # abandoned: the sandbox is being deleted
            self._failure = err
            raise
        except Exception as err:  # noqa: BLE001 - reported by wait()
            self._failure = err
        finally:
            stdout.close()
            stderr.close()
            self._done.set()
        with contextlib.suppress(Exception):
            await self._handle.cleanup()

    async def write(self, data: bytes) -> None:
        async with self._stdin_lock:
            for start in range(0, len(data), STDIN_CHUNK):
                if self._done.is_set() or self._killed:
                    raise SandboxError("prized exec: the process has exited; its stdin is closed")
                chunk = base64.b64encode(data[start : start + STDIN_CHUNK]).decode()
                try:
                    result = await self._sandbox.exec(
                        ["sh", "-c", _STDIN_WRITE, "sh", self._stdin_path, chunk], timeout=60
                    )
                except PrizedError as err:
                    raise _error("stdin write", err) from err
                if result.exit_code == 4:
                    raise SandboxError("prized exec: the process has exited; its stdin is closed")
                if result.exit_code != 0:
                    raise SandboxError(
                        f"prized stdin write: exit {result.exit_code} (the process may have exited) "
                        f"{result.stderr.strip()[-300:]}"
                    )

    async def wait(self) -> int:
        """The exit code; -1 once its sandbox was deleted at teardown. A process lost while the
        rollout still needs it raises SandboxError."""
        await self._done.wait()
        return self._exit_code()

    async def poll(self) -> int | None:
        """None while it runs, then its exit code, or -1 if it was lost or abandoned."""
        if not self._done.is_set():
            return None
        return self._returncode if self._returncode is not None else -1

    def _exit_code(self) -> int:
        if self._returncode is not None:
            return self._returncode
        if isinstance(self._failure, asyncio.CancelledError):
            return -1  # abandoned: its sandbox was deleted at teardown
        # Lost (the sandbox gone, the API out of reach past the SDK's wait): a sandbox error, so
        # verifiers records the cause and its retry rules for SandboxError apply.
        raise SandboxError(f"prized exec: lost the process: {self._failure!r}")

    async def _signal(self, name: str) -> None:
        if self._done.is_set():
            return
        try:
            await self._handle.kill(name)
        except PrizedError as err:
            if err.code != "not_found" and not self._done.is_set():
                raise _error(f"{name} to the process", err) from err

    async def terminate(self) -> None:
        await self._signal("SIGTERM")

    async def kill(self) -> None:
        await self._signal("SIGKILL")
        self._killed = True  # its stdin FIFO has no reader now: a write would wait 30 s for one

    def abandon(self) -> None:
        """Stop following the process (its sandbox is being deleted)."""
        self._pump.cancel()


class PrizedBackend(RuntimeBackend):
    """One rollout's sandbox: a fork of a parent in which `setup` ran (or, with `fork = false`,
    a sandbox of its own that runs `setup` itself)."""

    config_class = PrizedConfig
    is_local = False
    # No task images, other users, published ports, Compose or egress policies on Prized yet:
    # verifiers refuses tasks that need them before anything is provisioned.
    SUPPORTS_TASK_IMAGES: ClassVar[bool] = False
    SUPPORTS_USER_SWITCHING: ClassVar[bool] = False
    SUPPORTS_PORT_EXPOSURE: ClassVar[bool] = False
    SUPPORTS_COMPOSE: ClassVar[bool] = False
    scripts_dir: ClassVar[str] = f"{HOME}/.cache/vf-scripts"

    config: PrizedConfig

    def __init__(self, config: PrizedConfig, name: str | None = None) -> None:
        super().__init__(name)
        workdir = config.resolved_workdir
        self.workdir = workdir if workdir.startswith("/") else posixpath.join(DEFAULT_WORKDIR, workdir)
        # As the Prime and Docker runtimes do: the config and info name the workdir in use, which
        # verifiers reads back (a judge looking at the solver's files, say).
        self.config = config.model_copy(update={"workdir": self.workdir})
        self.info = CustomRuntimeInfo(**self.config.model_dump())
        self.sandbox: Any = None
        self._creds: _pool.Credentials | None = None
        self._pool: _pool.ParentPool | None = None
        self._parent: _pool.Parent | None = None
        self._processes: weakref.WeakSet[PrizedProcess] = weakref.WeakSet()

    # -- lifecycle

    async def start(self) -> None:
        if self.sandbox is not None:
            raise SandboxError("prized runtime already started")
        creds = self._creds = _pool.credentials(self.config)
        try:
            if self.config.fork:
                pool = _pool.pool_for(self.config, creds)
                self.sandbox, self._parent = await pool.acquire()
                self._pool = pool
            else:
                self.sandbox = await _pool.build_sandbox(
                    _pool.Recipe.of(self.config, creds), creds, role="rollout", pause=False
                )
            self.info.id = self.sandbox.id
            if self.workdir != DEFAULT_WORKDIR:
                made = await self.sandbox.exec(["mkdir", "-p", "--", self.workdir], timeout=60)
                if made.exit_code != 0:
                    raise SandboxError(
                        f"prized: cannot use workdir {self.workdir!r}: {made.stderr.strip()[-300:]} "
                        "(commands run as the user prized, without root: use a path under "
                        "/home/prized or /tmp)"
                    )
        except SandboxError:
            raise
        except PrizedError as err:
            raise _error("sandbox provisioning failed", err) from err

    async def teardown(self) -> None:
        sandbox, self.sandbox = self.sandbox, None  # the idempotency guard; info.id stays
        if sandbox is None:
            return
        for process in list(self._processes):
            process.abandon()
        await _pool.discard(sandbox)
        if self._pool is not None and self._parent is not None:
            self._pool.released(self._parent)

    def cleanup(self) -> None:
        # The atexit backstop: synchronous, best effort, idempotent.
        if self.info.id is not None and self._creds is not None and _pool.is_tracked(self.info.id):
            _pool.delete_now(self.info.id, self._creds)

    # -- commands and files

    def _live(self) -> Any:
        if self.sandbox is None:
            raise SandboxError("prized runtime is not started, or already stopped")
        return self.sandbox

    def _path(self, path: str) -> str:
        return posixpath.normpath(path if path.startswith("/") else posixpath.join(self.workdir, path))

    async def run(self, runtime: Runtime, argv: list[str], env: dict[str, str]) -> ProgramResult:
        sandbox = self._live()
        limit = self.config.blocking_run_timeout
        try:
            result = await sandbox.exec(
                ["sh", "-c", 'exec "$@" < /dev/null', "sh", *argv],
                cwd=self.workdir,
                env=runtime.process_env(env) or None,
                # No limit: run detached and polled. A limit up to 240 s: one blocking call.
                timeout=limit or 0,
                full_output=bool(limit),
            )
        except CommandTimeoutError as err:
            raise SandboxError(
                f"prized exec: the command ran past blocking_run_timeout ({limit:g}s) and was killed"
            ) from err
        except PrizedError as err:
            raise _error("exec failed", err) from err
        if result.truncated:
            # Never a partial answer passed off as whole (a file read through `run`, say).
            raise SandboxError(
                f"prized exec: the command printed more than the SDK keeps ({result.truncated_reason}); "
                "send large output to a file and read it"
            )
        return ProgramResult(result.exit_code, result.stdout, result.stderr)

    async def exec(self, runtime: Runtime, argv: list[str], env: dict[str, str]) -> RuntimeProcess:
        sandbox = self._live()
        stdin_path = f"/tmp/vfp-stdin-{uuid.uuid4().hex}"
        try:
            handle = await sandbox.exec(
                # `exec`: the SDK's own shell gives way to the wrapper, so a signal meets the wrapper
                # and its exit code is the command's.
                ["exec", "sh", "-c", _EXEC_WRAPPER, "vfp-exec", stdin_path, *argv],
                cwd=self.workdir,
                env=runtime.process_env(env) or None,
                background=True,
            )
        except PrizedError as err:
            raise _error("streaming process failed to start", err) from err
        process = PrizedProcess(sandbox, handle, stdin_path)
        self._processes.add(process)
        return process

    async def run_background(self, runtime: Runtime, argv: list[str], env: dict[str, str], log: str) -> None:
        sandbox = self._live()
        try:
            await sandbox.exec(
                ["sh", "-c", f'exec "$@" > {shlex.quote(log)} 2>&1 < /dev/null', "sh", *argv],
                cwd=self.workdir,
                env=runtime.process_env(env) or None,
                background=True,
            )
        except PrizedError as err:
            raise _error("background launch failed", err) from err

    async def write(self, runtime: Runtime, path: str, data: bytes) -> None:
        sandbox = self._live()
        target = self._path(path)
        try:
            if target.startswith(HOME + "/"):
                await sandbox.files.write(target, data)
                return
            staged = f"{STAGING_DIR}/{uuid.uuid4().hex}"
            await sandbox.files.write(staged, data)
            moved = await sandbox.exec(["sh", "-c", _MOVE_INTO_PLACE, "sh", target, staged], timeout=120)
        except PrizedError as err:
            raise _error(f"write {path!r}", err) from err
        if moved.exit_code != 0:
            with contextlib.suppress(Exception):
                await sandbox.exec(["rm", "-f", "--", staged], timeout=30)
            raise SandboxError(f"prized write {path!r}: {moved.stderr.strip()[-500:]}")

    async def read(self, runtime: Runtime, path: str, max_bytes: int | None = None) -> bytes:
        sandbox = self._live()
        target = self._path(path)
        if max_bytes is not None or not target.startswith(HOME + "/"):
            # Bounded at the source (`head -c`), or outside the files API's reach: the base
            # class reads through `run`.
            return await super().read(runtime, target, max_bytes)
        try:
            data: bytes = await sandbox.files.read_bytes(target)
        except PrizedError as err:
            raise _error(f"read {path!r}", err) from err
        return data

    async def expose(self, port: int) -> str:
        raise SandboxError("the Prized runtime cannot expose a port: sandboxes have no public URLs yet")
