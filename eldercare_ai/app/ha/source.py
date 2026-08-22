"""Event source abstraction.

This interface is what lets the whole pipeline run without Home Assistant
(from a replay fixture). The rest of the pipeline does not know which
implementation runs underneath.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.events.models import RawEvent


class EventSource(Protocol):
    """A source of raw Home Assistant events."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    def stream(self) -> AsyncIterator[RawEvent]:
        """A continuous event stream. Reconnecting is the implementation's job."""
        ...
