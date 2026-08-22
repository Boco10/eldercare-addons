"""Data quality score (docs/07-ML-BEHAVIOR.md §4).

The behavioural evaluation is only trustworthy while the input sensors work.
When they do not, the system **must not draw strong conclusions** — it should
say "insufficient data" instead.

Five dimensions, each between 0.0 and 1.0. How they are grouped is not a matter
of taste:

**Hard signals** — they show an unambiguous data fault, so the weakest one drags
the score down hard:
  coverage      — are enough entities mapped at all
  availability  — how long the sensor sat in `unavailable`
  consistency   — did we lose events (visible from a mismatching `old_state`)

**Soft signals** — ambiguous, so they only count towards the average:
  activity      — did it report anything during the day
  continuity    — was there an unreasonably long silent stretch

The distinction is critical because **silence is ambiguous**: Home Assistant
only sends an event on a state CHANGE, so a sensor that stays "off" all day is
silent while being perfectly healthy. If silence counted as a data fault, a
resident lying motionless would push us into "insufficient data" — the system
would fall quiet exactly when something is most wrong. Silence is for anomaly
detection to interpret, not for data quality to suppress.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.events.models import RawEvent
from app.ha.entity_discovery import EntityMapping, EntityRole

log = logging.getLogger(__name__)

UNAVAILABLE_STATES = {"unavailable", "unknown", "none"}

# How many confirmed entities are needed for a trustworthy evaluation.
# The MVP definition of done expects at least 10 configurable entities; below 5
# the coverage alone makes any conclusion unreliable.
MIN_ENTITIES_FOR_FULL_COVERAGE = 8
MIN_ENTITIES_USABLE = 4

# Longest expected silence per role. A longer quiet stretch is suspicious —
# but not for every role: a door can stay shut for days.
EXPECTED_MAX_SILENCE_S: dict[EntityRole, float] = {
    EntityRole.PRESENCE: 8 * 3600,
    EntityRole.BED_OCCUPANCY: 26 * 3600,
    EntityRole.APPLIANCE_POWER: 30 * 3600,
    EntityRole.VISION_PERSON: 12 * 3600,
}

# A fault in a critical sensor hurts the score more.
ROLE_WEIGHT: dict[EntityRole, float] = {
    EntityRole.PRESENCE: 1.0,
    EntityRole.BED_OCCUPANCY: 1.0,
    EntityRole.SMOKE: 0.8,
    EntityRole.CO: 0.8,
    EntityRole.SOS: 0.8,
    EntityRole.APPLIANCE_POWER: 0.6,
    EntityRole.DOOR_ENTRY: 0.5,
    EntityRole.VISION_PERSON: 0.5,
}
DEFAULT_WEIGHT = 0.4


@dataclass(slots=True)
class EntityQuality:
    entity_id: str
    role: EntityRole
    events: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    last_state: str | None = None
    unavailable_since: datetime | None = None
    unavailable_seconds: float = 0.0
    longest_gap_s: float = 0.0
    sequence_breaks: int = 0
    """How many events we lost (see DataQualityTracker.add)."""
    issues: list[str] = field(default_factory=list)

    @property
    def weight(self) -> float:
        return ROLE_WEIGHT.get(self.role, DEFAULT_WEIGHT)


@dataclass(slots=True)
class QualityReport:
    score: float
    coverage: float
    availability: float
    activity: float
    continuity: float
    entity_count: int
    consistency: float = 1.0
    problem_entities: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Is the data good enough to draw any conclusion at all."""
        return self.score >= 0.80

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "coverage": round(self.coverage, 3),
            "availability": round(self.availability, 3),
            "activity": round(self.activity, 3),
            "continuity": round(self.continuity, 3),
            "consistency": round(self.consistency, 3),
            "entity_count": self.entity_count,
            "problem_entities": self.problem_entities,
        }


