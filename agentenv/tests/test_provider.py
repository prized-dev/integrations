"""The provider against the fake SDK: config, sizing, labels, ports, cleanup, reconnect and teardown."""

from __future__ import annotations

import asyncio
import logging
from importlib.metadata import entry_points

import pytest
from agent_env.config.errors import ConfigError
from agent_env.providers.sandbox_providers.sandbox import (
    NetworkMode,
    NetworkPolicy,
    NetworkPolicyUnsupportedError,
)
from agent_env.providers.sandbox_providers.sandbox_provider import (
    SANDBOX_MODE_CONTAINER,
    SANDBOX_MODE_VM,
    SandboxNotFoundError,
    build_sandbox_provider,
)

from agentenv_prized.provider import (
    DEFAULT_TEMPLATE,
    MARK_LABEL,
    PORTS_LABEL,
    PrizedSandboxProvider,
    sandbox_labels,
    sandbox_shape,
)
from agentenv_prized.sandbox import PrizedSandbox

from .conftest import FAKE_KEY, docker_ready, root_mode
from .fakes import Result, error

PREVIEW = {"previewUrlTemplate": "https://{port}-sbx-0000000000000001.p.example.test/"}


# --- registration and config ------------------------------------------------------------------


def test_entry_point_names_the_provider():
    (entry,) = [ep for ep in entry_points(group="agent_env.sandbox_providers") if ep.name == "prized"]
    assert entry.load() is PrizedSandboxProvider


def test_both_sandbox_modes_and_vm_images():
    assert PrizedSandboxProvider.SUPPORTED_SANDBOX_MODES == frozenset({SANDBOX_MODE_VM, SANDBOX_MODE_CONTAINER})
    assert PrizedSandbox.type == "prized"


