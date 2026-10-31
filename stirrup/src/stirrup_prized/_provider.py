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

"""Prized Sandboxes as a Stirrup code execution backend.

``PrizedCodeExecToolProvider`` is a drop-in for Stirrup's ``E2BCodeExecToolProvider``: the
same constructor shape, the same ``CodeExecToolProvider`` methods, the same error kinds in
``CommandResult``. Commands run with bash as the user ``prized`` in ``/home/prized/work``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import shlex
import uuid
from collections.abc import Awaitable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path, PurePosixPath
from typing import Any

from prized import AsyncSandbox, CommandTimeoutError, ExecResult, PrizedError
from pydantic import ValidationError
from stirrup.constants import SANDBOX_REQUEST_TIMEOUT, SANDBOX_TIMEOUT
from stirrup.core.models import ImageContentBlock, Tool, ToolUseCountMetadata
from stirrup.tools.code_backends.base import (
    SHELL_TIMEOUT,
    CodeExecToolProvider,
    CodeExecutionParams,
    CommandResult,
    OutputSourceRoot,
    UploadedFile,
    UploadFilesResult,
)

logger = logging.getLogger(__name__)

# The guest layout of Prized's native templates: user `prized`, this home, and the
# directory commands start in. Relative paths resolve against WORKDIR.
HOME = "/home/prized"
WORKDIR = "/home/prized/work"
DEFAULT_TEMPLATE = "prized-base-v1"
DOCKER_TEMPLATE = "prized-docker-v1"
DOCKER_MODE_DATE = "2026-11-09"

# This backend's own files in the sandbox: staged long commands and files moved in or
# out of paths the files API doesn't serve.
_STATE_DIR = f"{HOME}/.stirrup-prized"
_XFER_DIR = f"{_STATE_DIR}/xfer"
# This backend's own short commands (stat, realpath, find, cp).
_HELPER_TIMEOUT = 60
# A command longer than this goes up as a script file first: one argument to bash -c
# can't pass Linux's 128 KiB per-string limit, and quoting can grow it.
_STAGE_COMMAND_BYTES = 64 * 1024
# How much of a timed-out command's output goes into the error the model sees.
_TIMEOUT_TAIL_CHARS = 8_000
# Exit codes of the helper scripts below.
_EXIT_MISSING = 44
_EXIT_NOT_FILE = 45

# Every command first enters the working directory, made again if a command removed it,
# and puts it back as it ends: sandboxes from some template builds stop answering once
# /home/prized/work is gone.
_ENTER = (
    f"trap '[ -d {WORKDIR} ] || mkdir -p -- {WORKDIR} 2>/dev/null' EXIT\n"
    f"{{ cd -- {WORKDIR} || {{ mkdir -p -- {WORKDIR} && cd -- {WORKDIR}; }}; }} 2>/dev/null\n"
)

_NETWORK_OFF_MESSAGE = (
    "network=False (E2B's allow_internet_access=False) isn't supported yet: a Prized sandbox always has "
    "outbound internet. Docker mode (template 'prized-docker-v1', due {date}) will run commands in a "
    "container with no network. Until then, run offline tasks on a backend that can cut the network, "
    "rather than online by mistake."
).format(date=DOCKER_MODE_DATE)
_IMAGE_MESSAGE = (
    "image= needs Docker mode (template 'prized-docker-v1'), due {date}. Today a sandbox runs "
    "prized-base-v1 (Ubuntu 24.04, Python 3.12, Node 22, git) or prized-agent-v1 (adds Claude Code and "
    "Codex). Install what a task needs with commands, or prepare a sandbox once, pause it, and pass "
    "fork_from=<its id> so every run starts from a copy."
).format(date=DOCKER_MODE_DATE)
_DOCKER_TEMPLATE_MESSAGE = (
    "Template 'prized-docker-v1' (Docker mode) is due {date} and this version of stirrup-prized doesn't "
    "drive it yet. Use template 'prized-base-v1' or 'prized-agent-v1' until then."
).format(date=DOCKER_MODE_DATE)

# E2B's AsyncSandbox.create keywords and what they become here.
_E2B_RENAMES = {"envs": "env_vars", "metadata": "labels"}
# E2B keywords with no Prized meaning that change nothing about the run: dropped.
_E2B_IGNORED = frozenset({"secure", "timeout", "template", "request_timeout"})
# prized.AsyncSandbox.create keywords accepted as they are.
_CREATE_KEYS = frozenset({"env_vars", "labels", "auto_pause_min", "auto_delete_min", "memory_mb", "idempotency_key"})
# Connection options, for create and connect alike.
_CONNECTION_KEYS = frozenset({"api_key", "base_url", "edge_url", "http_client", "max_retries", "poll_outage"})
# A fork keeps its parent's template, shape and pause settings.
_NOT_FOR_FORKS = frozenset({"auto_pause_min", "auto_delete_min", "memory_mb"})
# Minutes after a sandbox pauses before it's deleted, unless sandbox_kwargs say otherwise:
# a run that crashed before __aexit__ doesn't leave a paused sandbox behind for good.
_DEFAULT_AUTO_DELETE_MIN = 60

# Command endpoint refusals that are the command's own fault: shown to the model as
# invalid_argument, as E2B's InvalidArgumentException is.
_INVALID_ARGUMENT_CODES = frozenset(
    {"bad_request", "missing_command", "cwd_not_absolute", "bad_env", "body_too_large", "spawn_failed"}
)
# Failures that leave the sandbox standing and may pass: shown to the model rather than
# raised, because Stirrup's Agent ends the whole run on an exception from a tool.
_TRANSIENT_CODES = frozenset(
    {"network_error", "sandbox_unreachable", "resolve_failed", "rate_limited", "bad_response", "sandbox_not_ready"}
)
_ADVICE = {
    "timeout": (
        "The request to the sandbox timed out, so the command may or may not have run. "
        "Check for its effects before running it again."
    ),
    "sandbox_unreachable": (
        "The sandbox didn't answer, so the command may or may not have run. "
        "Check for its effects before running it again."
    ),
    "invalid_argument": (
        "Avoid NUL and other control bytes and nested 'bash -lc'; write long content with a quoted "
        "heredoc or a base64 string."
    ),
    "command_timeout": (
        "Run long jobs in the background (nohup CMD > job.log 2>&1 &) and read the log in later "
        "commands, or split the work into shorter steps."
    ),
}


class UnsupportedOptionError(ValueError):
    """An option this backend can't honour yet (network off, an image, a template Prized
    doesn't have). Raised before any sandbox is made, never silently ignored."""


