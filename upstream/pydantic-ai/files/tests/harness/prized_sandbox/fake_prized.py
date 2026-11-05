"""A stand-in for the Prized SDK's `AsyncSandbox`, for tests that must not reach Prized.

Each fake sandbox is a directory under `host_root`, and its commands run there with the host's
`bash`, in their own session, as Prized's edge runs them: a cancelled call leaves its command
running, exactly as a lost answer does on the real edge, so the backend must stop it itself.
The real `prized` module stays in place for its error and result types.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import prized
from prized import CommandTimeoutError, ExecResult, PrizedError


@dataclass
class _Record:
    id: str
    root: Path
    labels: dict[str, str]
    env_vars: dict[str, str]
    state: str = 'running'
    pauses: int = 0
    resumes: int = 0
    procs: set[asyncio.subprocess.Process] = field(default_factory=lambda: set[asyncio.subprocess.Process]())


class FakePrized:
    """The fake's control surface: its sandboxes, and switches for the failures tests need."""

    def __init__(self) -> None:
        self.host_root: Path | None = None
        self.sandboxes: dict[str, _Record] = {}
        self.created: list[str] = []
        self.refuse_create: str | None = None
        self.bad_keys: set[str] = {'dcp_rejected'}
        self.calls: list[tuple[str, str]] = []

    def record(self, sandbox_id: str) -> _Record:
        return self.sandboxes[sandbox_id]

    def delete_behind_backs(self, sandbox_id: str) -> None:
        """Delete a sandbox as its run time ending would: commands in flight die with it."""
        record = self.sandboxes[sandbox_id]
        record.state = 'deleted'
        for proc in list(record.procs):
            _kill_group(proc, signal.SIGKILL)

    async def reap(self) -> None:
        for record in self.sandboxes.values():
            for proc in list(record.procs):
                _kill_group(proc, signal.SIGKILL)
                with _suppress():
                    await proc.wait()

    # The class the backend calls, bound to this controller.
    @property
    def sandbox_class(self) -> type[FakeAsyncSandbox]:
        controller = self

        class _Bound(FakeAsyncSandbox):
            fake = controller

        return _Bound


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def _kill_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _check_key(fake: FakePrized, api_key: str | None) -> None:
    key = api_key if api_key is not None else os.environ.get('PRIZED_API_KEY', 'dcp_fake')
    if not key:
        raise PrizedError('missing_api_key')
    if key in fake.bad_keys:
        raise PrizedError('unauthorized', status=401)


class FakeCommandHandle:
    def __init__(
        self, sandbox: FakeAsyncSandbox, proc: asyncio.subprocess.Process, outputs: tuple[Path, Path], timeout: int
    ) -> None:
        self._sandbox, self._proc, self._outputs, self._timeout = sandbox, proc, outputs, timeout
        self._result: ExecResult | None = None

    async def wait(self) -> ExecResult:
        if self._result is None:
            self._result = await self._sandbox.collect(self._proc, self._outputs, self._timeout)
        return self._result

    async def kill(self, signal_name: str = 'SIGTERM') -> bool:
        if self._proc.returncode is not None:
            return False
        _kill_group(self._proc, getattr(signal, signal_name))
        return True

    async def cleanup(self) -> None:
        return None


# The edge wraps a detached or `full_output` command line, and refuses one this long (Linux's 128 KiB cap on
# one argument, after the wrapper's encoding).
WRAPPED_COMMAND_LIMIT = 64 * 1024


