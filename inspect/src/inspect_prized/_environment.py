"""Inspect AI sandbox environment backed by Prized Sandboxes (Firecracker microVMs)."""

from __future__ import annotations

import json
import posixpath
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging import getLogger
from typing import Literal, Union, overload

import anyio
from inspect_ai.util import (
    ExecResult,
    OutputLimitExceededError,
    SandboxConnection,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    SandboxUnavailableError,
    SandboxUserUnsupportedError,
    sandboxenv,
)
from prized import AsyncSandbox, CommandTimeoutError, PrizedError
from pydantic import BaseModel, Field
from typing_extensions import override

logger = getLogger(__name__)

HOME = "/home/prized"
WORKDIR = f"{HOME}/work"
STAGE = f"{HOME}/.inspect-prized"
LABEL = {"created-by": "inspect-prized"}
# PATH for every command: the system default, after ~/.local/bin (python_alias, pip --user scripts).
_PATH = f"{HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_ALIAS = f"mkdir -p {HOME}/.local/bin && ln -sf /usr/bin/python3 {HOME}/.local/bin/python"
RUN_LABEL = "inspect-run"  # one id per eval, so task cleanup can find everything the eval made
_GONE = ("not_found", "sandbox_deleted")
# prized 0.1.1+ (OutputReadError): the command ran, but its output could not be read back.
_OUTPUT_READ_FAILED = "output_read_failed"
_WRITE_FAILED_NOTE = (
    "\n[inspect-prized: the sandbox could not save all of this command's output, most often because its disk "
    "is full, so it stops where the write failed; free some space and run the command again]\n"
)
_FULL_OUTPUT = 100 * 1024 * 1024  # what exec(full_output=True) keeps per stream in one call
_POLLED_OUTPUT = 64 * 1024 * 1024  # what a polled exec (no timeout, or over 240 s) keeps: 64 Mi characters
_LARGE_ARGV = 256 * 1024  # argv bigger than this travels as a file (the exec route takes 1 MiB)
_HELPER_TIMEOUT = 120

# Every command runs under this wrapper. It keeps the command's own exit status (a command that
# kills itself with a signal reports 128+n, as under docker exec), feeds stdin from a staged file
# (Prized exec has no stdin), and reads a large argv from a NUL-separated file.
_RUN = r"""
__in=$1; __argv=$2; shift 2
if [ -n "$__argv" ]; then mapfile -d '' -t __a < "$__argv"; rm -f -- "$__argv"; set -- "${__a[@]}"; fi
if [ -n "$__in" ]; then "$@" < "$__in"; __rc=$?; rm -f -- "$__in"; else "$@" < /dev/null; __rc=$?; fi
exit $__rc
"""

_WRITE = r"""
umask 022
f=$1; s=$2
if [ -d "$f" ]; then rm -f -- "$s"; echo "Is a directory: $f" >&2; exit 21; fi
mkdir -p -- "$(dirname -- "$f")" 2>/dev/null
if [ -e "$f" ] && [ ! -w "$f" ]; then rm -f -- "$s"; echo "Permission denied: $f" >&2; exit 13; fi
if ! cat -- "$s" > "$f" 2>/dev/null; then rm -f -- "$s"; echo "Permission denied: $f" >&2; exit 13; fi
rm -f -- "$s"
"""

_READ = r"""
f=$1; s=$2; max=$3
if [ ! -e "$f" ]; then echo "No such file or directory: $f" >&2; exit 2; fi
if [ -d "$f" ]; then echo "Is a directory: $f" >&2; exit 21; fi
if [ ! -r "$f" ]; then echo "Permission denied: $f" >&2; exit 13; fi
size=$(stat -L -c %s -- "$f")
if [ "$size" -gt "$max" ]; then echo "$size"; exit 27; fi
mkdir -p -- "$(dirname -- "$s")" && cp -L -- "$f" "$s" || { echo "Permission denied: $f" >&2; exit 13; }
"""


