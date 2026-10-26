"""Pydantic AI's workspace conformance suite, run live against Prized.

Needs PRIZED_API_KEY. Every sandbox it makes carries the label `pydantic-ai-prized-test=<run id>`,
and all of them are deleted when the session ends.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from pydantic_ai_prized import PrizedSandboxBackend, destroy_sandbox

pytestmark = pytest.mark.skipif(not os.environ.get('PRIZED_API_KEY'), reason='needs PRIZED_API_KEY')

RUN_ID = uuid.uuid4().hex[:12]
LABELS = {
    'pydantic-ai-prized-test': RUN_ID,
    **dict(item.split('=', 1) for item in os.environ.get('PRIZED_TEST_LABELS', '').split(',') if '=' in item),
}


@pytest.fixture(scope='session')
def shared_ref() -> Iterator[WorkspaceRef]:
    from prized import Sandbox

    sandbox = Sandbox.create('prized-base-v1', labels=LABELS, vcpu=1, memory_mb=2048, max_runtime_min=60)
    try:
        yield WorkspaceRef(provider='prized', id=sandbox.id)
    finally:
        for leftover in Sandbox.list(labels={'pydantic-ai-prized-test': RUN_ID}):
            leftover.delete()
        sandbox.close()


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class TestPrizedConformance(WorkspaceBackendSuite):
    @pytest.fixture
    def backend(self, shared_ref: WorkspaceRef) -> WorkspaceBackend:
        # A fresh backend object per test (each test has its own event loop), on one shared sandbox.
        return PrizedSandboxBackend(ref=shared_ref)

    @pytest.fixture
    def fresh_backend(self) -> Callable[[], WorkspaceBackend]:
        return lambda: PrizedSandboxBackend(
            labels=LABELS, vcpu=1, memory_mb=2048, max_runtime_min=60, auto_pause_min=15
        )

    @pytest.fixture
    def attach_backend(self) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return lambda ref: PrizedSandboxBackend(ref=ref)

    @pytest.fixture
    def destroy_environment(self):
        async def destroy(backend: WorkspaceBackend) -> None:
            assert backend.ref is not None
            await destroy_sandbox(backend.ref.id)

        return destroy