class FakeFiles:
    """`sandbox.files`: writes land on the host, and only under `host_root` (the edge's `/home/prized`)."""

    def __init__(self, sandbox: FakeAsyncSandbox) -> None:
        self._sandbox = sandbox

    async def write(self, path: str, data: str | bytes, mode: int | None = None) -> None:
        self._sandbox.live()
        root = self._sandbox.fake.host_root
        assert root is not None
        target = Path(path)
        if not target.is_relative_to(root):
            raise PrizedError('path_outside_root', status=403, sandbox_id=self._sandbox.id)
        payload = data.encode() if isinstance(data, str) else data

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

        await asyncio.to_thread(write)

    async def read_bytes(self, path: str) -> bytes:
        """As the edge's file API: follows symlinks, honors permissions, and refuses what isn't a regular file."""
        self._sandbox.fake.calls.append(('read', path))
        self._sandbox.live()
        root = self._sandbox.fake.host_root
        assert root is not None
        target = Path(path)
        if not target.is_relative_to(root):
            raise PrizedError('path_outside_root', status=403, sandbox_id=self._sandbox.id)

        def read() -> bytes:
            mode = target.stat().st_mode  # raises FileNotFoundError, NotADirectoryError, PermissionError
            if not stat.S_ISREG(mode):
                raise IsADirectoryError(path)
            return target.read_bytes()

        try:
            return await asyncio.to_thread(read)
        except (FileNotFoundError, NotADirectoryError):
            raise PrizedError('not_found', status=404, sandbox_id=self._sandbox.id) from None
        except IsADirectoryError:
            raise PrizedError('not_regular_file', status=400, sandbox_id=self._sandbox.id) from None
        except PermissionError:
            raise PrizedError('permission_denied', status=403, sandbox_id=self._sandbox.id) from None


