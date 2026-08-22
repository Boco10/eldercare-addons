"""Replay event source — replaying a JSONL fixture without Home Assistant.

The learning period is 3-4 weeks (docs/07-ML-BEHAVIOR.md §5), which cannot be
waited out during development. Because of the speed-up (`ha_replay_speed`) the
pipeline must NEVER read the wall clock — always the event's `timestamp` field.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from app.events.models import RawEvent

log = logging.getLogger(__name__)


class ReplayEventSource:
    """Replays a JSONL file or folder in time order."""

    def __init__(self, path: Path, speed: float = 1.0) -> None:
        self.path = path
        self.speed = max(speed, 0.0)
        self._files: list[Path] = []

    async def connect(self) -> None:
        if self.path.is_dir():
            self._files = sorted(self.path.glob("*.jsonl"))
        elif self.path.is_file():
            self._files = [self.path]
        else:
            raise FileNotFoundError(f"Replay source not found: {self.path}")
        log.info("Replay source: %d files, %.0fx speed", len(self._files), self.speed)

    async def close(self) -> None:
        self._files = []

    async def stream(self) -> AsyncIterator[RawEvent]:
        previous_ts: datetime | None = None

        for file in self._files:
            log.info("Replay: %s", file.name)
            for line in file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = self._parse(json.loads(line))
                if event is None:
                    continue

                # Wait out the real gap between events, divided by the speed.
                if previous_ts and self.speed > 0:
                    delta = (event.timestamp - previous_ts).total_seconds() / self.speed
                    if delta > 0:
                        await asyncio.sleep(min(delta, 5.0))
                previous_ts = event.timestamp

                yield event

    @staticmethod
    def _parse(raw: dict) -> RawEvent | None:
        if raw.get("event_type") != "state_changed":
            return None
        data = raw.get("data", {})
        new_state = data.get("new_state") or {}
        old_state = data.get("old_state") or {}
        if not data.get("entity_id") or not new_state:
            return None
        return RawEvent(
            entity_id=data["entity_id"],
            state=new_state.get("state", "unknown"),
            previous_state=old_state.get("state"),
            timestamp=datetime.fromisoformat(raw["time_fired"]),
            attributes=new_state.get("attributes", {}),
        )
