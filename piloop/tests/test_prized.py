"""The ``prized`` provisioner, against a stand-in for the Prized SDK: no network, no key, and piloop's conformance check
of every provisioner. Shaped after piloop's own tests of ``modal`` and ``daytona``."""

import re
import subprocess
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import prized
import pytest
from fake_prized import Box, FakePrized
from piloop.capabilities import (
    PROVISIONERS,
    Check,
    InstanceState,
    Launch,
    ProvisionerHost,
    StartRefused,
    check_provisioning,
)
from piloop.otel import Tracer
from pydantic import SecretStr

import piloop_prized.prized as module
from piloop_prized import ParentNotReady, Prized, PrizedProvisioning, SetupFailed
from piloop_prized.__main__ import main

LAUNCH = Launch(
    name='agents-0123abcd',
    pool='agents',
    argv=['piloop', 'worker', '--server', 'https://piloop.test', '--pool', 'agents'],
    env={'PILOOP_HTTPS_TOKEN': SecretStr("plt_se'cret")},
)
OWNER = {'piloop-owner': 'owner-a'}


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePrized]:
    fake = FakePrized()
    monkeypatch.setattr(prized.Sandbox, 'create', fake.create)
    monkeypatch.setattr(prized.Sandbox, 'list', fake.list)
    monkeypatch.setattr(prized.Sandbox, 'connect', fake.connect)
    monkeypatch.setattr(prized.Sandbox, 'delete_by_labels', fake.delete_by_labels)
    monkeypatch.setattr(module, 'installed_piloop', lambda: 'piloop==0.5.0')
    yield fake
    # A call that a deadline left running ends against this fake, before the real SDK is back.
    for gate in (fake.setup_gate, fake.fork_gate, fake.list_gate):
        if gate is not None:
            gate.set()
    for thread in threading.enumerate():
        if thread.name.startswith(('within: ', 'piloop-prized: ')):
            thread.join(timeout=5)
            assert not thread.is_alive(), f'{thread.name} outlived its test'


def opened(owner: str, **keys: object) -> PrizedProvisioning:
    config = PROVISIONERS.validated({'provider': 'prized', 'cpu': 2, 'memory_mb': 8192, **keys})
    assert isinstance(config, Prized)
    return config.open(ProvisionerHost(owner, Tracer(None)))


def workers(sdk: FakePrized) -> list[Box]:
    return [b for b in sdk.boxes.values() if 'piloop-name' in b.labels]


def parents(sdk: FakePrized) -> list[Box]:
    return [b for b in sdk.boxes.values() if b.labels.get('piloop-role') == 'parent']


@pytest.mark.parametrize('fork', [True, False])
def test_prized_passes_the_provisioner_conformance_check(sdk: FakePrized, fork: bool) -> None:
    check_provisioning(lambda owner: opened(owner, fork=fork), LAUNCH, timeout=1)


def test_the_entry_point_names_the_provider() -> None:
    assert PROVISIONERS.get('prized').model is Prized
    assert PROVISIONERS.sources()['prized'].package == 'piloop-prized'


def test_a_worker_forks_the_warm_parent_and_reads_its_credential_from_a_file(sdk: FakePrized) -> None:
    provisioning = opened('owner-a', env={'PILOOP_TICK_SECONDS': '2'}, timeout_hours=2)
    assert sdk.calls == [], 'opening touches no network'
    provisioning.start(LAUNCH)
    [parent], [worker] = parents(sdk), workers(sdk)
    assert parent.state == 'paused' and worker.parent == parent.id and sdk.setups == [parent.id]
    assert parent.options['vcpu'] == 2 and parent.options['memory_mb'] == 8192
    assert (parent.options['max_runtime_min'], parent.options['auto_pause_min']) == (120, 0)
    assert worker.labels == {'piloop-owner': 'owner-a', 'piloop-name': LAUNCH.name, 'piloop-pool': 'agents'}
    exports, mode = worker.files[module.CREDENTIAL]
    assert mode == 0o600
    assert exports == "export PILOOP_TICK_SECONDS=2\nexport PILOOP_HTTPS_TOKEN='plt_se'\"'\"'cret'\n"
    [(command, how)] = worker.commands
    assert how['background'] and command.startswith(f'. {module.CREDENTIAL} && rm -f {module.CREDENTIAL} && ')
    assert command.endswith(
        f'exec piloop worker --server https://piloop.test --pool agents >> {module.WORKER_LOG} 2>&1'
    )
    everywhere = repr([(b.labels, b.options, b.commands) for b in sdk.boxes.values()])
    assert 'plt_se' not in everywhere, 'the credential is in no label, option or command line'
    [instance] = provisioning.list()
    assert (instance.name, instance.pool, instance.state, instance.handle) == (
        LAUNCH.name,
        'agents',
        InstanceState.RUNNING,
        {'id': worker.id},
    )