def test_built_from_config_through_agent_env(tmp_path, monkeypatch):
    """agent-env finds the plugin by its entry point and resolves the env: reference into api_key."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[sandbox.providers.prized.config]\napi_key = "env:PRIZED_TEST_KEY"\ntemplate = "prized-docker-v1"\n'
    )
    monkeypatch.setenv("AGENT_ENV_CONFIG", str(config))
    monkeypatch.setenv("PRIZED_TEST_KEY", FAKE_KEY)
    from agent_env.config import reset_config

    reset_config()
    provider = build_sandbox_provider("prized")
    assert type(provider).__name__ == "PrizedSandboxProvider"
    assert provider._api_key == FAKE_KEY


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "requires a non-empty 'api_key'"),
        ({"api_key": ""}, "requires a non-empty 'api_key'"),
        ({"api_key": FAKE_KEY, "region": "us"}, "unknown key(s): ['region']"),
        ({"api_key": FAKE_KEY, "template": ""}, "'template' must be a non-empty string"),
        ({"api_key": FAKE_KEY, "base_url": 3}, "'base_url' must be a non-empty string"),
    ],
)
def test_from_config_refuses_bad_config(config, message):
    with pytest.raises(ConfigError) as caught:
        PrizedSandboxProvider.from_config(**config)
    assert message in str(caught.value)


def test_from_config_defaults_to_the_docker_template():
    provider = PrizedSandboxProvider.from_config(api_key=FAKE_KEY)
    assert provider._template == DEFAULT_TEMPLATE == "prized-docker-v1"


# --- sizing and labels ------------------------------------------------------------------------


def test_shape_rounds_up_to_whole_units():
    assert sandbox_shape(1.0, 8192, 10) == (1, 8192, 10)
    assert sandbox_shape(0.5, 1000.5, 9.2) == (1, 1001, 10)
    assert sandbox_shape(2.1, 4096, 30) == (3, 4096, 30)


@pytest.mark.parametrize("shape", [(33, 1024, 10), (1, 131073, 10), (1, 1024, 129)])
def test_shape_beyond_the_ceiling_is_refused_not_shrunk(shape):
    with pytest.raises(ValueError, match="cannot size"):
        sandbox_shape(*shape)


def test_labels_carry_the_mark_ports_and_attribution_within_limits(caplog):
    attribution = {"team": "rl", "run id": "r/1", "none": None, **{f"k{i}": "v" * 300 for i in range(40)}}
    with caplog.at_level(logging.WARNING):
        labels = sandbox_labels(attribution, [8080, 9000])
    assert labels[MARK_LABEL] == "true"
    assert labels[PORTS_LABEL] == "8080,9000"
    assert labels["agentenv-team"] == "rl"
    assert labels["agentenv-run-id"] == "r/1"
    assert "agentenv-none" not in labels
    assert len(labels) == 32
    assert all(len(key) <= 64 and len(value) <= 256 for key, value in labels.items())
    assert "left off" in caplog.text


def test_attribution_cant_pose_as_the_ports_label():
    labels = sandbox_labels({"ports": "8080", "agentenv.ports": "1"}, [])
    assert PORTS_LABEL not in labels
    assert labels["agentenv-ports"] == "8080"


# --- create -----------------------------------------------------------------------------------


async def test_create_vm_sends_template_shape_runtime_and_labels(provider, cloud):
    sandbox = await provider.create_vm(
        cpu=1.5, memory=4096, disk_size_gb=20, timeout=7201, setup_for_gateway=False, attribution={"team": "rl"}
    )
    (record,) = cloud.sandboxes.values()
    assert record.template == "prized-docker-v1"
    kwargs = record.create_kwargs
    assert (kwargs["vcpu"], kwargs["memory_mb"], kwargs["disk_gb"]) == (2, 4096, 20)
    assert kwargs["max_runtime_min"] == 121
    assert kwargs["api_key"] == FAKE_KEY
    assert record.labels == {MARK_LABEL: "true", "agentenv-team": "rl"}
    assert (sandbox.type, sandbox.mode, sandbox.sandbox_id) == ("prized", "vm", record.id)
    assert sandbox.network_policy == NetworkPolicy()
    assert sandbox.tunnel_urls == {}
    assert cloud.commands == []


async def test_create_vm_with_setup_waits_for_docker_and_settles_root(provider, cloud):
    docker_ready(cloud)
    root_mode(cloud, "docker")
    cloud.answer(lambda command: Result() if "--name agentenv-prized-root" in command else None)
    sandbox = await provider.create_vm(timeout=600)
    assert any("docker compose version" in command for command in cloud.commands)
    assert await sandbox.root_mode() == "docker"


async def test_port_urls_that_need_a_key_are_refused_and_the_sandbox_deleted(provider, cloud):
    """The API names port URLs once ingress is on, but they answer only requests with a Prized key."""
    cloud.document = dict(PREVIEW)
    with pytest.raises(ValueError, match="carry a Prized API key"):
        await provider.create_vm(exposed_ports=[8080, 8080, 3000], setup_for_gateway=False)
    (record,) = cloud.sandboxes.values()
    assert record.state == "deleted"
    assert record.labels[PORTS_LABEL] == "8080,3000"


async def test_exposed_ports_without_port_urls_delete_the_sandbox(provider, cloud):
    with pytest.raises(ValueError, match="no port URLs"):
        await provider.create_vm(exposed_ports=[8080], setup_for_gateway=False)
    (record,) = cloud.sandboxes.values()
    assert record.state == "deleted"


@pytest.mark.parametrize("port", [22, 81, 0, 70000])
async def test_reserved_or_invalid_ports_are_refused_before_create(provider, cloud, port):
    with pytest.raises(ValueError):
        await provider.create_vm(exposed_ports=[port], setup_for_gateway=False)
    assert cloud.sandboxes == {}


async def test_unsatisfiable_requests_are_refused_before_create(provider, cloud):
    with pytest.raises(ValueError, match="per-call image"):
        await provider.create_vm(image="ubuntu:24.04", setup_for_gateway=False)
    with pytest.raises(ValueError, match="cannot size"):
        await provider.create_vm(cpu=64, setup_for_gateway=False)
    with pytest.raises(ValueError, match="7 days"):
        await provider.create_vm(timeout=8 * 24 * 3600, setup_for_gateway=False)
    assert cloud.sandboxes == {}


async def test_boot_mode_is_ignored_with_a_warning(provider, cloud, caplog):
    with caplog.at_level(logging.WARNING):
        await provider.create_vm(boot_mode="uefi", setup_for_gateway=False)
    assert "Ignoring boot_mode=uefi" in caplog.text
    assert len(cloud.sandboxes) == 1


async def test_restricted_egress_is_refused_before_create(provider, cloud):
    policy = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allow_hosts=("example.com",))
    assert not provider.supports_network_policy(policy)
    assert provider.supports_network_policy(NetworkPolicy())
    with pytest.raises(NetworkPolicyUnsupportedError):
        await provider.create_vm(network_policy=policy, setup_for_gateway=False)
    assert cloud.sandboxes == {}


async def test_a_refused_shape_is_a_value_error(provider, cloud):
    cloud.create_error = error("invalid_resources", 400)
    with pytest.raises(ValueError, match="Prized refused the sandbox"):
        await provider.create_vm(setup_for_gateway=False)


async def test_a_platform_failure_keeps_its_error(provider, cloud):
    cloud.create_error = error("sandbox_capacity", 503)
    with pytest.raises(Exception) as caught:
        await provider.create_vm(setup_for_gateway=False)
    assert getattr(caught.value, "code", None) == "sandbox_capacity"


async def test_failed_setup_deletes_the_sandbox(provider, cloud):
    docker_ready(cloud, ok=False)
    with pytest.raises(RuntimeError, match="prized-docker-v1"):
        await provider.create_vm(timeout=600)
    (record,) = cloud.sandboxes.values()
    assert record.state == "deleted"


async def test_cancelled_setup_deletes_the_sandbox(provider, cloud):
    started = asyncio.Event()
    original = cloud.run

    async def slow_run(command: str):
        if "docker compose version" in command:
            started.set()
            await asyncio.sleep(3600)
        return await original(command)

    cloud.run = slow_run
    task = asyncio.ensure_future(provider.create_vm(timeout=600))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (record,) = cloud.sandboxes.values()
    assert record.state == "deleted"


async def test_an_agent_sandbox_needs_its_port_reachable(provider, cloud):
    """create_sandbox is a VM exposing the agent's port, so it waits on port URLs like any exposed port."""
    docker_ready(cloud)
    with pytest.raises(ValueError, match="exposed ports can't be reached yet"):
        await provider.create_sandbox(image_name="agent:latest", port=8000, env={"A": "b"}, timeout=600)
    (record,) = cloud.sandboxes.values()
    assert (record.state, record.labels[PORTS_LABEL]) == ("deleted", "8000")
    assert cloud.commands == []


