"""Tests for the semantic engine, on the recorded days.

Regenerating the fixtures:  python test/fixtures/generate.py
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from app.events.models import EventClass, SemanticType
from app.events.normalizer import Normalizer
from app.events.semantic_engine import SemanticEngine
from app.events.thresholds import Thresholds
from app.ha.entity_discovery import EntityRole, suggest
from app.ha.replay_client import ReplayEventSource

# The fixtures live next to the tests, so the package runs on its own.
# They used to expect a `/fixtures` mount: without it 14 tests failed for a
# reason the error message never revealed — a missing mount, not a bug.
FIXTURES = Path(os.getenv("FIXTURES_DIR", str(Path(__file__).parent / "fixtures"))) / "days"


def build_mappings() -> dict:
    """Automatic suggestions for the dev-stack entities, confirmed."""
    entities = {
        "binary_sensor.bedroom_presence": {"device_class": "occupancy"},
        "binary_sensor.bathroom_presence": {"device_class": "occupancy"},
        "binary_sensor.kitchen_presence": {"device_class": "occupancy"},
        "binary_sensor.livingroom_presence": {"device_class": "occupancy"},
        "binary_sensor.bed_occupancy": {"device_class": "occupancy"},
        "binary_sensor.front_door": {"device_class": "door"},
        "binary_sensor.smoke_detector": {"device_class": "smoke"},
        "sensor.coffee_machine_power": {"device_class": "power"},
        "sensor.frigate_livingroom_person": {},
    }
    mappings = {}
    for entity_id, attrs in entities.items():
        mapping = suggest(entity_id, attrs)
        mapping.confirmed = True
        mappings[entity_id] = mapping
    return mappings


async def run_fixture(name: str, thresholds: Thresholds | None = None) -> list:
    """Run one fixture day through, returning the semantic events produced."""
    source = ReplayEventSource(FIXTURES / f"{name}.jsonl", speed=1e9)
    await source.connect()
    normalizer = Normalizer()
    engine = SemanticEngine(build_mappings(), thresholds)

    produced = []
    async for raw in source.stream():
        event = normalizer.process(raw)
        if event:
            produced.extend(engine.process(event))
    await source.close()
    return produced


def types_of(events) -> Counter:
    return Counter(e.type for e in events)


# ---------------------------------------------------------------- discovery

@pytest.mark.parametrize(("entity_id", "attrs", "role", "room"), [
    ("binary_sensor.bedroom_presence", {"device_class": "occupancy"},
     EntityRole.PRESENCE, "bedroom"),
    ("binary_sensor.bed_occupancy", {"device_class": "occupancy"}, EntityRole.BED_OCCUPANCY, None),
    ("binary_sensor.front_door", {"device_class": "door"}, EntityRole.DOOR_ENTRY, None),
    ("binary_sensor.smoke_detector", {"device_class": "smoke"}, EntityRole.SMOKE, None),
    # The name does not reveal the room -> the user supplies it in the UI.
    ("sensor.coffee_machine_power", {"device_class": "power"}, EntityRole.APPLIANCE_POWER, None),
    ("binary_sensor.bathroom_presence", {}, EntityRole.PRESENCE, "bathroom"),
    ("sensor.frigate_livingroom_person", {}, EntityRole.VISION_PERSON, "livingroom"),
])
def test_entity_suggestion(entity_id, attrs, role, room):
    mapping = suggest(entity_id, attrs)
    assert mapping.role is role
    if room:
        assert mapping.room == room
    # A suggestion NEVER takes effect without confirmation.
    assert mapping.confirmed is False


def test_helper_domains_are_not_auto_suggested():
    """Helper entities are often mirrors of a real sensor — processing both
    would produce every event twice."""
    for entity_id in ("input_boolean.bedroom_presence", "input_number.coffee_machine_power",
                      "automation.morning_routine"):
        assert suggest(entity_id, {"device_class": "occupancy"}).role is EntityRole.UNKNOWN


@pytest.mark.asyncio
async def test_redundant_sensors_produce_one_event():
    """Two sensors for one room must not yield two semantic events."""
    from app.events.models import RawEvent

    mappings = build_mappings()
    mappings["binary_sensor.bedroom_mmwave"] = suggest(
        "binary_sensor.bedroom_mmwave", {"device_class": "occupancy"})
    mappings["binary_sensor.bedroom_mmwave"].confirmed = True

    engine = SemanticEngine(mappings)
    base = datetime.fromisoformat("2026-07-01T09:00:00+02:00")

    first = engine.process(RawEvent("binary_sensor.bedroom_presence", "on", "off", base))
    second = engine.process(RawEvent("binary_sensor.bedroom_mmwave", "on", "off",
                                     base.replace(second=2)))
    # Neither produces an event (entry), but if either did, they cannot both
    # be for the same room.
    rooms = [(e.type, e.room) for e in (*first, *second)]
    assert len(rooms) == len(set(rooms)), f"duplicated event: {rooms}"


def test_appliance_name_extraction():
    assert suggest("sensor.coffee_machine_power", {"device_class": "power"}).appliance == "coffee"


# --------------------------------------------------------------- normal day

@pytest.mark.asyncio
async def test_normal_day_produces_routine_events():
    events = await run_fixture("normal_day")
    counts = types_of(events)

    assert counts[SemanticType.BED_EXIT] >= 1, "the wake-up has to be recognised"
    assert counts[SemanticType.APPLIANCE_USED] >= 1, "the coffee maker has to be recognised"
    assert counts[SemanticType.ROOM_OCCUPANCY] >= 3

    # A normal day has NO critical event.
    assert not [e for e in events if e.event_class is EventClass.CRITICAL]


@pytest.mark.asyncio
async def test_normal_day_has_no_extended_stay():
    events = await run_fixture("normal_day")
    assert types_of(events)[SemanticType.EXTENDED_ROOM_STAY] == 0


# ------------------------------------------------------------- anomaly days

@pytest.mark.asyncio
async def test_extended_bathroom_detected():
    events = await run_fixture("extended_bathroom")
    extended = [e for e in events if e.type is SemanticType.EXTENDED_ROOM_STAY]
    assert extended, "the 52-minute bathroom stay has to be recognised"
    assert extended[0].room == "bathroom"
    assert extended[0].fields["duration"] > 1800
    # The baseline arrives in phase 3 — until then, honestly None.
    assert extended[0].fields["baseline_percentile"] is None


@pytest.mark.asyncio
async def test_night_wakings_detected():
    events = await run_fixture("night_wakings")
    assert types_of(events)[SemanticType.NIGHT_WAKING] >= 3


@pytest.mark.asyncio
async def test_late_wakeup_has_no_coffee():
    events = await run_fixture("late_wakeup")
    assert types_of(events)[SemanticType.APPLIANCE_USED] == 0, "the coffee did not happen"
    assert types_of(events)[SemanticType.NIGHT_WAKING] >= 3


@pytest.mark.asyncio
async def test_no_activity_produces_inactivity_period():
    events = await run_fixture("no_activity")
    inactivity = [e for e in events if e.type is SemanticType.INACTIVITY_PERIOD]
    assert inactivity, "the 9-hour movement-free stretch has to be recognised"
    assert inactivity[0].fields["duration"] >= 4 * 3600


# ---------------------------------------------------------- fall validation

@pytest.mark.asyncio
async def test_fall_candidate_is_confirmed_with_corroboration():
    events = await run_fixture("fall_candidate")
    counts = types_of(events)
    assert counts[SemanticType.POSSIBLE_FALL] >= 1
    assert counts[SemanticType.CONFIRMED_FALL] >= 1, "stillness has to confirm it"

    fall = next(e for e in events if e.type is SemanticType.CONFIRMED_FALL)
    assert fall.event_class is EventClass.CRITICAL
    assert fall.is_critical, "a critical event alerts without a cloud"
    assert fall.fields["evidence"], "a confirmation carries an evidence list"


@pytest.mark.asyncio
async def test_low_confidence_vision_is_ignored():
    """An uncertain camera signal must not start a fall sequence."""
    engine = SemanticEngine(build_mappings(), Thresholds(fall_min_confidence=0.95))
    from app.events.models import RawEvent

    events = engine.process(RawEvent(
        entity_id="sensor.frigate_livingroom_person",
        state="on_floor", previous_state="standing",
        timestamp=datetime.fromisoformat("2026-07-01T14:00:00+02:00"),
        attributes={"confidence": 0.80, "camera": "livingroom"},
    ))
    assert events == []


@pytest.mark.asyncio
async def test_fall_confirms_without_any_further_events():
    """SAFETY TEST: when nothing happens next to an unconscious person, the
    alert still has to be raised. The periodic tick() is what does it."""
    from app.events.models import RawEvent

    engine = SemanticEngine(build_mappings())
    base = datetime.fromisoformat("2026-07-01T14:00:00+02:00")

    first = engine.process(RawEvent("sensor.frigate_livingroom_person", "on_floor", "standing",
                                    base, {"confidence": 0.85, "camera": "livingroom"}))
    assert not [e for e in first if e.type is SemanticType.CONFIRMED_FALL], \
        "we do not confirm immediately"

    # No sensor event arrives at all — only the clock moves.
    later = engine.tick(base.replace(minute=2))
    fall = [e for e in later if e.type is SemanticType.CONFIRMED_FALL]
    assert fall, "without tick() the alert would never fire"
    assert fall[0].event_class is EventClass.CRITICAL


@pytest.mark.asyncio
async def test_movement_after_candidate_cancels_fall():
    """If the person moves again, the fall candidate is refuted — no alert."""
    from app.events.models import RawEvent

    engine = SemanticEngine(build_mappings())
    base = datetime.fromisoformat("2026-07-01T14:00:00+02:00")

    engine.process(RawEvent("sensor.frigate_livingroom_person", "on_floor", "standing",
                            base, {"confidence": 0.85, "camera": "livingroom"}))
    after = engine.process(RawEvent("sensor.frigate_livingroom_person", "standing", "on_floor",
                                    base.replace(second=20), {"confidence": 0.9}))
    assert not [e for e in after if e.type is SemanticType.CONFIRMED_FALL]


# ------------------------------------------------- NEGATIVE TESTS (the point)

@pytest.mark.asyncio
async def test_sensor_failure_is_not_behavioral_anomaly():
    """A sensor fault is a system state, NOT a behavioural anomaly."""
    events = await run_fixture("sensor_failure")
    unavailable = [e for e in events if e.type is SemanticType.SENSOR_UNAVAILABLE]
    assert unavailable, "a dropped-out sensor has to be reported"
    assert all(e.event_class is EventClass.SYSTEM for e in unavailable)
    assert not [e for e in events if e.event_class is EventClass.CRITICAL], \
        "a sensor fault can NEVER be a critical alert"


@pytest.mark.asyncio
async def test_low_data_quality_produces_no_critical_alert():
    """No strong conclusion may be drawn from incomplete data."""
    events = await run_fixture("low_data_quality")
    assert not [e for e in events if e.event_class is EventClass.CRITICAL]


# ---------------------------------------------------------------- catalogue

@pytest.mark.asyncio
async def test_all_emitted_types_are_in_catalog():
    """No type may be produced that is not in the catalogue."""
    for name in ("normal_day", "late_wakeup", "fall_candidate", "no_activity"):
        for event in await run_fixture(name):
            assert isinstance(event.type, SemanticType)
            payload = event.to_payload()
            assert payload["catalog_version"] == 1
            assert json.dumps(payload)  # it has to be serialisable
