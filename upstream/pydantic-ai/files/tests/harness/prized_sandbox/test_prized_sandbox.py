"""PrizedSandbox and PrizedSandboxBackend over the fake Prized SDK (see `fake_prized.py`)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import anyio
import pytest

from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import WorkspaceError, WorkspaceRef, WorkspaceTimeoutError, WorkspaceUnavailableError
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.prized_sandbox import PrizedSandbox, PrizedSandboxBackend

from .._tool_calls import call_tools
from .fake_prized import FakePrized

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def test_building_does_no_io_and_has_no_ref() -> None:
    assert PrizedSandboxBackend().ref is None


def test_refs_of_another_provider_or_without_an_id_are_refused() -> None:
    with pytest.raises(ValueError, match='unsupported workspace provider'):
        PrizedSandboxBackend(ref=WorkspaceRef(provider='e2b', id='x'))
    with pytest.raises(ValueError, match='cannot be empty'):
        PrizedSandboxBackend(ref=WorkspaceRef(provider='prized', id=' '))


@pytest.mark.parametrize(
    'kwargs',
    [{'working_dir': 'relative'}, {'auto_pause_min': -1}, {'vcpu': 0}, {'defer_loading': True}, {'env': {'A=B': '1'}}],
)
def test_bad_settings_fail_where_they_are_written(kwargs: dict[str, object]) -> None:
    with pytest.raises((UserError, ValueError)):
        PrizedSandbox(**kwargs)  # pyright: ignore[reportArgumentType]


def test_the_capability_declines_refs_it_does_not_own() -> None:
    class Ctx:
        run_id = 'run-1'

    capability = PrizedSandbox()
    assert capability.get_workspace(Ctx(), ref=WorkspaceRef(provider='modal', id='sb-1')) is None  # pyright: ignore[reportArgumentType]
    backend = capability.get_workspace(Ctx(), ref=WorkspaceRef(provider='prized', id='sbx_1'))  # pyright: ignore[reportArgumentType]
    assert isinstance(backend, PrizedSandboxBackend) and backend.ref == WorkspaceRef(provider='prized', id='sbx_1')


def test_api_key_stays_out_of_repr() -> None:
    assert 'dcp_secret' not in repr(PrizedSandbox(api_key='dcp_secret'))


async def test_first_use_creates_one_labelled_sandbox_even_when_racing(fake_prized: FakePrized) -> None:
    backend = PrizedSandboxBackend(labels={'team': 'evals'})
    results = await asyncio.gather(*(backend.run(['echo', str(i)], timeout=30) for i in range(5)))
    assert [r.stdout for r in results] == [f'{i}\n' for i in range(5)]
    assert len(fake_prized.created) == 1
    assert backend.ref == WorkspaceRef(provider='prized', id=fake_prized.created[0])
    assert fake_prized.record(fake_prized.created[0]).labels == {'created-by': 'pydantic-ai', 'team': 'evals'}
    await backend.aclose()


async def test_env_layers_per_command_over_the_backend(fake_prized: FakePrized) -> None:
    backend = PrizedSandboxBackend(env={'A': 'backend', 'B': 'backend'})
    result = await backend.run('echo "$A $B"', shell=True, env={'B': 'command'}, timeout=30)
    assert result.stdout == 'backend command\n'


async def test_a_new_sandbox_gets_its_working_dir(fake_prized: FakePrized, tmp_path: Path) -> None:
    directory = str(tmp_path.resolve() / 'project' / 'src')
    backend = PrizedSandboxBackend(working_dir=directory)
    assert (await backend.run(['pwd', '-P'], timeout=30)).stdout == f'{directory}\n'
    assert await backend.working_dir() == directory


async def test_an_attached_sandbox_without_its_working_dir_is_an_error(fake_prized: FakePrized, tmp_path: Path) -> None:
    first = PrizedSandboxBackend()
    await first.run(['true'], timeout=30)
    assert first.ref is not None
    attached = PrizedSandboxBackend(ref=first.ref, working_dir=str(tmp_path / 'missing'))
    with pytest.raises(WorkspaceError, match='may not exist'):
        await attached.run(['true'], timeout=30)


async def test_a_deleted_sandbox_is_unavailable_and_never_replaced(fake_prized: FakePrized) -> None:
    first = PrizedSandboxBackend()
    await first.run(['true'], timeout=30)
    assert first.ref is not None
    await PrizedSandbox().destroy(first.ref)
    await PrizedSandbox().destroy(first.ref)  # already gone: quiet
    with pytest.raises(WorkspaceUnavailableError, match="workspace='new'"):
        await PrizedSandboxBackend(ref=first.ref).run(['true'], timeout=30)
    assert len(fake_prized.created) == 1


async def test_a_refused_key_is_unavailable_without_quoting_the_key(fake_prized: FakePrized) -> None:
    with pytest.raises(WorkspaceUnavailableError, match='unauthorized') as caught:
        await PrizedSandboxBackend(api_key='dcp_rejected').run(['true'], timeout=30)
    assert 'dcp_rejected' not in str(caught.value)


async def test_a_refused_create_is_unavailable(fake_prized: FakePrized) -> None:
    fake_prized.refuse_create = 'paid_balance_required'
    with pytest.raises(WorkspaceUnavailableError, match='paid_balance_required'):
        await PrizedSandboxBackend().run(['true'], timeout=30)


async def test_a_timeout_raises_with_partial_output_and_stops_the_command(fake_prized: FakePrized) -> None:
    backend = PrizedSandboxBackend()
    with pytest.raises(WorkspaceTimeoutError) as caught:
        await backend.run('echo started; sleep 5; touch finished', shell=True, timeout=1)
    assert caught.value.stdout == 'started\n'
    await anyio.sleep(5)
    assert (await backend.run(['ls'], timeout=30)).stdout == ''


async def test_cancelling_a_command_stops_it(fake_prized: FakePrized) -> None:
    backend = PrizedSandboxBackend()
    await backend.run(['true'], timeout=30)
    with anyio.move_on_after(1):
        await backend.run('sleep 3; touch finished', shell=True, timeout=30)
    await anyio.sleep(3.5)
    assert (await backend.run(['ls'], timeout=30)).stdout == ''


async def test_a_long_or_unbounded_command_runs_detached(fake_prized: FakePrized) -> None:
    backend = PrizedSandboxBackend()
    assert (await backend.run(['echo', 'no limit'])).stdout == 'no limit\n'
    assert (await backend.run(['echo', 'long'], timeout=3600)).stdout == 'long\n'


async def test_reads_use_the_file_api_and_fall_back_to_the_shell(fake_prized: FakePrized) -> None:
    """A regular file under the home directory is one file API call; anything it refuses goes through the shell."""
    backend = PrizedSandboxBackend()
    await backend.run('printf data > plain; mkfifo pipe; mkdir dir', shell=True, timeout=30)
    assert await backend.read_bytes('plain') == b'data'
    root = await backend.working_dir()
    assert ('read', f'{root}/plain') in fake_prized.calls
    with pytest.raises(IsADirectoryError):
        await backend.read_bytes('dir')
    with pytest.raises(FileNotFoundError):
        await backend.read_bytes('missing')


@pytest.mark.parametrize('timeout', [30, None])
async def test_a_long_command_line_runs_from_a_staged_file(fake_prized: FakePrized, timeout: float | None) -> None:
    """The edge refuses a wrapped command line past about 64 KiB, so a long one is written to a file and sourced."""
    backend = PrizedSandboxBackend()
    payload = 'x' * (100 * 1024)  # past the edge's limit for a wrapped line, under Linux's 128 KiB per argument
    result = await backend.run(['sh', '-c', 'printf %s "$1" | wc -c', 'sh', payload], timeout=timeout)
    assert result.stdout.strip() == str(len(payload))
    assert fake_prized.host_root is not None
    assert list((fake_prized.host_root / 'scripts').iterdir()) == []  # the file removed itself


async def test_a_sandbox_deleted_during_a_command_is_unavailable(
    fake_prized: FakePrized, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('pydantic_ai_harness.prized_sandbox._backend._WATCH_INTERVAL', 0.2)
    backend = PrizedSandboxBackend()
    await backend.run(['true'], timeout=30)
    assert backend.ref is not None
    sandbox_id = backend.ref.id

    async def delete_soon() -> None:
        await anyio.sleep(0.5)
        fake_prized.delete_behind_backs(sandbox_id)

    async with anyio.create_task_group() as group:
        group.start_soon(delete_soon)
        with pytest.raises(WorkspaceUnavailableError, match=r'no longer exists|deleted'):
            await backend.run(['sleep', '20'], timeout=30)


async def test_pause_keeps_files_and_the_next_command_resumes(fake_prized: FakePrized) -> None:
    capability = PrizedSandbox()
    backend = PrizedSandboxBackend()
    await backend.run('echo kept > state.txt', shell=True, timeout=30)
    assert backend.ref is not None
    await capability.pause(backend.ref)
    record = fake_prized.record(backend.ref.id)
    assert (record.state, record.pauses) == ('paused', 1)
    assert (await capability.backend(backend.ref).run(['cat', 'state.txt'], timeout=30)).stdout == 'kept\n'
    assert (record.state, record.resumes) == ('running', 1)


async def test_fork_copies_the_sandbox_and_the_copies_diverge(fake_prized: FakePrized) -> None:
    capability = PrizedSandbox()
    source = PrizedSandboxBackend()
    await source.run('echo before > state.txt', shell=True, timeout=30)
    assert source.ref is not None
    ref = await capability.fork(source.ref)
    assert ref.provider == 'prized' and ref != source.ref
    copy = capability.backend(ref)
    assert (await copy.run(['cat', 'state.txt'], timeout=30)).stdout == 'before\n'
    await copy.run('echo after > state.txt', shell=True, timeout=30)
    assert (await source.run(['cat', 'state.txt'], timeout=30)).stdout == 'before\n'
    assert fake_prized.record(ref.id).labels['forked-from'] == source.ref.id


async def test_coder_tools_run_in_the_sandbox(fake_prized: FakePrized) -> None:
    results = await call_tools(
        [PrizedSandbox(), Coder()],
        [
            ('write_file', {'path': 'hello.py', 'content': "print('hi from prized')\n"}),
            ('shell', {'command': 'python3 hello.py'}),
        ],
    )
    assert 'hi from prized' in results[1]
    assert len(fake_prized.created) == 1
