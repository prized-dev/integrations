"""Checks that need no network: argument rules and ref ownership."""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import WorkspaceRef

from pydantic_ai_prized import PrizedSandbox, PrizedSandboxBackend

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def test_building_does_no_io_and_has_no_ref() -> None:
    assert PrizedSandboxBackend(api_key='dcp_unused').ref is None


def test_a_ref_from_another_provider_is_refused() -> None:
    with pytest.raises(ValueError):
        PrizedSandboxBackend(ref=WorkspaceRef(provider='modal', id='sb-1'))


def test_working_dir_must_be_absolute() -> None:
    with pytest.raises(ValueError):
        PrizedSandboxBackend(working_dir='work')
    with pytest.raises(UserError):
        PrizedSandbox(working_dir='work')


class _RunContext:
    run_id = 'run-1'


def test_capability_declines_refs_it_does_not_own() -> None:
    capability = PrizedSandbox()
    ctx = _RunContext()
    assert capability.get_workspace(ctx, ref=WorkspaceRef(provider='daytona', id='x')) is None  # type: ignore[arg-type]
    backend = capability.get_workspace(ctx, ref=WorkspaceRef(provider='prized', id='sbx_1'))  # type: ignore[arg-type]
    assert isinstance(backend, PrizedSandboxBackend) and backend.ref == WorkspaceRef(provider='prized', id='sbx_1')


@pytest.mark.parametrize(
    ('command', 'shell', 'error'),
    [('true', False, TypeError), (['true'], True, TypeError), ([], False, TypeError), (['a\x00b'], False, ValueError)],
)
async def test_command_form_is_checked_before_any_io(command: object, shell: bool, error: type[Exception]) -> None:
    backend = PrizedSandboxBackend(ref=WorkspaceRef(provider='prized', id='sbx_never_contacted'), api_key='dcp_x')
    with pytest.raises(error):
        await backend.run(command, shell=shell)  # type: ignore[arg-type]


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan')])
async def test_bad_timeouts_are_value_errors(timeout: float) -> None:
    backend = PrizedSandboxBackend(ref=WorkspaceRef(provider='prized', id='sbx_never_contacted'), api_key='dcp_x')
    with pytest.raises(ValueError):
        await backend.run(['true'], timeout=timeout)
