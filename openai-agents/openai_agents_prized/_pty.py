"""Processes that outlive one call: what `exec_command` and `write_stdin` run on.

A command that is still running when `exec_command`'s wait ends keeps
running in the background in the sandbox (its own session, a `script` TTY on
request, stdin a FIFO); each poll waits in the sandbox until it exits or the
caller's deadline nears. A process this session may have started is always
tracked until it is known to be gone: a start whose answer is lost is killed
at once, and if even that fails, by `pty_terminate_all`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import shlex
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agents.sandbox.errors import ExecTransportError
from agents.sandbox.session.pty_output import collect_pty_output
from agents.sandbox.session.pty_types import (
    PTY_PROCESSES_MAX,
    PTY_PROCESSES_WARNING,
    PtyExecUpdate,
    allocate_pty_process_id,
    clamp_pty_yield_time_ms,
    process_id_to_prune_from_meta,
    resolve_pty_write_yield_time_ms,
)

from openai_agents_prized._core import PrizedSessionBase, ran_for_sure_not
from openai_agents_prized._options import PRIZED_HOME
from openai_agents_prized._scripts import PTY_KILL, PTY_POLL, PTY_START, PTY_STDIN, PTY_STDIN_ENV

if TYPE_CHECKING:
    from pathlib import Path

    from agents.sandbox.types import User

logger = logging.getLogger("openai_agents_prized")

PTY_ROOT = "/tmp/openai-agents-prized/pty"  # noqa: S108  # inside the sandbox
"""Where background processes keep their stdin FIFO, output, and exit code."""

_PTY_POLL_MARGIN_S = 0.15
"""How much earlier than the caller's deadline a server-side wait ends, for the trip back."""

_PTY_POLL_MAX_BYTES = 2 * 1024 * 1024
"""Output one poll returns at most; base64 keeps it inside the edge's 4 MiB stdout."""


@dataclass
class PtyProcess:
    """One background process started by `pty_exec_start`."""

    dir: str
    tty: bool
    offset: int = 0
    exit_code: int | None = None
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    output_closed: asyncio.Event = field(default_factory=asyncio.Event)
    poll_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """One poll at a time: two would read the same bytes from the same offset."""
    last_used: float = field(default_factory=time.monotonic)


