"""Prized Sandboxes for Inspect AI: each sample runs in its own Firecracker microVM."""

from ._environment import PrizedSandboxConfig, PrizedSandboxEnvironment

__version__ = "0.1.0"
__all__ = ["PrizedSandboxConfig", "PrizedSandboxEnvironment", "__version__"]
