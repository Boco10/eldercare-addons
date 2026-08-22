"""Semantic event building — meaning from a raw state change.

The catalogue is docs/15-EVENT-CATALOG.md. A new event type belongs in both.

Two ground rules the code keeps:
  1. The wall clock is NEVER read — only the events' `timestamp` field.
     Without that, an accelerated replay would produce false results.
  2. A `possible_fall` never alerts on its own. A `confirmed_fall` is only
     produced after multi-signal validation (docs/09-ALERTS.md §4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.events.models import EventClass, RawEvent, SemanticEvent, SemanticType
from app.events.thresholds import Thresholds
from app.ha.entity_discovery import EntityMapping, EntityRole

log = logging.getLogger(__name__)

ON_STATES = {"on", "true", "open", "detected", "home"}
OFF_STATES = {"off", "false", "closed", "clear", "not_home"}
UNAVAILABLE = {"unavailable", "unknown", "none"}


@dataclass(slots=True)
class _RoomState:
    entered_at: datetime | None = None
    last_seen: datetime | None = None


@dataclass(slots=True)
class _ApplianceState:
    started_at: datetime | None = None
    peak_watts: float = 0.0


@dataclass(slots=True)
class _FallCandidate:
    detected_at: datetime
    room: str | None
    camera: str | None
    confidence: float


@dataclass(slots=True)
class _State:
    """The engine's internal state. Serialisable, so a restart can resume."""

    rooms: dict[str, _RoomState] = field(default_factory=dict)
    appliances: dict[str, _ApplianceState] = field(default_factory=dict)
    in_bed_since: datetime | None = None
    last_room: str | None = None
    last_activity: datetime | None = None
    inactivity_reported_at: datetime | None = None
    pending_exit: datetime | None = None
    away_since: datetime | None = None
    fall_candidate: _FallCandidate | None = None
    unavailable_since: dict[str, datetime] = field(default_factory=dict)


