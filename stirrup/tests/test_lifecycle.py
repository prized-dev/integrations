# Copyright 2026 Prized
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Create, fork, delete, and the options that can't work yet."""

import asyncio
import inspect
import logging

import pytest
from prized import PrizedError
from stirrup.tools.code_backends.base import CodeExecToolProvider

from stirrup_prized import PrizedCodeExecToolProvider, UnsupportedOptionError
from tests import fakes

# E2BCodeExecToolProvider.__init__'s keywords in Stirrup 0.2.1 (e2b.py): every one works here.
E2B_KEYWORDS = {
    "timeout",
    "request_timeout",
    "template",
    "allowed_commands",
    "create_gate",
    "sandbox_kwargs",
    "shell_timeout",
}


def test_takes_every_e2b_keyword():
    params = inspect.signature(PrizedCodeExecToolProvider.__init__).parameters
    assert E2B_KEYWORDS <= params.keys()
    assert issubclass(PrizedCodeExecToolProvider, CodeExecToolProvider)
    assert not PrizedCodeExecToolProvider.__abstractmethods__


async def test_create_maps_e2b_options_and_delete_on_exit(world):
    provider = PrizedCodeExecToolProvider(
        timeout=3600,
        sandbox_kwargs={
            "envs": {"TASK": "t1"},
            "metadata": {"run": "r1"},
            "allow_internet_access": True,
            "secure": True,
            "api_key": "dcp_other",
            "auto_pause_min": 5,
        },
    )
    async with provider as tool:
        remote = world.only()
        assert tool.name == "code_exec"
        assert provider.sandbox is not None and provider.sandbox.id == remote.id
        assert remote.create_kwargs == {
            "template": "prized-base-v1",
            "vcpu": None,
            "memory_gb": None,
            "disk_gb": None,
            "env_vars": {"TASK": "t1"},
            "labels": {"run": "r1"},
            "max_runtime_min": 60,
            "auto_delete_min": 60,
            "timeout": 180,
            "auto_pause_min": 5,
            "api_key": "dcp_other",
        }
        result = await provider.run_command("echo $TASK")
        assert result.stdout == "t1\n"
        assert remote.calls[-1][1]["env"] is None  # sandbox-wide variables were set at create
    assert remote.state == "deleted"
    assert world.handles[0].closed
    assert provider.sandbox is None


@pytest.mark.parametrize(("seconds", "minutes"), [(600, 10), (601, 11), (30, 1), (86_400, 1440)])
async def test_lifetime_rounds_up_to_minutes(world, seconds, minutes):
    async with PrizedCodeExecToolProvider(timeout=seconds, template="prized-agent-v1", vcpu=4, memory_gb=8, disk_gb=20):
        kwargs = world.only().create_kwargs
    assert kwargs["max_runtime_min"] == minutes
    assert (kwargs["template"], kwargs["vcpu"], kwargs["memory_gb"], kwargs["disk_gb"]) == ("prized-agent-v1", 4, 8, 20)


async def test_prized_kwargs_pass_through(world):
    sandbox_kwargs = {"env_vars": {"A": "1"}, "labels": {"x": "y"}, "auto_delete_min": 0, "idempotency_key": "k1"}
    async with PrizedCodeExecToolProvider(sandbox_kwargs=sandbox_kwargs, request_timeout=300):
        kwargs = world.only().create_kwargs
    assert kwargs["env_vars"] == {"A": "1"} and kwargs["labels"] == {"x": "y"}
    assert kwargs["auto_delete_min"] == 0 and kwargs["idempotency_key"] == "k1" and kwargs["timeout"] == 300


async def test_create_gate_is_entered_around_create(world):
    class RecordingGate:
        async def __aenter__(self):
            world.events.append("gate_enter")

        async def __aexit__(self, *_):
            world.events.append("gate_exit")

    async with PrizedCodeExecToolProvider(create_gate=RecordingGate()):
        pass
    assert world.events[:3] == ["gate_enter", "create", "gate_exit"]


async def test_semaphore_works_as_a_gate(world):
    gate = asyncio.Semaphore(2)
    providers = [PrizedCodeExecToolProvider(create_gate=gate) for _ in range(4)]
    await asyncio.gather(*(p.__aenter__() for p in providers))
    assert len(world.remotes) == 4
    await asyncio.gather(*(p.__aexit__(None, None, None) for p in providers))
    assert all(r.state == "deleted" for r in world.remotes.values())


