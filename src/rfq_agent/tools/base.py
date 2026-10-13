"""Common tool interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class ToolHit(BaseModel):
    page_id: str
    title: str
    text: str


class ToolTimeoutError(TimeoutError):
    """Raised by a tool to simulate an upstream timeout."""


@runtime_checkable
class ToolProvider(Protocol):
    name: str

    async def search(self, query: str) -> list[ToolHit]: ...