class PrizedSandboxConfig(BaseModel, frozen=True):
    """Configuration for the ``prized`` sandbox (``sandbox=("prized", PrizedSandboxConfig(...))``)."""

    template: str = "prized-base-v1"
    """``prized-base-v1`` (Ubuntu 24.04 with Python, Node and git) or ``prized-agent-v1``
    (adds Claude Code and Codex)."""

    vcpu: int | None = None
    """vCPUs per sample's sandbox (template default when unset)."""

    memory_gb: float | None = None
    """Memory per sandbox in GB (template default when unset)."""

    disk_gb: int | None = None
    """Disk per sandbox in GB (template default when unset)."""

    env: dict[str, str] = Field(default_factory=dict)
    """Variables set for every command."""

    labels: dict[str, str] = Field(default_factory=dict)
    """Extra labels on each sandbox (it always carries ``created-by=inspect-prized``)."""

    max_runtime_min: int = 60
    """Minutes of running before Prized deletes a sandbox, even if cleanup never ran. 60 is the
    free tier's maximum; on a paid plan, raise it for samples that run longer."""

    keep: bool = False
    """Keep sandboxes after the sample (pause them) instead of deleting them, for debugging."""

    setup: list[str] = Field(default_factory=list)
    """Shell commands that install what the task needs (in place of a Dockerfile). They run once,
    before the first sample, in a sandbox that is then paused; each sample starts as a fork of it,
    with the install done (files, memory and running processes)."""

    setup_timeout: int = 1800
    """Seconds each ``setup`` command may run."""

    python_alias: bool = True
    """Make ``python`` run ``python3``, as in Python images (many evals call ``python``; the template
    has only ``python3``). It is a link in ``~/.local/bin``, which goes first on ``PATH``."""

    def __hash__(self) -> int:  # Inspect caches sandbox configs by hash; the dict fields need this
        return hash(json.dumps(self.model_dump(), sort_keys=True))


