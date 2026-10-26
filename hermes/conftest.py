"""Test bootstrap: resolve hermes-agent imports and the plugin modules.

The plugin's unit tests import ``agent.*`` and ``tools.*`` from a hermes-agent
checkout with the pluggable terminal-backend extension point (PR #94400). The
checkout is chosen in this order: ``HERMES_AGENT_REPO``; the current directory
when it is a hermes-agent checkout (``scripts/run_tests.sh`` runs from there and
clears the environment); then ``~/.hermes/hermes-agent``.
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent


def _hermes_agent_repo() -> Path:
    explicit = os.environ.get("HERMES_AGENT_REPO")
    if explicit:
        return Path(explicit).expanduser()
    cwd = Path.cwd()
    if (cwd / "agent" / "terminal_env_provider.py").is_file():
        return cwd
    return Path.home() / ".hermes" / "hermes-agent"


for p in (str(_hermes_agent_repo()), str(_REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

collect_ignore = ["__init__.py", "prized_environment.py"]
