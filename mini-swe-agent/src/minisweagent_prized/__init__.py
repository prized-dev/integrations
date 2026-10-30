"""Prized Sandboxes for mini-SWE-agent: an environment, a SWE-bench runner and a SWE-bench evaluator.

- ``PrizedEnvironment``: ``environment_class: minisweagent_prized.PrizedEnvironment``
- ``python -m minisweagent_prized.swebench``: ``mini-extra swebench`` with ``--environment-class prized``
- ``python -m minisweagent_prized.evaluate``: SWE-bench's evaluation, one Prized sandbox per instance
"""

from .environment import PrizedEnvironment, PrizedEnvironmentConfig

__all__ = ["PrizedEnvironment", "PrizedEnvironmentConfig"]
__version__ = "0.1.0"
