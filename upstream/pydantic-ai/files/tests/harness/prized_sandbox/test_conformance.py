"""Pydantic AI's workspace backend conformance suite, run against `PrizedSandboxBackend`.

`TestFakePrizedSandboxBackend` runs everywhere, over the fake Prized SDK, where commands act on a
temporary host directory. `TestLivePrizedSandboxBackend` runs the same rules against a real sandbox
and is gated like `test_prized_live.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite
from pydantic_ai_harness.prized_sandbox import PrizedSandbox, PrizedSandboxBackend

from .conftest import LIVE_AUTO_PAUSE_MIN, LIVE_LABELS, LIVE_MAX_RUNTIME_MIN, live_connection
from .fake_prized import FakePrized


async def _delete(backend: WorkspaceBackend) -> None:
    assert isinstance(backend, PrizedSandboxBackend) and backend.ref is not None
    await PrizedSandbox(**live_connection()).destroy(backend.ref)


class TestFakePrizedSandboxBackend(WorkspaceBackendSuite):
    @pytest.fixture
    def backend(self, fake_prized: FakePrized) -> PrizedSandboxBackend:
        return PrizedSandboxBackend()

    @pytest.fixture
    def fresh_backend(self, fake_prized: FakePrized) -> Callable[[], WorkspaceBackend]:
        return PrizedSandboxBackend

    @pytest.fixture
    def attach_backend(self) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return lambda ref: PrizedSandboxBackend(ref=ref)

    @pytest.fixture
    def destroy_environment(self) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        return _delete


def _live_backend() -> PrizedSandboxBackend:
    return PrizedSandboxBackend(
        vcpu=2,
        memory_mb=4096,
        labels=LIVE_LABELS,
        auto_pause_min=LIVE_AUTO_PAUSE_MIN,
        max_runtime_min=LIVE_MAX_RUNTIME_MIN,
        **live_connection(),
    )


@pytest.mark.prized_live
# The live tier runs without coverage; in CI only its gate fixtures run, to skip it.
class TestLivePrizedSandboxBackend(WorkspaceBackendSuite):  # pragma: lax no cover
    # Class-scoped so the rules share one sandbox instead of creating one each; the suite runs
    # its destroy rule last.
    @pytest.fixture(scope='class')
    @classmethod
    async def backend(cls) -> AsyncIterator[PrizedSandboxBackend]:
        backend = _live_backend()
        try:
            await backend.working_dir()  # creates the sandbox
            yield backend
        finally:
            try:
                if backend.ref is not None:
                    await _delete(backend)
            finally:
                await backend.aclose()

    # The suite never deletes what `fresh_backend` creates, and uses it for its destructive rules too,
    # so each sandbox is deleted here.
    @pytest.fixture
    async def fresh_backend(self) -> AsyncIterator[Callable[[], WorkspaceBackend]]:
        created: list[PrizedSandboxBackend] = []

        def create() -> WorkspaceBackend:
            backend = _live_backend()
            created.append(backend)
            return backend

        yield create
        for backend in created:
            try:
                if backend.ref is not None:
                    await _delete(backend)
            finally:
                await backend.aclose()

    # The suite drops the backend it attaches; each one opened its own handle, which is closed here.
    @pytest.fixture
    async def attach_backend(self) -> AsyncIterator[Callable[[WorkspaceRef], WorkspaceBackend]]:
        attached: list[PrizedSandboxBackend] = []

        def attach(ref: WorkspaceRef) -> WorkspaceBackend:
            backend = PrizedSandboxBackend(ref=ref, **live_connection())
            attached.append(backend)
            return backend

        yield attach
        for backend in attached:
            await backend.aclose()

    @pytest.fixture
    def destroy_environment(self) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        return _delete
