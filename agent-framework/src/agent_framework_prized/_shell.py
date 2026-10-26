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

"""A Microsoft Agent Framework shell tool backed by a Prized sandbox (a Firecracker microVM)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import posixpath
import shlex
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

import prized
from agent_framework import FunctionTool, tool
from agent_framework_tools.shell import (
    ShellCommandError,
    ShellEnvironmentProviderOptions,
    ShellEnvironmentSnapshot,
    ShellExecutionError,
    ShellFamily,
    ShellMode,
    ShellPolicy,
    ShellRequest,
    ShellResult,
    default_instructions_formatter,
)
from prized import CommandTimeoutError, PrizedError

try:  # the structured result the framework's own shell tools give the model (since 1.0.0b261002)
    from agent_framework_tools.shell._types import _parse_shell_result, _shell_result_to_text
except ImportError:  # pragma: no cover - older agent-framework-tools
    _parse_shell_result = None  # type: ignore[assignment]

    def _shell_result_to_text(result: ShellResult) -> str:  # type: ignore[misc]
        return result.format_for_model()


if TYPE_CHECKING:
    from prized import AsyncSandbox
    from prized import ExecResult as PrizedExecResult

logger = logging.getLogger(__name__)

OnClose = Literal["delete", "pause", "keep"]
OutputCallback = Callable[[str, str], "Awaitable[None] | None"]

_DEFAULT_TEMPLATE = "prized-base-v1"
_DEFAULT_WORKING_DIR = "/home/prized/work"
_STATE_DIR = "/home/prized/.prized/maf"
_STATE_FILE = f"{_STATE_DIR}/shell-state.sh"
_LOCAL_BIN = "/home/prized/.local/bin"
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_OUTPUT = 64 * 1024
_DEFAULT_AUTO_PAUSE_MIN = 10
# A sandbox this tool creates and deletes on close is deleted this long after it
# pauses, in case nothing closes it.
_DEFAULT_AUTO_DELETE_MIN = 60
_GONE_STATES = frozenset({"deleting", "deleted", "error"})
_MAYBE_GONE_CODES = frozenset({"not_found", "sandbox_not_running", "sandbox_deleted"})
_LABELS = {"agent-framework": "shell"}
# The templates' Python is "externally managed": without this the model's `pip install`
# fails. With it, pip installs for the user (no root needed) into ~/.local, whose bin
# directory every command also gets on its PATH.
_PIP_ENV = {"PIP_BREAK_SYSTEM_PACKAGES": "1"}
_OS_DESCRIPTION = "Linux (a Prized sandbox: an Ubuntu 24.04 microVM)"
_CAPPED_NOTE = (
    "\n[prized: the sandbox kept only the first 4 MiB of this command's output; redirect large output "
    "to a file and read its tail]\n"
)
_REPLACED_NOTE = (
    "[prized: the previous sandbox was gone (its run time ended or it was deleted after being "
    "paused), so this ran in a fresh, empty sandbox: earlier files, installs and shell state are not there]\n"
)
_UNSET: Any = object()

# Linux caps one argument at 128 KiB, and `bash -c` gets the whole script as one. A
# command the SDK runs detached (streamed, no timeout, or over 4 minutes) is quoted
# into its start script twice, so a script whose quoted form could pass the cap is
# staged in a file first.
_ARG_MAX = 128 * 1024
_STAGE_MARGIN = 16 * 1024
# Per-process variables the sandbox's command server sets, and bash's own, never kept.
_VOLATILE = (
    "PWD|SHLVL|_|PIPESTATUS|FUNCNAME|INVOCATION_ID|JOURNAL_STREAM|SYSTEMD_EXEC_PID|RUNTIME_DIRECTORY|BASH_*|__prized_*"
)
# Sandboxes from some template builds stop answering every call
# once their default directory, /home/prized/work, is gone, so a command that removed
# it puts it back as it ends.
_REPAIR = "[[ -d /home/prized/work ]] || /bin/mkdir -p -- /home/prized/work"
# Persistent mode, as in one long-lived bash. Before a command: note the environment
# the command server gave this shell, restore the saved session (shell options,
# directory, variables, functions), and set an EXIT trap. The command runs through
# `eval` on the script's first line, so its line numbers are its own and a heredoc it
# leaves open can't swallow what follows. After it (at its end, or at `exit`, unless a
# timeout's TERM ended it): save what the commands changed. An exported variable is
# saved only if a command set or changed it, so `env=` stays in charge of the rest and
# its values are never written to disk. The state lives in the sandbox: it survives
# pause, resume and forks.
_SAVE_FUNCTION = (
    "__prized_save() { "
    f"__prized_saved=1; {{ builtin set +xv; {_REPAIR}; }} 2>/dev/null; "
    "local __prized_rc=$1 __prized_n __prized_e; "
    f"[[ -d {_STATE_DIR} ]] || /bin/mkdir -p -- {_STATE_DIR} 2>/dev/null; "
    '__prized_e=" $(builtin compgen -e) "; '
    "{ builtin shopt -p; builtin shopt -u nocasematch; "
    "builtin printf 'builtin cd -- %q\\n' \"$PWD\"; "
    'for __prized_n in "${!__prized_start[@]}"; do '
    f"case $__prized_n in {_VOLATILE}) continue ;; esac; "
    "[[ -v $__prized_n ]] || builtin printf 'builtin unset -v %q\\n' \"$__prized_n\"; done; "
    "while IFS= builtin read -r __prized_n; do "
    f"case $__prized_n in {_VOLATILE}) continue ;; esac; "
    'if [[ $__prized_e == *[[:space:]]"$__prized_n"[[:space:]]* ]]; then '
    '[[ -n ${__prized_start[$__prized_n]+set} && ${__prized_start[$__prized_n]} == "${!__prized_n}" ]] '
    '|| builtin declare -p -- "$__prized_n"; '
    'elif [[ $__prized_base != *[[:space:]]"$__prized_n"[[:space:]]* ]]; then builtin declare -p -- "$__prized_n"; '
    "fi; done < <(builtin compgen -v); "
    "while IFS= builtin read -r __prized_n; do "
    '[[ $__prized_n == __prized_* ]] || builtin declare -f -- "$__prized_n"; '
    "done < <(builtin compgen -A function); "
    "while IFS=' ' builtin read -r _ _ __prized_n; do "
    "builtin printf 'builtin export -f %q\\n' \"$__prized_n\"; done < <(builtin declare -F -x); "
    f'}} > "{_STATE_FILE}.$$" 2>/dev/null && /bin/mv -f -- "{_STATE_FILE}.$$" {_STATE_FILE}; '
    'builtin exit "$__prized_rc"; }'
)
# At `exit`: save, unless the command's end already did, or `exit` ran inside one of
# the command's functions (its locals are in scope then and must not be saved).
_EXIT_TRAP = (
    "{ __prized_rc=$?; builtin set +xv; } 2>/dev/null; "
    '[[ -n ${__prized_saved-} || -n ${FUNCNAME[0]-} ]] || __prized_save "$__prized_rc"'
)
# A timeout's TERM ends the command without saving, as the kill of a blocking one does.
_TERM_TRAP = f"{{ builtin set +xv; {_REPAIR}; }} 2>/dev/null; __prized_saved=1; builtin exit 143"


def _enter(workdir: str) -> str:
    """Start in the working directory, made again if a command removed it."""
    q = shlex.quote(workdir)
    return (
        f"{{ builtin cd -- {q} || {{ /bin/mkdir -p -- {q} && builtin cd -- {q}; }}; }} 2>/dev/null "
        f"|| {{ builtin printf 'prized: cannot enter the working directory %s\\n' {q} >&2; builtin exit 1; }}"
    )


# pip-installed commands run: ~/.local/bin is on PATH (kept first if it was missing).
_PATH_LINE = f'case ":$PATH:" in *":{_LOCAL_BIN}:"*) ;; *) builtin export PATH="{_LOCAL_BIN}:$PATH" ;; esac'


def _bash_quote(text: str) -> str:
    """``text`` as one bash word on one line ($'...')."""
    out = []
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "'":
            out.append("\\'")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "$'" + "".join(out) + "'"


def _persistent_script(workdir: str, load: str) -> str:
    """``load`` puts the command's text in ``$__prized_c``."""
    return (
        f"{_PATH_LINE}; "
        "declare -A __prized_start=(); "
        'while IFS= builtin read -r __prized_n; do __prized_start[$__prized_n]="${!__prized_n}"; '
        "done < <(builtin compgen -e); "
        '__prized_base=" $(builtin compgen -v) "; '
        f"{_enter(workdir)}; "
        f"[[ -r {_STATE_FILE} ]] && builtin . {_STATE_FILE} >/dev/null 2>&1; "
        f"{_PATH_LINE}; "
        f"{_SAVE_FUNCTION}; "
        f"builtin trap {shlex.quote(_EXIT_TRAP)} EXIT; builtin trap {shlex.quote(_TERM_TRAP)} TERM; "
        f'{load}; builtin eval -- "$__prized_c"; '
        '{ __prized_rc=$?; builtin set +xv; } 2>/dev/null; __prized_save "$__prized_rc"\n'
    )