def test_every_worker_forks_the_same_parent_and_the_setup_runs_once(sdk: FakePrized) -> None:
    provisioning = opened('owner-a', setup=['npm install -g @openai/codex@0.166.0'])
    for i in range(3):
        provisioning.start(LAUNCH.model_copy(update={'name': f'agents-{i}'}))
    [parent] = parents(sdk)
    assert {w.parent for w in workers(sdk)} == {parent.id} and len(workers(sdk)) == 3
    assert sdk.setups == [parent.id]
    script, mode = parent.files[module.SETUP]
    assert mode == 0o700 and 'uv tool install --quiet piloop==0.5.0\nnpm install -g @openai/codex@0.166.0\n' in script
    assert {i.name for i in provisioning.list()} == {'agents-0', 'agents-1', 'agents-2'}, 'the parent is never listed'


def test_a_restarted_daemon_forks_the_parent_that_it_set_up_before(sdk: FakePrized) -> None:
    opened('owner-a').start(LAUNCH)
    opened('owner-a').start(LAUNCH.model_copy(update={'name': 'agents-again'}))
    [parent] = parents(sdk)
    assert sdk.setups == [parent.id] and {w.parent for w in workers(sdk)} == {parent.id}


def test_another_recipe_or_another_owner_sets_up_a_parent_of_its_own(sdk: FakePrized) -> None:
    opened('owner-a').start(LAUNCH)
    opened('owner-a', setup=['echo other']).start(LAUNCH.model_copy(update={'name': 'agents-b'}))
    opened('owner-b').start(LAUNCH)
    assert len(parents(sdk)) == 3 and len({p.labels['piloop-recipe'] for p in parents(sdk)}) == 2
    assert all(p.state == 'paused' for p in parents(sdk)), 'no recipe deletes another'


def test_a_parent_left_mid_setup_by_a_daemon_that_is_gone_is_deleted(sdk: FakePrized) -> None:
    labels = opened('owner-a')._warm.labels  # type: ignore[union-attr]
    stale = sdk.make('prized-agent-v1', dict(labels), {})
    stale.created_at = datetime.now(UTC) - timedelta(hours=2)
    young = sdk.make('prized-agent-v1', dict(labels), {})  # another pool's of the same recipe, mid-setup
    opened('owner-a').start(LAUNCH)
    assert stale.state == 'deleted' and young.state == 'running'
    assert len([p for p in parents(sdk) if p.state == 'paused']) == 1


def test_a_failed_setup_is_a_refused_start_and_the_next_start_tries_again(sdk: FakePrized) -> None:
    sdk.setup_result = (127, 'line 9: lake: command not found\n')
    provisioning = opened('owner-a')
    with pytest.raises(SetupFailed, match='exited 127: line 9: lake: command not found'):
        provisioning.start(LAUNCH)
    assert isinstance(SetupFailed('x'), StartRefused)
    assert [p.state for p in parents(sdk)] == ['deleted'] and workers(sdk) == []
    sdk.setup_result = (0, '')
    provisioning.start(LAUNCH)
    assert [p.state for p in parents(sdk)] == ['deleted', 'paused'] and len(workers(sdk)) == 1


def test_a_setup_past_setup_seconds_is_refused(sdk: FakePrized) -> None:
    sdk.setup_times_out = True
    with pytest.raises(SetupFailed, match='ran past setup_seconds = 30'):
        opened('owner-a', setup_seconds=30).start(LAUNCH)
    assert [p.state for p in parents(sdk)] == ['deleted']


