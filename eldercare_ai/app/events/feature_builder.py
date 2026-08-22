"""Building daily routine features from semantic events.

The list of features to learn: docs/07-ML-BEHAVIOR.md §6.
The shape of the cloud payload: docs/05-API-CONTRACT.md §6.

Two rules the code keeps:

  1. **No wall-clock read.** The day rolls over on incoming event timestamps,
     so an accelerated replay gives the same result as a real-time run.
  2. **Baseline-dependent fields are `None` for now.** `usual_wakeup_window`
     and `movement_reduction_percent` only become real in phase 3. Until then
     leaving them empty is more honest than inventing a number for the AI.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from typing import Any

from app.events.data_quality import QualityReport, day_bounds
from app.events.models import SemanticEvent, SemanticType

log = logging.getLogger(__name__)

EVENING_START_HOUR = 18
"""From this hour on, going to bed counts as going to bed for the night."""


@dataclass(slots=True)
class DailyFeatures:
    """Aggregated features of one observation day."""

    date: date

    # --- sleep and waking ---
    wakeup_time: time | None = None
    bedtime: time | None = None
    time_in_bed_s: float | None = None
    night_wakings: int = 0
    night_waking_total_s: float = 0.0

    # --- kitchen and appliances ---
    first_kitchen_activity: time | None = None
    appliances_used: list[str] = field(default_factory=list)
    appliance_uses: int = 0

    # --- bathroom ---
    bathroom_visits: int = 0
    bathroom_total_s: float = 0.0
    bathroom_longest_s: float = 0.0

    # --- movement ---
    room_transitions: int = 0
    rooms_visited: list[str] = field(default_factory=list)
    activity_events: int = 0
    longest_inactivity_s: float = 0.0

    # --- leaving home ---
    home_exits: int = 0
    total_away_s: float = 0.0

    # --- irregularities ---
    extended_stays: int = 0
    critical_events: int = 0
    sensor_issues: int = 0

    # --- day type ---
    is_weekend: bool = False

    # --- baseline-dependent fields: deliberately empty until phase 3 ---
    usual_wakeup_window: tuple[time, time] | None = None
    movement_reduction_percent: float | None = None

    def to_payload(self) -> dict[str, Any]:
        """The shape sent to the cloud (docs/05-API-CONTRACT.md §6)."""
        def fmt(value: time | None) -> str | None:
            return value.strftime("%H:%M") if value else None

        payload = asdict(self)
        payload["date"] = self.date.isoformat()
        for key in ("wakeup_time", "bedtime", "first_kitchen_activity"):
            payload[key] = fmt(getattr(self, key))
        payload["usual_wakeup_window"] = (
            [fmt(self.usual_wakeup_window[0]), fmt(self.usual_wakeup_window[1])]
            if self.usual_wakeup_window else None
        )
        for key in ("time_in_bed_s", "night_waking_total_s", "bathroom_total_s",
                    "bathroom_longest_s", "longest_inactivity_s", "total_away_s"):
            if payload[key] is not None:
                payload[key] = round(payload[key], 1)
        return payload


@dataclass(slots=True)
class DailyFeatureSet:
    """The daily features plus data quality. This is what is stored and uploaded."""

    features: DailyFeatures
    quality: QualityReport
    event_count: int

    @property
    def date(self) -> date:
        return self.features.date

    @property
    def usable(self) -> bool:
        """May we draw any conclusion from this day at all."""
        return self.quality.usable

    def to_payload(self, timezone_name: str) -> dict[str, Any]:
        return {
            "date": self.features.date.isoformat(),
            "timezone": timezone_name,
            "data_quality": round(self.quality.score, 3),
            "data_quality_detail": self.quality.to_dict(),
            "features": self.features.to_payload(),
            # No score until phase 3 — we send an empty field rather than zero, so
            # the cloud can tell "none" apart from "zero".
            "local_anomaly_score": None,
            "reasons": [] if self.usable else ["insufficient_data"],
        }


class FeatureBuilder:
    """Collects semantic events, split by day.

    `add()` returns the closed day when the first event of the next day arrives —
    so closing is deterministic and does not depend on the system clock.
    """

    def __init__(self, day_start_hour: int = 4) -> None:
        self.day_start_hour = day_start_hour
        self._current_day: date | None = None
        self._day_start: datetime | None = None
        self._events: list[SemanticEvent] = []
        self._last_timestamp: datetime | None = None

    def add(self, event: SemanticEvent) -> DailyFeatures | None:
        """Add an event. On a day change it returns the closed day's features."""
        start, _ = day_bounds(event.timestamp, self.day_start_hour)
        day = start.date()

        finished: DailyFeatures | None = None
        if self._current_day is not None and day != self._current_day:
            finished = self.finalize()

        if self._current_day is None or finished is not None:
            self._current_day = day
            self._day_start = start
            self._events = []

        self._events.append(event)
        self._last_timestamp = event.timestamp
        return finished

    def finalize(self) -> DailyFeatures | None:
        """Close the collected day. Call it on shutdown too."""
        if self._current_day is None or not self._events:
            return None
        features = self._compute(self._current_day, self._events)
        log.info("Daily summary ready: %s (%d events, wake-up: %s)",
                 features.date, len(self._events),
                 features.wakeup_time.strftime("%H:%M") if features.wakeup_time else "unknown")
        self._current_day = None
        self._events = []
        return features

    @property
    def pending_events(self) -> int:
        return len(self._events)

    @property
    def current_day(self) -> date | None:
        return self._current_day

    # ------------------------------------------------------------ computation

    def _compute(self, day: date, events: list[SemanticEvent]) -> DailyFeatures:
        features = DailyFeatures(date=day, is_weekend=day.weekday() >= 5)
        rooms: set[str] = set()
        appliances: set[str] = set()
        bed_entry_at: datetime | None = None

        for event in sorted(events, key=lambda e: e.timestamp):
            local = event.timestamp
            fields = event.fields

            match event.type:
                case SemanticType.BED_EXIT:
                    # The first bed exit after dawn is the wake-up. Earlier ones
                    # are night wakings — counted separately by night_waking.
                    if features.wakeup_time is None and local.hour >= self.day_start_hour:
                        features.wakeup_time = local.time()
                    if bed_entry_at is not None:
                        features.time_in_bed_s = (features.time_in_bed_s or 0.0) + (
                            local - bed_entry_at).total_seconds()
                        bed_entry_at = None

                case SemanticType.BED_ENTRY:
                    # Bedtime is the FIRST evening bed entry. The last one cannot
                    # be used: after every night waking the resident goes back to
                    # bed, so the pre-dawn return would be recorded as bedtime.
                    if features.bedtime is None and local.hour >= EVENING_START_HOUR:
                        features.bedtime = local.time()
                    bed_entry_at = local

                case SemanticType.NIGHT_WAKING:
                    features.night_wakings += 1
                    if (duration := fields.get("duration")) is not None:
                        features.night_waking_total_s += float(duration)

                case SemanticType.ROOM_OCCUPANCY:
                    room = event.room or "unknown"
                    rooms.add(room)
                    duration = float(fields.get("duration") or 0.0)
                    if room == "kitchen" and features.first_kitchen_activity is None:
                        features.first_kitchen_activity = local.time()
                    if room in ("bathroom", "toilet"):
                        features.bathroom_visits += 1
                        features.bathroom_total_s += duration
                        features.bathroom_longest_s = max(features.bathroom_longest_s, duration)

                case SemanticType.ROOM_TRANSITION:
                    features.room_transitions += 1
                    for key in ("from_room", "to_room"):
                        if value := fields.get(key):
                            rooms.add(str(value))

                case SemanticType.APPLIANCE_USED:
                    features.appliance_uses += 1
                    if appliance := fields.get("appliance"):
                        appliances.add(str(appliance))
                    if features.first_kitchen_activity is None and event.room == "kitchen":
                        features.first_kitchen_activity = local.time()

                case SemanticType.EXTENDED_ROOM_STAY:
                    features.extended_stays += 1

                case SemanticType.INACTIVITY_PERIOD:
                    features.longest_inactivity_s = max(
                        features.longest_inactivity_s, float(fields.get("duration") or 0.0))

                case SemanticType.HOME_EXIT:
                    features.home_exits += 1

                case SemanticType.HOME_RETURN:
                    features.total_away_s += float(fields.get("away_duration") or 0.0)

                case SemanticType.SENSOR_UNAVAILABLE | SemanticType.SENSOR_IMPLAUSIBLE:
                    features.sensor_issues += 1

            if event.is_critical:
                features.critical_events += 1
            # Activity proxy: every behavioural signal. The reduction against the
            # baseline can be computed from it in phase 3.
            if event.event_class.value == "behavioral":
                features.activity_events += 1

        features.rooms_visited = sorted(rooms)
        features.appliances_used = sorted(appliances)
        return features