class FakeAsyncSandbox:
    fake: FakePrized

    def __init__(self, record: _Record) -> None:
        self._record = record
        self.id = record.id
        self.state = record.state
        self.labels = dict(record.labels)
        self.closed = False
        self.files = FakeFiles(self)

    @classmethod
    async def create(
        cls,
        template: str = 'prized-base-v1',
        *,
        env_vars: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
        api_key: str | None = None,
        **_: object,
    ) -> FakeAsyncSandbox:
        _check_key(cls.fake, api_key)
        if cls.fake.refuse_create is not None:
            raise PrizedError(cls.fake.refuse_create, status=402)
        if template not in ('prized-base-v1', 'prized-agent-v1'):
            raise PrizedError('template_not_found', status=404)
        assert cls.fake.host_root is not None, 'set fake.host_root first'
        sandbox_id = f'sbx_{uuid.uuid4().hex[:16]}'
        root = cls.fake.host_root / sandbox_id
        # Off the event loop: the suite's blockbuster refuses blocking file calls in async code.
        await asyncio.to_thread((root / 'work').mkdir, parents=True)
        record = _Record(sandbox_id, root, dict(labels or {}), dict(env_vars or {}))
        cls.fake.sandboxes[sandbox_id] = record
        cls.fake.created.append(sandbox_id)
        return cls(record)

    @classmethod
    async def connect(cls, sandbox_id: str, *, api_key: str | None = None, **_: object) -> FakeAsyncSandbox:
        _check_key(cls.fake, api_key)
        record = cls.fake.sandboxes.get(sandbox_id)
        if record is None or record.state == 'deleted':
            raise PrizedError('not_found', status=404, sandbox_id=sandbox_id)
        return cls(record)

    def live(self) -> _Record:
        if self.closed:
            raise prized.HandleClosedError('handle is closed', sandbox_id=self.id)
        record = self._record
        if record.state == 'deleted':
            raise PrizedError('not_found', status=404, sandbox_id=self.id)
        if record.state == 'paused':
            record.state = 'running'
            record.resumes += 1
        self.state = record.state
        return record

    async def refresh(self) -> FakeAsyncSandbox:
        if self._record.state == 'deleted':
            raise PrizedError('not_found', status=404, sandbox_id=self.id)
        self.state = self._record.state
        return self

    async def _spawn(
        self, command: str, cwd: str | None, env: Mapping[str, str] | None
    ) -> tuple[asyncio.subprocess.Process, tuple[Path, Path]]:
        record = self.live()
        directory = cwd if cwd is not None else str(record.root / 'work')
        if not await asyncio.to_thread(os.path.isdir, directory):
            raise PrizedError('spawn_failed', status=400, sandbox_id=self.id)
        full_env = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(record.root),
            **record.env_vars,
            **(env or {}),
        }
        # Output goes to files, as on the edge: the command's exit is what ends the call, not the
        # pipes closing, which a background child that inherited them would hold open.
        logs = record.root / '.fake-runs'
        name = uuid.uuid4().hex
        out_path, err_path = logs / f'{name}.out', logs / f'{name}.err'

        def open_logs() -> tuple[int, int]:
            logs.mkdir(exist_ok=True)
            return os.open(out_path, os.O_WRONLY | os.O_CREAT, 0o600), os.open(
                err_path, os.O_WRONLY | os.O_CREAT, 0o600
            )

        out_fd, err_fd = await asyncio.to_thread(open_logs)
        try:
            proc = await asyncio.create_subprocess_exec(
                'bash',
                '-c',
                command,
                cwd=directory,
                env=full_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out_fd,
                stderr=err_fd,
                start_new_session=True,
            )
        finally:
            os.close(out_fd)
            os.close(err_fd)
        record.procs.add(proc)
        return proc, (out_path, err_path)

    async def collect(self, proc: asyncio.subprocess.Process, outputs: tuple[Path, Path], timeout: int) -> ExecResult:
        """Wait for the command itself, as the edge does. Shielded, so a cancelled caller leaves it running."""
        out_path, err_path = outputs

        async def output() -> tuple[str, str]:
            stdout = await asyncio.to_thread(out_path.read_bytes)
            stderr = await asyncio.to_thread(err_path.read_bytes)
            return stdout.decode(errors='replace'), stderr.decode(errors='replace')

        waited = asyncio.ensure_future(proc.wait())
        try:
            await asyncio.wait_for(asyncio.shield(waited), timeout or None)
        except TimeoutError:
            _kill_group(proc, signal.SIGKILL)
            await waited
            stdout, stderr = await output()
            raise CommandTimeoutError(
                ExecResult(stdout, stderr, -1, timed_out=True), timeout, sandbox_id=self.id
            ) from None
        finally:
            if waited.done():
                self._record.procs.discard(proc)
        if self._record.state == 'deleted':
            raise PrizedError('sandbox_deleted', status=410, sandbox_id=self.id)
        stdout, stderr = await output()
        code = proc.returncode if proc.returncode is not None and proc.returncode >= 0 else 128 - (proc.returncode or 0)
        return ExecResult(stdout, stderr, code)

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        background: bool = False,
        full_output: bool = False,
        **_: object,
    ) -> ExecResult | FakeCommandHandle:
        self.fake.calls.append(('exec', self.id))
        if (background or full_output) and len(command.encode()) > WRAPPED_COMMAND_LIMIT:
            raise PrizedError(
                'spawn_failed', 'fork/exec /bin/bash: argument list too long', status=400, sandbox_id=self.id
            )
        proc, outputs = await self._spawn(command, cwd, env)
        limit = 60 if timeout is None else timeout
        if background:
            return FakeCommandHandle(self, proc, outputs, limit)
        return await self.collect(proc, outputs, limit)

    async def fork(
        self, count: int = 1, *, labels: Mapping[str, str] | None = None, **_: object
    ) -> list[FakeAsyncSandbox]:
        record = self.live()
        copies: list[FakeAsyncSandbox] = []
        for _i in range(count):
            sandbox_id = f'sbx_{uuid.uuid4().hex[:16]}'
            assert self.fake.host_root is not None
            root = self.fake.host_root / sandbox_id
            await asyncio.to_thread(shutil.copytree, record.root, root, symlinks=True)
            copy = _Record(
                sandbox_id, root, dict(labels if labels is not None else record.labels), dict(record.env_vars)
            )
            self.fake.sandboxes[sandbox_id] = copy
            self.fake.created.append(sandbox_id)
            copies.append(type(self)(copy))
        return copies

    async def pause(self, **_: object) -> None:
        record = self.live()
        record.state = 'paused'
        record.pauses += 1
        self.state = 'paused'

    async def resume(self, **_: object) -> None:
        self.live()

    async def delete(self, **_: object) -> None:
        if self._record.state == 'deleted':
            return
        self.fake.delete_behind_backs(self.id)
        self.state = 'deleted'

    async def close(self) -> None:
        self.closed = True