def test_a_start_that_waits_for_the_setup_fails_and_the_next_waits_for_the_same_one(sdk: FakePrized) -> None:
    sdk.setup_gate = threading.Event()
    provisioning = opened('owner-a', start_seconds=0.3)
    for _ in range(2):
        with pytest.raises(ParentNotReady, match='still being set up'):
            provisioning.start(LAUNCH)
    assert len(parents(sdk)) == 1, 'one setup, however many starts wait for it'
    sdk.setup_gate.set()
    provisioning.start(LAUNCH)
    assert len(parents(sdk)) == 1 and len(workers(sdk)) == 1 and not issubclass(ParentNotReady, StartRefused)


def test_a_full_machine_sets_up_another_parent_up_to_max_parents(sdk: FakePrized) -> None:
    provisioning = opened('owner-a', max_parents=2)
    provisioning.start(LAUNCH)
    [first] = parents(sdk)
    sdk.fits[first.id] = 0
    provisioning.start(LAUNCH.model_copy(update={'name': 'agents-2'}))
    [_, second] = parents(sdk)
    assert workers(sdk)[-1].parent == second.id
    sdk.fits[second.id] = 0
    with pytest.raises(StartRefused, match=r'every warm parent \(2\) is full'):
        provisioning.start(LAUNCH.model_copy(update={'name': 'agents-3'}))
    assert len(parents(sdk)) == 2


def test_a_parent_that_prized_deleted_is_set_up_again(sdk: FakePrized) -> None:
    provisioning = opened('owner-a')
    provisioning.start(LAUNCH)
    [gone] = parents(sdk)
    gone.state = 'deleted'  # its parent_hours ran out
    provisioning.start(LAUNCH.model_copy(update={'name': 'agents-2'}))
    assert [p.state for p in parents(sdk)] == ['deleted', 'paused']


def test_a_parent_that_you_name_is_forked_and_never_set_up_or_deleted(
    sdk: FakePrized, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, 'FULL_SECONDS', 0.0)  # a full machine is tried again at once
    ready = sdk.make('prized-agent-v1', {'piloop-role': 'parent', 'piloop-prebuilt': 'true'}, {})
    ready.state = 'paused'
    provisioning = opened('owner-a', parent=ready.id)
    provisioning.start(LAUNCH)
    assert (
        [w.parent for w in workers(sdk)] == [ready.id]
        and sdk.setups == []
        and ('create', 'prized-agent-v1') not in sdk.calls
    )
    sdk.fits[ready.id] = 0
    with pytest.raises(StartRefused, match='is full'):
        provisioning.start(LAUNCH.model_copy(update={'name': 'agents-2'}))
    ready.state = 'deleted'
    with pytest.raises(StartRefused, match='python -m piloop_prized parent'):
        provisioning.start(LAUNCH.model_copy(update={'name': 'agents-3'}))


def test_a_fresh_sandbox_runs_the_setup_before_its_worker(sdk: FakePrized) -> None:
    opened('owner-a', fork=False, setup=['echo ready']).start(LAUNCH)
    assert parents(sdk) == [] and sdk.setups == []
    [worker] = workers(sdk)
    assert worker.options['auto_pause_min'] == 0 and worker.files[module.SETUP][1] == 0o700
    [(command, how)] = worker.commands
    assert how['background'] and command.startswith(f'bash {module.SETUP} > {module.SETUP_LOG} 2>&1 || ')


@pytest.mark.parametrize(
    'code', ['sandbox_quota_exceeded', 'insufficient_balance', 'paid_balance_required', 'invalid_resources']
)
def test_what_prized_refuses_is_a_refused_start_and_anything_else_is_tried_again(sdk: FakePrized, code: str) -> None:
    sdk.create_fails = prized.PrizedError(code, status=402)
    for fork in (True, False):
        with pytest.raises(StartRefused, match=code):
            opened(f'owner-{fork}', fork=fork).start(LAUNCH)
    sdk.create_fails = prized.PrizedError('network_error')
    with pytest.raises(prized.PrizedError, match='network_error') as raised:
        opened('owner-c', fork=False).start(LAUNCH)
    assert not isinstance(raised.value, StartRefused)


def test_a_fork_that_prized_refuses_is_a_refused_start(sdk: FakePrized) -> None:
    provisioning = opened('owner-a')
    provisioning.start(LAUNCH)
    [parent] = parents(sdk)
    sdk.fork_errors[parent.id] = prized.PrizedError('sandbox_quota_exceeded', reason='running_vcpu')
    with pytest.raises(StartRefused, match='running_vcpu'):
        provisioning.start(LAUNCH.model_copy(update={'name': 'agents-2'}))