@sandboxenv(name="prized")
class PrizedSandboxEnvironment(SandboxEnvironment):
    """One Prized sandbox per sample: created in ``sample_init``, deleted in ``sample_cleanup``."""

    _live: dict[str, set[str]] = {}  # task name -> sandboxes not yet cleaned up

    def __init__(self, sandbox: AsyncSandbox, config: PrizedSandboxConfig) -> None:
        super().__init__()
        self._sandbox = sandbox
        self._forks: list[PrizedSandboxEnvironment] = []
        self._config = config

    @property
    def sandbox(self) -> AsyncSandbox:
        """The underlying ``prized.AsyncSandbox``."""
        return self._sandbox

    # ------------------------------------------------------------------ lifecycle

    @override
    @classmethod
    def config_deserialize(cls, config: dict) -> BaseModel:  # type: ignore[type-arg]
        return PrizedSandboxConfig(**config)

    @override
    @classmethod
    def default_concurrency(cls) -> int | None:
        return 10  # the free tier runs 10 sandboxes at once; set --max-sandboxes for more

    @override
    @classmethod
    async def task_init(cls, task_name: str, config: SandboxEnvironmentConfigType | None) -> None:
        # Inspect calls this before any sample starts (with task_name "startup"), in the context
        # that the samples and the final task_cleanup (task_name "shutdown") run in.
        if _run.get() is None:
            _run.set(secrets.token_hex(8))
            _templates.set({})
        cfg = _config(config)
        if cfg.setup:
            await _template(cfg)

    @override
    @classmethod
    async def sample_init(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None, metadata: dict[str, str]
    ) -> dict[str, SandboxEnvironment]:
        cfg = _config(config)
        options = _options(cfg, task_name)
        sandbox = None
        if cfg.setup:
            template = await _template(cfg)
            try:
                sandbox = (await template.fork(1, labels=options["labels"]))[0]
            except PrizedError as err:
                # Copies start on the template's machine; when it has no room (fork_capacity), or
                # the fork fails otherwise, start a fresh sandbox and run the setup in it.
                logger.info("Prized: fork of template %s failed (%s); creating a sandbox", template.id, err)
        if sandbox is None:
            sandbox = await AsyncSandbox.create(**options)
            try:
                await _prepare(sandbox, cfg)
            except BaseException:
                await _delete(sandbox.id)
                await sandbox.close()
                raise
        cls._live.setdefault(task_name, set()).add(sandbox.id)
        return {"default": cls(sandbox, cfg)}

    @override
    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        cfg = _config(config)
        envs = [env.as_type(PrizedSandboxEnvironment) for env in environments.values()]
        for env in envs:  # copies made with fork() go with the sample (envs grows as we walk it)
            envs.extend(env._forks)
        for env in envs:
            sb = env._sandbox
            try:
                if cfg.keep:
                    await sb.pause(wait=False)
                else:
                    # Wait for the delete (unless interrupted): a deleting sandbox still counts against
                    # the workspace's limit, and the next sample takes this slot as soon as we return.
                    await sb.delete(wait=not interrupted)
                cls._live.get(task_name, set()).discard(sb.id)
            except PrizedError as err:
                if err.code in _GONE:
                    cls._live.get(task_name, set()).discard(sb.id)
                else:
                    logger.warning("Prized sandbox %s cleanup failed (%s); task cleanup retries it", sb.id, err)
            finally:
                await sb.close()

    @override
    @classmethod
    async def task_cleanup(cls, task_name: str, config: SandboxEnvironmentConfigType | None, cleanup: bool) -> None:
        templates = _templates.get(None) or {}
        for entry in list(templates.values()):
            if entry.sandbox is not None:
                if cleanup:
                    await _delete(entry.sandbox.id)
                await entry.sandbox.close()
        templates.clear()
        if not cleanup or _config(config).keep:
            return
        for sandbox_id in list(cls._live.get(task_name, ())):
            await _delete(sandbox_id)
            cls._live.get(task_name, set()).discard(sandbox_id)
        # In an eval, task_name is "shutdown" here, not the name the samples ran under, so the
        # loop above finds nothing: delete by the eval's label whatever sample cleanup missed.
        run = _run.get()
        if run is not None:
            try:
                deleted = await AsyncSandbox.delete_by_labels({RUN_LABEL: run})
            except PrizedError as err:
                logger.warning("Prized sandboxes labelled %s=%s: cleanup failed (%s)", RUN_LABEL, run, err)
                return
            for ids in cls._live.values():
                ids.difference_update(deleted)

    @override
    @classmethod
    async def cli_cleanup(cls, id: str | None) -> None:
        """``inspect sandbox cleanup prized [ID]``: delete one sandbox this package made, or all."""
        if id is None:
            deleted = await AsyncSandbox.delete_by_labels(LABEL)
            print(f"Deleted {len(deleted)} Prized sandbox(es) labelled created-by=inspect-prized.")
            return
        sb = await AsyncSandbox.connect(id)
        try:
            if sb.labels.get("created-by") != LABEL["created-by"]:
                print(f"{id} was not created by inspect-prized; left alone.")
                return
            await sb.delete()
            print(f"Deleted {id}.")
        finally:
            await sb.close()

    async def fork(self, count: int = 1) -> list[PrizedSandboxEnvironment]:
        """Live copies of this sandbox: its files, memory and running processes as they are now.

        Not part of Inspect's interface: reach it with
        ``sandbox().as_type(PrizedSandboxEnvironment).fork(n)``, e.g. to try several continuations
        from one point of a trajectory. The copies are cleaned up with the sample.
        """
        with self._available():
            copies = await self._sandbox.fork(count)
        envs = [PrizedSandboxEnvironment(copy, self._config) for copy in copies]
        self._forks.extend(envs)
        return envs

    @override
    async def connection(self, *, user: str | None = None) -> SandboxConnection:
        return SandboxConnection(type="prized", command=f"prized sandbox ssh {self._sandbox.id}")

    # ------------------------------------------------------------------ exec

    @override
    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        if user not in (None, "prized", "1001"):
            raise SandboxUserUnsupportedError(
                f"Prized sandboxes run commands as the user 'prized' only (no root); cannot run as {user!r}."
            )
        if not cmd:
            raise ValueError("cmd must not be empty")
        token = secrets.token_hex(8)
        in_path = argv_path = ""
        argv = list(cmd)
        with self._available():
            if input is not None:
                in_path = f"{STAGE}/in-{token}"
                await self._sandbox.files.write(in_path, input)
            if sum(len(a.encode()) + 1 for a in argv) > _LARGE_ARGV:
                argv_path = f"{STAGE}/argv-{token}"
                await self._sandbox.files.write(argv_path, b"".join(a.encode() + b"\0" for a in argv))
                argv = []
        run = ["bash", "-c", _RUN, "inspect-prized", in_path, argv_path, *argv]
        kwargs: dict = {"cwd": self._resolve(cwd) if cwd else WORKDIR, "timeout": timeout or 0, "full_output": True}
        if env:
            kwargs["env"] = dict(env)
        with self._available():
            try:
                res = await self._sandbox.exec(run, **kwargs)
            except CommandTimeoutError as err:
                raise TimeoutError(f"Command timed out after {timeout} seconds: {cmd}") from err
            except PrizedError as err:
                if err.code == _OUTPUT_READ_FAILED:
                    raise _unread(self._sandbox.id, err) from err
                raise
        limit = SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE
        over = len(res.stdout.encode()) > limit or len(res.stderr.encode()) > limit
        # prized 0.1.1+ says why output was cut; a full disk is not an output limit.
        disk_full = res.truncated and getattr(res, "truncated_reason", None) == "write_failed"
        if over or (res.truncated and not disk_full):
            # Over Inspect's limit (10 MiB a stream by default) this raises as the Docker sandbox does;
            # output past what the SDK keeps raises at that size.
            kept = min(limit, _FULL_OUTPUT if timeout and timeout <= 240 else _POLLED_OUTPUT)
            raise OutputLimitExceededError(limit_str=_size_str(kept), truncated_output=_head(res.stdout, kept))
        if res.exit_code == 126 and "Permission denied" in res.stderr:
            raise PermissionError(f"Permission denied executing command: {cmd[0]}")
        stderr = res.stderr + _WRITE_FAILED_NOTE if disk_full else res.stderr
        return ExecResult(success=res.exit_code == 0, returncode=res.exit_code, stdout=res.stdout, stderr=stderr)

    # ------------------------------------------------------------------ files

    @override
    async def write_file(self, file: str, contents: str | bytes) -> None:
        path = self._resolve(file)
        staged = f"{STAGE}/w-{secrets.token_hex(8)}"
        with self._available():
            await self._sandbox.files.write(staged, contents)
            res = await self._sandbox.exec(["bash", "-c", _WRITE, "w", path, staged], timeout=_HELPER_TIMEOUT)
        if res.exit_code == 21:
            raise IsADirectoryError(f"Failed to write file: {file} is a directory")
        if res.exit_code == 13:
            raise PermissionError(f"Failed to write file: permission denied: {file}")
        if res.exit_code != 0:
            raise RuntimeError(f"Failed to write file {file}: {res.stderr.strip()}")

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    @override
    async def read_file(self, file: str, text: bool = True) -> Union[str | bytes]:
        path = self._resolve(file)
        staged = f"{STAGE}/r-{secrets.token_hex(8)}"
        limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        with self._available():
            res = await self._sandbox.exec(
                ["bash", "-c", _READ, "r", path, staged, str(limit)], timeout=_HELPER_TIMEOUT
            )
        if res.exit_code == 2:
            raise FileNotFoundError(f"File not found: {file}")
        if res.exit_code == 21:
            raise IsADirectoryError(f"Failed to read file: {file} is a directory")
        if res.exit_code == 13:
            raise PermissionError(f"Failed to read file: permission denied: {file}")
        if res.exit_code == 27:
            raise OutputLimitExceededError(
                limit_str=SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, truncated_output=None
            )
        if res.exit_code != 0:
            raise RuntimeError(f"Failed to read file {file}: {res.stderr.strip()}")
        with self._available():
            try:
                data = await self._sandbox.files.read_bytes(staged)
            finally:
                await self._sandbox.files.remove(staged)
        return data.decode("utf-8") if text else data

    @contextmanager
    def _available(self) -> Iterator[None]:
        """Raise Inspect's SandboxUnavailableError when the sandbox is gone or can't be woken."""
        try:
            yield
        except PrizedError as err:
            if err.code in _GONE or err.code == "sandbox_not_running":
                raise SandboxUnavailableError(f"Prized sandbox {self._sandbox.id} is not available: {err}") from err
            raise

    def _resolve(self, path: str) -> str:
        return path if path.startswith("/") else posixpath.normpath(posixpath.join(WORKDIR, path))

    @override
    def default_polling_interval(self) -> float:
        return 0.5


