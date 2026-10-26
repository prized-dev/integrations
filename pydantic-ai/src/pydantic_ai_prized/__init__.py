"""Run Pydantic AI agents' workspaces in Prized Sandboxes (Firecracker microVMs)."""

from ._backend import PROVIDER, PrizedSandboxBackend, destroy_sandbox
from ._capability import PrizedSandbox
from ._version import __version__

__all__ = ('PROVIDER', 'PrizedSandbox', 'PrizedSandboxBackend', '__version__', 'destroy_sandbox')
