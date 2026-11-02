"""verifiers.v1.runtimes, reduced to what a custom runtime plugin meets: the backend contract and
its defaults (base.py), the Runtime view (runtime.py), the plugin loader (backends/custom.py) and
provision_runtime (__init__.py)."""

import asyncio
import atexit
import base64
import contextlib
import importlib
import shlex
import uuid
import weakref
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from verifiers.v1.configs.runtime import CustomRuntimeConfig
from verifiers.v1.errors import SandboxError


@dataclass(frozen=True)
class ProgramResult:
    exit_code: int
    stdout: str
    stderr: str


class RuntimeProcess(ABC):
    stdout: AsyncIterator[bytes]
    stderr: AsyncIterator[bytes]

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def wait(self) -> int: ...

    @abstractmethod
    async def poll(self) -> int | None: ...

    @abstractmethod
    async def terminate(self) -> None: ...

    @abstractmethod
    async def kill(self) -> None: ...


class BaseRuntimeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    borrowed: bool = False


class CustomRuntimeInfo(BaseRuntimeInfo):
    model_config = ConfigDict(extra="allow")
    type: str = "custom"
    import_path: str


async def _shielded(coro):
    task = asyncio.ensure_future(coro)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as e:
            cancelled = e
        except BaseException:
            pass
    if cancelled is not None:
        raise cancelled
    return task.result()


_LIVE: "weakref.WeakSet[RuntimeBackend]" = weakref.WeakSet()


def _cleanup_at_exit() -> None:
    for backend in list(_LIVE):
        with contextlib.suppress(Exception):
            backend.cleanup()


atexit.register(_cleanup_at_exit)


class RuntimeBackend(ABC):
    is_local: bool = False
    SUPPORTS_TASK_IMAGES: ClassVar[bool] = False
    SUPPORTS_USER_SWITCHING: ClassVar[bool] = False
    SUPPORTS_PORT_EXPOSURE: ClassVar[bool] = False
    SUPPORTS_COMPOSE: ClassVar[bool] = False
    scripts_dir: ClassVar[str] = "/tmp/vf-scripts"

    def __init__(self, name: str | None = None) -> None:
        self.name = name or f"vf-{uuid.uuid4().hex[:12]}"
        self.env: dict[str, str] = {}
        self._background: list[RuntimeProcess] = []
        self.stopped = False

    def bind(self, *, env: Mapping[str, str] | None = None) -> "Runtime":
        return Runtime(self, env=self.env if env is None else env)

    @property
    def type(self) -> str:
        return self.config.type

    @abstractmethod
    async def start(self) -> None: ...

    async def stop(self) -> None:
        self.stopped = True
        await _shielded(self.teardown())

    async def teardown(self) -> None:
        try:
            for process in self._background:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(5):
                        await process.kill()
                        await process.wait()
            self._background.clear()
        finally:
            await asyncio.to_thread(self.cleanup)

    def cleanup(self) -> None:
        pass

    @abstractmethod
    async def exec(self, runtime: "Runtime", argv: list[str], env: dict[str, str]) -> RuntimeProcess: ...

    async def run(self, runtime: "Runtime", argv: list[str], env: dict[str, str]) -> ProgramResult:
        process = await self.exec(runtime, ["sh", "-c", 'exec "$@" < /dev/null', "sh", *argv], env)
        stdout, stderr, code = await asyncio.gather(
            _drain(process.stdout), _drain(process.stderr), process.wait()
        )
        return ProgramResult(code, stdout.decode(errors="replace"), stderr.decode(errors="replace"))

    async def run_background(
        self, runtime: "Runtime", argv: list[str], env: dict[str, str], log: str
    ) -> None:
        script = f'exec "$@" > {shlex.quote(log)} 2>&1 < /dev/null'
        process = await self.exec(runtime, ["sh", "-c", script, "sh", *argv], env)
        self._background.append(process)

    async def read(self, runtime: "Runtime", path: str, max_bytes: int | None = None) -> bytes:
        result = await runtime.run(
            [
                "sh",
                "-c",
                (
                    "t=$(mktemp) || exit 1; "
                    'if [ -n "$1" ]; then head -c "$1" -- "$2"; else cat -- "$2"; fi '
                    '> "$t" || { rm -f "$t"; exit 1; }; '
                    'base64 < "$t"; rc=$?; rm -f "$t"; exit $rc'
                ),
                "sh",
                "" if max_bytes is None else str(max_bytes),
                path,
            ],
            {},
        )
        if result.exit_code:
            raise SandboxError(f"read {path!r}: {result.stderr.strip()[-500:]}")
        return base64.b64decode(result.stdout)

    @abstractmethod
    async def write(self, runtime: "Runtime", path: str, data: bytes) -> None: ...

    def host_url(self, url: str) -> str:
        return url

    async def prepare_execution(self, routes: list[str] | None, network: Any = None) -> None:
        pass

    @property
    def published_port(self) -> int | None:
        return None

    async def expose(self, port: int) -> str:
        return f"http://127.0.0.1:{port}"