def _unread(sandbox_id: str, err: PrizedError) -> SandboxUnavailableError:
    """The command ran, but Prized could not deliver its output (prized 0.1.1+ OutputReadError).

    Not an ExecResult: Inspect's rule is that a provider failure is never presented as the command's
    output. As SandboxUnavailableError (the exec machinery failed) a tool call gets it as a tool
    error and the sample keeps running, while a scorer gets an exception rather than empty output.
    The message says the command ran, its exit code, and where its output is kept.
    """
    exit_code = getattr(err, "exit_code", None)
    paths = " and ".join(p for p in (getattr(err, "stdout_path", None), getattr(err, "stderr_path", None)) if p)
    why = f" ({err.reason})" if err.reason else ""
    kept = (
        f" It is kept in the sandbox for an hour: read {paths} rather than running the command again." if paths else ""
    )
    return SandboxUnavailableError(
        f"Prized sandbox {sandbox_id}: the command ran (exit code {exit_code}), but its output could not be read "
        f"back{why}.{kept}"
    )


_run: ContextVar[str | None] = ContextVar("inspect_prized_run", default=None)


class _Template:
    def __init__(self) -> None:
        self.lock = anyio.Lock()
        self.sandbox: AsyncSandbox | None = None


_templates: ContextVar[dict[str, _Template]] = ContextVar("inspect_prized_templates")


