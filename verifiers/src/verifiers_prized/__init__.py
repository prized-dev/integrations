"""Prized Sandboxes as a verifiers runtime: `import_path = "verifiers_prized.PrizedBackend"`."""

from typing import Any

from . import pool as _pool
from .backend import PrizedBackend, PrizedProcess
from .config import PrizedConfig
from .pool import close_pools

__version__ = "0.1.0"

__all__ = ["RUN_ID", "PrizedBackend", "PrizedConfig", "PrizedProcess", "__version__", "close_pools"]


def __getattr__(name: str) -> Any:
    # RUN_ID is read when asked for: a forked child gets one of its own (pool.py).
    if name == "RUN_ID":
        return _pool.RUN_ID
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
