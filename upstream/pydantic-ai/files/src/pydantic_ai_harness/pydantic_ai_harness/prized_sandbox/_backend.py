"""A [Prized](https://prized.dev) sandbox behind Pydantic AI's `WorkspaceBackend` protocol.

SDK behavior verified 2026-11-05 against prized 0.1.2 on Prized's production API. Re-check these before
changing acquisition, command, or error handling:

- `AsyncSandbox.create` returns once the sandbox is `running` (about a quarter of a second from a warm pool).
  The SDK sends an idempotency key, so its own retries never make a second sandbox, and a create that is
  interrupted or never answered deletes what it may have made.
- A command runs under `bash -c` as the non-root user `prized`, in `/home/prized/work` unless `cwd` says
  otherwise. One call (a `timeout` of up to 240 seconds) is answered when the command ends, even while a
  background child it started still runs; that `bash -c` leads its own process group, and the call keeps the
  first 4 MiB of each stream. `full_output=True` would keep more, but wraps the command so that `bash -c` no
  longer leads a group, so it is not used. A longer or unbounded command runs detached in its own session
  through a background handle, which `kill()` stops with every process it started.
- A wrapped command line (a detached one, or with `full_output`) over about 64 KiB fails with `spawn_failed`
  ("argument list too long"), so a long command line is written to a file in the sandbox and sourced.
- The file API works under `/home/prized` only. `files.read_bytes` follows symlinks and honors permissions;
  it refuses a directory or FIFO (`not_regular_file`), an unreadable file (`permission_denied`), and a
  missing one (`not_found`, which also means a deleted sandbox). `files.write` replaces a file whole, so it
  writes even a file whose mode forbids it: writes therefore go through the shell, reads through the API.
- A command that runs past its `timeout` is killed and the call raises `CommandTimeoutError`, whose `result`
  holds the output printed before.
- A background process a command starts (`cmd &`, `nohup`) keeps running after the call ends.
- A paused sandbox resumes on its next command or file call, with its memory and running processes.
  `fork()` copies a running or paused sandbox live (files, memory and processes) into new sandboxes.
- A deleted sandbox answers `not_found`; one being deleted answers `sandbox_deleted`. A blocking call in
  flight when its sandbox is deleted can go unanswered until the HTTP timeout, so commands check meanwhile.
- A missing `cwd` is refused with `spawn_failed` before anything runs.
- Missing and refused keys raise `PrizedError` with `code` `missing_api_key`, `invalid_api_key`,
  `unauthorized`, `token_expired` or `key_access_denied`; the SDK never quotes the key.
"""

from __future__ import annotations

import asyncio
import logging
import math
import posixpath
import shlex
import uuid
from collections.abc import AsyncGenerator, Awaitable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, TypeVar

import anyio
import anyio.lowlevel

from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    SupportsCommands,
    SupportsFilesystem,
    WorkspaceBackend,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)
from pydantic_ai.workspaces.workspace import _ShellFilesystem  # pyright: ignore[reportPrivateUsage]
from pydantic_ai_harness._workspace_provider import (
    absolute_path,
    check_env,
    check_provider,
    check_timeout,
    command_argv,
    require_asyncio,
    run_to_completion,
    running_on_asyncio,
    stop_shielded,
)

if TYPE_CHECKING:
    from prized import AsyncSandbox

    from pydantic_ai.workspaces import WorkspaceCommand

__all__ = ('PrizedSandboxBackend',)

_T = TypeVar('_T')

try:
    import prized
except ImportError as _import_error:
    raise ImportError('Install `pydantic-ai-harness[prized]` to use PrizedSandbox.') from _import_error

PROVIDER = 'prized'
DEFAULT_TEMPLATE = 'prized-base-v1'