async def _drain(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


@dataclass(frozen=True, eq=False)
class Runtime:
    backend: RuntimeBackend
    env: Mapping[str, str] = field(default_factory=dict)
    user: str | None = None
    home: str | None = None

    def __post_init__(self) -> None:
        if self.user is not None and not self.backend.SUPPORTS_USER_SWITCHING:
            raise ValueError(f"the {self.backend.type} runtime cannot run as another user")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))

    def with_env(self, env: Mapping[str, str]) -> "Runtime":
        return replace(self, env=env)

    def with_user(self, user: str | None, home: str | None = None) -> "Runtime":
        return replace(self, user=user, home=home if user is not None else None)

    def process_env(self, env: dict[str, str]) -> dict[str, str]:
        home = {} if self.home is None else {"HOME": self.home}
        return {**self.env, **home, **env}

    @property
    def info(self) -> Any:
        return self.backend.info

    async def run(self, argv: list[str], env: dict[str, str]) -> ProgramResult:
        return await self.backend.run(self, argv, env)

    async def exec(self, argv: list[str], env: dict[str, str]) -> RuntimeProcess:
        return await self.backend.exec(self, argv, env)

    async def run_background(self, argv: list[str], env: dict[str, str], log: str) -> None:
        await self.backend.run_background(self, argv, env, log)

    async def write(self, path: str, data: bytes) -> None:
        await self.backend.write(self, path, data)

    async def read(self, path: str, max_bytes: int | None = None) -> bytes:
        if max_bytes is None:
            return await self.backend.read(self, path)
        data = await self.backend.read(self, path, max_bytes=max_bytes + 1)
        if len(data) > max_bytes:
            raise SandboxError(f"read {path!r}: over the {max_bytes} byte limit")
        return data


def import_backend(import_path: str) -> type[RuntimeBackend]:
    module_path, _, name = import_path.rpartition(".")
    backend = getattr(importlib.import_module(module_path), name)
    if not (isinstance(backend, type) and issubclass(backend, RuntimeBackend)):
        raise TypeError(f"custom runtime {import_path!r} must be a RuntimeBackend subclass")
    config_class = getattr(backend, "config_class", CustomRuntimeConfig)
    if not (isinstance(config_class, type) and issubclass(config_class, CustomRuntimeConfig)):
        raise TypeError("must set config_class to a CustomRuntimeConfig subclass")
    if backend.SUPPORTS_TASK_IMAGES or backend.SUPPORTS_COMPOSE:
        missing = {"image", "workdir"} - config_class.model_fields.keys()
        if missing:
            raise TypeError(f"its config_class must declare {sorted(missing)}")
    return backend


def load_config(data: dict[str, Any]) -> CustomRuntimeConfig:
    """What `TypeAdapter(RuntimeConfig).validate_python` does for `type = "custom"`."""
    config_class = getattr(import_backend(data["import_path"]), "config_class", CustomRuntimeConfig)
    return config_class.model_validate(data)


def make_backend(config: CustomRuntimeConfig, name: str | None = None) -> RuntimeBackend:
    backend = import_backend(config.import_path)(config, name)
    _LIVE.add(backend)
    return backend


@contextlib.asynccontextmanager
async def provision_runtime(
    config: CustomRuntimeConfig, name: str | None = None, env: dict[str, str] | None = None
) -> AsyncIterator[Runtime]:
    backend = make_backend(config, name)
    backend.env = dict(env or {})
    try:
        await backend.start()
        yield backend.bind()
    finally:
        await backend.stop()


__all__ = [
    "BaseRuntimeInfo",
    "CustomRuntimeConfig",
    "CustomRuntimeInfo",
    "ProgramResult",
    "Runtime",
    "RuntimeBackend",
    "RuntimeProcess",
    "import_backend",
    "load_config",
    "make_backend",
    "provision_runtime",
]