def test_a_sandbox_whose_worker_did_not_start_is_deleted_so_that_the_next_start_makes_another(
    sdk: FakePrized,
) -> None:
    provisioning = opened('owner-a')
    provisioning.start(LAUNCH.model_copy(update={'name': 'warm-up'}))  # the parent is ready
    provisioning.stop('warm-up')
    sdk.write_fails = prized.PrizedError('sandbox_unreachable')
    with pytest.raises(prized.PrizedError, match='sandbox_unreachable'):
        provisioning.start(LAUNCH)

    def tries() -> list[str]:
        return [w.state for w in workers(sdk) if w.labels['piloop-name'] == LAUNCH.name]

    assert tries() == ['deleted'] and provisioning.list() == []
    sdk.write_fails = None
    provisioning.start(LAUNCH)
    assert tries() == ['deleted', 'running']


def test_a_repeated_start_never_makes_a_second_sandbox(sdk: FakePrized) -> None:
    provisioning = opened('owner-a', start_seconds=0.2)
    provisioning.start(LAUNCH.model_copy(update={'name': 'warm-up'}))  # the parent is ready
    sdk.fork_gate = threading.Event()
    for _ in range(2):  # the first holds its fork; the second waits for the first
        with pytest.raises(TimeoutError, match=r'did not answer in 0\.2 s'):
            provisioning.start(LAUNCH)
    sdk.fork_gate.set()
    for _ in range(50):
        if any(w.labels['piloop-name'] == LAUNCH.name and w.commands for w in workers(sdk)):
            break
        threading.Event().wait(0.02)
    provisioning.start(LAUNCH)
    assert len([w for w in workers(sdk) if w.labels['piloop-name'] == LAUNCH.name]) == 1


def test_stop_deletes_by_the_workers_labels_and_a_name_that_is_gone_is_a_success(sdk: FakePrized) -> None:
    mine, other = opened('owner-a'), opened('owner-b')
    mine.start(LAUNCH)
    other.start(LAUNCH)
    mine.stop(LAUNCH.name)
    mine.stop(LAUNCH.name)
    mine.stop('never-started')
    assert ('delete_by_labels', {'piloop-owner': 'owner-a', 'piloop-name': LAUNCH.name}) in sdk.calls
    assert [w.state for w in sdk.by_labels(OWNER) if 'piloop-name' in w.labels] == ['deleted']
    assert [i.state for i in other.list()] == [InstanceState.RUNNING], "an owner never stops another's"
    assert [p.state for p in parents(sdk)] == ['paused', 'paused'], 'a stop leaves the warm parent'


@pytest.mark.parametrize(
    ('state', 'listed'),
    [
        ('starting', InstanceState.PENDING),
        ('resizing', InstanceState.PENDING),
        ('running', InstanceState.RUNNING),
        ('pausing', InstanceState.ENDED),
        ('paused', InstanceState.ENDED),
        ('deleting', InstanceState.ENDED),
        ('error', InstanceState.ENDED),
    ],
)
def test_a_sandbox_is_listed_by_its_state_until_prized_drops_it(
    sdk: FakePrized, state: str, listed: InstanceState
) -> None:
    provisioning = opened('owner-a')
    provisioning.start(LAUNCH)
    [worker] = workers(sdk)
    worker.state = state
    assert [i.state for i in provisioning.list()] == [listed]
    worker.state = 'deleted'
    assert provisioning.list() == []


@pytest.mark.parametrize('verb', ['list', 'start', 'stop'])
def test_a_prized_call_that_hangs_fails_in_its_seconds(sdk: FakePrized, verb: str) -> None:
    provisioning = opened('owner-a', call_seconds=0.05, start_seconds=0.05)
    sdk.list_gate = threading.Event()
    verbs = {
        'list': provisioning.list,
        'start': lambda: provisioning.start(LAUNCH),
        'stop': lambda: provisioning.stop('x'),
    }
    with pytest.raises(TimeoutError, match=r'did not answer in 0\.05 s'):
        verbs[verb]()
    sdk.list_gate.set()


