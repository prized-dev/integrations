"""Stand-in for the parts of stirrup.core.models a code backend touches."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, model_validator

P = TypeVar("P", bound=BaseModel)
M = TypeVar("M")


class ImageContentBlock(BaseModel, frozen=True):
    """Raw image bytes. Stirrup checks the type with filetype and opens the image with PIL;
    this stand-in only checks the PNG and JPEG signatures."""

    data: bytes

    @model_validator(mode="after")
    def _check(self) -> ImageContentBlock:
        if not (self.data.startswith(b"\x89PNG\r\n\x1a\n") or self.data.startswith(b"\xff\xd8\xff")):
            raise ValueError("not a PNG or JPEG image")
        return self


class ToolUseCountMetadata(BaseModel):
    num_uses: int = 1


class ToolResult(BaseModel, Generic[M]):
    content: Any
    success: bool = True
    metadata: M | None = None


class Tool(BaseModel, Generic[P, M]):
    name: str
    description: str
    parameters: type[P]
    executor: Callable[[P], ToolResult[M] | Awaitable[ToolResult[M]]]


class ToolProvider(ABC):
    @abstractmethod
    async def __aenter__(self) -> Tool | list[Tool]: ...

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:  # noqa: B027
        return None
