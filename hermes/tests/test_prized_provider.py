"""The provider half: registration, availability, secret stripping, and what create forwards."""

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    monkeypatch.delenv("PRIZED_API_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("hermes_plugin_prized", _REPO / "__init__.py",
                                                  submodule_search_locations=[str(_REPO)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "hermes_plugin_prized", module)
    spec.loader.exec_module(module)
    return module


def test_registers_one_terminal_backend_named_prized(plugin):
    registered = []

    class Ctx:
        plugin_id = "prized"

        def register_terminal_environment_provider(self, provider):
            registered.append(provider)

    plugin.register(Ctx())
    (provider,) = registered
    assert provider.name == "prized"
    from agent.terminal_env_registry import BUILTIN_BACKEND_NAMES

    assert provider.name not in BUILTIN_BACKEND_NAMES
    assert provider.is_remote and provider.is_container
    assert provider.default_cwd == "/home/prized/work"


def test_the_api_key_is_stripped_from_agent_subprocesses(plugin):
    assert plugin.PrizedProvider().strip_env_keys == frozenset({"PRIZED_API_KEY"})


def test_availability_follows_the_key(plugin, monkeypatch):
    provider = plugin.PrizedProvider()
    assert provider.is_available() is False
    assert provider.probe()[0] == "needs_setup"
    monkeypatch.setenv("PRIZED_API_KEY", "dcp_x")
    assert provider.is_available() is True
    assert provider.probe() == ("ready", "")


def test_create_without_a_key_fails_before_any_request(plugin):
    with pytest.raises(ValueError, match="PRIZED_API_KEY"):
        plugin.PrizedProvider().create_environment(cwd="/home/prized/work", timeout=30)


def test_create_forwards_persistence_and_plugin_settings_not_hermes_container_sizing(plugin, monkeypatch):
    seen = {}

    class FakeEnv:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setenv("PRIZED_API_KEY", "dcp_x")
    monkeypatch.setattr(plugin._env_module(), "PrizedEnvironment", FakeEnv)
    monkeypatch.setattr(plugin, "_read_settings", lambda _id: {"vcpu": 4})
    plugin.PrizedProvider().create_environment(
        cwd="/home/prized/work", timeout=30, task_id="t1", image="docker/image:1",
        container_config={"container_persistent": False, "container_cpu": 1, "container_memory": 5120},
        future_kwarg=True)
    assert seen["persistent_filesystem"] is False
    assert seen["settings"] == {"vcpu": 4}
    assert seen["task_id"] == "t1" and seen["api_key"] == "dcp_x"


def test_doctor_reports_invalid_settings_instead_of_raising(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_read_settings", lambda _id: {"vcpu": 99})
    rows = plugin.PrizedProvider().doctor_checks()
    assert rows[0][0] is False  # no key
    assert rows[1][0] is False and "vcpu" in rows[1][2]