class PrizedPtyMixin(PrizedSessionBase):
    """`pty_exec_start`, `pty_write_stdin`, and `pty_terminate_all` for a Prized session."""

    def supports_pty(self) -> bool:
        """Whether `exec_command` may leave a process running: always."""
        return True

    async def pty_exec_start(  # noqa: PLR0913  # the SDK's signature
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        """Start a command that may outlive this call, and collect its first output.

        The command runs in the background in the sandbox. Its output so far
        comes back after `yield_time_s`, or sooner when it exits; while it runs,
        `pty_write_stdin` sends it input (with `tty=True`) and collects more.
        Without a `timeout` it runs until it exits or the session ends.

        Returns:
            The output so far, and the process id while the process runs, or
            its exit code once it has exited.
        """
        argv = self._without_run_as_prefix(
            self._prepare_exec_command(*command, shell=shell, user=user)
        )
        if timeout is not None:
            argv = ["timeout", "-s", "KILL", str(max(1, math.ceil(timeout))), *argv]
        async with self._pty_lock:
            process_id = allocate_pty_process_id(self._reserved_pty_process_ids)
            self._reserved_pty_process_ids.add(process_id)
        entry = PtyProcess(
            dir=f"{PTY_ROOT}/{self.state.session_id.hex}/{process_id}-{uuid.uuid4().hex[:8]}",
            tty=tty,
        )
        # Tracked from before the start is sent until the process is known to
        # run (registered below) or known to be gone.
        self._pty_unconfirmed[entry.dir] = entry
        try:
            started = await self._run(
                ["bash", "-c", PTY_START, "bash", entry.dir, shlex.join(argv), str(int(tty))],
                timeout=self.state.timeouts.fast_op_s,
                error_command=command,
            )
        except BaseException as exc:
            self._reserved_pty_process_ids.discard(process_id)
            if ran_for_sure_not(exc):
                self._pty_unconfirmed.pop(entry.dir, None)
            else:
                # The answer may be lost after the process started: end it.
                await self._finish(self._terminate_pty_processes([entry]))
            raise
        if not started.ok():
            self._reserved_pty_process_ids.discard(process_id)
            await self._finish(self._terminate_pty_processes([entry]))
            raise ExecTransportError(
                command=command,
                context={
                    "backend": "prized",
                    "reason": "pty_start_failed",
                    "exit_code": started.exit_code,
                    "stderr": started.stderr.decode("utf-8", "replace"),
                },
                message="Prized could not start the process",
                retryable=False,
            )
        async with self._pty_lock:
            pruned = self._prune_pty_processes_if_needed()
            self._pty_processes[process_id] = entry
            self._pty_unconfirmed.pop(entry.dir, None)
            count = len(self._pty_processes)
        if pruned is not None:
            await self._terminate_pty_processes([pruned])
        if count >= PTY_PROCESSES_WARNING:
            logger.warning("PTY process count reached warning threshold: %s active sessions", count)
        yield_time_ms = 10_000 if yield_time_s is None else int(yield_time_s * 1000)
        return await self._collect_pty_update(
            process_id,
            entry,
            yield_time_ms=clamp_pty_yield_time_ms(yield_time_ms),
            max_output_tokens=max_output_tokens,
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        """Send input to a running process, then collect its output.

        Returns:
            The output since the last collection, and the process id while the
            process runs, or its exit code once it has exited.

        Raises:
            RuntimeError: If `chars` is not empty and the process has no TTY.
            ExecTransportError: If the input could not all be delivered to a
                process that is still running.
        """
        async with self._pty_lock:
            entry = self._resolve_pty_session_entry(
                pty_processes=self._pty_processes, session_id=session_id
            )
        if chars:
            if not entry.tty:
                msg = "stdin is not available for this process"
                raise RuntimeError(msg)
            written = await self._run(
                ["sh", "-c", PTY_STDIN, "sh", entry.dir],
                timeout=self.state.timeouts.fast_op_s,
                cwd=PRIZED_HOME,
                extra_env={PTY_STDIN_ENV: chars},
            )
            if not written.ok():
                await self._poll_pty(entry, wait_s=0)
                if not entry.output_closed.is_set():
                    raise ExecTransportError(
                        command=("pty_write_stdin", str(session_id)),
                        context={
                            "backend": "prized",
                            "reason": "stdin_not_delivered",
                            "exit_code": written.exit_code,
                            "stderr": written.stderr.decode("utf-8", "replace"),
                        },
                        message=(
                            "Prized could not deliver all of the input to the process "
                            "(it is not reading it); part of it may have arrived"
                        ),
                    )
                # The process has exited, so nothing reads input any more: its
                # update below says so.
        yield_time_ms = 250 if yield_time_s is None else int(yield_time_s * 1000)
        update = await self._collect_pty_update(
            session_id,
            entry,
            yield_time_ms=resolve_pty_write_yield_time_ms(
                yield_time_ms=yield_time_ms, input_empty=chars == ""
            ),
            max_output_tokens=max_output_tokens,
        )
        entry.last_used = time.monotonic()
        return update

    async def pty_terminate_all(self) -> None:
        """Kill every process this session started, or may have started, and remove their files."""
        async with self._pty_lock:
            entries = list(self._pty_processes.values())
            self._pty_processes.clear()
            self._reserved_pty_process_ids.clear()
        entries.extend(e for e in self._pty_unconfirmed.values() if e not in entries)
        await self._terminate_pty_processes(entries)

    async def _collect_pty_update(
        self,
        process_id: int,
        entry: PtyProcess,
        *,
        yield_time_ms: int,
        max_output_tokens: int | None,
    ) -> PtyExecUpdate:
        async def poll_output(deadline: float) -> None:
            wait_s = max(0.0, deadline - time.monotonic() - _PTY_POLL_MARGIN_S)
            await self._poll_pty(entry, wait_s=wait_s)

        async def wait_for_output(remaining_s: float) -> None:
            await asyncio.sleep(min(0.05, remaining_s))

        async def settle_output() -> None:
            # The last poll ran up to the deadline; what came after it is
            # collected by the next call.
            return

        output, original_token_count, closed = await collect_pty_output(
            output_chunks=entry.output_chunks,
            output_lock=entry.output_lock,
            output_notify=entry.output_notify,
            is_done=entry.output_closed.is_set,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
            poll_output=poll_output,
            settle_output=settle_output,
            wait_for_output=wait_for_output,
        )
        if not closed:
            return PtyExecUpdate(
                process_id=process_id,
                output=output,
                exit_code=None,
                original_token_count=original_token_count,
            )
        async with self._pty_lock:
            self._pty_processes.pop(process_id, None)
            self._reserved_pty_process_ids.discard(process_id)
        await self._run_quietly(["rm", "-rf", "--", entry.dir])
        return PtyExecUpdate(
            process_id=None,
            output=output,
            exit_code=entry.exit_code,
            original_token_count=original_token_count,
        )

    async def _poll_pty(self, entry: PtyProcess, *, wait_s: float) -> None:
        """Wait in the sandbox until the process exits or `wait_s` passes; take its new output."""
        async with entry.poll_lock:
            if entry.output_closed.is_set():
                return
            result = await self._run(
                [
                    "bash",
                    "-c",
                    PTY_POLL,
                    "bash",
                    entry.dir,
                    str(entry.offset),
                    str(int(wait_s * 1000)),
                    str(_PTY_POLL_MAX_BYTES),
                ],
                timeout=wait_s + self.state.timeouts.fast_op_s,
                cwd=PRIZED_HOME,
            )
            header, _, body = result.stdout.partition(b"\n")
            fields = header.decode("ascii", "replace").split()
            if not result.ok() or len(fields) != 3:  # noqa: PLR2004
                raise ExecTransportError(
                    command=("pty_poll", entry.dir),
                    context={
                        "backend": "prized",
                        "reason": "pty_poll_failed",
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.decode("utf-8", "replace"),
                    },
                    message="Prized could not read the process's output",
                )
            done, code, offset = fields
            chunk = base64.b64decode(body.strip()) if body.strip() else b""
            async with entry.output_lock:
                if chunk:
                    entry.output_chunks.append(chunk)
                entry.offset = int(offset)
            if chunk:
                entry.output_notify.set()
            if done == "1":
                entry.exit_code = int(code) if code.lstrip("-").isdigit() else None
                entry.output_closed.set()
                entry.output_notify.set()

    def _prune_pty_processes_if_needed(self) -> PtyProcess | None:
        if len(self._pty_processes) < PTY_PROCESSES_MAX:
            return None
        meta = [
            (pid, entry.last_used, entry.output_closed.is_set())
            for pid, entry in self._pty_processes.items()
        ]
        pid = process_id_to_prune_from_meta(meta)
        if pid is None:
            return None
        self._reserved_pty_process_ids.discard(pid)
        return self._pty_processes.pop(pid, None)

    async def _terminate_pty_processes(self, entries: list[PtyProcess]) -> None:
        """Kill processes; one whose kill is not confirmed stays for `pty_terminate_all`."""
        if not entries:
            return
        for entry in entries:
            self._pty_unconfirmed[entry.dir] = entry
        killed = await self._run_quietly(
            ["bash", "-c", PTY_KILL, "bash", *(entry.dir for entry in entries)]
        )
        if not killed:
            logger.warning(
                "Could not confirm that %s Prized background process(es) ended; "
                "pty_terminate_all tries again",
                len(entries),
            )
            return
        for entry in entries:
            self._pty_unconfirmed.pop(entry.dir, None)