def _stateless_script(workdir: str, load: str) -> str:
    repair = f"{{ {_REPAIR}; }} 2>/dev/null"
    return (
        f"{_enter(workdir)}; {_PATH_LINE}; builtin trap {shlex.quote(repair)} EXIT; "
        f'{load}; builtin eval -- "$__prized_c"; '
        f'{{ __prized_rc=$?; builtin set +xv; {_REPAIR}; }} 2>/dev/null; builtin exit "$__prized_rc"\n'
    )


_PERSISTENT_DESCRIPTION = (
    "Run a bash command in an isolated Linux sandbox (a Prized microVM: Ubuntu 24.04, Python 3.12 as "
    "python3, Node 22, git, internet access; user prized, no sudo; pip install works without root) and "
    "return its stdout, stderr and exit code. Commands run in a persistent session: `cd`, "
    "variables and shell functions carry over to the next call. Each command starts in {workdir} the "
    "first time. {limit} To leave a process running, start it with `nohup CMD > out.log 2>&1 &`."
)
_STATELESS_DESCRIPTION = (
    "Run a bash command in an isolated Linux sandbox (a Prized microVM: Ubuntu 24.04, Python 3.12 as "
    "python3, Node 22, git, internet access; user prized, no sudo; pip install works without root) and "
    "return its stdout, stderr and exit code. Each command runs in a fresh shell in {workdir}: `cd` and "
    "variables do not carry over, files do. {limit} To leave a process running, start it with "
    "`nohup CMD > out.log 2>&1 &`."
)