async def test_a_second_cancellation_still_deletes_the_sandbox(provider, cloud):
    started = asyncio.Event()
    original = cloud.run

    async def slow_run(command: str):
        if "docker compose version" in command:
            started.set()
            await asyncio.sleep(3600)
        return await original(command)

    cloud.run = slow_run
    cloud.delete_delay = 0.2
    task = asyncio.ensure_future(provider.create_vm(timeout=600))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(50):
        if all(record.state == "deleted" for record in cloud.sandboxes.values()):
            break
        await asyncio.sleep(0.05)
    (record,) = cloud.sandboxes.values()
    assert record.state == "deleted"


def test_a_run_that_ends_right_after_a_double_cancel_still_deletes(cloud):
    """asyncio.run cancels the tasks left when its main coroutine returns; the cleanup delete finishes anyway."""
    provider = PrizedSandboxProvider(api_key=FAKE_KEY, sandbox_cls=cloud.cls())

    async def main():
        started = asyncio.Event()
        original = cloud.run

        async def slow_run(command: str):
            if "docker compose version" in command:
                started.set()
                await asyncio.sleep(3600)
            return await original(command)

        cloud.run = slow_run
        cloud.delete_delay = 0.3
        task = asyncio.ensure_future(provider.create_vm(timeout=600))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0)

    asyncio.run(main())
    (record,) = cloud.sandboxes.values()
    assert record.state == "deleted"


# --- reconnect and teardown -------------------------------------------------------------------


async def test_get_sandbox_from_a_fresh_provider_restores_the_handle(cloud, caplog):
    first = PrizedSandboxProvider(api_key=FAKE_KEY, sandbox_cls=cloud.cls())
    made = await first.create_vm(setup_for_gateway=False)
    fresh = PrizedSandboxProvider(api_key=FAKE_KEY, sandbox_cls=cloud.cls())
    again = await fresh.get_sandbox(made.sandbox_id)
    assert (again.type, again.mode, again.sandbox_id) == ("prized", "vm", made.sandbox_id)
    assert again.network_policy == NetworkPolicy()
    assert again.tunnel_urls == {}
    # A sandbox whose labels name ports it has no usable URLs for reconnects with none, saying so.
    cloud.sandboxes[made.sandbox_id].labels[PORTS_LABEL] = "8080"
    with caplog.at_level(logging.WARNING):
        assert (await fresh.get_sandbox(made.sandbox_id)).tunnel_urls == {}
    assert "no port URLs" in caplog.text


async def test_get_sandbox_of_a_gone_sandbox_raises_not_found(provider, cloud):
    with pytest.raises(SandboxNotFoundError):
        await provider.get_sandbox("sbx_ffffffffffffffff")
    made = await provider.create_vm(setup_for_gateway=False)
    cloud.sandboxes[made.sandbox_id].state = "deleting"
    with pytest.raises(SandboxNotFoundError):
        await provider.get_sandbox(made.sandbox_id)


async def test_terminate_is_idempotent(provider, cloud):
    made = await provider.create_vm(setup_for_gateway=False)
    await made.terminate()
    await made.terminate()
    await provider.terminate_sandbox(made.sandbox_id)
    assert cloud.sandboxes[made.sandbox_id].state == "deleted"
    with pytest.raises(SandboxNotFoundError):
        await provider.get_sandbox(made.sandbox_id)


async def test_terminate_sandbox_deletes_by_id_even_in_error(provider, cloud):
    made = await provider.create_vm(setup_for_gateway=False)
    cloud.sandboxes[made.sandbox_id].state = "error"
    await provider.terminate_sandbox(made.sandbox_id)
    assert cloud.sandboxes[made.sandbox_id].state == "deleted"


async def test_close_releases_sdk_handles_and_the_loop_client_and_keeps_sandboxes(cloud):
    clients = []

    class Client:
        closed = False

        async def aclose(self):
            self.closed = True

    def factory():
        clients.append(Client())
        return clients[-1]

    provider = PrizedSandboxProvider(api_key=FAKE_KEY, sandbox_cls=cloud.cls(), http_client_factory=factory)
    made = await provider.create_vm(setup_for_gateway=False)
    await provider.get_sandbox(made.sandbox_id)
    assert len(clients) == 1
    assert all(r.create_kwargs["http_client"] is clients[0] for r in cloud.sandboxes.values())
    await provider.close()
    assert cloud.closed == 2 and clients[0].closed
    assert cloud.sandboxes[made.sandbox_id].state == "running"
