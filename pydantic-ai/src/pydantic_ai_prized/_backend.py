"""A Prized sandbox behind Pydantic AI's `WorkspaceBackend` protocol.

Commands run through the sandbox's exec endpoint as the user `prized`. File operations are derived
from commands by Pydantic AI's `Workspace` (base64 over the shell), so they work on every path the
user can reach, not only under `/home/prized`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import posixpath
import shlex
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, TypeVar

import anyio
import anyio.lowlevel

# _core is the SDK's internal module: _blocking_exec below needs its exec body and timeout helpers,
# so pyproject.toml pins prized to the 0.1 series.
from prized import AsyncSandbox, ExecResult, PrizedError
from prized import _core as _sdk_core
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    CommandResult,
    SupportsCommands,
    WorkspaceBackend,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

if TYPE_CHECKING:
    from pydantic_ai.workspaces import WorkspaceCommand

__all__ = ('PROVIDER', 'PrizedSandboxBackend', 'destroy_sandbox')

logger = logging.getLogger(__name__)

PROVIDER = 'prized'
"""The `WorkspaceRef.provider` of a Prized sandbox."""

DEFAULT_TEMPLATE = 'prized-base-v1'

# One exec call runs a command for up to an hour (the edge's cap); a command with a longer timeout runs
# detached in the sandbox and is polled. A command without a timeout gets the hour.
_MAX_BLOCKING_SECONDS = 3600
# How long one command runs when the caller sets no timeout: the edge's ceiling for a single call is
# an hour, and a detached command has none, so `None` means no limit.
_CREATE_TIMEOUT = 180.0
_REQUEST_TIMEOUT = 30.0
# Past the command's own limit, how long to wait for the edge to answer before stopping it ourselves.
_ANSWER_SLACK = 15.0
_STOP_GRACE = 5.0
# How often a command in flight checks that its sandbox still exists.
_WATCH_INTERVAL = 5.0
# The edge keeps the first 4 MiB of each stream of a single call.
_BLOCKING_OUTPUT_LIMIT = 4 * 1024 * 1024
_DETACHED_OUTPUT_LIMIT = 64 * 1024 * 1024
_RUN_DIR = '/tmp/pydantic-ai-prized'

_CREDENTIAL_CODES = frozenset(
    {'missing_api_key', 'invalid_api_key', 'unauthorized', 'token_expired', 'key_access_denied'}
)
# Of those, the ones a person with no account meets (no key, a placeholder, a refused key): an agent can sign
# its person up.
_NO_KEY_CODES = frozenset({'missing_api_key', 'invalid_api_key', 'unauthorized'})
_SIGN_UP_HINT = (
    'No Prized account yet? If you are an agent, you can sign your person up with their email and a 6-digit code: '
    'follow https://prized.dev/auth.md?via=pydantic-ai'
)
# Refusals a retry cannot fix: the sandbox cannot be created or resumed as asked.
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
    }
)
# Answers that may mean the sandbox is gone; a lookup decides.
_MAYBE_GONE_CODES = frozenset(
    {'not_found', 'sandbox_not_running', 'sandbox_unreachable', 'sandbox_error', 'sandbox_deleted', 'network_error'}
)
_GONE_STATES = frozenset({'deleting', 'deleted', 'error'})

_T = TypeVar('_T')


def _running_on_asyncio() -> bool:
    try:
        import sniffio
    except ModuleNotFoundError:  # pragma: no cover
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True
    try:
        return sniffio.current_async_library() == 'asyncio'
    except sniffio.AsyncLibraryNotFoundError:
        return False


def _require_asyncio() -> None:
    if not _running_on_asyncio():
        raise UserError('PrizedSandbox needs the asyncio event loop: the Prized client runs on asyncio.')


async def _run_to_completion(call: Callable[[], Awaitable[_T]]) -> _T:
    """Await `call` in its own task and let it finish even if the caller is cancelled meanwhile.

    Used for a create, so a sandbox that is being billed is always recorded; the caller's
    cancellation is re-raised once `call` has finished.
    """
    task = asyncio.ensure_future(call())
    cancelled: BaseException | None = None
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.wait([task])
            except asyncio.CancelledError as error:
                cancelled = error
    if cancelled is not None:
        task.exception()
        raise cancelled
    return task.result()


_pending_stops: set[asyncio.Task[None]] = set()


async def _stop_shielded(stop: Callable[[], Awaitable[object]], grace: float = _STOP_GRACE) -> None:
    """Run `stop` to completion (bounded by `grace`) even while the caller is being cancelled."""

    async def bounded() -> None:
        with anyio.move_on_after(grace, shield=True):
            try:
                await stop()
            except Exception as error:
                logger.warning('Could not stop a command in the Prized sandbox: %r', error)

    # A task of its own: a native `Task.cancel()` would cut through an AnyIO shield.
    task = asyncio.ensure_future(bounded())
    _pending_stops.add(task)
    task.add_done_callback(_pending_stops.discard)
    with anyio.move_on_after(grace + 1.0, shield=True):
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass


def _command_argv(command: WorkspaceCommand, shell: bool) -> list[str]:
    """The argv that runs `command`, with the same `shell` rules as Pydantic AI's local backend."""
    if isinstance(command, str):
        if not shell:
            raise TypeError('a string command requires shell=True; pass an argv sequence otherwise')
        return ['/bin/sh', '-c', command]
    if isinstance(command, (bytes, bytearray)):
        raise TypeError('a bytes command is not supported; pass a string or argv sequence')
    if shell:
        raise TypeError('an argv sequence cannot be combined with shell=True; pass a single command string')
    argv = list(command)
    if not argv:
        raise TypeError('an argv sequence needs at least the program to run')
    for argument in argv:
        if type(argument) is not str:
            raise TypeError('argv elements must be strings')
        if '\x00' in argument:
            raise ValueError('argv elements must not contain NUL bytes')
    return argv


