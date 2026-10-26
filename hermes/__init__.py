"""Prized terminal backend plugin for Hermes Agent.

Registers ``terminal.backend: prized``: Hermes runs its terminal, file and
``execute_code`` commands in a Prized sandbox, a Firecracker microVM that
pauses between turns WITH its memory, so a dev server or any other background
process the agent started is still running at the next command.

Install into ``~/.hermes/plugins/prized/``, then:

    hermes plugins enable prized
    hermes config set terminal.backend prized
    # PRIZED_API_KEY=dcp_... in ~/.hermes/.env (Dashboard -> Settings -> API keys)

Optional settings (``plugins.entries.prized.settings.*`` in config.yaml):
``scope`` (``instance``: one sandbox per Hermes home, the default; ``session``:
one per task), ``template``, ``vcpu``, ``memory_mb``, ``auto_pause_min``,
``max_runtime_min``, ``labels``, ``api_url``. See README.md.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

#: Manifest name; the default settings namespace.
_PLUGIN_ID = "prized"
_SETTING_KEYS = ("scope", "template", "vcpu", "memory_mb", "auto_pause_min", "max_runtime_min", "labels",
                 "api_url")
_KEY_NAME = "PRIZED_API_KEY"


def _env_module():
    if __package__:
        from . import prized_environment
    else:  # tests import the repo root directly
        import prized_environment
    return prized_environment


def _failed_read_marker() -> Optional[type]:
    """Core's marker class for a fallback config, wherever this Hermes defines it."""
    import importlib

    for module in ("hermes_cli.config_read_errors", "hermes_cli.config"):
        try:
            marker = getattr(importlib.import_module(module), "FailedConfigRead")
        except (ImportError, AttributeError):
            continue
        if isinstance(marker, type):
            return marker
    return None


def _read_settings(plugin_id: str) -> Dict[str, Any]:
    """Raw plugin settings from one config snapshot, or raise if config.yaml could not be read.

    When config.yaml cannot be parsed, core serves a fallback copy instead of
    raising; settings taken from it could silently drop the sizing or labels
    the operator just wrote, so this refuses instead. Lookup order matches
    ``PluginContext.get_config``: ``settings`` first, then the legacy ``config``.
    """
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    marker = _failed_read_marker()
    if marker is not None and isinstance(config, marker):
        raise RuntimeError(
            "Prized backend refused to start: config.yaml could not be read "
            f"({getattr(config, 'read_error', None)}), so its plugin settings cannot be "
            "trusted. Fix config.yaml (`hermes config check`).")
    entry: Any = config
    for part in ("plugins", "entries", plugin_id):
        entry = entry.get(part) if isinstance(entry, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    settings = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
    legacy = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    return {key: settings[key] if key in settings else legacy.get(key) for key in _SETTING_KEYS}


def _get_key() -> Optional[str]:
    from agent.secret_scope import get_secret

    value = get_secret(_KEY_NAME)
    return value.strip() if isinstance(value, str) and value.strip() else None


class PrizedProvider(TerminalEnvironmentProvider):
    """Prized Sandboxes: Firecracker microVMs that pause with their memory."""

    name = "prized"
    display_name = "Prized Sandboxes"
    is_remote = True
    is_container = True
    # Sandboxes are isolated microVMs with no host paths mounted, so dangerous-command
    # approval is skipped there, as for the other cloud sandboxes (skip_container_guards
    # defaults to is_container). Ephemeral runs always create their own sandbox and never
    # adopt a stored one, so two of them can never share a VM.
    session_isolated_when_nonpersistent = False

    def __init__(self, plugin_id: str = _PLUGIN_ID):
        self._plugin_id = plugin_id

    def _settings(self) -> dict:
        return _env_module().resolve_settings(_read_settings(self._plugin_id))

    @property
    def description(self) -> str:
        return ("Run commands in a Prized sandbox: a Linux microVM that pauses between turns "
                "with its memory, so background processes keep running.")

    @property
    def env_description(self) -> str:
        return "a Prized sandbox (Ubuntu 24.04 microVM; user prized without sudo; home /home/prized)"

    @property
    def cache_path_base(self) -> Optional[str]:
        return "~/.hermes"

    @property
    def default_cwd(self) -> Optional[str]:
        return "/home/prized/work"

    @property
    def strip_env_keys(self) -> frozenset:
        return frozenset({_KEY_NAME})

    def is_available(self) -> bool:
        try:
            return _get_key() is not None
        except Exception:
            return False

    def check_requirements(self, config: Dict[str, Any]) -> bool:
        import logging

        if self.is_available():
            return True
        logging.getLogger(__name__).error(
            "Prized backend requires PRIZED_API_KEY: create a key in the Prized dashboard "
            "(Settings > API keys) and put it in ~/.hermes/.env as PRIZED_API_KEY.")
        return False

    def probe(self):
        if self.is_available():
            return ("ready", "")
        return ("needs_setup", "Set PRIZED_API_KEY to use the Prized backend.")

    def setup_instructions(self):
        return [
            "Linux microVMs that pause between turns with their memory and disk.",
            "Sign up and create an API key at https://prized.dev/?via=hermes (Settings > API keys).",
            "Save it in ~/.hermes/.env as PRIZED_API_KEY.",
            "Defaults: prized-base-v1, 2 vCPU, 4 GB, auto-pause after 10 idle minutes.",
            "A sandbox runs 24 hours by default (60 minutes on the free tier); Hermes then starts a fresh one.",
        ]

    def doctor_checks(self):
        key = None
        try:
            key = _get_key()
        except Exception:
            pass
        rows = [(key is not None, "Prized API key",
                 "(configured)" if key else "(required: set PRIZED_API_KEY in ~/.hermes/.env)")]
        try:
            s = self._settings()
        except Exception as exc:
            rows.append((False, "Prized settings", f"(invalid plugin settings: {exc})"))
        else:
            rows.append((True, "Prized settings",
                         f"{s['template']}, {s['vcpu']} vCPU, {s['memory_mb']} MB, "
                         f"auto-pause {s['auto_pause_min'] or 'off'} min, scope {s['scope']}"))
        return rows

    def create_environment(self, *, cwd, timeout, task_id="default", image=None,
                           container_config=None, **kwargs):
        # ``image`` is the Docker image setting and does not apply: the ``template``
        # plugin setting picks the sandbox image. container_cpu/memory are Hermes-wide
        # defaults (1 CPU, 5 GB) that exceed the free tier's 4 GB, so sizing comes from
        # the plugin's own ``vcpu`` / ``memory_mb`` settings instead.
        key = _get_key()
        if key is None:
            raise ValueError("Prized backend requires PRIZED_API_KEY "
                             "(Settings > API keys at https://prized.dev/?via=hermes).")
        cc = container_config or {}
        return _env_module().PrizedEnvironment(
            api_key=key, cwd=cwd, timeout=timeout, task_id=task_id,
            persistent_filesystem=bool(cc.get("container_persistent", True)),
            settings=_read_settings(self._plugin_id))


def register(ctx):
    ctx.register_terminal_environment_provider(
        PrizedProvider(plugin_id=getattr(ctx, "plugin_id", None) or _PLUGIN_ID))
