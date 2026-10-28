"""Prized Sandboxes for the OpenAI Agents SDK.

```python
from agents import Runner
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig
from openai_agents_prized import PrizedSandboxClient

agent = SandboxAgent(name="coder", instructions="Work in the sandbox.")
run_config = RunConfig(sandbox=SandboxRunConfig(client=PrizedSandboxClient()))
result = await Runner.run(agent, "Write fizzbuzz.py and run it.", run_config=run_config)
```
"""

from openai_agents_prized._api import PrizedAPIError, PrizedError, PrizedSandboxGoneError
from openai_agents_prized._version import __version__
from openai_agents_prized.sandbox import (
    DEFAULT_PRIZED_TEMPLATE,
    DEFAULT_PRIZED_WORKSPACE_ROOT,
    PrizedSandboxClient,
    PrizedSandboxClientOptions,
    PrizedSandboxSession,
    PrizedSandboxSessionState,
    PrizedSandboxTimeouts,
)

__all__ = [
    "DEFAULT_PRIZED_TEMPLATE",
    "DEFAULT_PRIZED_WORKSPACE_ROOT",
    "PrizedAPIError",
    "PrizedError",
    "PrizedSandboxClient",
    "PrizedSandboxClientOptions",
    "PrizedSandboxGoneError",
    "PrizedSandboxSession",
    "PrizedSandboxSessionState",
    "PrizedSandboxTimeouts",
    "__version__",
]
