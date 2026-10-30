"""Prized Sandboxes as a sandbox provider for Scale's AgentEnv Framework (``agent-env``).

Installed, it registers as the ``prized`` sandbox provider through agent-env's ``agent_env.sandbox_providers``
entry point; select it in ``.agentenv/config.toml`` (see the README).
"""

from agentenv_prized.provider import DEFAULT_TEMPLATE, PrizedSandboxProvider
from agentenv_prized.sandbox import PrizedSandbox, RootMode

__all__ = ["DEFAULT_TEMPLATE", "PrizedSandbox", "PrizedSandboxProvider", "RootMode"]
__version__ = "0.1.0"