_ASYNCIO_ONLY = 'PrizedSandbox needs the asyncio event loop: the Prized SDK runs on asyncio.'
_AUTH_MESSAGE = (
    'Prized did not accept the API key ({code}). Set PRIZED_API_KEY to a key with Sandboxes access '
    '(https://prized.dev/dashboard/workspace/api-keys), or pass `api_key=`.'
)
_CREDENTIAL_CODES = frozenset(
    {'missing_api_key', 'invalid_api_key', 'unauthorized', 'token_expired', 'key_access_denied'}
)
# Refusals a retry cannot fix: the sandbox cannot be created, resumed or forked as asked.
_REFUSAL_CODES = frozenset(
    {
        'paid_balance_required',
        'insufficient_balance',
        'workspace_held',
        'workspace_closing',
        'template_not_found',
        'template_inactive',
        'invalid_resources',
        'unknown_field',
        'bad_request',
        'invalid_max_runtime',
        'sandbox_quota_exceeded',
        'parent_not_forkable',
    }
)
# Answers that may mean the sandbox is gone; one lookup decides.
_MAYBE_GONE_CODES = frozenset(
    {'not_found', 'sandbox_not_running', 'sandbox_unreachable', 'sandbox_error', 'sandbox_deleted', 'network_error'}
)
_GONE_STATES = frozenset({'deleting', 'deleted', 'error'})

# Bound sandbox creation, attachment, and deletion so a wedged control plane cannot hang a run.
_LIFECYCLE_TIMEOUT = 180.0
_REQUEST_TIMEOUT = 30.0
_TEARDOWN_TIMEOUT = 30.0
# A command with a timeout up to this many seconds is one exec call; a longer or unbounded one runs detached.
_MAX_BLOCKING_SECONDS = 240
# Past the command's own limit, how long to wait for the edge to answer before stopping it ourselves.
_ANSWER_SLACK = 15.0
# How often a command in flight checks that its sandbox still exists.
_WATCH_INTERVAL = 5.0
# One exec call keeps the first 4 MiB of each stream; a detached command's handle keeps up to 64 Mi characters.
_BLOCKING_OUTPUT_LIMIT = 4 * 1024 * 1024
_DETACHED_OUTPUT_LIMIT = 64 * 1024 * 1024
_OUTPUT_PREVIEW_CHARS = 64 * 1024
_RUN_DIR = '/tmp/pydantic-ai-prized'
# A command line longer than this goes through a script file (see the module docstring).
_MAX_INLINE_COMMAND_BYTES = 16 * 1024
_SCRIPT_DIR = '/home/prized/.cache/pydantic-ai-prized'
# Where the file API reaches.
_HOME = '/home/prized'
_LABELS = {'created-by': 'pydantic-ai'}

logger = logging.getLogger(__name__)


def _check_ref(ref: WorkspaceRef) -> None:
    """Raise `ValueError` unless `ref` names a Prized sandbox."""
    check_provider(ref, PROVIDER)
    if not ref.id.strip():
        raise ValueError('Prized workspace ref id cannot be empty')


def _credential_error(error: prized.PrizedError) -> WorkspaceUnavailableError:
    # The SDK never quotes the key back; the code alone is safe to show.
    return WorkspaceUnavailableError(_AUTH_MESSAGE.format(code=error.code))


def _gone(sandbox_id: str, why: str) -> WorkspaceUnavailableError:
    return WorkspaceUnavailableError(
        f"The Prized sandbox {sandbox_id!r} {why}. Pass `workspace='new'` to start a fresh sandbox."
    )


def _too_much_output(stdout: str, stderr: str, limit: int) -> WorkspaceOutputLimitError:
    return WorkspaceOutputLimitError(
        f'Prized command output exceeded {limit // (1024 * 1024)} MiB per stream; '
        'redirect it to a file and read part of it instead',
        limit=limit,
        stdout=stdout[:_OUTPUT_PREVIEW_CHARS],
        stderr=stderr[:_OUTPUT_PREVIEW_CHARS],
    )


