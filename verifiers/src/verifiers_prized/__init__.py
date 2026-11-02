"""Prized Sandboxes as a verifiers runtime: `import_path = "verifiers_prized.PrizedBackend"`."""

from .backend import PrizedBackend, PrizedProcess
from .config import PrizedConfig
from .pool import RUN_ID, close_pools

__version__ = "0.1.0"

__all__ = ["RUN_ID", "PrizedBackend", "PrizedConfig", "PrizedProcess", "__version__", "close_pools"]