class SemanticEngine:
    """Builds semantic events from raw ones.

    Usage:
        engine = SemanticEngine(mappings, thresholds)
        for raw in stream:
            for event in engine.process(raw):
                ...
    """

    def __init__(self, mappings: dict[str, EntityMapping] | None = None,
                 thresholds: Thresholds | None = None) -> None:
        self.mappings = mappings or {}
        self.t = thresholds or Thresholds()
        self._s = _State()
        self._recent: dict[tuple[SemanticType, str | None], datetime] = {}

    # ------------------------------------------------------------------- entry

    def process(self, raw: RawEvent) -> list[SemanticEvent]:
        mapping = self.mappings.get(raw.entity_id)
        # An unconfirmed suggestion does NOT take effect: a wrong meaning causes
        # a wrong alert. The user approves it in the local UI (docs/02-ADDON.md §4).
        if mapping is None or not mapping.active:
            return []

        events: list[SemanticEvent] = []

        # Sensor fault: NOT a behavioural anomaly but a system state.
        if raw.state.lower() in UNAVAILABLE:
            events.extend(self._on_unavailable(raw))
            return events
        self._s.unavailable_since.pop(raw.entity_id, None)

        # Time-based checks: the incoming event's timestamp "advances" the clock.
        events.extend(self._check_pending(raw.timestamp))

        handler = {
            EntityRole.PRESENCE: self._on_presence,
            EntityRole.BED_OCCUPANCY: self._on_bed,
            EntityRole.DOOR_ENTRY: self._on_door,
            EntityRole.APPLIANCE_POWER: self._on_appliance,
            EntityRole.SMOKE: self._on_smoke,
            EntityRole.CO: self._on_co,
            EntityRole.SOS: self._on_sos,
            EntityRole.VISION_PERSON: self._on_vision,
        }.get(mapping.role)

        if handler:
            events.extend(handler(raw, mapping))
        return self._deduplicate(events)

    def _deduplicate(self, events: list[SemanticEvent]) -> list[SemanticEvent]:
        """Filter out redundant sensors.

        If two devices report the same phenomenon (mmWave + PIR in one room, or a
        template sensor mirroring another), every event would be produced twice:
        counted twice in the daily features and alerted twice.
        """
        kept: list[SemanticEvent] = []
        for event in events:
            key = (event.type, event.room)
            previous = self._recent.get(key)
            if previous is not None and \
                    (event.timestamp - previous).total_seconds() < self.t.semantic_dedup_window_s:
                log.debug("Duplicate semantic event filtered out: %s (%s)",
                          event.type.value, event.room or "-")
                continue
            self._recent[key] = event.timestamp
            kept.append(event)
        return kept

    # ---------------------------------------------------------------- presence

    def _on_presence(self, raw: RawEvent, m: EntityMapping) -> list[SemanticEvent]:
        room = m.room or "unknown"
        state = self._s.rooms.setdefault(room, _RoomState())
        events: list[SemanticEvent] = []

        if raw.state.lower() in ON_STATES:
            self._mark_activity(raw.timestamp)

            # Coming home: after an absence, the first presence closes it.
            if self._s.away_since:
                events.append(self._event(
                    SemanticType.HOME_RETURN, EventClass.BEHAVIORAL, raw.timestamp,
                    fields={"away_duration": (raw.timestamp - self._s.away_since).total_seconds()},
                ))
                self._s.away_since = None
            self._s.pending_exit = None

            # Room change: from the previous room to here, if close in time.
            if (self._s.last_room and self._s.last_room != room
                    and (prev := self._s.rooms.get(self._s.last_room))
                    and prev.last_seen
                    and (raw.timestamp - prev.last_seen).total_seconds()
                    <= self.t.room_transition_window_s):
                events.append(self._event(
                    SemanticType.ROOM_TRANSITION, EventClass.BEHAVIORAL, raw.timestamp,
                    room=room,
                    fields={
                        "from_room": self._s.last_room,
                        "to_room": room,
                        "transition_time": (raw.timestamp - prev.last_seen).total_seconds(),
                    },
                ))

            if state.entered_at is None:
                state.entered_at = raw.timestamp
            state.last_seen = raw.timestamp
            self._s.last_room = room

        elif raw.state.lower() in OFF_STATES and state.entered_at:
            duration = (raw.timestamp - state.entered_at).total_seconds()
            state.last_seen = raw.timestamp

            events.append(self._event(
                SemanticType.ROOM_OCCUPANCY, EventClass.BEHAVIORAL, raw.timestamp,
                room=room,
                fields={"start": state.entered_at.isoformat(),
                        "end": raw.timestamp.isoformat(),
                        "duration": duration},
            ))

            # Extended stay. The baseline_percentile becomes real in phase 3;
            # until then a fixed threshold decides and the field stays None —
            # we do not claim a precision we do not have.
            if duration >= self.t.extended_stay_for(room):
                events.append(self._event(
                    SemanticType.EXTENDED_ROOM_STAY, EventClass.BEHAVIORAL, raw.timestamp,
                    room=room,
                    fields={"duration": duration, "baseline_percentile": None,
                            "threshold_s": self.t.extended_stay_for(room)},
                ))

            state.entered_at = None
            self._mark_activity(raw.timestamp)

        return events

    # --------------------------------------------------------------------- bed

    def _on_bed(self, raw: RawEvent, m: EntityMapping) -> list[SemanticEvent]:
        room = m.room or "bedroom"
        events: list[SemanticEvent] = []

        if raw.state.lower() in ON_STATES:
            self._s.in_bed_since = raw.timestamp
            events.append(self._event(
                SemanticType.BED_ENTRY, EventClass.BEHAVIORAL, raw.timestamp, room=room,
            ))
        elif raw.state.lower() in OFF_STATES:
            duration = ((raw.timestamp - self._s.in_bed_since).total_seconds()
                        if self._s.in_bed_since else None)
            events.append(self._event(
                SemanticType.BED_EXIT, EventClass.BEHAVIORAL, raw.timestamp, room=room,
                fields={"duration_in_bed": duration},
            ))
            # Night waking: the time of leaving the bed decides, not its length.
            if self.t.is_night(raw.timestamp.hour):
                events.append(self._event(
                    SemanticType.NIGHT_WAKING, EventClass.BEHAVIORAL, raw.timestamp, room=room,
                    fields={"duration_in_bed": duration},
                ))
            self._s.in_bed_since = None
            self._mark_activity(raw.timestamp)

        return events

    # -------------------------------------------------------------------- door

    def _on_door(self, raw: RawEvent, m: EntityMapping) -> list[SemanticEvent]:
        if raw.state.lower() not in ON_STATES:
            return []

        events = [self._event(
            SemanticType.DOOR_OPENED, EventClass.CONTEXTUAL, raw.timestamp,
            room=m.room, fields={"door": raw.entity_id},
        )]

        if self._s.away_since:
            # A door opening during an absence means arriving home; presence confirms.
            pass
        else:
            # Exit candidate: if no presence follows, this becomes home_exit.
            self._s.pending_exit = raw.timestamp

        return events

    # --------------------------------------------------------------- appliance

    def _on_appliance(self, raw: RawEvent, m: EntityMapping) -> list[SemanticEvent]:
        try:
            watts = float(raw.state)
        except (TypeError, ValueError):
            return []

        appliance = m.appliance or raw.entity_id
        state = self._s.appliances.setdefault(appliance, _ApplianceState())

        if watts >= self.t.appliance_on_watts:
            if state.started_at is None:
                state.started_at = raw.timestamp
                state.peak_watts = watts
            else:
                state.peak_watts = max(state.peak_watts, watts)
            self._mark_activity(raw.timestamp)
            return []

        # Dropped below the threshold -> the use has ended.
        if state.started_at is None:
            return []

        duration = (raw.timestamp - state.started_at).total_seconds()
        started_at, peak = state.started_at, state.peak_watts
        state.started_at, state.peak_watts = None, 0.0

        # A very short spike is noise (standby), not use.
        if duration < self.t.appliance_min_duration_s:
            return []

        return [self._event(
            SemanticType.APPLIANCE_USED, EventClass.BEHAVIORAL, raw.timestamp, room=m.room,
            fields={
                "appliance": appliance,
                "start": started_at.isoformat(),
                "end": raw.timestamp.isoformat(),
                "peak_power": peak,
                "energy_wh": round(peak * duration / 3600, 2),
            },
        )]

    # ------------------------------------------------------------------ kritikus

    def _on_smoke(self, raw: RawEvent, m: EntityMapping) -> list[SemanticEvent]:
        if raw.state.lower() not in ON_STATES:
            return []
        return [self._event(SemanticType.SMOKE_DETECTED, EventClass.CRITICAL, raw.timestamp,
                            room=m.room, fields={"device": raw.entity_id})]

    def _on_co(self, raw: RawEvent, m: EntityMapping) -> list[SemanticEvent]:
        if raw.state.lower() not in ON_STATES:
            return []
        return [self._event(SemanticType.CO_DETECTED, EventClass.CRITICAL, raw.timestamp,
                            room=m.room, fields={"device": raw.entity_id})]

    def _on_sos(self, raw: RawEvent, m: EntityMapping) -> list[SemanticEvent]:
        if raw.state.lower() not in ON_STATES:
            return []
        return [self._event(SemanticType.SOS_TRIGGERED, EventClass.CRITICAL, raw.timestamp,
                            room=m.room, fields={"source": raw.entity_id})]

    # --------------------------------------------------------------------- vision

    def _on_vision(self, raw: RawEvent, m: EntityMapping) -> list[SemanticEvent]:
        """Camera signal. A single vision report NEVER produces a fall alert."""
        confidence = float(raw.attributes.get("confidence", 0.0) or 0.0)
        camera = raw.attributes.get("camera") or m.room
        room = m.room

        if raw.state.lower() not in ("on_floor", "lying", "fall", "fallen"):
            # A standing or moving person: any fall candidate is refuted.
            if self._s.fall_candidate:
                log.info("Fall candidate discarded: the person is moving again.")
                self._s.fall_candidate = None
            return []

        if confidence < self.t.fall_min_confidence:
            log.debug("Vision signal dropped, low confidence: %.2f", confidence)
            return []

        events = [self._event(
            SemanticType.PERSON_ON_FLOOR, EventClass.CONTEXTUAL, raw.timestamp, room=room,
            confidence=confidence, source="vision",
            fields={"camera": camera, "duration": 0},
        ), self._event(
            SemanticType.POSSIBLE_FALL, EventClass.CONTEXTUAL, raw.timestamp, room=room,
            confidence=confidence, source="vision",
            fields={"camera": camera, "snapshot_reference": None},
        )]

        # Very high confidence -> confirm at once, without waiting.
        if confidence >= self.t.fall_immediate_confidence:
            events.append(self._confirm_fall(raw.timestamp, room, camera, confidence,
                                             ["vision_high_confidence"]))
        else:
            self._s.fall_candidate = _FallCandidate(raw.timestamp, room, camera, confidence)

        return events

    # --------------------------------------------------------- time-based check

    def tick(self, now: datetime) -> list[SemanticEvent]:
        """Time-based check WITHOUT AN INCOMING EVENT.

        This is a safety requirement, not a convenience. `_check_pending` would
        only run when a new event arrives — but next to an unconscious person,
        what happens is that nothing happens. Without a periodic call the fall
        candidate would never be confirmed and the alert would never fire.

        In production main.py calls this every second with the real clock; in
        replay mode the incoming timestamps drive it (so it stays deterministic).
        """
        return self._check_pending(now)

    def _check_pending(self, now: datetime) -> list[SemanticEvent]:
        """The event timestamp advances the clock — no wall-clock read."""
        events: list[SemanticEvent] = []

        # 1. Confirm a fall candidate after sustained stillness.
        if (c := self._s.fall_candidate) and \
                (now - c.detected_at).total_seconds() >= self.t.fall_stillness_s:
            still_for = (now - c.detected_at).total_seconds()
            if not self._s.last_activity or self._s.last_activity <= c.detected_at:
                events.append(self._confirm_fall(
                    now, c.room, c.camera, c.confidence,
                    ["person_on_floor", f"no_movement_{int(still_for)}s"],
                ))
            self._s.fall_candidate = None

        # 2. Leaving home: a door opening with no presence afterwards.
        if (pe := self._s.pending_exit) and \
                (now - pe).total_seconds() >= self.t.exit_confirm_window_s:
            events.append(self._event(
                SemanticType.HOME_EXIT, EventClass.BEHAVIORAL, pe,
                confidence=0.8, fields={"door": "entry", "confirmed_at": now.isoformat()},
            ))
            self._s.away_since = pe
            self._s.pending_exit = None

        # 3. Inactivity. The night threshold differs — sleep is not an anomaly.
        if self._s.last_activity and not self._s.away_since and self._s.in_bed_since is None:
            gap = (now - self._s.last_activity).total_seconds()
            limit = (self.t.inactivity_night_threshold_s
                     if self.t.is_night(self._s.last_activity.hour)
                     else self.t.inactivity_threshold_s)
            already = (self._s.inactivity_reported_at
                       and self._s.inactivity_reported_at >= self._s.last_activity)
            if gap >= limit and not already:
                events.append(self._event(
                    SemanticType.INACTIVITY_PERIOD, EventClass.BEHAVIORAL, now,
                    room=self._s.last_room,
                    fields={"start": self._s.last_activity.isoformat(),
                            "end": now.isoformat(), "duration": gap,
                            "last_room": self._s.last_room,
                            "expected_max_duration": limit},
                ))
                self._s.inactivity_reported_at = now

        return events

    def _on_unavailable(self, raw: RawEvent) -> list[SemanticEvent]:
        if raw.entity_id in self._s.unavailable_since:
            return []
        self._s.unavailable_since[raw.entity_id] = raw.timestamp
        return [self._event(
            SemanticType.SENSOR_UNAVAILABLE, EventClass.SYSTEM, raw.timestamp,
            source="derived",
            fields={"entity_id": raw.entity_id, "since": raw.timestamp.isoformat()},
        )]

    # ----------------------------------------------------------------- helpers

    def _confirm_fall(self, ts: datetime, room: str | None, camera: str | None,
                      confidence: float, evidence: list[str]) -> SemanticEvent:
        log.warning("Fall confirmed (%s) — critical alert, no cloud involved.", ", ".join(evidence))
        return self._event(
            SemanticType.CONFIRMED_FALL, EventClass.CRITICAL, ts, room=room,
            confidence=confidence, source="vision",
            fields={"evidence": evidence, "camera": camera, "snapshot_reference": None},
        )

    def _mark_activity(self, ts: datetime) -> None:
        if self._s.last_activity is None or ts > self._s.last_activity:
            self._s.last_activity = ts

    @staticmethod
    def _event(type_: SemanticType, class_: EventClass, ts: datetime, *,
               room: str | None = None, confidence: float = 1.0,
               source: str = "sensor", fields: dict | None = None) -> SemanticEvent:
        return SemanticEvent(type=type_, event_class=class_, timestamp=ts,
                             confidence=confidence, source=source, room=room,
                             fields=fields or {})