async def destroy_sandbox(
    sandbox_id: str, *, api_key: str | None = None, base_url: str | None = None, edge_url: str | None = None
) -> None:
    """Delete a Prized sandbox by id, in any state, without resuming it.

    A sandbox that no longer exists returns quietly; a refused key raises `WorkspaceUnavailableError`.

    Raises:
        UserError: The event loop is not asyncio.
        TimeoutError: Prized did not answer in time.
    """
    require_asyncio(_ASYNCIO_ONLY)
    if not sandbox_id.strip():
        raise ValueError('Prized workspace ref id cannot be empty')
    with anyio.fail_after(_LIFECYCLE_TIMEOUT):
        try:
            sandbox = await prized.AsyncSandbox.connect(
                sandbox_id, api_key=api_key, base_url=base_url, edge_url=edge_url
            )
        except prized.PrizedError as error:
            if error.code == 'not_found':
                return
            if error.code in _CREDENTIAL_CODES:
                raise _credential_error(error) from error
            raise
        try:
            await sandbox.delete()
        except prized.PrizedError as error:
            if error.code in _CREDENTIAL_CODES:
                raise _credential_error(error) from error
            if error.code not in ('not_found', 'sandbox_deleted'):
                raise
        finally:
            await _close_quietly(sandbox)


async def _close_quietly(sandbox: AsyncSandbox) -> None:
    """Release a handle's HTTP connections, bounded and shielded; a failure is logged, not raised."""
    with anyio.move_on_after(_TEARDOWN_TIMEOUT, shield=True):
        try:
            await sandbox.close()
        except Exception as error:
            logger.warning('Could not close the Prized SDK client: %r', error)


