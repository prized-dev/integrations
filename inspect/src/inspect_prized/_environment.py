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

try:  # the batch's shared provider state, as Inspect's own Docker sandbox keeps it
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_state
except ImportError:  # pragma: no cover

    def sandbox_lifecycle_state():  # type: ignore[no-redef]
        return None


logger = getLogger(__name__)

HOME = "/home/prized"
WORKDIR = f"{HOME}/work"
STAGE = f"{HOME}/.inspect-prized"
LABEL = {"created-by": "inspect-prized"}
# PATH for every command: the system default, after ~/.local/bin (python_alias, pip --user scripts).
_PATH = f"{HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_ALIAS = f"mkdir -p {HOME}/.local/bin && ln -sf /usr/bin/python3 {HOME}/.local/bin/python"
RUN_LABEL = "inspect-run"  # one id per eval, so task cleanup can find everything the eval made
# A setup config gets a template on its second sample (the first runs setup in its own sandbox, so a
# setup only one sample uses never builds one), and an eval gets at most MAX_TEMPLATES of them: each
# paused template holds its disk (and any compute it keeps) until the eval ends.
MAX_TEMPLATES = 4
# A paused template is deleted this long after it pauses even if task cleanup never runs (the eval was
# killed). Copies inherit it, so a forked sample kept with keep=True is deleted 7 days after it pauses.
_TEMPLATE_DELETE_MIN = 7 * 24 * 60
_QUOTA = "sandbox_quota_exceeded"
# Quotas a paused template counts toward (its disk, and compute it keeps for resume). It doesn't count
# toward the number of sandboxes, so a sandbox_limit refusal is no reason to delete it.
_TEMPLATE_QUOTAS = ("retained_disk", "running_vcpu", "running_memory", "paused_limit")
# A paused template doesn't count toward the number of sandboxes, but one that isn't paused (in `error`,
# say) does: at this quota, templates are read again and those are deleted.
_COUNT_QUOTA = "sandbox_limit"
# Fork refusals that mean the template can't be forked again (deleted, in `error` or another state than
# paused, or no longer forkable): it is deleted then, since in `error` it still counts toward the number
# of sandboxes.
_TEMPLATE_GONE = ("not_found", "sandbox_deleted", "parent_not_running", "parent_not_forkable", "handle_closed")
_FORK_SETTLE_S = 200.0  # longer than the SDK's fork budget (180 s): copies still starting finish first
_GONE_WAIT_S = _FORK_SETTLE_S + 180.0  # another sample's drop: its fork wait plus the delete itself
_READ_S = 60.0  # at the sandbox limit, how long a create may spend reading templates again
# Samples that start while a template is being set up wait for it and fork it, as many as fit on its
# machine beside the copy for the sample setting it up: one machine holds 31, 15, 7 and 3 copies of a 1,
# 2, 4 and 8 vCPU sandbox (more memory or disk fits fewer). The rest would wait for the setup, the pause
# and the first fork only to find the machine full, so they run setup in their own sandboxes at once.
_MACHINE_VCPU = 32
_DEFAULT_VCPU = 4  # of a sandbox that asks for none (2 on the free tier), until the create answers
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
    """Shell commands that install what the task needs (in place of a Dockerfile). A config's first
    sample runs them in its own sandbox; for its second they run once more in a sandbox that is then
    paused, and that sample and every later one start as a fork of it, with the install done (files,
    memory and running processes). Of the samples that start while that setup runs (the first samples
    of an eval start together), as many as fit on the template's machine wait for it, and the rest run
    setup in their own sandboxes."""

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
        # The free tier runs 10 sandboxes at once; set --max-sandboxes for more. A paused setup
        # template counts toward disk (and compute it keeps): at those quotas it is deleted to make room.
        return 10

    @override
    @classmethod
    async def task_init(cls, task_name: str, config: SandboxEnvironmentConfigType | None) -> None:
        # Inspect calls this (task_name "startup") before a config's first sample, and
        # task_cleanup ("shutdown") after the last; both see the batch's lifecycle state.
        # Templates are made on a config's second sample (_template), not here.
        _state(create=True)

    @override
    @classmethod
    async def sample_init(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None, metadata: dict[str, str]
    ) -> dict[str, SandboxEnvironment]:
        cfg = _config(config)
        options = _options(cfg, task_name)
        sandbox = None
        state = _state()
        entry = await _template(state, cfg) if cfg.setup and state is not None else None
        if entry is not None and entry.sandbox is not None and not entry.failed:
            template = entry.sandbox
            refused: PrizedError | None = None
            entry.forking += 1
            try:
                sandbox = (await template.fork(1, labels=options["labels"]))[0]
            except PrizedError as err:
                refused = err
            finally:
                entry.forking -= 1
            if refused is not None:
                if refused.code == _QUOTA and refused.reason in _TEMPLATE_QUOTAS:
                    # No room for the template and this sample both: the template gives up its disk
                    # (and compute), and this config's samples run setup in their own sandboxes.
                    await _drop_template(entry)
                elif refused.code in _TEMPLATE_GONE and refused.sandbox_id in (None, template.id):
                    # The template itself (not just a copy) can't be forked again: delete it now, not at
                    # task cleanup. In `error` it counts toward the number of sandboxes, which _create
                    # never frees, and paused it holds its disk.
                    await _drop_template(entry, f"it can't be forked ({refused.code})")
                # Copies start on the template's machine; when it has no room (fork_capacity), or
                # the fork fails otherwise, start a fresh sandbox and run the setup in it.
                logger.info("Prized: fork of template %s failed (%s); creating a sandbox", template.id, refused)
        if sandbox is None:
            # A config's first sample, outside an eval's lifecycle (no task_init), past
            # MAX_TEMPLATES, or when the fork failed: run the setup in the sample's own sandbox.
            sandbox = await _create(state, options)
            try:
                await _prepare(sandbox, cfg)
            except Exception:
                await _discard(sandbox, wait=True)  # a deleting sandbox still counts toward the next create
                raise
            except BaseException:
                await _discard(sandbox)  # cancelled: don't hold up the shutdown
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
                    logger.warning("Prized sandbox %s cleanup failed (%s)", sb.id, err)
            finally:
                await sb.close()

    @override
    @classmethod
    async def task_cleanup(cls, task_name: str, config: SandboxEnvironmentConfigType | None, cleanup: bool) -> None:
        state = _state()
        if state is not None and state.templates:
            # Templates hold no sample's state, so they go even with --no-sandbox-cleanup; the label
            # sweep catches one whose delete failed.
            for entry in list(state.templates.values()):
                # A sample still starting finds no template (or a closed handle, which it leaves alone).
                sandbox, entry.sandbox, entry.failed = entry.sandbox, None, True
                if sandbox is not None:
                    await _discard(sandbox)
            state.templates.clear()
            try:
                await AsyncSandbox.delete_by_labels({RUN_LABEL: state.run_id, "inspect-role": "template"})
            except PrizedError as err:
                logger.warning("Prized: cleanup of this eval's setup templates failed (%s)", err)
        try:
            if not cleanup or _config(config).keep:
                return
            for sandbox_id in list(cls._live.get(task_name, ())):
                await _delete(sandbox_id)
                cls._live.get(task_name, set()).discard(sandbox_id)
            # In an eval, task_name is "shutdown" here, not the name the samples ran under, so the
            # loop above finds nothing: delete by the eval's label whatever sample cleanup missed.
            # Inspect calls this once per sandbox config; one sweep covers them all.
            if state is not None and not state.swept:
                try:
                    deleted = await AsyncSandbox.delete_by_labels({RUN_LABEL: state.run_id})
                except PrizedError as err:
                    logger.warning("Prized sandboxes labelled %s=%s: cleanup failed (%s)", RUN_LABEL, state.run_id, err)
                    return
                state.swept = True
                for ids in cls._live.values():
                    ids.difference_update(deleted)
        finally:
            if state is not None:
                state.done = True  # the next task_init starts a new eval's state

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
        _check_user(user)
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
        run = ["/bin/bash", "-c", _RUN, "inspect-prized", in_path, argv_path, *argv]
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
    async def write_file(self, file: str, contents: str | bytes, *, user: str | None = None) -> None:
        _check_user(user)  # `user=` on file methods: inspect_ai PR 6107
        path = self._resolve(file)
        staged = f"{STAGE}/w-{secrets.token_hex(8)}"
        with self._available():
            await self._sandbox.files.write(staged, contents)
            res = await self._sandbox.exec(["/bin/bash", "-c", _WRITE, "w", path, staged], timeout=_HELPER_TIMEOUT)
        if res.exit_code == 21:
            raise IsADirectoryError(f"Failed to write file: {file} is a directory")
        if res.exit_code == 13:
            raise PermissionError(f"Failed to write file: permission denied: {file}")
        if res.exit_code != 0:
            raise RuntimeError(f"Failed to write file {file}: {res.stderr.strip()}")

    @overload
    async def read_file(self, file: str, text: Literal[True] = True, *, user: str | None = None) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False], *, user: str | None = None) -> bytes: ...

    @override
    async def read_file(self, file: str, text: bool = True, *, user: str | None = None) -> Union[str | bytes]:
        _check_user(user)
        path = self._resolve(file)
        staged = f"{STAGE}/r-{secrets.token_hex(8)}"
        limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        with self._available():
            res = await self._sandbox.exec(
                ["/bin/bash", "-c", _READ, "r", path, staged, str(limit)], timeout=_HELPER_TIMEOUT
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


class _Template:
    def __init__(self) -> None:
        self.lock = anyio.Lock()
        self.sandbox: AsyncSandbox | None = None
        self.failed = False  # its build failed, or it was deleted (for room, or unforkable): samples run setup
        self.forking = 0  # forks of it in flight (a parent is not deleted while its copies start)
        self.gone: anyio.Event | None = None  # set once a template being deleted is gone
        # The sample creating it and running its setup (its token), or None. Only that sample clears it.
        self.builder: object | None = None
        self.created: anyio.Event | None = None  # set once the builder's create has answered (or it ended)
        self.vcpu: int | None = None  # its vCPU, from the create's answer
        self.waiting = 0  # samples waiting for its setup, to fork it once it is paused


class _State:
    """One eval batch's state: its run label and the set-up templates its samples fork."""

    def __init__(self) -> None:
        self.run_id = secrets.token_hex(8)
        self.templates: dict[str, _Template] = {}
        self.seen: set[str] = set()  # setup configs that have had a sample
        self.swept = False
        self.done = False  # task_cleanup ran (outside Inspect's scope, the state outlives the eval)


# Outside a lifecycle scope (an older Inspect, or task_init called directly), task_init keeps the
# state here; samples in the same context see it.
_fallback: ContextVar[_State | None] = ContextVar("inspect_prized_state", default=None)


def _state(create: bool = False) -> _State | None:
    scope = sandbox_lifecycle_state()
    if scope is not None:
        return scope.get(_State)
    state = _fallback.get()
    if create and (state is None or state.done):
        state = _State()
        _fallback.set(state)
    return state


def _options(cfg: PrizedSandboxConfig, task_name: str) -> dict:
    options: dict = {
        "template": cfg.template,
        "labels": {**cfg.labels, **LABEL, "inspect-task": _label_value(task_name)},
        "max_runtime_min": cfg.max_runtime_min,
        "auto_pause_min": 0,
    }
    state = _state()
    if state is not None and not cfg.keep:
        options["labels"][RUN_LABEL] = state.run_id
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
    steps = [("python_alias", _ALIAS, 60)] if cfg.python_alias else []
    steps += [("setup command", command, cfg.setup_timeout) for command in cfg.setup]
    for what, command, timeout in steps:
        res = await sandbox.exec(["/bin/bash", "-c", command], timeout=timeout, full_output=True)
        if res.exit_code != 0:
            raise RuntimeError(
                f"Prized sandbox {sandbox.id}: {what} failed (exit {res.exit_code}): {command}\n"
                f"{(res.stderr or res.stdout)[-2000:]}"
            )


def _template_key(cfg: PrizedSandboxConfig) -> str:
    # Labels and keep don't change what a template holds (fork() sets each sample's labels).
    return json.dumps(cfg.model_dump(exclude={"labels", "keep"}), sort_keys=True)


async def _drop_template(entry: _Template, why: str = "no room for it and a sample at the quota") -> None:
    """Delete a template (to make room at a quota, or once it can't be forked), once forks of it in
    flight have settled.

    Returns once it is gone (a deleting sandbox still counts), also to a caller that finds another
    sample already deleting it.
    """
    entry.failed = True  # no new forks
    if entry.gone is not None:
        with anyio.move_on_after(_GONE_WAIT_S):
            await entry.gone.wait()
        return
    if entry.sandbox is None:
        return
    gone = entry.gone = anyio.Event()
    try:
        with anyio.move_on_after(_FORK_SETTLE_S):
            while entry.forking:
                await anyio.sleep(0.2)
        sandbox, entry.sandbox = entry.sandbox, None
        if sandbox is None:
            return
        logger.warning("Prized: deleting setup template %s: %s", sandbox.id, why)
        await _discard(sandbox, wait=True)
    finally:
        if entry.sandbox is not None:  # cancelled before the delete: the next caller deletes it
            entry.gone = None
        gone.set()


async def _create(state: _State | None, options: dict) -> AsyncSandbox:
    """Create a sample's sandbox. At a quota templates count toward, delete them one at a time (given-up
    ones first, and waiting for one another sample is deleting only when no other is left) until it fits.

    Paused, they count toward disk and kept compute. Toward the number of sandboxes only once they aren't
    paused (in `error`, say): at that quota each is read again, so one is found even when no fork of it
    comes to find out.
    """
    drops = 0
    while True:
        try:
            return await AsyncSandbox.create(**options)
        except PrizedError as err:
            entries = list(state.templates.values()) if state is not None else []
            fresh = [e for e in entries if e.sandbox is not None and e.gone is None]
            fresh.sort(key=lambda e: not e.failed)  # templates no sample forks any more go first
            pending = [e for e in entries if e.gone is not None and not e.gone.is_set()]
            at_count = err.code == _QUOTA and err.reason == _COUNT_QUOTA
            if at_count:  # only a template that isn't paused counts here: the first found, if any
                counted = []
                with anyio.move_on_after(_READ_S):
                    for e in fresh:
                        if await _counts(e):
                            counted.append(e)
                            break
                fresh = counted
            target = (fresh or pending or [None])[0]
            room = err.code == _QUOTA and (err.reason in _TEMPLATE_QUOTAS or at_count)
            reason = err.reason  # `err` is unbound once the except clause ends
            if not room or target is None or drops > len(entries):
                if err.code == _QUOTA:
                    logger.warning(
                        "Prized: the workspace is at its sandbox quota (%s). Run with a lower --max-sandboxes, "
                        "or delete sandboxes you no longer need.",
                        err.reason,
                    )
                raise
        drops += 1
        await _drop_template(target, f"no room for it and a sample at the quota ({reason})")


async def _counts(entry: _Template) -> bool:
    """Whether a template counts toward the number of sandboxes: it isn't paused (in `error`, say), or
    another sample started deleting it meanwhile (a deleting sandbox counts until it is gone)."""
    sandbox = entry.sandbox
    if entry.gone is not None or sandbox is None:
        return entry.gone is not None
    try:
        await sandbox.refresh()
    except PrizedError:
        return entry.gone is not None  # deleted, or unreadable: a fork of it finds out
    return entry.gone is not None or sandbox.state != "paused"


async def _template(state: _State, cfg: PrizedSandboxConfig) -> _Template | None:
    """The config's template entry, its paused sandbox (with cfg.setup done) built on first need.

    None, or an entry without a sandbox, for a config's first sample, past MAX_TEMPLATES configs, for a
    sample that starts while the template is being set up and finds as many waiting for it as fit on its
    machine (_may_wait), or once the config's template failed or was deleted: those samples run setup
    themselves.
    """
    key = _template_key(cfg)
    if key not in state.seen:
        state.seen.add(key)
        return None
    entry = state.templates.get(key)
    if entry is None:
        if len(state.templates) >= MAX_TEMPLATES:
            return None
        entry = state.templates.setdefault(key, _Template())
    while entry.builder is not None:
        created = entry.created
        if entry.vcpu is None and created is not None and not created.is_set():
            await created.wait()  # how many fit depends on its vCPU, which its create answers with
            continue  # its builder may have been cancelled meanwhile, and another taken over
        if entry.waiting >= _may_wait(cfg, entry.vcpu):
            return None
        return await _build_template(state, cfg, entry, waits=True)
    return await _build_template(state, cfg, entry)


def _may_wait(cfg: PrizedSandboxConfig, vcpu: int | None) -> int:
    """How many samples may wait for a template being set up (and fork it), beside the one setting it up."""
    vcpu = vcpu or cfg.vcpu or _DEFAULT_VCPU
    return max(0, (_MACHINE_VCPU - vcpu) // vcpu - 1)


def _claim(entry: _Template, token: object) -> None:
    entry.builder = token
    if entry.created is None or entry.created.is_set():  # samples may wait on one not yet set: keep it
        entry.created = anyio.Event()


async def _build_template(state: _State, cfg: PrizedSandboxConfig, entry: _Template, waits: bool = False) -> _Template:
    me = object()
    if waits:
        entry.waiting += 1
    elif entry.builder is None and entry.sandbox is None and not entry.failed:
        _claim(entry, me)  # before any await, so samples that start meanwhile see it
    try:
        async with entry.lock:
            if waits:  # its wait is over: it no longer takes room from the samples that come next
                waits, entry.waiting = False, entry.waiting - 1
            if entry.sandbox is None and not entry.failed:
                # Whoever runs the setup owns it: after a cancelled build, a sample that waited for it
                # takes over (and a sample still queued behind keeps no claim it doesn't act on).
                _claim(entry, me)
                await _set_up_template(state, cfg, entry)
            return entry
    finally:
        if waits:
            entry.waiting -= 1
        if entry.builder is me:
            entry.builder = None
            if entry.created is not None:
                entry.created.set()


async def _set_up_template(state: _State, cfg: PrizedSandboxConfig, entry: _Template) -> None:
    options = _options(cfg, "template")
    options["labels"].update({"inspect-role": "template", RUN_LABEL: state.run_id})
    options["auto_delete_min"] = _TEMPLATE_DELETE_MIN
    sandbox = None
    try:
        sandbox = await AsyncSandbox.create(**options)
        entry.vcpu = getattr(getattr(sandbox, "resources", None), "vcpu", None) or None
        if entry.created is not None:
            entry.created.set()
        await _prepare(sandbox, cfg)
        await sandbox.pause()
    except Exception as err:
        # The samples run setup themselves from now on. A failed or timed-out setup command
        # fails this sample too, rather than running the same setup again in its own sandbox.
        entry.failed = True
        if sandbox is not None:
            await _discard(sandbox, wait=True)  # the sample's own create comes next
        if isinstance(err, (RuntimeError, CommandTimeoutError)):
            raise
        logger.info("Prized: setup template for this config not made (%s); samples run setup", err)
        return
    except BaseException:
        if sandbox is not None:
            await _discard(sandbox)
        raise
    entry.sandbox = sandbox


def _check_user(user: str | None) -> None:
    if user not in (None, "prized", "1001"):
        raise SandboxUserUnsupportedError(
            f"Prized sandboxes run commands as the user 'prized' only (no root); cannot run as {user!r}."
        )


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


async def _discard(sandbox: AsyncSandbox, wait: bool = False) -> None:
    """Delete a sandbox this package just made and release it, even while being cancelled."""
    with anyio.CancelScope(shield=True):
        try:
            await sandbox.delete(wait=wait)
        except PrizedError as err:
            if err.code not in _GONE:
                logger.warning("Prized sandbox %s cleanup failed: %s", sandbox.id, err)
        finally:
            await sandbox.close()


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
