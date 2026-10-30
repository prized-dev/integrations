"""mini-SWE-agent's SWE-bench batch runner (``mini-extra swebench``) with Prized sandboxes.

    python -m minisweagent_prized.swebench --subset verified --split test -m <model> -w 32 -o out/

takes the same options as ``mini-extra swebench`` and runs each instance in its
own Prized sandbox. mini-SWE-agent's runner passes the instance's image only to
the environments it knows by name, so this module adds ``prized`` to them before
it starts the runner. Prized options go through ``-c``, e.g.
``-c environment.vcpu=4``.
"""

from __future__ import annotations

import sys

from jinja2 import StrictUndefined, Template
from minisweagent.environments import _ENVIRONMENT_MAPPING, get_environment
from minisweagent.run.benchmarks import swebench as _swebench

NAME = "prized"
CLASS_PATH = "minisweagent_prized.PrizedEnvironment"
_NAMES = {NAME, CLASS_PATH, "minisweagent_prized.environment.PrizedEnvironment"}
_original_get_sb_environment = _swebench.get_sb_environment


def get_sb_environment(config: dict, instance: dict):
    """As mini-SWE-agent's own, plus the instance's image for the ``prized`` environment."""
    env_config = {**config.get("environment", {})}
    if env_config.get("environment_class") not in _NAMES:
        return _original_get_sb_environment(config, instance)
    env_config["environment_class"] = CLASS_PATH
    env_config["image"] = _swebench.get_swebench_docker_image_name(instance)
    env = get_environment(env_config)
    if startup_command := config.get("run", {}).get("env_startup_command"):
        startup_command = Template(startup_command, undefined=StrictUndefined).render(**instance)
        out = env.execute({"command": startup_command})
        if out["returncode"] != 0:
            raise RuntimeError(f"Error executing startup command: {out}")
    return env


def install() -> None:
    """Makes ``prized`` a known environment for mini-SWE-agent's SWE-bench runner."""
    _ENVIRONMENT_MAPPING.setdefault(NAME, CLASS_PATH)
    _swebench.get_sb_environment = get_sb_environment


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a == "--environment-class" or a.startswith("--environment-class=") for a in argv):
        argv += ["--environment-class", NAME]
    install()
    _swebench.app(argv)


if __name__ == "__main__":
    main()
