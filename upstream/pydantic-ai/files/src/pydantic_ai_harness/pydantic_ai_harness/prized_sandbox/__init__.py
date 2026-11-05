"""Prized sandbox capability: gives agents a Firecracker microVM that pauses with its memory and forks live.

`PrizedSandbox` is the supported entry point; build an agent with it and add tools that use the
workspace, such as `Coder`. `PrizedSandboxBackend` is the Prized implementation of Pydantic AI's
workspace backend protocol, public for applications that want to create or attach to a sandbox
themselves and pass it to a run as `workspace=`.
"""

from pydantic_ai_harness.prized_sandbox._backend import PrizedSandboxBackend
from pydantic_ai_harness.prized_sandbox._capability import PrizedSandbox

__all__ = [
    'PrizedSandbox',
    'PrizedSandboxBackend',
]