class PrizedSandboxBackend(WorkspaceBackend, SupportsCommands, SupportsFilesystem):
    """A [Prized](https://prized.dev) sandbox as a Pydantic AI [`WorkspaceBackend`][pydantic_ai.workspaces.WorkspaceBackend].

    A Prized sandbox is a Firecracker microVM with its own kernel that pauses with its memory and running
    processes, resumes where it stopped, and forks into live copies.

    Building one does no I/O. The first operation creates a sandbox from `template`, or attaches to the
    one `ref` names; a paused sandbox resumes on its first command. The backend never pauses or deletes
    the sandbox: whoever holds the ref does (`PrizedSandbox.pause` and `PrizedSandbox.destroy`), or the
    sandbox's own timers do (`auto_pause_min`, `auto_delete_min`, `max_runtime_min`).
    `get_sandbox()` returns its `prized.AsyncSandbox` for everything else the SDK can do.

    Commands run under `bash` as the user `prized` with stdin at EOF; a shell string runs under
    `/bin/sh -c`. A timeout or cancellation stops the command's whole process group. A file under
    `/home/prized` is read with Prized's file API in one call; every other file operation goes through the
    shell, as core derives it for a command-only backend, so it reaches every path the `prized` user can.

    A deleted sandbox, a refused key, and a create Prized refuses (balance, quota, template) raise
    `WorkspaceUnavailableError`.

    Args:
        ref: Identity of an existing sandbox to attach to on first use.
        template: Template a new sandbox starts from: `prized-base-v1` (Ubuntu 24.04 with Python, Node and
            git) or `prized-agent-v1` (adds Claude Code and Codex).
        vcpu: vCPUs of a new sandbox; the template's default when `None`.
        memory_mb: Memory of a new sandbox in MiB; the template's default when `None`.
        disk_gb: Disk of a new sandbox in GiB; the template's default when `None`.
        working_dir: Absolute directory commands start in and relative paths resolve against;
            `/home/prized/work` when `None`. Created on a new sandbox; an attached sandbox must already have it.
        env: Environment variables every command gets, also on an attached sandbox; a command's `env` is
            layered on top. Nothing is read from the host.
        labels: Labels for a new sandbox, to find it later; `created-by=pydantic-ai` is always added.
        auto_pause_min: Idle minutes before a new sandbox pauses itself, memory kept; the next command
            resumes it. `0` turns it off, and `None` keeps Prized's default (off).
        auto_delete_min: Minutes paused before a new sandbox deletes itself; `None` keeps Prized's default (off).
        max_runtime_min: Minutes of running before a new sandbox is deleted; Prized's default when `None`.
        api_key: A Prized API key; `PRIZED_API_KEY` from the environment when `None`.
        base_url: The Prized API; Prized's own when `None`.
        edge_url: The command endpoint; the one the API names for each sandbox when `None`.
    """

    def __init__(
        self,
        *,
        ref: WorkspaceRef | None = None,
        template: str = DEFAULT_TEMPLATE,
        vcpu: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
        auto_pause_min: int | None = None,
        auto_delete_min: int | None = None,
        max_runtime_min: int | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        edge_url: str | None = None,
    ) -> None:
        if ref is not None:
            _check_ref(ref)
        check_env(env)
        self._ref = ref
        self._template = template
        self._vcpu, self._memory_mb, self._disk_gb = vcpu, memory_mb, disk_gb
        self._working_dir = absolute_path('working_dir', working_dir)
        self._env = dict(env or {})
        self._labels = {**_LABELS, **dict(labels or {})}
        self._auto_pause_min = auto_pause_min
        self._auto_delete_min = auto_delete_min
        self._max_runtime_min = max_runtime_min
        self._api_key, self._base_url, self._edge_url = api_key, base_url, edge_url
        self._sandbox: AsyncSandbox | None = None
        # `working_dir()` must return a canonical absolute path, resolved once with `pwd -P`.
        self._resolved_working_dir: str | None = None
        # A sandbox this backend created still needs its `working_dir` made.
        self._working_dir_pending = False
        self._lock = anyio.Lock()
        # Set while nothing will call `aclose()`: the handle then closes when the last operation in flight ends.
        self._close_after_operation = False
        self._operations = 0

    @property
    def ref(self) -> WorkspaceRef | None:
        """Identity of the sandbox (`provider='prized'`, `id='sbx_…'`), or `None` before one is created."""
        return self._ref

    async def get_sandbox(self) -> AsyncSandbox:
        """Return a `prized.AsyncSandbox` for the sandbox, creating or attaching to it on first use.

        Use it for what the workspace API does not cover, such as `fork(count)`, preview URLs for a port, or
        extending its run time. The handle is yours: close it with `await sandbox.close()` (the sandbox keeps
        running). Attaching by `ref` to a sandbox that no longer exists raises `WorkspaceUnavailableError`; it
        never creates a replacement.
        """
        async with self._operation():
            sandbox = await self._acquire()
            async with self._mapped_errors(sandbox, 'Could not read the Prized sandbox'):
                with anyio.fail_after(_LIFECYCLE_TIMEOUT):
                    return await prized.AsyncSandbox.connect(
                        sandbox.id, api_key=self._api_key, base_url=self._base_url, edge_url=self._edge_url
                    )

    async def pause(self) -> None:
        """Pause the sandbox now, memory and running processes kept, creating or attaching on first use.

        No compute is charged while it is paused; the next command or attach resumes it where it stopped.
        """
        async with self._operation():
            sandbox = await self._acquire()
            async with self._mapped_errors(sandbox, 'Could not pause the Prized sandbox'):
                await sandbox.pause()

    async def fork(self) -> WorkspaceRef:
        """Copy the sandbox into a new one, live, and return the copy's ref, creating or attaching on first use.

        The copy has the sandbox's files, memory and running processes as they are now, and the two change
        independently after; the sandbox itself keeps running. Pass the copy's ref as `workspace=` to run
        an agent in it, and delete it with `PrizedSandbox().destroy(ref)` when you are done.
        """
        async with self._operation():
            sandbox = await self._acquire()

            async def make_copy() -> str:
                # `labels=` replaces the copy's labels, so the sandbox's own are passed on with the marker.
                labels = {**sandbox.labels, **self._labels, 'forked-from': sandbox.id}
                async with self._mapped_errors(sandbox, 'Could not fork the Prized sandbox'):
                    with anyio.fail_after(_LIFECYCLE_TIMEOUT):
                        (copy,) = await sandbox.fork(1, labels=labels)
                # Logged here, so the copy's id is recorded even if the caller was cancelled meanwhile.
                logger.info('Forked Prized sandbox %s into %s', sandbox.id, copy.id)
                await _close_quietly(copy)
                return copy.id

            copy_id = await run_to_completion(make_copy)
        return WorkspaceRef(provider=PROVIDER, id=copy_id)

    async def _acquire(self) -> AsyncSandbox:
        """The sandbox, ready for commands, created or attached on first use."""
        require_asyncio(_ASYNCIO_ONLY)
        async with self._lock:
            sandbox = self._sandbox
            if sandbox is None:
                if self._ref is None:
                    # Creation finishes even if the caller is cancelled, so a billed sandbox is never left unrecorded.
                    sandbox = await run_to_completion(self._create)
                else:
                    sandbox = await self._attach(self._ref.id)
                self._sandbox = sandbox
            if self._working_dir_pending:
                # A caller cancelled during creation stops here; the next operation makes the directory.
                await anyio.lowlevel.checkpoint_if_cancelled()
                await self._make_working_dir(sandbox)
        await anyio.lowlevel.checkpoint_if_cancelled()
        return sandbox

    async def _create(self) -> AsyncSandbox:
        try:
            sandbox = await prized.AsyncSandbox.create(
                self._template,
                vcpu=self._vcpu,
                memory_mb=self._memory_mb,
                disk_gb=self._disk_gb,
                env_vars=self._env or None,
                labels=self._labels,
                auto_pause_min=self._auto_pause_min,
                auto_delete_min=self._auto_delete_min,
                max_runtime_min=self._max_runtime_min,
                timeout=_LIFECYCLE_TIMEOUT,
                api_key=self._api_key,
                base_url=self._base_url,
                edge_url=self._edge_url,
            )
        except prized.PrizedError as error:
            if error.code in _CREDENTIAL_CODES:
                raise _credential_error(error) from error
            if error.code in _REFUSAL_CODES:
                raise WorkspaceUnavailableError(
                    f'Prized could not create the sandbox ({error.code}): {error.message}'
                ) from error
            raise
        # Recorded here, before any setup that can fail and even if the caller was cancelled meanwhile
        # (`run_to_completion` re-raises the cancellation only after this returns).
        self._sandbox = sandbox
        self._ref = WorkspaceRef(provider=PROVIDER, id=sandbox.id)
        self._working_dir_pending = self._working_dir is not None
        # Logged at creation, so the id can be found whatever ends the run before it is stored.
        logger.info('Created Prized sandbox %s', sandbox.id)
        return sandbox

    async def _attach(self, sandbox_id: str) -> AsyncSandbox:
        try:
            with anyio.fail_after(_LIFECYCLE_TIMEOUT):
                sandbox = await prized.AsyncSandbox.connect(
                    sandbox_id, api_key=self._api_key, base_url=self._base_url, edge_url=self._edge_url
                )
        except prized.PrizedError as error:
            if error.code in _CREDENTIAL_CODES:
                raise _credential_error(error) from error
            if error.code == 'not_found':
                raise _gone(
                    sandbox_id, 'was not found (it was deleted, expired, or belongs to another workspace)'
                ) from error
            raise
        if sandbox.state in _GONE_STATES:
            await _close_quietly(sandbox)
            raise _gone(sandbox_id, f'is {sandbox.state}')
        return sandbox

    async def _make_working_dir(self, sandbox: AsyncSandbox) -> None:
        directory = self._working_dir
        assert directory is not None
        with anyio.CancelScope(shield=True):
            async with self._mapped_errors(sandbox, f'Could not create working_dir {directory!r}'):
                result = await sandbox.exec(
                    shlex.join(['mkdir', '-p', '--', directory]), cwd='/', timeout=int(_REQUEST_TIMEOUT)
                )
            if result.exit_code != 0:
                raise WorkspaceError(
                    f'Could not create working_dir {directory!r} in Prized sandbox {sandbox.id}: {result.stderr.strip()}'
                )
            self._working_dir_pending = False

    async def aclose(self) -> None:
        """Release the SDK's HTTP connections. The sandbox keeps running; a later operation attaches by `ref`.

        `PrizedSandbox` calls this when the run that used the backend ends.
        """
        if not running_on_asyncio():
            return
        if self._operations:
            # An operation in flight, such as a create or a cancelled command still being stopped, needs the
            # handle: the last one to finish closes it.
            self._close_after_operation = True
            return
        if self._sandbox is None:
            return

        async def close() -> None:
            # Under the lock, so a sandbox still being created is recorded before its handle closes.
            async with self._lock:
                sandbox = self._detach()
            if sandbox is not None:
                await _close_quietly(sandbox)

        await run_to_completion(close)

    def _detach(self) -> AsyncSandbox | None:
        """Forget the handle and what was learned through it; a later operation attaches by `ref` again."""
        sandbox, self._sandbox = self._sandbox, None
        self._resolved_working_dir = None
        return sandbox

    @asynccontextmanager
    async def _operation(self) -> AsyncGenerator[None]:
        self._operations += 1
        try:
            yield
        finally:
            self._operations -= 1
            if self._operations == 0 and self._close_after_operation and self._sandbox is not None:
                # Detached before any await, so an operation starting meanwhile opens its own handle.
                sandbox = self._detach()
                assert sandbox is not None
                await run_to_completion(lambda: _close_quietly(sandbox))

    @asynccontextmanager
    async def _mapped_errors(self, sandbox: AsyncSandbox, context: str) -> AsyncGenerator[None]:
        """Raise `WorkspaceUnavailableError` for a sandbox that is gone or a refused key; let transient errors through."""
        try:
            yield
        except prized.PrizedError as error:
            if isinstance(error, prized.CommandTimeoutError):
                raise
            if error.code in _CREDENTIAL_CODES:
                raise _credential_error(error) from error
            if error.code == 'sandbox_deleted':
                raise _gone(sandbox.id, 'was deleted') from error
            if error.code in _MAYBE_GONE_CODES and (gone := await self._probe(sandbox)) is not None:
                raise gone from error
            if error.code in _REFUSAL_CODES:
                # A resume on the first command can be refused just like a create.
                raise WorkspaceUnavailableError(f'{context} ({error.code}): {error.message}') from error
            if error.code == 'spawn_failed':
                if 'argument list too long' in str(error):
                    raise WorkspaceError(
                        f'{context}: the command line is too long for Prized sandbox {sandbox.id}; '
                        'pass large data in a file instead'
                    ) from error
                if self._working_dir is not None:
                    raise WorkspaceError(
                        f'{context}: working_dir {self._working_dir!r} may not exist in Prized sandbox {sandbox.id} '
                        f'({error.message}). Create it there, or pass a working_dir that exists.'
                    ) from error
                raise WorkspaceError(f'{context}: {error.message}') from error
            raise

    async def _probe(self, sandbox: AsyncSandbox) -> WorkspaceUnavailableError | None:
        """`WorkspaceUnavailableError` if the sandbox is deleted or failed; an inconclusive lookup counts as alive."""
        with anyio.CancelScope(shield=True), anyio.move_on_after(_REQUEST_TIMEOUT):
            try:
                await sandbox.refresh()
            except prized.PrizedError as error:
                if error.code == 'not_found':
                    return _gone(sandbox.id, 'no longer exists (it was deleted or reached its run time)')
                return None
            except Exception:
                return None
            if sandbox.state in _GONE_STATES:
                return _gone(sandbox.id, f'is {sandbox.state}')
        return None

    async def working_dir(self) -> str:
        """Return the canonical directory commands start in, probed once per connection."""
        async with self._operation():
            if self._resolved_working_dir is None:
                result = await self.run(['pwd', '-P'], timeout=_REQUEST_TIMEOUT)
                printed = result.stdout.removesuffix('\n')
                if result.exit_code != 0 or not posixpath.isabs(printed):
                    raise WorkspaceError(
                        'Could not determine the working directory of the Prized sandbox: '
                        f'`pwd -P` exited {result.exit_code} and printed {result.stdout!r}.'
                    )
                self._resolved_working_dir = printed
            return self._resolved_working_dir

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        async with self._operation():
            check_timeout(timeout)
            argv = command_argv(command, shell)
            check_env(env)
            # Acquiring the sandbox has its own bounds; the timeout is the command's alone.
            sandbox = await self._acquire()
            command_env = {**self._env, **(env or {})}
            if timeout is None or timeout > _MAX_BLOCKING_SECONDS:
                return await self._run_detached(sandbox, argv, command_env, timeout)
            return await self._run_blocking(sandbox, argv, command_env, timeout)

    async def _staged(self, sandbox: AsyncSandbox, command: str) -> str:
        """`command`, or for a long one, a line that sources it from a file written with the file API.

        Sourced, so `$$` stays the edge's `bash -c`; the file removes itself before the command runs.
        """
        if len(command.encode()) <= _MAX_INLINE_COMMAND_BYTES:
            return command
        path = f'{_SCRIPT_DIR}/{uuid.uuid4().hex}.sh'
        async with self._mapped_errors(sandbox, 'Could not stage the command'):
            await sandbox.files.write(path, f'rm -f {shlex.quote(path)}\n{command}\n')
        return f'. {shlex.quote(path)}'

    async def _run_blocking(
        self, sandbox: AsyncSandbox, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> CommandResult:
        """One exec call. The line records its process group, so a cancelled call can stop it."""
        pid_file = f'{_RUN_DIR}/{uuid.uuid4().hex}.pid'
        # `$$` is the edge's `bash -c`, which leads its own process group.
        line = await self._staged(
            sandbox,
            f'{{ mkdir -p {_RUN_DIR} && printf %s "$$" > {pid_file}; }} 2>/dev/null; '
            f'{shlex.join(argv)} </dev/null; __pai_status=$?; rm -f {pid_file}; exit $__pai_status',
        )
        limit = max(1, math.ceil(timeout))

        async def stop() -> None:
            await sandbox.exec(_stop_script(pid_file), cwd='/', timeout=int(_REQUEST_TIMEOUT))

        try:
            with anyio.fail_after(limit + _ANSWER_SLACK):
                async with self._mapped_errors(sandbox, 'Could not run the command'):
                    result = await self._watched(
                        sandbox,
                        sandbox.exec(line, cwd=self._working_dir, env=env or None, timeout=limit),
                    )
        except prized.CommandTimeoutError as error:
            raise WorkspaceTimeoutError(
                f'Command timed out after {timeout:g} seconds', stdout=error.stdout or '', stderr=error.stderr or ''
            ) from error
        except TimeoutError as error:
            # The edge did not answer in time: stop the command ourselves.
            await stop_shielded(stop, grace=_REQUEST_TIMEOUT)
            raise WorkspaceTimeoutError(f'Command timed out after {timeout:g} seconds') from error
        except WorkspaceError:
            raise
        except BaseException:
            # Cancelled, or the answer was lost: the command may still be running.
            await stop_shielded(stop, grace=_REQUEST_TIMEOUT)
            raise
        if result.truncated:
            raise _too_much_output(result.stdout, result.stderr, _BLOCKING_OUTPUT_LIMIT)
        return CommandResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def _run_detached(
        self, sandbox: AsyncSandbox, argv: Sequence[str], env: Mapping[str, str], timeout: float | None
    ) -> CommandResult:
        """A long or unbounded command: started detached in its own session and polled until it exits."""
        limit = 0 if timeout is None else math.ceil(timeout)
        line = await self._staged(sandbox, f'{shlex.join(argv)} </dev/null')
        async with self._mapped_errors(sandbox, 'Could not start the command'):
            handle = await sandbox.exec(line, cwd=self._working_dir, env=env or None, timeout=limit, background=True)

        async def stop() -> None:
            if await handle.kill():
                await anyio.sleep(2)
                await handle.kill('SIGKILL')

        try:
            async with self._mapped_errors(sandbox, 'Could not wait for the command'):
                result = await handle.wait()
        except prized.CommandTimeoutError as error:
            raise WorkspaceTimeoutError(
                f'Command timed out after {timeout:g} seconds', stdout=error.stdout or '', stderr=error.stderr or ''
            ) from error
        except WorkspaceError:
            raise
        except BaseException:
            await stop_shielded(stop, grace=_REQUEST_TIMEOUT)
            raise
        with anyio.move_on_after(_REQUEST_TIMEOUT, shield=True):
            try:
                await handle.cleanup()
            except Exception:
                pass
        if result.truncated:
            raise _too_much_output(result.stdout, result.stderr, _DETACHED_OUTPUT_LIMIT)
        return CommandResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def read_bytes(self, path: str) -> bytes:
        """Read a file with the file API in one call when it is under `/home/prized`; through the shell otherwise.

        Whatever the file API refuses (not a regular file, unreadable, missing, outside the home directory) is
        read through the shell instead, which raises the builtin error the protocol promises.
        """
        async with self._operation():
            sandbox = await self._acquire()
            target = path if posixpath.isabs(path) else posixpath.join(await self.working_dir(), path)
            if target == _HOME or target.startswith(_HOME + '/'):
                try:
                    return await sandbox.files.read_bytes(target)
                except prized.PrizedError as error:
                    if error.code in _CREDENTIAL_CODES:
                        raise _credential_error(error) from error
            return await _ShellFilesystem(self).read_bytes(path)

    async def write_bytes(self, path: str, data: bytes) -> None:
        await _ShellFilesystem(self).write_bytes(path, data)

    async def stat(self, path: str) -> FileEntry:
        return await _ShellFilesystem(self).stat(path)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        return await _ShellFilesystem(self).list_dir(path)

    async def make_dir(self, path: str) -> None:
        await _ShellFilesystem(self).make_dir(path)

    async def remove(self, path: str) -> None:
        await _ShellFilesystem(self).remove(path)

    async def exists(self, path: str) -> bool:
        return await _ShellFilesystem(self).exists(path)

    async def _watched(self, sandbox: AsyncSandbox, call: Awaitable[_T]) -> _T:
        """Await an exec call while checking, every few seconds, that the sandbox still exists.

        A call in flight when its sandbox is deleted gets no answer until the HTTP timeout, so without
        this a deleted sandbox would read as a slow command.
        """
        task = asyncio.ensure_future(call)
        try:
            while True:
                done, _ = await asyncio.wait([task], timeout=_WATCH_INTERVAL)
                if done:
                    return task.result()
                if (gone := await self._probe(sandbox)) is not None:
                    raise gone
        finally:
            if not task.done():
                task.cancel()
                with anyio.CancelScope(shield=True):
                    await asyncio.wait([task])


def _stop_script(pid_file: str) -> str:
    """Stop the command whose shell's pid is in `pid_file`: TERM, then KILL after two seconds.

    The shell leads its own process group on Prized's edge, so the whole group is signalled; its process
    tree is signalled too, in case a future edge stops giving each call a group of its own (the group is
    then never the edge's: only a pid that leads its own group is signalled as one). Waits up to two
    seconds for the file, in case the cancelled call has not started yet.
    """
    quoted = shlex.quote(pid_file)
    return (
        'tree() { for c in $(ps -o pid= --ppid "$1" 2>/dev/null); do echo "$c"; tree "$c"; done; }; '
        f'f={quoted}; i=0; while [ ! -s "$f" ] && [ $i -lt 20 ]; do sleep 0.1; i=$((i+1)); done; '
        '[ -s "$f" ] || exit 0; pg=$(cat "$f"); '
        # Taken before any signal: once the shell dies its children are reparented and no tree leads to them.
        'pids="$pg $(tree "$pg")"; group=0; kill -0 -- "-$pg" 2>/dev/null && group=1; '
        'stop() { [ $group = 1 ] && kill "-$1" -- "-$pg" 2>/dev/null; for p in $pids; do kill "-$1" "$p" 2>/dev/null; done; }; '
        'alive() { [ $group = 1 ] && kill -0 -- "-$pg" 2>/dev/null && return 0; for p in $pids; do kill -0 "$p" 2>/dev/null && return 0; done; return 1; }; '
        'stop TERM; i=0; while alive && [ $i -lt 20 ]; do sleep 0.1; i=$((i+1)); done; '
        'stop KILL; rm -f "$f"; exit 0'
    )