def test_secret_env_reaches_the_credential_file_alone(sdk: FakePrized, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('CAS_TOKEN', 'cas-secret')
    opened('owner-a', secret_env=['CAS_TOKEN']).start(LAUNCH)
    [worker] = workers(sdk)
    assert 'export CAS_TOKEN=cas-secret\n' in str(worker.files[module.CREDENTIAL][0])
    assert 'cas-secret' not in repr([(b.labels, b.options, b.commands) for b in sdk.boxes.values()])
    monkeypatch.delenv('CAS_TOKEN')
    with pytest.raises(StartRefused, match='CAS_TOKEN'):
        opened('owner-b', secret_env=['CAS_TOKEN']).start(LAUNCH)
    assert [b.labels['piloop-owner'] for b in sdk.boxes.values()].count('owner-b') == 0, 'refused before any sandbox'


def test_a_table_written_for_modal_validates_once_its_image_is_gone() -> None:
    modal = {
        'cpu': 2,
        'memory_mb': 12288,
        'memory_limit_mb': 12288,
        'timeout_hours': 5,
        'start_seconds': 600,
        'call_seconds': 60,
        'env': {'PILOOP_PLUGINS': '{"cas": {"provider": "http", "url": "https://cas.test"}}'},
    }
    config = PROVISIONERS.validated({'provider': 'prized', **modal})
    assert isinstance(config, Prized) and config.memory() == 12288


@pytest.mark.parametrize(
    ('keys', 'error'),
    [
        ({'image': 'ghcr.io/acme/piloop-worker:0.3'}, 'not an image: custom images come with Docker mode'),
        ({'image_id': 'im-123'}, 'Put what the image adds in `setup`'),
        ({'registry': {'kind': 'basic', 'secret': 's'}}, 'not an image'),
        ({'gpu': 'L4'}, 'no GPU'),
        ({'outbound_domains': ['gateway.test']}, 'no egress allowlist'),
        ({'secrets': ['piloop-cas']}, 'secret_env'),
        ({'app': 'piloop'}, 'a Modal key'),
        ({'cloud': 'gcp'}, "a pin to 'gcp' cannot hold"),
        ({'region': 'us-east'}, 'us-west-2'),
        ({'memory_mb': 9000, 'memory_limit_mb': 8192}, 'memory_limit_mb is below memory_mb'),
        ({'parent': 'sbx_1', 'fork': False}, 'a parent is forked'),
        ({'parent': 'sbx_1', 'setup': ['echo']}, 'holds its setup already'),
        ({'env': {'NOT-A-NAME': '1'}}, 'is no variable name'),
        ({'timeout_hours': 169}, 'timeout_hours'),
        ({'cpu': 33}, 'cpu'),
        ({'disk_gib': 20}, 'disk_gib'),
    ],
)
def test_the_prized_table_refuses_what_a_sandbox_cannot_honour_and_says_why(
    keys: dict[str, object], error: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        PROVISIONERS.validated({'provider': 'prized', **keys})


def test_aws_and_oregon_are_taken_as_they_are() -> None:
    config = PROVISIONERS.validated({'provider': 'prized', 'cloud': 'aws', 'region': 'us-west-2'})
    assert isinstance(config, Prized)


def test_the_doctor_finds_the_sdk_and_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('PRIZED_API_KEY', 'dcp_000')
    provider = Prized()
    assert provider.checks() == [Check('prized', 'prized is installed'), Check('prized login', 'PRIZED_API_KEY is set')]
    monkeypatch.delenv('PRIZED_API_KEY')
    [_, login] = provider.checks()
    assert login.fails and 'dcp_000' not in str(provider.checks())


def test_the_piloop_of_each_sandbox_is_the_daemons_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Dist:
        version = '0.5.0'
        url: str | None = None

        def read_text(self, name: str) -> str | None:
            return self.url

    dist = Dist()
    monkeypatch.setattr(module, 'distribution', lambda name: dist)
    assert module.installed_piloop() == 'piloop==0.5.0'
    dist.url = '{"url": "https://github.com/hiloopai/piloop", "vcs_info": {"vcs": "git", "commit_id": "a1c2e67"}}'
    assert module.installed_piloop() == 'git+https://github.com/hiloopai/piloop@a1c2e67'
    dist.url = '{"url": "file:///src/piloop", "dir_info": {"editable": true}}'
    assert module.installed_piloop() == 'piloop==0.5.0'


def test_cleanup_deletes_every_sandbox_of_the_server_parents_too(
    sdk: FakePrized, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opened('owner-a').start(LAUNCH)
    opened('owner-b').start(LAUNCH)
    (tmp_path / 'owner').write_text('owner-a\n')
    assert main(['cleanup', '--home', str(tmp_path)]) == 0
    assert {b.state for b in sdk.by_labels(OWNER)} == {'deleted'} and len(sdk.by_labels(OWNER)) == 2
    assert {b.state for b in sdk.by_labels({'piloop-owner': 'owner-b'})} == {'running', 'paused'}
    assert len(capsys.readouterr().out.split()) == 2


def test_parent_sets_one_up_ahead_that_a_table_names(
    sdk: FakePrized, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    table = tmp_path / 'compute.toml'
    table.write_text('provider = "prized"\ncpu = 2\nmemory_mb = 12288\nsetup = ["echo lean"]\n')
    assert main(['parent', str(table)]) == 0
    parent = capsys.readouterr().out.strip()
    assert sdk.boxes[parent].state == 'paused' and 'piloop-owner' not in sdk.boxes[parent].labels
    opened('owner-a', parent=parent).start(LAUNCH)
    assert [w.parent for w in workers(sdk)] == [parent]
    assert main(['cleanup', '--owner', 'owner-a']) == 0
    assert sdk.boxes[parent].state == 'paused', "no server's cleanup deletes a parent that it did not set up"


# The scripts themselves, run by bash with stand-ins for curl, uv and piloop: what the sandbox runs.


def stand_ins(bin_dir: Path, log: Path) -> None:
    bin_dir.mkdir(parents=True)
    for name in ('curl', 'uv', 'piloop'):
        tool = bin_dir / name
        tool.write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\nenv | grep -E "^(PILOOP_|CAS_)" | sort >> {log}\n')
        tool.chmod(0o755)


def test_the_setup_script_installs_uv_piloop_then_the_setup_lines_and_stops_at_a_failure(tmp_path: Path) -> None:
    log = tmp_path / 'log'
    stand_ins(tmp_path / '.local' / 'bin', log)
    config = Prized(setup=['echo first >> "$HOME/log"', 'false', 'echo never >> "$HOME/log"'])
    script = tmp_path / 'setup.sh'
    script.write_text(module.setup_script(config, 'piloop==0.5.0'))
    env = {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    done = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True)
    assert done.returncode == 1
    assert log.read_text().splitlines() == ['uv tool install --quiet piloop==0.5.0', 'first']


def test_the_worker_command_reads_the_credential_deletes_it_and_runs_the_worker(
    sdk: FakePrized, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ('CREDENTIAL', 'SETUP', 'SETUP_LOG', 'WORKER_LOG'):
        monkeypatch.setattr(module, name, str(tmp_path / name.lower()))
    log = tmp_path / 'log'
    stand_ins(tmp_path / '.local' / 'bin', log)
    for fork in (True, False):
        log.write_text('')
        provisioning = opened(f'owner-{fork}', fork=fork, setup=[f'echo setup >> {log}'])
        provisioning.start(LAUNCH)
        box = workers(sdk)[-1]
        for path, (data, _) in box.files.items():
            Path(path).write_text(str(data))
        [(command, _)] = box.commands
        env = {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
        assert subprocess.run(['bash', '-c', command], env=env).returncode == 0
        ran = log.read_text().splitlines()
        assert ran[-2:] == [
            'piloop worker --server https://piloop.test --pool agents',
            "PILOOP_HTTPS_TOKEN=plt_se'cret",
        ]
        assert ('setup' in ran) is not fork, 'a fork runs no setup: its parent did'
        assert not Path(module.CREDENTIAL).exists(), 'the worker deleted its credential'


def test_a_fresh_sandbox_whose_setup_fails_deletes_its_credential_and_starts_no_worker(
    sdk: FakePrized, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ('CREDENTIAL', 'SETUP', 'SETUP_LOG', 'WORKER_LOG'):
        monkeypatch.setattr(module, name, str(tmp_path / name.lower()))
    log = tmp_path / 'log'
    stand_ins(tmp_path / '.local' / 'bin', log)
    opened('owner-a', fork=False, setup=['exit 3']).start(LAUNCH)
    [box] = workers(sdk)
    for path, (data, _) in box.files.items():
        Path(path).write_text(str(data))
    [(command, _)] = box.commands
    done = subprocess.run(['bash', '-c', command], env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'})
    assert done.returncode == 1 and not Path(module.CREDENTIAL).exists()
    assert 'piloop worker' not in log.read_text()
