"""The rest of the Agents SDK's provider contract (docs/sandbox/custom-providers.md).

- Options and session state parse by their `prized` type in a fresh
  interpreter once the package is imported, and not before; unknown and
  malformed options are refused, and a valid round trip holds.
- A session state saved by 0.1.0, the first release (tests/state_0_1_0.json),
  still deserializes and resumes: in its paused sandbox, or in a new one with
  the same options when that sandbox is gone.
- Malformed timeouts, which options carry as given, are refused by `create()`
  before any request is sent.
- Exposed ports are refused, never served some other way.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agents.sandbox.errors import ExposedPortUnavailableError
from pydantic import ValidationError

from openai_agents_prized import (
    PrizedSandboxClientOptions,
    PrizedSandboxSession,
    PrizedSandboxSessionState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai_agents_prized import PrizedSandboxClient
    from tests.fake_prized import FakePrized

SAVED_0_1_0 = Path(__file__).with_name("state_0_1_0.json")

FRESH = r"""
import json, sys

from agents.sandbox.session import BaseSandboxClientOptions, SandboxSessionState

saved = json.loads(sys.argv[1])
out = {}
try:
    out["before_import"] = type(BaseSandboxClientOptions.parse({"type": "prized"})).__name__
except Exception as exc:
    out["before_import"] = type(exc).__name__

import openai_agents_prized

options = BaseSandboxClientOptions.parse(
    {"type": "prized", "template": "prized-agent-v1", "vcpu": 4, "pause_on_exit": True}
)
out["options"] = type(options).__name__
dumped = json.loads(json.dumps(options.model_dump(mode="json")))
out["round_trip"] = BaseSandboxClientOptions.parse(dumped) == options
out["refused"] = []
for bad in (
    {"type": "prized", "exposed_ports": [8080]},
    {"type": "prized", "vcpu": "four"},
    {"type": "prized", "template": 5},
):
    try:
        BaseSandboxClientOptions.parse(bad)
    except Exception as exc:
        out["refused"].append(type(exc).__name__)
state = SandboxSessionState.parse(saved)
out["state"] = type(state).__name__
out["state_round_trip"] = SandboxSessionState.parse(json.loads(state.model_dump_json())) == state
print(json.dumps(out))
"""


def _saved() -> dict[str, Any]:
    payload = json.loads(SAVED_0_1_0.read_text())
    assert isinstance(payload, dict)
    return payload


def test_types_register_on_import_in_a_fresh_interpreter(tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, "-I", "-c", FRESH, json.dumps(_saved())],
        capture_output=True,
        text=True,
        cwd=tmp_path,  # not the source tree: the installed package is what is imported
        check=False,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["before_import"] != "PrizedSandboxClientOptions"
    assert out["options"] == "PrizedSandboxClientOptions"
    assert out["round_trip"] is True
    assert len(out["refused"]) == 3
    assert out["state"] == "PrizedSandboxSessionState"
    assert out["state_round_trip"] is True


async def test_a_state_saved_by_0_1_0_resumes_its_paused_sandbox(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    payload = _saved()
    payload["sandbox_id"] = fake.add_sandbox(state="paused", template="prized-agent-v1")
    client = make_client()
    state = client.deserialize_session_state(payload)
    assert isinstance(state, PrizedSandboxSessionState)
    assert (state.template, state.vcpu, state.memory_mb, state.disk_gb) == (
        "prized-agent-v1",
        2,
        4096,
        20,
    )
    assert (state.base_env_vars, state.labels) == ({"APP_MODE": "test"}, {"team": "agents"})
    assert (state.auto_pause_min, state.auto_delete_min, state.max_runtime_min) == (10, 1440, 120)
    assert state.pause_on_exit is True
    assert state.timeouts.start_s == 120.0
    session = await client.resume(state)
    async with session:
        assert fake.sandbox(payload["sandbox_id"]).state == "running"
        result = await session.exec("echo $GREETING")
        assert result.stdout.decode().strip() == "hi"
    assert fake.sandbox(payload["sandbox_id"]).state == "paused"  # pause_on_exit, as saved
    assert len(fake.sandboxes) == 1


async def test_a_state_saved_by_0_1_0_whose_sandbox_is_gone_gets_one_with_its_options(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    client = make_client()
    session = await client.resume(client.deserialize_session_state(_saved()))
    inner = session._inner
    assert isinstance(inner, PrizedSandboxSession)
    assert inner.sandbox_id != "sbx_9f8e7d6c5b4a3921"
    (create,) = [r.json for r in fake.calls("POST", r"^/api/v1/sandboxes$")]
    assert create == {
        "template": "prized-agent-v1",
        "resources": {"vcpu": 2, "memoryMb": 4096, "diskGb": 20},
        "envVars": {"APP_MODE": "test"},
        "labels": {"team": "agents"},
        "autoPauseMin": 10,
        "autoDeleteMin": 1440,
        "maxRuntimeMin": 120,
    }
    await client.delete(session)
    assert fake.sandbox(inner.sandbox_id).state == "paused"  # pause_on_exit, as saved


async def test_malformed_timeouts_are_refused_before_any_request(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    options = PrizedSandboxClientOptions(timeouts={"start_s": -1})
    with pytest.raises(ValidationError, match="start_s"):
        await make_client().create(options=options)
    assert fake.calls("POST", r"^/api/v1/sandboxes$") == []


async def test_exposed_ports_are_refused(
    make_client: Callable[..., PrizedSandboxClient], fake: FakePrized
) -> None:
    with pytest.raises(TypeError, match="exposed_ports"):
        PrizedSandboxClientOptions.model_validate({"exposed_ports": [8080]})
    # A state that names a port (written by hand, or by another tool) still gets
    # no endpoint: Prized has no inbound network, and nothing is served instead.
    payload = _saved()
    payload["sandbox_id"] = fake.add_sandbox(state="paused", template="prized-agent-v1")
    payload["exposed_ports"] = [8080]
    client = make_client()
    session = await client.resume(client.deserialize_session_state(payload))
    with pytest.raises(ExposedPortUnavailableError) as refused:
        await session.resolve_exposed_port(8080)
    assert refused.value.context["reason"] == "backend_unavailable"
    await client.delete(session)