def _options(cfg: PrizedSandboxConfig, task_name: str) -> dict:
    options: dict = {
        "template": cfg.template,
        "labels": {**cfg.labels, **LABEL, "inspect-task": _label_value(task_name)},
        "max_runtime_min": cfg.max_runtime_min,
        "auto_pause_min": 0,
    }
    run = _run.get()
    if run is not None and not cfg.keep:
        options["labels"][RUN_LABEL] = run
    env_vars = {"PATH": _PATH} if cfg.python_alias else {}
    env_vars.update(cfg.env)
    if env_vars:
        options["env_vars"] = env_vars
    for name in ("vcpu", "memory_gb", "disk_gb"):
        if getattr(cfg, name) is not None:
            options[name] = getattr(cfg, name)
    return options


async def _prepare(sandbox: AsyncSandbox, cfg: PrizedSandboxConfig) -> None:
    """Link python (python_alias), then run the setup commands, in order."""
    steps = [(_ALIAS, 60)] if cfg.python_alias else []
    steps += [(command, cfg.setup_timeout) for command in cfg.setup]
    for command, timeout in steps:
        res = await sandbox.exec(["bash", "-c", command], timeout=timeout, full_output=True)
        if res.exit_code != 0:
            raise RuntimeError(
                f"Prized sandbox {sandbox.id}: setup command failed (exit {res.exit_code}): {command}\n"
                f"{(res.stderr or res.stdout)[-2000:]}"
            )


async def _template(cfg: PrizedSandboxConfig) -> AsyncSandbox:
    """The eval's paused sandbox with cfg.setup done, which samples fork; made on first use."""
    templates = _templates.get(None)
    if templates is None:
        templates = {}
        _templates.set(templates)
    entry = templates.setdefault(json.dumps(cfg.model_dump(), sort_keys=True), _Template())
    async with entry.lock:
        if entry.sandbox is None:
            options = _options(cfg, "template")
            options["labels"]["inspect-role"] = "template"
            sandbox = await AsyncSandbox.create(**options)
            try:
                await _prepare(sandbox, cfg)
                await sandbox.pause()
            except BaseException:
                await _delete(sandbox.id)
                await sandbox.close()
                raise
            entry.sandbox = sandbox
        return entry.sandbox


def _config(config: SandboxEnvironmentConfigType | None) -> PrizedSandboxConfig:
    if config is None or isinstance(config, str):
        return PrizedSandboxConfig()
    if isinstance(config, PrizedSandboxConfig):
        return config
    if isinstance(config, BaseModel):
        return PrizedSandboxConfig(**config.model_dump())
    raise TypeError(f"Unsupported config for the prized sandbox: {type(config).__name__}")


def _label_value(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)[:63].strip("-_.")
    return cleaned or "task"


def _head(text: str, size: int) -> str:
    """The start of ``text``, at most ``size`` bytes of it in UTF-8."""
    return text.encode()[:size].decode(errors="ignore")


def _size_str(size: int) -> str:
    return f"{size // (1024 * 1024)} MiB" if size >= 1024 * 1024 else f"{size} bytes"


async def _delete(sandbox_id: str) -> None:
    try:
        sb = await AsyncSandbox.connect(sandbox_id)
    except PrizedError as err:
        if err.code not in _GONE:
            logger.warning("Prized sandbox %s cleanup failed: %s", sandbox_id, err)
        return
    try:
        await sb.delete()
    except PrizedError as err:
        if err.code not in _GONE:
            logger.warning("Prized sandbox %s cleanup failed: %s", sandbox_id, err)
    finally:
        await sb.close()