def _check_timeout(timeout: float | None) -> None:
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError(f'timeout must be a positive finite number or None, got {timeout!r}.')


def _check_env(env: Mapping[str, str] | None) -> dict[str, str]:
    if env is None:
        return {}
    if any(type(key) is not str or type(value) is not str for key, value in env.items()):
        raise TypeError('env keys and values must be strings')
    return dict(env)


def _credential_error(error: PrizedError) -> WorkspaceUnavailableError:
    # The SDK never quotes the key back; the code alone is safe to show.
    return WorkspaceUnavailableError(
        f'Prized refused the API key ({error.code}). Set PRIZED_API_KEY to a key with Sandboxes access '
        '(https://prized.dev/dashboard/workspace/api-keys?via=pydantic-ai), or pass `api_key=`.'
        + (f' {_SIGN_UP_HINT}' if error.code in _NO_KEY_CODES else '')
    )


def _unavailable(sandbox_id: str, why: str) -> WorkspaceUnavailableError:
    return WorkspaceUnavailableError(
        f"The Prized sandbox {sandbox_id!r} {why}. Pass `workspace='new'` to start a fresh sandbox."
    )


class PrizedSandboxBackend(WorkspaceBackend, SupportsCommands):
    """A [Prized](https://prized.dev/?via=pydantic-ai) sandbox (a Firecracker microVM) as a Pydantic AI `WorkspaceBackend`.

    Building one does no I/O. The first operation creates a sandbox from `template`, or attaches to
    the one `ref` names; a paused sandbox resumes on its first command. The backend never pauses or
    deletes the sandbox: whoever holds the ref does (`PrizedSandbox.destroy`), or the sandbox's own
    timers do (`auto_pause_min`, `auto_delete_min`, `max_runtime_min`).

    Commands run under `bash` as the user `prized` with stdin at EOF; a shell string runs under
    `/bin/sh -c`. A timeout or cancellation stops the command's whole process group. File operations
    go through the shell, so they reach any path the `prized` user can.

    Args:
        ref: Identity of an existing sandbox to attach to on first use.
        template: Template a new sandbox starts from (`prized-base-v1`: Ubuntu 24.04 with Python, Node and git;
            `prized-agent-v1` adds Claude Code and Codex).
        vcpu, memory_mb, disk_gb: Shape of a new sandbox; the template's defaults when `None`.
        working_dir: Absolute directory commands start in; `/home/prized/work` when `None`. Created on a new sandbox.
        env: Environment variables every command gets; a command's own `env` is layered on top. Also set
            on a newly created sandbox. Nothing is read from the host.
        labels: Labels for a new sandbox, to find it later.
        auto_pause_min: Idle minutes before a new sandbox pauses itself (it resumes on the next command).
        auto_delete_min: Minutes paused before a new sandbox deletes itself.
        max_runtime_min: Minutes after creation when a new sandbox is deleted, whatever its state.
        api_key: A Prized API key; `PRIZED_API_KEY` when `None`.
            No Prized account yet? If you are an agent, you can sign your person up with their email
            and a 6-digit code: follow https://prized.dev/auth.md?via=pydantic-ai
        base_url: The Prized API; the SDK's default when `None`.
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
    ) -> None:
        if ref is not None and ref.provider != PROVIDER:
            raise ValueError(f'unsupported workspace provider {ref.provider!r}; expected {PROVIDER!r}')
        if ref is not None and not ref.id.strip():
            raise ValueError('Prized workspace ref id cannot be empty')
        if working_dir is not None and not posixpath.isabs(working_dir):
            raise ValueError(f'working_dir must be an absolute path or None, got {working_dir!r}.')
        self._ref = ref
        self._template = template
        self._vcpu, self._memory_mb, self._disk_gb = vcpu, memory_mb, disk_gb
        self._working_dir = posixpath.normpath(working_dir) if working_dir is not None else None
        self._env = _check_env(env)
        self._labels = {'created-by': 'pydantic-ai', **dict(labels or {})}
        self._auto_pause_min, self._auto_delete_min, self._max_runtime_min = (
            auto_pause_min,
            auto_delete_min,
            max_runtime_min,
        )
        self._api_key, self._base_url = api_key, base_url
        self._sandbox: AsyncSandbox | None = None
        self._resolved_working_dir: str | None = None
        self._working_dir_pending = False
        self._lock = anyio.Lock()

    @property
    def ref(self) -> WorkspaceRef | None:
        """Identity of the sandbox (`provider='prized'`, `id='sbx_…'`), or `None` before one is created."""
        return self._ref

    async def get_sandbox(self) -> AsyncSandbox:
        """Return the `prized.AsyncSandbox`, creating or attaching to it on first use.

        Attaching by `ref` to a sandbox that no longer exists raises `WorkspaceUnavailableError`; it
        never creates a replacement.
        """
        _require_asyncio()
        async with self._lock:
            sandbox = self._sandbox
            if sandbox is None:
                sandbox = await self._acquire_locked()
            if self._working_dir_pending:
                await anyio.lowlevel.checkpoint_if_cancelled()
                await self._make_working_dir(sandbox)
        await anyio.lowlevel.checkpoint_if_cancelled()
        return sandbox

    async def _acquire_locked(self) -> AsyncSandbox:
        if self._ref is not None:
            sandbox = await self._attach(self._ref.id)
            self._sandbox = sandbox
            return sandbox

        async def create() -> AsyncSandbox:
            created = await self._create()
            # Recorded before any setup that can fail, and never changed afterwards.
            self._sandbox = created
            self._ref = WorkspaceRef(provider=PROVIDER, id=created.id)
            self._working_dir_pending = self._working_dir is not None
            logger.info('Created Prized sandbox %s', created.id)
            return created

        # A create finishes even if the caller is cancelled, so a sandbox is never left unrecorded.
        return await _run_to_completion(create)

    async def _create(self) -> AsyncSandbox:
        try:
            return await AsyncSandbox.create(
                self._template,
                vcpu=self._vcpu,
                memory_mb=self._memory_mb,
                disk_gb=self._disk_gb,
                env_vars=self._env or None,
                labels=self._labels,
                auto_pause_min=self._auto_pause_min,
                auto_delete_min=self._auto_delete_min,
                max_runtime_min=self._max_runtime_min,
                timeout=_CREATE_TIMEOUT,
                api_key=self._api_key,
                base_url=self._base_url,
            )
        except PrizedError as error:
            if error.code in _CREDENTIAL_CODES:
                raise _credential_error(error) from error
            if error.code in _REFUSAL_CODES:
                raise WorkspaceUnavailableError(
                    f'Prized could not create the sandbox ({error.code}): {error.message}'
                ) from error
            raise

    async def _attach(self, sandbox_id: str) -> AsyncSandbox:
        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self._api_key, base_url=self._base_url)
        except PrizedError as error:
            if error.code in _CREDENTIAL_CODES:
                raise _credential_error(error) from error
            if error.code == 'not_found':
                raise _unavailable(
                    sandbox_id, 'was not found (it was deleted, expired, or belongs to another workspace)'
                ) from error
            raise
        if sandbox.state in _GONE_STATES:
            await sandbox.close()
            raise _unavailable(sandbox_id, f'is {sandbox.state}')
        return sandbox

    async def _make_working_dir(self, sandbox: AsyncSandbox) -> None:
        directory = self._working_dir
        assert directory is not None
        with anyio.CancelScope(shield=True):
            async with self._mapped_errors(sandbox, f'Could not create working_dir {directory!r}'):
                result = await sandbox.exec(f'mkdir -p -- {shlex.quote(directory)}', timeout=_REQUEST_TIMEOUT)
            if result.exit_code != 0:
                raise WorkspaceError(
                    f'Could not create working_dir {directory!r} in Prized sandbox {sandbox.id}: {result.stderr.strip()}'
                )
            self._working_dir_pending = False

    async def aclose(self) -> None:
        """Release the SDK's HTTP connections. The sandbox keeps running; a later operation attaches by `ref`."""
        async with self._lock:
            sandbox, self._sandbox = self._sandbox, None
        if sandbox is not None:
            with anyio.move_on_after(_REQUEST_TIMEOUT, shield=True):
                try:
                    await sandbox.close()
                except Exception as error:  # pragma: no cover
                    logger.warning('Could not close the Prized SDK client: %r', error)

    @asynccontextmanager
    async def _mapped_errors(self, sandbox: AsyncSandbox, context: str) -> AsyncIterator[None]:
        """Raise `WorkspaceUnavailableError` for a sandbox that is gone or a refused key; let transient errors through."""
        try:
            yield
        except PrizedError as error:
            if error.code in _CREDENTIAL_CODES:
                raise _credential_error(error) from error
            if error.code in _MAYBE_GONE_CODES and (gone := await self._probe(sandbox)) is not None:
                raise gone from error
            if error.code in _REFUSAL_CODES:
                # A resume on the first command can be refused just like a create.
                raise WorkspaceUnavailableError(f'{context} ({error.code}): {error.message}') from error
            if error.code == 'spawn_failed':
                raise WorkspaceError(
                    f'{context}: the command could not start in Prized sandbox {sandbox.id} '
                    f'(does working_dir {self._working_dir!r} exist?)'
                ) from error
            raise

    async def _probe(self, sandbox: AsyncSandbox) -> WorkspaceUnavailableError | None:
        """`WorkspaceUnavailableError` if the sandbox is deleted or failed; an inconclusive lookup counts as alive."""
        with anyio.CancelScope(shield=True), anyio.move_on_after(_REQUEST_TIMEOUT):
            try:
                await sandbox.refresh()
            except PrizedError as error:
                if error.code == 'not_found':
                    return _unavailable(sandbox.id, 'no longer exists (it was deleted or reached its max runtime)')
                return None
            except Exception:
                return None
            if sandbox.state in _GONE_STATES:
                return _unavailable(sandbox.id, f'is {sandbox.state}')
        return None

    async def working_dir(self) -> str:
        """The canonical directory commands start in, resolved once with `pwd -P`."""
        if self._resolved_working_dir is None:
            sandbox = await self.get_sandbox()
            async with self._mapped_errors(sandbox, 'Could not determine the working directory'):
                result = await sandbox.exec('pwd -P', cwd=self._working_dir, timeout=_REQUEST_TIMEOUT)
            printed = result.stdout.removesuffix('\n')
            if result.exit_code != 0 or not posixpath.isabs(printed):
                raise WorkspaceError(
                    f'Could not determine the working directory of Prized sandbox {sandbox.id}: {result.stderr}'
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
        _check_timeout(timeout)
        argv = _command_argv(command, shell)
        command_env = {**self._env, **_check_env(env)}
        cwd = await self.working_dir()
        # Acquisition has its own bounds; the timeout is the command's alone.
        sandbox = await self.get_sandbox()
        if timeout is not None and timeout > _MAX_BLOCKING_SECONDS:
            return await self._run_detached(sandbox, argv, cwd, command_env, timeout)
        return await self._run_blocking(sandbox, argv, cwd, command_env, timeout)

    async def _run_blocking(
        self, sandbox: AsyncSandbox, argv: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float | None
    ) -> CommandResult:
        """One exec call. The line records its process group, so a cancelled call can stop it."""
        pid_file = f'{_RUN_DIR}/{uuid.uuid4().hex}.pid'
        # `$$` is the edge's `bash -c`, which leads its own process group.
        line = (
            f'mkdir -p {_RUN_DIR} && printf %s "$$" > {pid_file}; '
            f'{shlex.join(argv)}; __pai_status=$?; rm -f {pid_file}; exit $__pai_status'
        )
        limit = _MAX_BLOCKING_SECONDS if timeout is None else max(1, math.ceil(timeout))
        described = (
            f'{timeout:g} seconds'
            if timeout is not None
            else f'the {_MAX_BLOCKING_SECONDS} s limit of one Prized command'
        )

        async def stop() -> None:
            await sandbox.exec(_stop_script(pid_file), timeout=_REQUEST_TIMEOUT)

        try:
            with anyio.fail_after(limit + _ANSWER_SLACK):
                async with self._mapped_errors(sandbox, 'Could not run the command'):
                    result = await self._exec_watched(sandbox, _blocking_exec(sandbox, line, cwd, env, limit))
        except TimeoutError as error:
            # The edge did not answer in time: stop the command ourselves.
            await _stop_shielded(stop)
            raise WorkspaceTimeoutError(f'Command timed out after {described}') from error
        except WorkspaceUnavailableError:
            raise
        except BaseException:
            # Cancelled, or the answer was lost: the command may still be running.
            await _stop_shielded(stop)
            raise
        if result.timed_out:
            raise WorkspaceTimeoutError(
                f'Command timed out after {described}', stdout=result.stdout, stderr=result.stderr
            )
        if result.truncated:
            raise WorkspaceOutputLimitError(
                'Prized command output exceeded 4 MiB per stream; redirect it to a file and read part of it instead',
                limit=_BLOCKING_OUTPUT_LIMIT,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return CommandResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def _exec_watched(self, sandbox: AsyncSandbox, call: Awaitable[_T]) -> _T:
        """Await an exec call while checking, every few seconds, that the sandbox still exists.

        A call in flight when its sandbox is deleted gets no answer until the HTTP timeout, so
        without this a deleted sandbox would read as a slow command.
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

    async def _run_detached(
        self, sandbox: AsyncSandbox, argv: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float | None
    ) -> CommandResult:
        """A long or unbounded command: started detached in its own session and polled until it exits."""
        limit = 0 if timeout is None else math.ceil(timeout)
        async with self._mapped_errors(sandbox, 'Could not start the command'):
            handle = await sandbox.exec(shlex.join(argv), cwd=cwd, env=env or None, timeout=limit, background=True)

        async def stop() -> None:
            if await handle.kill():
                await anyio.sleep(2)
                await handle.kill('SIGKILL')

        try:
            async with self._mapped_errors(sandbox, 'Could not wait for the command'):
                result = await handle.wait()
        except WorkspaceUnavailableError:
            raise
        except BaseException:
            await _stop_shielded(stop)
            raise
        with anyio.move_on_after(_REQUEST_TIMEOUT, shield=True):
            try:
                await handle.cleanup()
            except Exception:
                pass
        if result.timed_out:
            raise WorkspaceTimeoutError(
                f'Command timed out after {timeout:g} seconds', stdout=result.stdout, stderr=result.stderr
            )
        if result.truncated:
            raise WorkspaceOutputLimitError(
                'Prized command output exceeded 64 Mi characters per stream; redirect it to a file instead',
                limit=_DETACHED_OUTPUT_LIMIT,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return CommandResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)


async def _blocking_exec(sandbox: AsyncSandbox, line: str, cwd: str, env: Mapping[str, str], limit: int) -> ExecResult:
    """One exec call answered when the command ends, for any limit up to the edge's hour.

    `AsyncSandbox.exec` detaches commands over 240 s and polls them; that costs extra calls per
    command and caps the command's length, so this sends the single call directly.
    """
    body = _sdk_core.exec_body(line, cwd, env or None, limit)
    return await sandbox._exec(body, _sdk_core.EXEC, _sdk_core.exec_http_timeout(limit))


def _stop_script(pid_file: str) -> str:
    """Stop the process group named in `pid_file`: TERM, then KILL after two seconds.

    Waits up to two seconds for the file, in case the cancelled call has not started yet.
    """
    quoted = shlex.quote(pid_file)
    return (
        f'f={quoted}; i=0; while [ ! -s "$f" ] && [ $i -lt 20 ]; do sleep 0.1; i=$((i+1)); done; '
        '[ -s "$f" ] || exit 0; pg=$(cat "$f"); '
        'kill -TERM -- "-$pg" 2>/dev/null; i=0; '
        'while kill -0 -- "-$pg" 2>/dev/null && [ $i -lt 20 ]; do sleep 0.1; i=$((i+1)); done; '
        'kill -KILL -- "-$pg" 2>/dev/null; rm -f "$f"; exit 0'
    )


async def destroy_sandbox(sandbox_id: str, *, api_key: str | None = None, base_url: str | None = None) -> None:
    """Delete a Prized sandbox by id, in any state. A sandbox that is already gone returns quietly."""
    _require_asyncio()
    if not sandbox_id.strip():
        raise ValueError('Prized workspace ref id cannot be empty')
    try:
        sandbox = await AsyncSandbox.connect(sandbox_id, api_key=api_key, base_url=base_url)
    except PrizedError as error:
        if error.code == 'not_found':
            return
        if error.code in _CREDENTIAL_CODES:
            raise _credential_error(error) from error
        raise
    try:
        await sandbox.delete()
    except PrizedError as error:
        if error.code != 'not_found':
            raise
    finally:
        await sandbox.close()