class DataQualityTracker:
    """Tracks one day's data quality from raw events."""

    def __init__(self, mappings: dict[str, EntityMapping]) -> None:
        self.mappings = mappings
        self._entities: dict[str, EntityQuality] = {}
        self._gap_tracker: dict[str, datetime] = defaultdict(lambda: None)  # type: ignore[arg-type]

    def reset(self) -> None:
        self._entities.clear()
        self._gap_tracker.clear()

    def add(self, event: RawEvent) -> None:
        mapping = self.mappings.get(event.entity_id)
        if mapping is None or not mapping.active:
            return

        quality = self._entities.get(event.entity_id)
        if quality is None:
            quality = EntityQuality(event.entity_id, mapping.role)
            self._entities[event.entity_id] = quality
            quality.first_seen = event.timestamp

        # Measure the silent stretch since the previous report.
        if quality.last_seen is not None:
            gap = (event.timestamp - quality.last_seen).total_seconds()
            quality.longest_gap_s = max(quality.longest_gap_s, gap)

        # DETECTING DATA LOSS — this is the one reliable signal.
        #
        # Home Assistant sends the previous state with every event. If it does not
        # match what we last saw, we lost events in between (a network dropout, a
        # restart, an overloaded sensor).
        #
        # This matters because it separates SILENCE from DATA LOSS: a resident who
        # lay still all day produces no sequence breaks, only few events.
        if (quality.last_state is not None and event.previous_state is not None
                and event.previous_state.lower() != quality.last_state.lower()):
            quality.sequence_breaks += 1

        quality.events += 1
        quality.last_seen = event.timestamp
        quality.last_state = event.state

        state = event.state.lower()
        if state in UNAVAILABLE_STATES:
            if quality.unavailable_since is None:
                quality.unavailable_since = event.timestamp
        elif quality.unavailable_since is not None:
            quality.unavailable_seconds += (
                event.timestamp - quality.unavailable_since).total_seconds()
            quality.unavailable_since = None

    def score(self, day_start: datetime, day_end: datetime) -> QualityReport:
        """The day's data quality. `day_end` is the last processed event time."""
        span = max((day_end - day_start).total_seconds(), 1.0)
        confirmed = [m for m in self.mappings.values() if m.active]
        expected = len(confirmed)

        # --- coverage: are enough entities mapped ---
        if expected >= MIN_ENTITIES_FOR_FULL_COVERAGE:
            coverage = 1.0
        elif expected <= MIN_ENTITIES_USABLE:
            coverage = expected / (MIN_ENTITIES_USABLE * 2)
        else:
            coverage = 0.5 + 0.5 * (expected - MIN_ENTITIES_USABLE) / (
                MIN_ENTITIES_FOR_FULL_COVERAGE - MIN_ENTITIES_USABLE)

        if expected == 0:
            log.debug("No confirmed entity — data quality is 0.")
            return QualityReport(0.0, 0.0, 0.0, 0.0, 0.0, 0, ["no confirmed entity"])

        problems: list[str] = []
        availability_parts: list[tuple[float, float]] = []
        activity_parts: list[tuple[float, float]] = []
        continuity_parts: list[tuple[float, float]] = []
        consistency_parts: list[tuple[float, float]] = []

        for mapping in confirmed:
            quality = self._entities.get(mapping.entity_id)
            weight = ROLE_WEIGHT.get(mapping.role, DEFAULT_WEIGHT)

            # It never reported during the day.
            #
            # CAREFUL: silence is AMBIGUOUS. Home Assistant only sends an event on
            # a state CHANGE, so a sensor that stays "off" all day is silent — even
            # when it works perfectly.
            #
            # This must not be scored as a fault: if a resident lying motionless
            # produced few events, and that pushed us into "insufficient data",
            # the system would fall silent exactly when it matters most. Silence
            # is for anomaly detection to interpret, not for data quality to
            # suppress. A genuinely broken sensor shows up as `unavailable` —
            # that is punished below.
            if quality is None or quality.events == 0:
                availability_parts.append((weight, 1.0))   # no evidence of a fault
                activity_parts.append((weight, 0.5))       # a mild signal, not a fault
                continuity_parts.append((weight, 1.0))
                consistency_parts.append((weight, 1.0))
                problems.append(f"{mapping.entity_id}: no signal "
                                "(a quiet day or a dead sensor — cannot be told apart)")
                continue

            # --- consistency: did we lose events ---
            break_ratio = quality.sequence_breaks / max(quality.events, 1)
            consistency_parts.append((weight, max(0.0, 1.0 - 2.0 * break_ratio)))
            if quality.sequence_breaks:
                problems.append(f"{mapping.entity_id}: {quality.sequence_breaks} "
                                "missed events")

            # --- availability ---
            unavailable = quality.unavailable_seconds
            if quality.unavailable_since is not None:
                unavailable += (day_end - quality.unavailable_since).total_seconds()
            available = max(0.0, 1.0 - unavailable / span)
            availability_parts.append((weight, available))
            if available < 0.9:
                problems.append(f"{mapping.entity_id}: {(1 - available):.0%} dropout(s)")

            # --- activity: did it report a meaningful amount ---
            activity_parts.append((weight, 1.0 if quality.events >= 2 else 0.5))

            # --- continuity ---
            limit = EXPECTED_MAX_SILENCE_S.get(mapping.role)
            if limit is None:
                continuity_parts.append((weight, 1.0))
            else:
                ratio = min(quality.longest_gap_s / limit, 2.0)
                continuity_parts.append((weight, max(0.0, 1.0 - max(0.0, ratio - 1.0))))
                if quality.longest_gap_s > limit:
                    problems.append(
                        f"{mapping.entity_id}: {quality.longest_gap_s / 3600:.1f} hours silent")

        availability = _weighted(availability_parts)
        activity = _weighted(activity_parts)
        continuity = _weighted(continuity_parts)
        consistency = _weighted(consistency_parts)

        # HARD dimensions: these show an unambiguous data fault, so the weakest one
        # drags the score down hard — a dead critical sensor cannot be averaged
        # away by everything else working.
        hard = [coverage, availability, consistency]
        # SOFT dimensions: ambiguous (a quiet day or a fault), so they only count
        # towards the average and cannot block the evaluation on their own.
        soft = [activity, continuity]

        mean = sum(hard + soft) / len(hard + soft)
        score = 0.6 * mean + 0.4 * min(hard)

        return QualityReport(
            score=round(score, 3), coverage=round(coverage, 3),
            availability=round(availability, 3), activity=round(activity, 3),
            continuity=round(continuity, 3), consistency=round(consistency, 3),
            entity_count=expected, problem_entities=problems[:10],
        )


def _weighted(parts: list[tuple[float, float]]) -> float:
    total = sum(weight for weight, _ in parts)
    if total == 0:
        return 0.0
    return sum(weight * value for weight, value in parts) / total


def day_bounds(moment: datetime, day_start_hour: int) -> tuple[datetime, datetime]:
    """The boundaries of the observation day.

    The day does NOT start at midnight: night-time wake-ups and the morning
    wake-up belong to the same logical day (docs/05-API-CONTRACT.md §6). The
    rollover therefore happens before dawn, while the resident is usually asleep.
    """
    start = moment.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
    if moment < start:
        start -= timedelta(days=1)
    return start, start + timedelta(days=1)
