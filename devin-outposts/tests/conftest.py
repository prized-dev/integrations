from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_outposts import TOKEN, FakeOutposts  # noqa: E402
from fake_prized import World  # noqa: E402

from prized_outposts.devin.api import OutpostsAPI  # noqa: E402
from prized_outposts.devin.orchestrator import Orchestrator  # noqa: E402
from prized_outposts.devin.plan import Rules  # noqa: E402
from prized_outposts.devin.remote import RemoteSource  # noqa: E402
from prized_outposts.machines import Machines, MachineSpec  # noqa: E402

REMOTE = str(Path(__file__).parent / "fake_remote.py")
OUTPOST = "outpost_env-test"


@pytest.fixture
def fake() -> Iterator[FakeOutposts]:
    os.chmod(REMOTE, 0o755)
    server = FakeOutposts(remote_file=REMOTE)
    yield server
    server.close()


@pytest.fixture
def world() -> Iterator[World]:
    w = World()
    yield w
    w.close()


@pytest.fixture
def make(fake: FakeOutposts, world: World) -> Iterator[Callable[..., Orchestrator]]:
    made: list[Orchestrator] = []

    def build(
        acceptor: str = "prized-test-1", warm_parent: str | None = None, **rules: object
    ) -> Orchestrator:
        api = OutpostsAPI(fake.url, TOKEN, acceptor)
        machines = Machines(
            MachineSpec(warm_parent=warm_parent, labels={"task": "devin-outposts"}),
            owner="prized-devin-outposts",
            sandbox_api=world.api(),
            sleep=lambda s: None,
        )
        orch = Orchestrator(
            api,
            machines,
            RemoteSource(static_base=f"{fake.url}/static"),
            outpost_id=OUTPOST,
            rules=Rules(acceptor_id=acceptor, **rules),
            workdir="/home/prized/work",
            poll_interval=0.1,
            sweep_interval=3600,
        )
        made.append(orch)
        return orch

    yield build
    for orch in made:
        orch.drain(10)
        orch.close()


def settle(orch: Orchestrator, until: Callable[[], bool], timeout: float = 30.0) -> None:
    """Run passes (each waits for the actions it started) until ``until()`` holds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        orch.tick()
        orch.drain(30)
        if until():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out; events: {orch.events[-12:]}")