class PrizedShellTool:
    """Run an Agent Framework agent's shell commands in a Prized sandbox.

    A Prized sandbox is a Firecracker microVM (Ubuntu 24.04 with Python 3.12, Node 22 and
    git) that pauses with its memory and running processes, resumes where it stopped,
    and forks into live copies. It is the isolation boundary:
    commands never run on your machine.

    ``PrizedShellTool`` has the public surface of the framework's ``DockerShellTool``
    (``start``, ``close``, ``run``, ``as_function``, ``async with``) and satisfies its
    ``ShellExecutor`` protocol, so it also works as ``create_harness_agent(shell_executor=...)``
    and with ``ShellEnvironmentProvider``.

    The sandbox is created on first use (or ``await start()``), or attached by
    ``sandbox_id``. ``await close()`` deletes a sandbox this tool created and keeps an
    attached one; ``on_close`` changes that. A closed tool starts again on its next
    command: in the same sandbox if it was paused or kept, otherwise in a new one. An idle
    sandbox pauses itself after ``auto_pause_min`` minutes and the next command resumes it,
    files, installs and processes intact.

    Example::

        from agent_framework import Agent
        from agent_framework_prized import PrizedShellTool

        async with PrizedShellTool(approval_mode="never_require") as shell:
            agent = Agent(client=client, tools=[shell.as_function()])
            print(await agent.run("Clone https://github.com/un33k/python-slugify and run its tests."))
    """

    def __init__(
        self,
        *,
        sandbox_id: str | None = None,
        template: str | None = None,
        vcpu: int | None = None,
        memory_gb: float | None = None,
        disk_gb: int | None = None,
        labels: Mapping[str, str] | None = None,
        mode: ShellMode = "persistent",
        working_dir: str = _DEFAULT_WORKING_DIR,
        env: Mapping[str, str] | None = None,
        timeout: float | None = _DEFAULT_TIMEOUT,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT,
        policy: ShellPolicy | None = None,
        approval_mode: Literal["always_require", "never_require"] = "always_require",
        on_command: Callable[[str], None] | None = None,
        on_output: OutputCallback | None = None,
        auto_pause_min: int | None = _UNSET,
        auto_delete_min: int | None = _UNSET,
        max_runtime_min: int | None = _UNSET,
        on_close: OnClose | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Configure the tool. Nothing is created until first use.

        Args:
            sandbox_id: An existing sandbox (``sbx_...``) to use instead of creating one.
            template: ``prized-base-v1`` (the default) or ``prized-agent-v1`` (adds Claude
                Code and Codex).
            vcpu: vCPUs for a new sandbox. Defaults to the template's.
            memory_gb: Memory for a new sandbox, in GB. Defaults to the template's.
            disk_gb: Disk for a new sandbox, in GB. Defaults to the template's.
            labels: Labels for a new sandbox, to find it later.
            mode: ``"persistent"`` (the default): ``cd``, exported variables and shell
                functions carry over between commands, as in one long-lived shell.
                ``"stateless"``: every command starts fresh in ``working_dir``. Files
                persist either way.
            working_dir: Absolute directory where commands start. Created if missing.
                Defaults to ``/home/prized/work``.
            env: Environment variables for every command. Code in the sandbox, the
                model's included, can read them: put there only what that code needs.
            timeout: Seconds before a command is killed; it then returns what it printed
                with ``timed_out=True``. ``None`` means no limit. Defaults to 120.
            max_output_bytes: Bytes of stdout, and of stderr, kept per command (the start
                and the end; the middle is cut). Defaults to 64 KiB.
            policy: A ``ShellPolicy`` checked before each command. A UX pre-filter, not a
                security boundary.
            approval_mode: ``"always_require"`` (the default) makes the function from
                :meth:`as_function` ask for approval before each command;
                ``"never_require"`` runs them unattended, the sandbox being the boundary.
            on_command: Called with each command that passes the policy (audit logging).
            on_output: Called as ``on_output(stream, text)`` (``stream`` is ``"stdout"``
                or ``"stderr"``) while a command prints, to show progress live. May be a
                coroutine function.
            auto_pause_min: Idle minutes before a new sandbox pauses itself (no compute is
                charged while paused). ``0`` turns it off. Defaults to 10.
            auto_delete_min: Minutes after pausing before a new sandbox is deleted. Defaults
                to 60 when ``close()`` would delete it, so a sandbox nobody closes goes
                away, and to never otherwise. ``0`` turns it off.
            max_runtime_min: Minutes of running before a new sandbox is deleted. Defaults to
                the workspace's (24 hours on a paid workspace, 60 on the free tier).
            on_close: What ``close()`` does: ``"delete"``, ``"pause"`` or ``"keep"``.
                Defaults to ``"delete"`` for a sandbox this tool created and ``"keep"`` for
                one it attached to.
            api_key: Prized API key. Defaults to ``PRIZED_API_KEY``.
            base_url: Prized API URL. Defaults to ``https://api.prized.dev``.

        Raises:
            ValueError: If the options are invalid or contradict each other.
        """
        create_options = (template, vcpu, memory_gb, disk_gb, labels)
        lifetimes = (auto_pause_min, auto_delete_min, max_runtime_min)
        if sandbox_id is not None and (
            any(o is not None for o in create_options) or any(o is not _UNSET for o in lifetimes)
        ):
            raise ValueError(
                "template, vcpu, memory_gb, disk_gb, labels, auto_pause_min, auto_delete_min and "
                "max_runtime_min apply to a new sandbox; they cannot be used with sandbox_id"
            )
        if sandbox_id is not None and (not isinstance(sandbox_id, str) or not sandbox_id):
            raise ValueError("sandbox_id must be a non-empty string")
        if mode not in ("persistent", "stateless"):
            raise ValueError(f"mode must be 'persistent' or 'stateless', got {mode!r}")
        if approval_mode not in ("always_require", "never_require"):
            raise ValueError("approval_mode must be 'always_require' or 'never_require'")
        if on_close not in (None, "delete", "pause", "keep"):
            raise ValueError("on_close must be 'delete', 'pause' or 'keep'")
        if not isinstance(working_dir, str) or not posixpath.isabs(working_dir):
            raise ValueError("working_dir must be an absolute path")
        if timeout is not None and not timeout > 0:
            raise ValueError("timeout must be a positive number of seconds, or None for no limit")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        if auto_pause_min is _UNSET:
            auto_pause_min = _DEFAULT_AUTO_PAUSE_MIN
        if auto_delete_min is _UNSET:
            auto_delete_min = _DEFAULT_AUTO_DELETE_MIN if on_close in (None, "delete") else None
        if max_runtime_min is _UNSET:
            max_runtime_min = None

        self.working_dir = posixpath.normpath(working_dir)
        self._mode: ShellMode = mode
        self._env = {**_PIP_ENV, **dict(env or {})}
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._policy = policy or ShellPolicy()
        self._approval_mode: Literal["always_require", "never_require"] = approval_mode
        self._on_command = on_command
        self._on_output = on_output
        self._template = template or _DEFAULT_TEMPLATE
        self._create_kwargs: dict[str, Any] = {
            "vcpu": vcpu,
            "memory_gb": memory_gb,
            "disk_gb": disk_gb,
            "env_vars": dict(_PIP_ENV),
            "labels": {**_LABELS, **dict(labels or {})},
            "auto_pause_min": auto_pause_min,
            "auto_delete_min": auto_delete_min,
            "max_runtime_min": max_runtime_min,
        }
        self._on_close = on_close
        self._api_key = api_key
        self._base_url = base_url
        # Which sandbox is in use is shared by every thread and event loop, under
        # `_state`. Each loop keeps its own handle (the SDK's connections belong to the
        # loop that opened them), valid for one generation: a start, a replacement or a
        # close starts the next.
        self._state = threading.Lock()
        self._current_id: str | None = None  # None: not started, or closed
        self._sandbox_id: str | None = sandbox_id
        self._owned = False
        # What the next start attaches to: (id, owned), or None to create a sandbox.
        self._resume: tuple[str, bool] | None = (sandbox_id, False) if sandbox_id is not None else None
        self._generation = 0
        self._handles: dict[asyncio.AbstractEventLoop, tuple[int, AsyncSandbox]] = {}
        self._last: AsyncSandbox | None = None
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._session_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._replaced = False

    # ---- lifecycle ----

    @property
    def sandbox_id(self) -> str | None:
        """The sandbox's id (``sbx_...``) once started, also after close.

        Pass it to ``PrizedShellTool(sandbox_id=...)`` to continue in the same sandbox
        later (close with ``on_close="pause"`` or ``"keep"``).
        """
        return self._sandbox_id

    @property
    def sandbox(self) -> AsyncSandbox:
        """The started ``prized.AsyncSandbox``, for anything else the Prized SDK offers.
        Use it from the event loop that last ran a command on this tool."""
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
        raise RuntimeError("No handle yet: run a command on this event loop first.")

    async def start(self) -> None:
        """Create (or attach to) the sandbox now instead of on first use. A no-op if
        it is started."""
        await self._ensure()

    async def close(self) -> None:
        """Delete, pause or keep the sandbox, per ``on_close``. Idempotent; a failed
        delete or pause raises and can be retried. The tool starts again on its next
        command."""
        for _ in range(5):
            with self._state:
                sandbox_id, owned = self._current_id, self._owned
                if sandbox_id is None:
                    self._generation += 1  # a start still in flight is discarded, not used
                    return
            action = self._on_close or ("delete" if owned else "keep")
            try:
                sandbox = await self._acquire(replace=False)
            except RuntimeError:
                with self._state:
                    if self._current_id is None:
                        return  # closed by another thread meanwhile
                raise
            if sandbox is not None and sandbox.id != sandbox_id:
                continue  # replaced meanwhile: close the new one
            cancelled = False
            if sandbox is not None:
                try:
                    if action == "delete":
                        await _despite_cancel(sandbox.delete())
                    elif action == "pause":
                        await _despite_cancel(sandbox.pause(wait=False))
                except asyncio.CancelledError:
                    cancelled = True  # it finished: record that, then pass the cancellation on
                    sandbox_gone = action == "delete"
                except PrizedError as e:
                    if e.code != "not_found":
                        raise  # nothing is released, so the close can be retried
                    sandbox_gone = True
                else:
                    sandbox_gone = action == "delete"
            else:
                sandbox_gone = True
            with self._state:
                if self._current_id != sandbox_id:
                    retry = True  # replaced while closing: close the new one too
                else:
                    retry = False
                    self._current_id = None
                    self._generation += 1  # a call still in flight can't replace it now
                    others = [(lp, h) for lp, (_, h) in self._handles.items() if h is not sandbox]
                    self._handles = {}
                    self._resume = None if sandbox_gone else (sandbox_id, owned)
            if not retry:
                _release(others)
            if sandbox is not None:
                await _close_quietly(sandbox)
            if cancelled:
                raise asyncio.CancelledError
            if not retry:
                return
        raise RuntimeError("The sandbox kept changing while closing; try again.")

    async def __aenter__(self) -> PrizedShellTool:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def pause(self) -> None:
        """Pause now: memory, disk and processes are kept, no compute is charged. The
        next command resumes it."""
        await (await self._ensure()).pause()

    async def fork(self, count: int = 1, *, on_close: OnClose = "delete") -> list[PrizedShellTool]:
        """Fork into ``count`` live copies with this sandbox's files, memory, running
        processes and shell state, each in a started tool with this one's settings.
        Closing a copy deletes it unless ``on_close`` says otherwise."""
        if on_close not in ("delete", "pause", "keep"):
            raise ValueError("on_close must be 'delete', 'pause' or 'keep'")
        copies = await (await self._ensure()).fork(count)
        result = []
        for copy in copies:
            twin = PrizedShellTool(
                sandbox_id=copy.id,
                mode=self._mode,
                working_dir=self.working_dir,
                timeout=self._timeout,
                max_output_bytes=self._max_output_bytes,
                policy=self._policy,
                approval_mode=self._approval_mode,
                on_command=self._on_command,
                on_output=self._on_output,
                on_close=on_close,
                api_key=self._api_key,
                base_url=self._base_url,
            )
            # A copy that is ever replaced gets the parent's shape and labels.
            twin._env = dict(self._env)
            twin._template, twin._create_kwargs = self._template, dict(self._create_kwargs)
            twin._publish(asyncio.get_running_loop(), 0, copy, owned=True)
            result.append(twin)
        return result

    # ---- the framework's surface ----

    async def run(
        self, command: str, *, timeout: float | None = None, on_output: OutputCallback | None = None
    ) -> ShellResult:
        """Run ``command`` in the sandbox and return its :class:`ShellResult`.

        Applies the policy and the ``on_command`` hook, but not approval (the framework
        asks for that when the model calls the function from :meth:`as_function`). A
        command that runs past its timeout is killed (with every process it started) and
        returns what it printed with ``timed_out=True``. A non-zero exit is a result, not
        an exception.

        Args:
            command: The bash command.
            timeout: Seconds for this command, instead of the tool's ``timeout``.
            on_output: Called as output arrives, instead of the tool's ``on_output``.

        Raises:
            ShellCommandError: The policy refused the command.
            ShellExecutionError: The sandbox could not run it (an API error, or an
                attached sandbox that is gone).
        """
        decision = self._policy.evaluate(ShellRequest(command=command, workdir=self.working_dir))
        if decision.decision == "deny":
            raise ShellCommandError(f"Command rejected by policy: {decision.reason}")
        if self._on_command is not None:
            try:
                self._on_command(command)
            except Exception:
                logger.exception("on_command hook raised")
        limit = self._timeout if timeout is None else timeout
        if limit is not None and not limit > 0:
            raise ValueError("timeout must be a positive number of seconds")
        callback = on_output or self._on_output
        started = time.monotonic()
        try:
            if self._mode == "persistent":
                # One shell session: its commands take turns, as in a single bash.
                async with self._session_lock(asyncio.get_running_loop()):
                    result, timed_out = await self._exec(command, limit, callback)
            else:
                result, timed_out = await self._exec(command, limit, callback)
        except ShellExecutionError:
            raise
        except (PrizedError, RuntimeError) as e:
            raise ShellExecutionError(f"The Prized sandbox could not run the command: {e}") from e
        elapsed = int((time.monotonic() - started) * 1000)
        return self._to_result(result, timed_out, result.duration_ms or elapsed)

    def as_function(self, *, name: str = "run_shell", description: str | None = None) -> FunctionTool:
        """A :class:`~agent_framework.FunctionTool` bound to this tool, for an agent's
        ``tools=[...]`` or a client's ``get_shell_tool(func=...)``.

        Args:
            name: The function name the model sees. Defaults to ``run_shell``. With
                approval on, use a name no auto-approval rule matches.
            description: The description the model sees. Defaults to one that says where
                commands run and how the session behaves.
        """

        async def _run_shell(command: str) -> str:
            try:
                result = await self.run(command)
            except ShellCommandError as exc:
                return str(exc)
            return _shell_result_to_text(result)

        text = description or self._default_description()
        _run_shell.__doc__ = text
        extra: dict[str, Any] = {} if _parse_shell_result is None else {"result_parser": _parse_shell_result}
        return tool(
            func=_run_shell,
            name=name,
            description=text,
            approval_mode=self._approval_mode,
            kind="shell",
            **extra,
        )

    def environment_options(
        self,
        *,
        probe_tools: tuple[str, ...] = ("git", "node", "python3", "pip"),
        probe_timeout: float = 15.0,
        instructions_formatter: Callable[[ShellEnvironmentSnapshot], str] | None = None,
    ) -> ShellEnvironmentProviderOptions:
        """Options for a ``ShellEnvironmentProvider`` (or ``create_harness_agent``'s
        ``shell_environment_provider_options``) that describe the sandbox, not this
        machine: bash on Linux even when your app runs on Windows or macOS."""
        formatter = instructions_formatter or default_instructions_formatter

        def describe(snapshot: ShellEnvironmentSnapshot) -> str:
            return formatter(dataclasses.replace(snapshot, os_description=_OS_DESCRIPTION))

        return ShellEnvironmentProviderOptions(
            probe_tools=probe_tools,
            override_family=ShellFamily.POSIX,
            probe_timeout=probe_timeout,
            instructions_formatter=describe,
        )

    # ---- internals: commands ----

    def _default_description(self) -> str:
        if self._timeout is None:
            limit = "Commands have no time limit."
        else:
            limit = f"A command is stopped after {self._timeout:g} seconds."
        template = _PERSISTENT_DESCRIPTION if self._mode == "persistent" else _STATELESS_DESCRIPTION
        return template.format(workdir=self.working_dir, limit=limit)

    async def _exec(
        self, command: str, limit: float | None, callback: OutputCallback | None
    ) -> tuple[PrizedExecResult, bool]:
        """Runs the command once, or again on a replacement if the sandbox was gone.
        Returns the result and whether the timeout killed it."""
        streams: dict[str, Any] = {}
        if callback is not None:
            streams = {"on_stdout": _guarded(callback, "stdout"), "on_stderr": _guarded(callback, "stderr")}
        sandbox = await self._ensure()

        async def once(target: AsyncSandbox) -> PrizedExecResult:
            script, staged = await self._script(target, command)
            try:
                result: PrizedExecResult = await target.exec(
                    script, cwd="/", env=self._env, timeout=0 if limit is None else limit, **streams
                )
            except Exception:
                if staged is not None:  # the script removes it when it runs
                    with contextlib.suppress(Exception):
                        await target.files.remove(staged)
                raise
            return result

        try:
            try:
                return await once(sandbox), False
            except PrizedError as e:
                if isinstance(e, CommandTimeoutError):
                    raise
                return await once(await self._replace_if_gone(sandbox, e)), False
        except CommandTimeoutError as e:
            return e.result, True

    async def _script(self, sandbox: AsyncSandbox, command: str) -> tuple[str, str | None]:
        """The bash script that runs ``command`` in this tool's mode, and the file it
        was staged in when it could be too long for one argument."""
        build = _persistent_script if self._mode == "persistent" else _stateless_script
        script = build(self.working_dir, f"__prized_c={_bash_quote(command)}")
        if 2 * len(shlex.quote(script).encode("utf-8")) + _STAGE_MARGIN <= _ARG_MAX:
            return script, None
        staged = f"{_STATE_DIR}/cmd-{uuid.uuid4().hex}.sh"
        await sandbox.files.write(staged, command.encode("utf-8"))
        # Read into the shell and removed before it runs, so nothing is left behind.
        return build(self.working_dir, f"__prized_c=$(< {staged}); /bin/rm -f -- {staged}"), staged

    def _to_result(self, result: PrizedExecResult, timed_out: bool, duration_ms: int) -> ShellResult:
        stdout, cut_out = _head_tail(result.stdout, self._max_output_bytes)
        stderr, cut_err = _head_tail(result.stderr, self._max_output_bytes)
        with self._state:
            replaced, self._replaced = self._replaced, False
        if replaced:
            stderr = _REPLACED_NOTE + stderr
        if result.truncated:
            stderr += _CAPPED_NOTE
        return ShellResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=-1 if timed_out else result.exit_code,
            duration_ms=duration_ms,
            truncated=cut_out or cut_err or result.truncated,
            timed_out=timed_out,
        )

    def _session_lock(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        with self._state:
            lock = self._session_locks.get(loop)
            if lock is None:
                lock = self._session_locks[loop] = asyncio.Lock()
            return lock

    # ---- internals: which sandbox ----

    def _lock_for(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        """Serializes the tasks of one event loop (threads meet in `_publish`)."""
        with self._state:
            for stale in [lp for lp in self._locks if lp.is_closed()]:
                del self._locks[stale]
                self._handles.pop(stale, None)
                self._session_locks.pop(stale, None)
            lock = self._locks.get(loop)
            if lock is None:
                lock = self._locks[loop] = asyncio.Lock()
            return lock

    async def _ensure(self) -> AsyncSandbox:
        """This loop's handle on the sandbox, starting (or replacing) it if needed."""
        loop = asyncio.get_running_loop()
        with self._state:
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
                if self._current_id is not None:
                    return
                generation, resume = self._generation, self._resume
            created = replaced = False
            if resume is not None:
                attached = await self._attach(resume[0], owned=resume[1])
                if attached is None:  # a sandbox this tool made, gone since: start over
                    sandbox, owned, created, replaced = await self._create(), True, True, True
                else:
                    sandbox, owned = attached, resume[1]
            else:
                sandbox, owned, created = await self._create(), True, True
            if not self._publish(loop, generation, sandbox, owned, replaced=replaced):
                # Another thread started it first, or it was closed meanwhile.
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
        loop = asyncio.get_running_loop()
        async with self._lock_for(loop):
            for _ in range(5):
                with self._state:
                    generation, sandbox_id, owned = self._generation, self._current_id, self._owned
                    cached = self._handles.get(loop)
                if sandbox_id is None:
                    # Closed meanwhile: a call in flight never starts a new sandbox.
                    raise RuntimeError("The sandbox was closed while this command was running.")
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
        try:
            return await prized.AsyncSandbox.connect(sandbox_id, api_key=self._api_key, base_url=self._base_url)
        except PrizedError as e:
            if e.code == "not_found":
                return None
            raise

    async def _create(self) -> AsyncSandbox:
        sandbox: AsyncSandbox = await prized.AsyncSandbox.create(
            self._template, api_key=self._api_key, base_url=self._base_url, **self._create_kwargs
        )
        return sandbox

    async def _attach(self, sandbox_id: str, *, owned: bool) -> AsyncSandbox | None:
        """A handle on an existing sandbox. One this tool made that is gone gives None;
        an attached one that is gone raises."""
        sandbox = await self._connect(sandbox_id)
        if sandbox is None or sandbox.state in _GONE_STATES:
            state = "gone" if sandbox is None else sandbox.state
            if sandbox is not None:
                if owned and sandbox.state == "error":
                    await _discard(sandbox)
                else:
                    await _close_quietly(sandbox)
            if owned:
                return None
            raise RuntimeError(f"Sandbox {sandbox_id} is {state}.")
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
            if self._generation != generation:
                return False
            self._generation += 1
            self._current_id = self._sandbox_id = sandbox.id
            self._owned = owned
            stale = [(lp, h) for lp, (_, h) in self._handles.items() if h is not sandbox]
            self._handles = {loop: (self._generation, sandbox)}
            self._last = sandbox
            if replaced:
                self._replaced = True
        _release(stale)
        return True

    async def _replace_if_gone(self, failed: AsyncSandbox, error: PrizedError) -> AsyncSandbox:
        """A sandbox to retry on after ``error`` on ``failed``, or ``error`` raised again.
        Only a sandbox this tool created is replaced, and only once it is gone."""
        if error.code not in _MAYBE_GONE_CODES:
            raise error
        sandbox = await self._acquire(failed=failed)
        if sandbox is failed or sandbox is None:
            raise error
        return sandbox


def _head_tail(text: str, cap: int) -> tuple[str, bool]:
    """``text`` cut to ``cap`` UTF-8 bytes: the first half and the last half, with a
    marker between, as the framework's own shell tools do."""
    data = text.encode("utf-8", "surrogatepass")
    if len(data) <= cap:
        return text, False
    head = data[: cap // 2].decode("utf-8", "replace")
    tail = data[len(data) - (cap - cap // 2) :].decode("utf-8", "replace")
    return f"{head}\n[... truncated {len(data) - cap} bytes ...]\n{tail}", True


_CLOSING: set[asyncio.Future[None]] = set()


def _release(handles: list[tuple[asyncio.AbstractEventLoop, AsyncSandbox]]) -> None:
    """Closes handles no longer in use, each on the event loop it belongs to (a loop
    that has stopped took its connections with it)."""
    try:
        here: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        here = None
    for loop, handle in handles:
        if loop.is_closed():
            continue
        if loop is here:
            task = loop.create_task(_close_quietly(handle))
            _CLOSING.add(task)
            task.add_done_callback(_CLOSING.discard)
        elif loop.is_running():
            asyncio.run_coroutine_threadsafe(_close_quietly(handle), loop)


async def _close_quietly(sandbox: AsyncSandbox) -> None:
    with contextlib.suppress(Exception):
        await sandbox.close()


async def _discard(sandbox: AsyncSandbox) -> None:
    """Deletes a sandbox this tool made and no longer uses, even if cancelled meanwhile."""

    async def go() -> None:
        with contextlib.suppress(Exception):
            await sandbox.delete()
        await _close_quietly(sandbox)

    await _despite_cancel(go())


async def _despite_cancel(work: Awaitable[None]) -> None:
    """Awaits ``work`` to its end even if the caller is cancelled meanwhile (then the
    cancellation is raised after it), so a delete is never cut short."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                break
            cancelled = True
    failure = task.exception()
    if failure is not None:
        raise failure
    if cancelled:
        raise asyncio.CancelledError


def _guarded(callback: OutputCallback, stream: str) -> Callable[[str], Awaitable[None]]:
    """An output callback whose errors are logged, not raised: a failing display must
    not kill the command."""

    async def deliver(text: str) -> None:
        try:
            outcome = callback(stream, text)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.exception("on_output callback raised")

    return deliver