def _quote_argv_for_shell(argv: list[str]) -> str:
    """argv as one bash command that runs exactly these arguments. The command word is
    quoted too, so bash can't read it as a keyword (``time``) or an assignment."""
    head = "'" + argv[0].replace("'", "'\"'\"'") + "'"
    return f"{head} {shlex.join(argv[1:])}" if len(argv) > 1 else head


def _absolute(path: str) -> str:
    """A sandbox path made absolute the way commands see it (relative to WORKDIR)."""
    if not isinstance(path, str) or path == "":
        raise ValueError("path must be a non-empty string")
    return path if path.startswith("/") else f"{WORKDIR}/{path}"


def _files_api_serves(path: str) -> bool:
    """Whether the Prized files API takes this absolute path: under /home/prized, no ``..``."""
    parts = PurePosixPath(path).parts
    return path.startswith(HOME + "/") and ".." not in parts


def _detail(result: ExecResult) -> str:
    return " ".join(result.stderr.split())[:300] or f"exit {result.exit_code}"


def _tail(text: str, limit: int = _TIMEOUT_TAIL_CHARS) -> str:
    return text if len(text) <= limit else "[...]" + text[-limit:]


def _check_seconds(name: str, value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a number of seconds > 0")
    return value


def _timeout_result(err: CommandTimeoutError, timeout: int | float) -> CommandResult:
    details = [f"The command ran past its {timeout:g}s timeout and was stopped."]
    if err.stdout:
        details.append("stdout before it stopped:\n" + _tail(err.stdout))
    if err.stderr:
        details.append("stderr before it stopped:\n" + _tail(err.stderr))
    return CommandResult(
        exit_code=1,
        stdout=err.stdout,
        stderr="\n".join(details),
        error_kind="timeout",
        advice=_ADVICE["command_timeout"],
    )


def _error_kind(err: PrizedError) -> str | None:
    """The CommandResult error_kind for a failed command call, or None to raise it."""
    if err.code == "timeout":
        return "timeout"
    if err.code in _INVALID_ARGUMENT_CODES:
        return "invalid_argument"
    if err.code in _TRANSIENT_CODES or (err.code == "http_error" and (err.status or 0) >= 500):
        return "sandbox_unreachable"
    return None


def _split_sandbox_kwargs(sandbox_kwargs: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """E2B-style ``sandbox_kwargs`` as (create keywords, connection keywords) for the SDK."""
    create: dict[str, Any] = {}
    connection: dict[str, Any] = {}
    for key, value in sandbox_kwargs.items():
        if key == "allow_internet_access":
            if value is False:
                raise UnsupportedOptionError(_NETWORK_OFF_MESSAGE)
            continue
        if key in _E2B_IGNORED:
            continue
        name = _E2B_RENAMES.get(key, key)
        if name in _CONNECTION_KEYS:
            connection[name] = value
        elif name in _CREATE_KEYS:
            if name in create:
                raise ValueError(f"sandbox_kwargs sets {name} twice (as {key!r} and as {name!r})")
            create[name] = value
        else:
            accepted = sorted(_CREATE_KEYS | _CONNECTION_KEYS | set(_E2B_RENAMES) | {"allow_internet_access"})
            raise TypeError(f"sandbox_kwargs: {key!r} isn't supported by the Prized backend. Accepted: {accepted}")
    return create, connection


async def _finish_despite_cancel(work: Awaitable[None]) -> None:
    """Run ``work`` to its end even if the caller is cancelled meanwhile; the
    cancellation goes on once it has."""
    task = asyncio.ensure_future(work)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        raise


class PrizedCodeExecToolProvider(CodeExecToolProvider):
    """Prized Sandboxes code execution tool provider.

    Usage with Agent::

        provider = PrizedCodeExecToolProvider(timeout=3600)
        agent = Agent(client=client, name="assistant", tools=[provider])
        async with agent.session() as session:
            await session.run("Run Python code")

    Standalone::

        async with PrizedCodeExecToolProvider(allowed_commands=[r"^python"]) as _:
            ...
    """

    def __init__(
        self,
        *,
        timeout: int = SANDBOX_TIMEOUT,
        request_timeout: int = SANDBOX_REQUEST_TIMEOUT,
        template: str | None = None,
        vcpu: int | None = None,
        memory_gb: float | None = None,
        disk_gb: int | None = None,
        image: str | None = None,
        network: bool = True,
        fork_from: str | None = None,
        allowed_commands: list[str] | None = None,
        create_gate: AbstractAsyncContextManager[object] | None = None,
        sandbox_kwargs: dict[str, Any] | None = None,
        shell_timeout: int = SHELL_TIMEOUT,
    ) -> None:
        """Configure the sandbox; nothing is created until ``__aenter__``.

        Args:
            timeout: Sandbox lifetime in seconds (default 10 minutes, as E2B's). Prized counts
                run time in whole minutes of running: this becomes ``max_runtime_min``, rounded
                up. Doesn't apply with ``fork_from`` (a copy keeps its parent's).
            request_timeout: Seconds to wait for the sandbox to be running (create or fork).
            template: ``prized-base-v1`` (default) or ``prized-agent-v1``. Prized has no images
                built from your Dockerfile yet; an unknown name raises UnsupportedOptionError.
            vcpu, memory_gb, disk_gb: The sandbox's shape (the template's defaults otherwise).
            image: Docker mode (due 2026-11-09). Raises UnsupportedOptionError for now.
            network: ``False`` raises UnsupportedOptionError until Docker mode ships; so does
                ``sandbox_kwargs={"allow_internet_access": False}``.
            fork_from: Id of a running or paused sandbox you prepared: every run starts in a
                live copy of it instead of a fresh sandbox. The copy keeps the parent's
                template, shape and settings; ``envs`` apply to each command instead.
            allowed_commands: Regex allowlist, as in every Stirrup backend.
            create_gate: Async context manager entered around each create or fork, to throttle
                many providers starting at once (``asyncio.Semaphore(8)`` caps them at 8).
            sandbox_kwargs: E2B-style extras: ``envs``, ``metadata`` (labels),
                ``allow_internet_access``, or Prized's own ``env_vars``, ``labels``,
                ``auto_pause_min``, ``auto_delete_min``, ``memory_mb``, ``idempotency_key``,
                and connection options ``api_key``, ``base_url``, ``edge_url``,
                ``http_client``, ``max_retries``, ``poll_outage``.
            shell_timeout: Per-command timeout in seconds for ``code_exec`` and for
                ``run_command(timeout=None)``. Commands over 240 s run detached and are polled,
                so a long build doesn't hold one connection open.
        """
        super().__init__(allowed_commands=allowed_commands, shell_timeout=shell_timeout)
        if image is not None:
            raise UnsupportedOptionError(_IMAGE_MESSAGE)
        if template == DOCKER_TEMPLATE:
            raise UnsupportedOptionError(_DOCKER_TEMPLATE_MESSAGE)
        if network is not True:
            raise UnsupportedOptionError(_NETWORK_OFF_MESSAGE)
        if template is not None and (not isinstance(template, str) or template == ""):
            raise ValueError("template must be a non-empty string")
        if fork_from is not None and (not isinstance(fork_from, str) or fork_from == ""):
            raise ValueError("fork_from must be a sandbox id")
        self._timeout = _check_seconds("timeout", timeout)
        self._request_timeout = _check_seconds("request_timeout", request_timeout)
        create, connection = _split_sandbox_kwargs(sandbox_kwargs or {})
        if fork_from is not None:
            shaped = [
                name
                for name, value in (
                    ("template", template),
                    ("vcpu", vcpu),
                    ("memory_gb", memory_gb),
                    ("disk_gb", disk_gb),
                )
                if value is not None
            ]
            shaped += sorted(_NOT_FOR_FORKS & create.keys())
            if shaped:
                raise ValueError(
                    f"fork_from: a copy keeps its parent's template, shape and pause settings, so "
                    f"{', '.join(shaped)} can't apply. Set them on the parent."
                )
        self._template = template
        self._fork_from = fork_from
        self._connection = connection
        self._labels: dict[str, str] | None = create.pop("labels", None)
        self._idempotency_key: str | None = create.pop("idempotency_key", None)
        env_vars: dict[str, str] | None = create.pop("env_vars", None)
        self._create_args: dict[str, Any] = {}
        # A fork can't take new sandbox-wide variables: they go with each command instead.
        self._exec_env: dict[str, str] | None = None
        if fork_from is None:
            self._create_args = {
                "template": template or DEFAULT_TEMPLATE,
                "vcpu": vcpu,
                "memory_gb": memory_gb,
                "disk_gb": disk_gb,
                "env_vars": env_vars,
                "labels": self._labels,
                "max_runtime_min": max(1, math.ceil(self._timeout / 60)),
                "auto_delete_min": create.pop("auto_delete_min", _DEFAULT_AUTO_DELETE_MIN),
                "timeout": self._request_timeout,
                **create,
                **connection,
            }
            if self._idempotency_key is not None:
                self._create_args["idempotency_key"] = self._idempotency_key
        else:
            self._exec_env = dict(env_vars) if env_vars else None
        self._create_gate = create_gate
        self._sb: AsyncSandbox | None = None

    # --- lifecycle ---------------------------------------------------------------

    @property
    def sandbox(self) -> AsyncSandbox | None:
        """The ``prized.AsyncSandbox`` while started (pause, fork, keep_alive, ...), else None."""
        return self._sb

    async def __aenter__(self) -> Tool[CodeExecutionParams, ToolUseCountMetadata]:
        """Create (or fork) the sandbox and return the code_exec tool."""
        if self._sb is not None:
            raise RuntimeError("This PrizedCodeExecToolProvider is already started.")
        gate: AbstractAsyncContextManager[object] = (
            self._create_gate if self._create_gate is not None else contextlib.nullcontext()
        )
        self._sb = await (self._create(gate) if self._fork_from is None else self._fork(gate))
        return self.get_code_exec_tool()

    async def _create(self, gate: AbstractAsyncContextManager[object]) -> AsyncSandbox:
        try:
            async with gate:
                return await AsyncSandbox.create(**self._create_args)
        except PrizedError as err:
            if err.code in ("template_not_found", "template_inactive"):
                raise UnsupportedOptionError(
                    f"Prized has no template {self._create_args['template']!r} ({err.code}). E2B template names "
                    f"don't carry over: use 'prized-base-v1' or 'prized-agent-v1', install what the task needs, "
                    f"or prepare a sandbox once and pass fork_from=<its id>. Images arrive with Docker mode "
                    f"({DOCKER_MODE_DATE})."
                ) from err
            raise

    async def _fork(self, gate: AbstractAsyncContextManager[object]) -> AsyncSandbox:
        assert self._fork_from is not None
        parent = await AsyncSandbox.connect(self._fork_from, **self._connection)
        try:
            async with gate:
                copies = await parent.fork(
                    1, labels=self._labels, timeout=self._request_timeout, idempotency_key=self._idempotency_key
                )
        finally:
            await parent.close()  # releases this handle only; the parent sandbox stays
        return copies[0]

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Delete the sandbox. A failed delete is raised unless another error is already on its way."""
        sb, self._sb = self._sb, None
        if sb is None:
            return
        try:
            # Shielded: a run cancelled while leaving still sends the DELETE.
            await _finish_despite_cancel(sb.delete())
        except Exception as err:
            logger.warning("Could not delete Prized sandbox %s (its run time still ends it): %s", sb.id, err)
            if exc_type is None:
                raise
        finally:
            await sb.close()

    def _require(self) -> AsyncSandbox:
        if self._sb is None:
            raise RuntimeError(
                "ExecutionEnvironment not started. Ensure current Agent is equipped with a CodeExecToolProvider."
            )
        return self._sb

    # --- commands ------------------------------------------------------------------

    async def run_command(self, cmd: str, *, timeout: int | None = None) -> CommandResult:
        """Run a bash command in the sandbox and return its raw CommandResult.

        Args:
            cmd: Shell command (bash syntax), run in /home/prized/work.
            timeout: Seconds; None means the provider's ``shell_timeout``, 0 means no limit.
        """
        sb = self._require()
        if timeout is None:
            timeout = self._shell_timeout
        argv, rejection = self._prepare_command(cmd)
        if rejection is not None:
            return rejection
        if argv is not None:
            cmd = _quote_argv_for_shell(argv)
        if "\x00" in cmd:
            return CommandResult(
                exit_code=1,
                stdout="",
                stderr="The command contains a NUL byte, which a shell command can't carry.",
                error_kind="invalid_argument",
                advice=_ADVICE["invalid_argument"],
            )
        staged: str | None = None
        finished = False
        try:
            if len(cmd.encode("utf-8", "surrogatepass")) > _STAGE_COMMAND_BYTES:
                staged = f"{_STATE_DIR}/cmd-{uuid.uuid4().hex}.sh"
                await sb.files.write(staged, cmd.encode("utf-8", "surrogatepass"))
                q = shlex.quote(staged)
                body = f"bash -- {q}; __stirrup_prized_rc=$?; rm -f -- {q}; exit $__stirrup_prized_rc"
            else:
                body = cmd
            result = await sb.exec(_ENTER + body, cwd="/", env=self._exec_env, timeout=timeout)
            finished = True
        except CommandTimeoutError as err:
            return _timeout_result(err, timeout)
        except PrizedError as err:
            kind = _error_kind(err)
            if kind is None:
                raise
            return CommandResult(exit_code=1, stdout="", stderr=str(err), error_kind=kind, advice=_ADVICE[kind])
        except (ValueError, TypeError) as err:  # refused by the SDK before anything was sent
            return CommandResult(
                exit_code=1,
                stdout="",
                stderr=str(err),
                error_kind="invalid_argument",
                advice=_ADVICE["invalid_argument"],
            )
        finally:
            if staged is not None and not finished:
                with contextlib.suppress(Exception):
                    await sb.files.remove(staged)
        return CommandResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def _sh(self, script: str) -> ExecResult:
        """One of this backend's own short commands; outside the allowlist, like E2B's realpath calls."""
        return await self._require().exec(script, cwd="/", timeout=_HELPER_TIMEOUT)

    async def _kind(self, path: str) -> str | None:
        """``stat -L -c %F`` of an absolute path ("directory", "regular file", ...), None if missing."""
        q = shlex.quote(path)
        result = await self._sh(f"if [ ! -e {q} ]; then exit {_EXIT_MISSING}; fi; exec stat -L -c %F -- {q}")
        if result.exit_code == _EXIT_MISSING:
            return None
        if result.exit_code != 0:
            raise OSError(f"Could not stat {path}: {_detail(result)}")
        return result.stdout.strip()

    # --- files -----------------------------------------------------------------------

    async def read_file_bytes(self, path: str) -> bytes:
        """Read a file from the sandbox.

        Under /home/prized it's one files API call (up to 100 MiB). Elsewhere, or through a
        symlink, the file is copied under /home/prized first.

        Raises:
            RuntimeError: If the environment isn't started.
            FileNotFoundError: If the file doesn't exist.
            ValueError: If the path isn't a regular file or can't be read.
        """
        sb = self._require()
        target = _absolute(path)
        if _files_api_serves(target):
            try:
                return await sb.files.read_bytes(target)
            except PrizedError as err:
                if err.code in ("not_found", "not_directory"):
                    raise FileNotFoundError(f"File not found: {path}") from err
                if err.code == "not_regular_file":
                    raise ValueError(f"Path is not a file: {path}") from err
                if err.code not in ("symlink_not_allowed", "path_outside_root", "invalid_path"):
                    raise
        return await self._read_via_copy(sb, target, path)

    async def _read_via_copy(self, sb: AsyncSandbox, target: str, path: str) -> bytes:
        staged = f"{_XFER_DIR}/{uuid.uuid4().hex}"
        src, dst = shlex.quote(target), shlex.quote(staged)
        result = await self._sh(
            f"if [ ! -e {src} ]; then exit {_EXIT_MISSING}; fi; if [ ! -f {src} ]; then exit {_EXIT_NOT_FILE}; fi; "
            f"mkdir -p -- {shlex.quote(_XFER_DIR)} && cp -L -- {src} {dst}"
        )
        try:
            if result.exit_code == _EXIT_MISSING:
                raise FileNotFoundError(f"File not found: {path}")
            if result.exit_code == _EXIT_NOT_FILE:
                raise ValueError(f"Path is not a file: {path}")
            if result.exit_code != 0:
                raise ValueError(f"Could not read {path}: {_detail(result)}")
            return await sb.files.read_bytes(staged)
        finally:
            with contextlib.suppress(Exception):
                await sb.files.remove(staged)

    async def write_file_bytes(self, path: str, content: bytes) -> None:
        """Write a file in the sandbox, creating parent directories. Under /home/prized the
        file lands whole or not at all; elsewhere it's staged there, then copied into place."""
        sb = self._require()
        target = _absolute(path)
        if _files_api_serves(target):
            try:
                await sb.files.write(target, content)
                return
            except PrizedError as err:
                if err.code not in ("symlink_not_allowed", "path_outside_root", "invalid_path"):
                    raise
        staged = f"{_XFER_DIR}/{uuid.uuid4().hex}"
        await sb.files.write(staged, content)
        src, dst = shlex.quote(staged), shlex.quote(target)
        result = await self._sh(
            f'mkdir -p -- "$(dirname -- {dst})" && cp -- {src} {dst}; rc=$?; rm -f -- {src}; exit $rc'
        )
        if result.exit_code != 0:
            raise OSError(f"Could not write {path}: {_detail(result)}")

    async def file_exists(self, path: str) -> bool:
        """Whether the path exists (a file, a directory, or a symlink, even a broken one)."""
        return await self._require().files.exists(_absolute(path))

    async def is_directory(self, path: str) -> bool:
        """Whether the path is a directory (following symlinks); False if it doesn't exist."""
        self._require()
        return await self._kind(_absolute(path)) == "directory"

    async def list_files(self, path: str) -> list[str]:
        """Every file under a directory, recursively, relative to it; [] for a file or a missing path."""
        self._require()
        q = shlex.quote(_absolute(path))
        result = await self._sh(f"if [ ! -d {q} ]; then exit 0; fi; cd -- {q} && exec find . -type f -print0")
        if result.truncated:
            raise RuntimeError(f"{path} has too many files to list in one call; list a narrower directory.")
        if result.exit_code != 0:
            logger.warning("Listing %s in the Prized sandbox was incomplete: %s", path, _detail(result))
        found = (entry.removeprefix("./") for entry in result.stdout.split("\0"))
        return sorted(entry for entry in found if entry and entry != ".")

    # --- output paths ----------------------------------------------------------------

    def output_source_roots(self) -> tuple[OutputSourceRoot, ...]:
        """Absolute roots for output paths: the working directory first, then the home it's in."""
        return (OutputSourceRoot(path=WORKDIR), OutputSourceRoot(path=HOME))

    async def resolve_output_source(self, source_path: str) -> str:
        """Resolve every symlink of an output source and require a regular file under /home/prized."""
        self._require()
        result = await self._sh(f"realpath -ze -- {shlex.quote(_absolute(source_path))}")
        if result.exit_code != 0:
            raise ValueError(f"Could not resolve Prized output source: {source_path}")
        resolved_paths = [p for p in result.stdout.split("\0") if p]
        if len(resolved_paths) != 1:
            raise ValueError(f"Could not resolve Prized output source: {source_path}")
        resolved = PurePosixPath(resolved_paths[0])
        try:
            resolved.relative_to(PurePosixPath(HOME))
        except ValueError as err:
            raise ValueError(f"Prized output source is outside the execution root {HOME}: {source_path}") from err
        kind = await self._kind(resolved.as_posix())
        if kind not in ("regular file", "regular empty file"):
            raise ValueError(f"Path is not a file (type: {kind})")
        return resolved.as_posix()

    async def output_destination_identity(self, destination: str, output_root: Path | str) -> str:
        """Validate a cross-environment destination (symlinks resolved) and return its canonical path.

        Raises:
            RuntimeError: If the environment isn't started.
            ValueError: If the destination escapes ``output_root`` or can't be resolved.
        """
        self._require()
        root, dest = shlex.quote(_absolute(str(output_root))), shlex.quote(_absolute(destination))
        result = await self._sh(f"realpath -zm -- {root} {dest}")
        if result.exit_code != 0:
            raise ValueError(
                "Could not resolve cross-environment output destination: "
                f"realpath exited {result.exit_code}: {(result.stderr or result.stdout).strip()}"
            )
        resolved_paths = [p for p in result.stdout.split("\0") if p]
        if len(resolved_paths) != 2:
            raise ValueError(
                "Could not resolve cross-environment output destination "
                f"(expected 2 NUL-terminated paths from realpath; stdout: {result.stdout!r}, "
                f"stderr: {result.stderr!r})"
            )
        resolved_root, resolved_destination = map(PurePosixPath, resolved_paths)
        try:
            resolved_destination.relative_to(resolved_root)
        except ValueError as err:
            raise ValueError(f"Output destination escapes output directory: {destination}") from err
        return resolved_destination.as_posix()

    # --- uploads and images ------------------------------------------------------------

    async def upload_files(
        self,
        *paths: Path | str,
        source_env: CodeExecToolProvider | None = None,
        dest_dir: str | None = None,
    ) -> UploadFilesResult:
        """Upload local files or directories (recursively) into the sandbox.

        With ``source_env``, the paths are in that environment and the base class copies
        them. Without ``dest_dir``, files land in /home/prized/work, a directory keeping its
        own name there (as E2B's backend does under /home/user).
        """
        self._require()
        if source_env is not None:
            return await super().upload_files(*paths, source_env=source_env, dest_dir=dest_dir)

        dest_base = dest_dir or WORKDIR
        result = UploadFilesResult()
        for source in paths:
            original_name = Path(source).name
            source = Path(source).resolve()
            if not source.exists():
                result.failed[str(source)] = "File or directory does not exist"
                logger.warning("Upload source does not exist: %s", source)
                continue
            try:
                if source.is_file():
                    dest = f"{dest_base}/{original_name}"
                    content = source.read_bytes()
                    await self.write_file_bytes(dest, content)
                    result.uploaded.append(UploadedFile(source_path=source, dest_path=dest, size=len(content)))
                elif source.is_dir():
                    for file_path in sorted(source.rglob("*")):
                        if not file_path.is_file():
                            continue
                        relative = file_path.relative_to(source).as_posix()
                        dest = f"{dest_base}/{relative}" if dest_dir else f"{dest_base}/{source.name}/{relative}"
                        content = file_path.read_bytes()
                        await self.write_file_bytes(dest, content)
                        result.uploaded.append(UploadedFile(source_path=file_path, dest_path=dest, size=len(content)))
            except Exception as err:
                result.failed[str(source)] = str(err)
                logger.exception("Failed to upload: %s", source)
        return result

    async def view_image(self, path: str) -> ImageContentBlock:
        """Read a .png, .jpg or .jpeg from the sandbox as an ImageContentBlock.

        Raises:
            RuntimeError: If the environment isn't started.
            FileNotFoundError: If the file doesn't exist.
            ValueError: For another type, a corrupt image, or a file that can't be read.
        """
        if not path.lower().endswith((".png", ".jpg", ".jpeg")):
            raise ValueError(f"Unsupported image type for `{path}`. Only .png, .jpg, or .jpeg are allowed.")
        try:
            file_bytes = await self.read_file_bytes(path)
        except PrizedError as err:
            # The view_image tool turns only FileNotFoundError and ValueError into a message for the model.
            raise ValueError(f"Could not read `{path}` from the sandbox: {err}") from err
        try:
            return ImageContentBlock(data=file_bytes)
        except ValidationError as err:
            raise ValueError("You submitted a corrupt/unsupported image file.") from err
