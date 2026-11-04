"""The ``prized`` provisioner against the real API: piloop's conformance check, then four workers forked from one warm
parent. Opt-in: ``PRIZED_LIVE=1`` and ``PRIZED_API_KEY``. Every sandbox it makes is deleted at the end, by label.

Its cost is bounded by its keys: at most 5 sandboxes at once (a parent and 4 workers) of 2 vCPU and 8 GiB, each
deleted by Prized after 30 minutes of running whatever happens here, so a few cents in practice and well under $2.
"""

import json
import os
import time
import uuid
from collections.abc import Iterator

import prized
import pytest
from piloop.capabilities import PROVISIONERS, InstanceState, Launch, ProvisionerHost, check_provisioning
from piloop.otel import Tracer
from pydantic import SecretStr

from piloop_prized import Prized, PrizedProvisioning, cleanup
from piloop_prized.prized import CREDENTIAL, OWNER, PARENT, ROLE, WORKER_LOG

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get('PRIZED_LIVE') != '1' or not os.environ.get('PRIZED_API_KEY'),
        reason='live: set PRIZED_LIVE=1 and PRIZED_API_KEY; it creates real sandboxes',
    ),
]

CAPPED = {'cpu': 2, 'memory_mb': 8192, 'timeout_hours': 0.5, 'max_parents': 1, 'parent_hours': 1}
"""The shape of an agent's sandbox in piloop's evals, with each sandbox's life capped at 30 minutes."""
TOKEN = 'plt_live_not_a_real_token'


def launch(name: str) -> Launch:
    return Launch(
        name=name,
        pool='live',
        argv=['piloop', 'worker', '--server', 'http://127.0.0.1:9', '--pool', 'live'],
        env={'PILOOP_HTTPS_TOKEN': SecretStr(TOKEN)},
    )


def opened(owner: str, **keys: object) -> PrizedProvisioning:
    config = PROVISIONERS.validated({'provider': 'prized', 'piloop': 'piloop==0.5.0', **CAPPED, **keys})
    assert isinstance(config, Prized)
    return config.open(ProvisionerHost(owner, Tracer(None)))


def gone(owner: str) -> None:
    """Delete every sandbox of ``owner``, and wait until Prized lists none."""
    cleanup(owner)
    deadline = time.monotonic() + 120
    while prized.Sandbox.list(labels={OWNER: owner}):
        assert time.monotonic() < deadline, f'sandboxes of {owner} are still listed 120 s after their delete'
        time.sleep(3)


@pytest.fixture
def owners() -> Iterator[list[str]]:
    made: list[str] = []
    yield made
    for owner in made:
        gone(owner)


def test_prized_passes_the_provisioner_conformance_check_on_live_sandboxes(owners: list[str]) -> None:
    name = f'live-{uuid.uuid4().hex[:8]}'
    owners += [f'conformance-{name}', f'conformance-other-{name}']
    check_provisioning(lambda owner: opened(owner), launch(name), timeout=300)


def test_four_workers_fork_one_warm_parent_and_start_their_workers(owners: list[str]) -> None:
    owner = f'live-{uuid.uuid4().hex[:12]}'
    owners.append(owner)
    provisioning = opened(owner, setup=['echo warm > "$HOME/warm.txt"'])
    seconds: dict[str, float] = {}
    for i in range(4):
        began = time.monotonic()
        provisioning.start(launch(f'live-{i}'))
        seconds[f'start {i}'] = round(time.monotonic() - began, 1)
    listed = provisioning.list()
    assert sorted(i.name for i in listed) == [f'live-{i}' for i in range(4)]
    assert {i.state for i in listed} == {InstanceState.RUNNING}
    [parent] = prized.Sandbox.list(labels={OWNER: owner, ROLE: PARENT})
    assert parent.state == 'paused'
    checks = {
        'warm': 'cat "$HOME/warm.txt"',
        'piloop': 'export PATH="$HOME/.local/bin:$PATH"; piloop --version',
        'credential': f'test -e {CREDENTIAL} && echo present || echo deleted',
        'token in env': f'env | grep -c {TOKEN} || true',
        'worker': f'sleep 5; pgrep -fc "piloop worker" || true; head -c 300 {WORKER_LOG} 2>/dev/null || true',
        'sudo': 'sudo -n true 2>/dev/null && echo root || echo no-root',
    }
    seen: dict[str, dict[str, str]] = {}
    for instance in listed:
        sandbox = prized.Sandbox.connect(str(instance.handle['id']))
        try:
            seen[instance.name] = {k: sandbox.exec(cmd, timeout=60).stdout.strip() for k, cmd in checks.items()}
        finally:
            sandbox.close()
    print(json.dumps({'seconds': seconds, 'parent': parent.id, 'seen': seen}, indent=2))
    for found in seen.values():
        assert found['warm'] == 'warm', 'a fork has the files of its parent'
        assert found['piloop'].startswith('piloop 0.5.0')
        assert found['credential'] == 'deleted' and found['token in env'] == '0'
    for i in range(4):
        provisioning.stop(f'live-{i}')
    assert {i.state for i in provisioning.list()} <= {InstanceState.ENDED}
