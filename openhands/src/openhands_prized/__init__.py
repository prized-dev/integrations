"""Run the OpenHands Agent Server in Prized Sandboxes."""

from typing import TYPE_CHECKING

from ._version import __version__

if TYPE_CHECKING:
    from .workspace import PrizedWorkspace

__all__ = ["PrizedWorkspace", "__version__"]


def __getattr__(name: str) -> object:
    # Lazy, so the CLI (`openhands-prized serve`, `--version`) imports the OpenHands SDK
    # only when it needs it, after it has set up its environment.
    if name == "PrizedWorkspace":
        from .workspace import PrizedWorkspace

        return PrizedWorkspace
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