async def test_fork_from_a_prepared_parent(world):
    parent = world.add(template="prized-base-v1")
    with open(parent.local("/home/prized/work/setup.txt"), "w") as f:
        f.write("prepared")
    provider = PrizedCodeExecToolProvider(
        fork_from=parent.id, sandbox_kwargs={"envs": {"TASK": "t2"}, "metadata": {"run": "r2"}}
    )
    async with provider:
        copy = world.remotes[provider.sandbox.id]
        assert copy.id != parent.id
        assert copy.create_kwargs["forked_from"] == parent.id
        assert copy.create_kwargs["labels"] == {"run": "r2"}
        assert copy.create_kwargs["timeout"] == 180
        assert await provider.read_file_bytes("setup.txt") == b"prepared"
        result = await provider.run_command("echo $TASK")
        assert result.stdout == "t2\n"
        assert copy.calls[-1][1]["env"] == {"TASK": "t2"}  # a copy takes variables per command
    assert copy.state == "deleted"
    assert parent.state == "running", "the parent is never deleted"
    assert world.events[0] == f"connect {parent.id}"
    assert all(h.closed for h in world.handles)


async def test_fork_from_a_missing_parent_raises(world):
    with pytest.raises(PrizedError) as info:
        await PrizedCodeExecToolProvider(fork_from="sbx_gone").__aenter__()
    assert info.value.code == "not_found"


@pytest.mark.parametrize(
    "kwargs",
    [{"vcpu": 2}, {"template": "prized-agent-v1"}, {"memory_gb": 4}, {"sandbox_kwargs": {"auto_pause_min": 5}}],
)
def test_fork_from_refuses_shape_options(kwargs):
    with pytest.raises(ValueError, match="copy keeps its parent's"):
        PrizedCodeExecToolProvider(fork_from="sbx_parent", **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"network": False}, "isn't supported yet"),
        ({"sandbox_kwargs": {"allow_internet_access": False}}, "isn't supported yet"),
        ({"image": "ghcr.io/aa/briefcase:latest"}, "Docker mode"),
        ({"template": "prized-docker-v1"}, "2026-11-09"),
    ],
)
def test_unsupported_options_fail_before_anything_is_created(world, kwargs, match):
    with pytest.raises(UnsupportedOptionError, match=match):
        PrizedCodeExecToolProvider(**kwargs)
    assert world.events == []
    assert issubclass(UnsupportedOptionError, ValueError)


async def test_unknown_template_says_what_to_use(world):
    with pytest.raises(UnsupportedOptionError, match="prized-base-v1") as info:
        await PrizedCodeExecToolProvider(template="e2b-sandbox").__aenter__()
    assert isinstance(info.value.__cause__, PrizedError)
    assert info.value.__cause__.code == "template_not_found"


def test_bad_arguments():
    with pytest.raises(TypeError, match="'domain'"):
        PrizedCodeExecToolProvider(sandbox_kwargs={"domain": "e2b.dev"})
    with pytest.raises(ValueError, match="twice"):
        PrizedCodeExecToolProvider(sandbox_kwargs={"envs": {"A": "1"}, "env_vars": {"B": "2"}})
    with pytest.raises(ValueError, match="timeout"):
        PrizedCodeExecToolProvider(timeout=0)
    with pytest.raises(ValueError, match="request_timeout"):
        PrizedCodeExecToolProvider(request_timeout=-1)


async def test_create_errors_propagate(world):
    world.fail("create", PrizedError("sandbox_quota_exceeded", status=403))
    provider = PrizedCodeExecToolProvider()
    with pytest.raises(PrizedError, match="sandbox_quota_exceeded"):
        await provider.__aenter__()
    assert provider.sandbox is None


async def test_not_started_and_started_twice(world):
    provider = PrizedCodeExecToolProvider()
    with pytest.raises(RuntimeError, match="not started"):
        await provider.run_command("true")
    with pytest.raises(RuntimeError, match="not started"):
        await provider.read_file_bytes("x")
    await provider.__aexit__(None, None, None)  # nothing to delete
    async with provider:
        with pytest.raises(RuntimeError, match="already started"):
            await provider.__aenter__()


async def test_failed_delete_raises_when_nothing_else_is(world):
    world.fail("delete", PrizedError("network_error"))
    provider = PrizedCodeExecToolProvider()
    with pytest.raises(PrizedError, match="network_error"):
        async with provider:
            pass
    assert world.handles[0].closed


async def test_failed_delete_does_not_hide_the_run_error(world, caplog):
    world.fail("delete", PrizedError("network_error"))
    with caplog.at_level(logging.WARNING), pytest.raises(KeyError):
        async with PrizedCodeExecToolProvider():
            raise KeyError("the agent's own error")
    assert "Could not delete Prized sandbox" in caplog.text
    assert world.handles[0].closed


async def test_delete_finishes_when_cancelled(world, monkeypatch):
    provider = PrizedCodeExecToolProvider()
    await provider.__aenter__()
    remote = world.only()
    started = asyncio.Event()
    real_delete = fakes.FakeAsyncSandbox.delete

    async def slow_delete(self, **kwargs):
        started.set()
        await asyncio.sleep(0.2)
        await real_delete(self, **kwargs)

    monkeypatch.setattr(fakes.FakeAsyncSandbox, "delete", slow_delete)
    task = asyncio.ensure_future(provider.__aexit__(None, None, None))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert remote.state == "deleted"
