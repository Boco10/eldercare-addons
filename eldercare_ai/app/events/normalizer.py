"""Validation, de-duplication and the internal Event format.

The first step of the pipeline (docs/01-ARCHITECTURE.md §4). No business logic
belongs here — only filtering and normalisation.
"""

from __future__ import annotations

import logging
from collections import deque

from app.events.models import RawEvent

log = logging.getLogger(__name__)

# Home Assistant states not worth passing on as a value, but still meaningful.
IGNORED_STATES = {"unknown", ""}
UNAVAILABLE_STATES = {"unavailable", "none", "None"}

DEDUP_WINDOW = 2000


class Normalizer:
    """Filters out duplicates and meaningless state changes."""

    def __init__(self, dedup_window: int = DEDUP_WINDOW) -> None:
        self._seen: deque[str] = deque(maxlen=dedup_window)
        self._seen_set: set[str] = set()
        self.stats = {"accepted": 0, "duplicate": 0, "ignored": 0, "unavailable": 0}

    def process(self, event: RawEvent) -> RawEvent | None:
        """None = dropped. The caller learns why from the stats."""
        if event.state in IGNORED_STATES:
            self.stats["ignored"] += 1
            return None

        # `unavailable` is NOT noise: it signals a sensor fault
        # (docs/15-EVENT-CATALOG.md §3.4). Passed on so data quality can see it.
        if event.state in UNAVAILABLE_STATES:
            self.stats["unavailable"] += 1

        # An unchanged state carries no information.
        if event.previous_state == event.state:
            self.stats["ignored"] += 1
            return None

        key = event.dedup_key
        if key in self._seen_set:
            self.stats["duplicate"] += 1
            return None

        if len(self._seen) == self._seen.maxlen and self._seen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)

        self.stats["accepted"] += 1
        return event
