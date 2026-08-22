"""Internal event models — per the docs/15-EVENT-CATALOG.md v1 catalogue.

IMPORTANT: the pipeline NEVER reads the wall clock (`datetime.now()`), only the
events' timestamps. Without that, an accelerated replay (3600x) would lie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

CATALOG_VERSION = 1


class EventClass(StrEnum):
    CRITICAL = "critical"
    BEHAVIORAL = "behavioral"
    CONTEXTUAL = "contextual"
    SYSTEM = "system"


class SemanticType(StrEnum):
    # critical
    SOS_TRIGGERED = "sos_triggered"
    SMOKE_DETECTED = "smoke_detected"
    CO_DETECTED = "co_detected"
    CONFIRMED_FALL = "confirmed_fall"
    # behavioral
    BED_ENTRY = "bed_entry"
    BED_EXIT = "bed_exit"
    NIGHT_WAKING = "night_waking"
    ROOM_TRANSITION = "room_transition"
    ROOM_OCCUPANCY = "room_occupancy"
    EXTENDED_ROOM_STAY = "extended_room_stay"
    INACTIVITY_PERIOD = "inactivity_period"
    APPLIANCE_USED = "appliance_used"
    HOME_EXIT = "home_exit"
    HOME_RETURN = "home_return"
    # contextual
    DOOR_OPENED = "door_opened"
    POSSIBLE_FALL = "possible_fall"
    PERSON_ON_FLOOR = "person_on_floor"
    NO_MOVEMENT_AFTER_CANDIDATE = "no_movement_after_candidate"
    VISITOR_PRESENT = "visitor_present"
    # system
    SENSOR_UNAVAILABLE = "sensor_unavailable"
    SENSOR_IMPLAUSIBLE = "sensor_implausible"
    LOW_BATTERY = "low_battery"
    DATA_QUALITY_DEGRADED = "data_quality_degraded"


class ReasonCode(StrEnum):
    LATE_WAKEUP = "late_wakeup"
    EARLY_WAKEUP = "early_wakeup"
    NO_KITCHEN_ACTIVITY = "no_kitchen_activity"
    MISSED_APPLIANCE_USE = "missed_appliance_use"
    REDUCED_MOVEMENT = "reduced_movement"
    PROLONGED_INACTIVITY = "prolonged_inactivity"
    INACTIVITY_UNUSUAL_LOCATION = "inactivity_unusual_location"
    INCREASED_NIGHT_ACTIVITY = "increased_night_activity"
    EXTENDED_BATHROOM_STAY = "extended_bathroom_stay"
    FREQUENT_BATHROOM_VISITS = "frequent_bathroom_visits"
    ROUTINE_SEQUENCE_DEVIATION = "routine_sequence_deviation"
    UNUSUAL_EXIT_TIME = "unusual_exit_time"
    EXTENDED_ABSENCE = "extended_absence"
    LONG_TERM_ACTIVITY_DECLINE = "long_term_activity_decline"
    # The two negative codes: NOT anomalies, they cannot raise the risk score.
    SENSOR_FAILURE = "sensor_failure"
    INSUFFICIENT_DATA = "insufficient_data"


NON_ANOMALY_REASONS = {ReasonCode.SENSOR_FAILURE, ReasonCode.INSUFFICIENT_DATA}


class AlertLevel(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(slots=True)
class RawEvent:
    """A normalised Home Assistant state_changed — not yet semantic."""

    entity_id: str
    state: str
    previous_state: str | None
    timestamp: datetime
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        return f"{self.entity_id}|{self.timestamp.isoformat()}|{self.state}"


@dataclass(slots=True)
class SemanticEvent:
    """The event as sent to the cloud. Shape: docs/15-EVENT-CATALOG.md §5."""

    type: SemanticType
    event_class: EventClass
    timestamp: datetime
    confidence: float = 1.0
    source: str = "sensor"
    room: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "catalog_version": CATALOG_VERSION,
            "type": self.type.value,
            "class": self.event_class.value,
            "timestamp": self.timestamp.isoformat(),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "fields": self.fields,
        }
        if self.room:
            payload["room"] = self.room
        return payload

    @property
    def is_critical(self) -> bool:
        """Critical event: alerts at once, locally — no cloud and no LLM."""
        return self.event_class is EventClass.CRITICAL
