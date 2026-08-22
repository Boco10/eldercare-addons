"""Tests for the daily features and the data quality."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.events.data_quality import DataQualityTracker, day_bounds
from app.events.feature_builder import DailyFeatureSet, FeatureBuilder
from app.events.models import EventClass, RawEvent, SemanticEvent, SemanticType
from app.events.normalizer import Normalizer
from app.events.semantic_engine import SemanticEngine
from app.ha.entity_discovery import EntityMapping, EntityRole
from app.ha.replay_client import ReplayEventSource

from .test_semantic_engine import build_mappings

# The fixtures live next to the tests, so the package runs on its own.
# They used to expect a `/fixtures` mount: without it 14 tests failed for a
# reason the error message never revealed — a missing mount, not a bug.
FIXTURES = Path(os.getenv("FIXTURES_DIR", str(Path(__file__).parent / "fixtures"))) / "days"
DAY = datetime.fromisoformat("2026-07-01T00:00:00+02:00")


def event(type_: SemanticType, hour: float, room: str | None = None,
          cls: EventClass = EventClass.BEHAVIORAL, **fields) -> SemanticEvent:
    return SemanticEvent(type=type_, event_class=cls, timestamp=DAY + timedelta(hours=hour),
                         room=room, fields=fields)


# ----------------------------------------------------------- day boundaries

def test_day_starts_at_dawn_not_midnight():
    """A night waking and the morning wake-up belong to ONE logical day."""
    at_two_am = datetime.fromisoformat("2026-07-02T02:00:00+02:00")
    start, end = day_bounds(at_two_am, day_start_hour=4)
    assert start.date().isoformat() == "2026-07-01", "02:00 still belongs to the previous day"
    assert end.date().isoformat() == "2026-07-02"


def test_day_boundary_after_dawn():
    at_ten = datetime.fromisoformat("2026-07-02T10:00:00+02:00")
    start, _ = day_bounds(at_ten, day_start_hour=4)
    assert start.date().isoformat() == "2026-07-02"


# ---------------------------------------------------------------- features

def test_wakeup_is_first_bed_exit_after_dawn():
    """The observation day = wake-up + daytime + bedtime + THE NIGHT THAT FOLLOWS.

    Because of the 04:00 rollover, a pre-dawn waking belongs to the previous day
    a docs/12-DECISIONS.md-ben.
    """
    builder = FeatureBuilder(day_start_hour=4)
    builder.add(event(SemanticType.BED_EXIT, 7.5))       # wake-up
    builder.add(event(SemanticType.BED_ENTRY, 22.0))     # bedtime
    builder.add(event(SemanticType.NIGHT_WAKING, 23.5))  # the night waking
    features = builder.finalize()

    assert features.wakeup_time.strftime("%H:%M") == "07:30"
    assert features.bedtime.strftime("%H:%M") == "22:00"
    assert features.night_wakings == 1


def test_pre_dawn_events_belong_to_previous_day():
    """A 02:00 waking still belongs to the previous observation day."""
    builder = FeatureBuilder(day_start_hour=4)
    builder.add(event(SemanticType.NIGHT_WAKING, 2.0))
    builder.add(event(SemanticType.BED_EXIT, 7.5))
    finished = builder.finalize()

    # The 02:00 event closed 06-30; the 07:30 one opened 07-01.
    assert finished.date.isoformat() == "2026-07-01"
    assert finished.night_wakings == 0, "the pre-dawn waking belongs to the previous day"


def test_bathroom_visits_aggregated():
    builder = FeatureBuilder()
    builder.add(event(SemanticType.ROOM_OCCUPANCY, 8.0, "bathroom", duration=600))
    builder.add(event(SemanticType.ROOM_OCCUPANCY, 14.0, "bathroom", duration=1800))
    builder.add(event(SemanticType.ROOM_OCCUPANCY, 15.0, "kitchen", duration=900))
    features = builder.finalize()

    assert features.bathroom_visits == 2
    assert features.bathroom_total_s == 2400
    assert features.bathroom_longest_s == 1800


def test_first_kitchen_activity_and_appliances():
    builder = FeatureBuilder()
    builder.add(event(SemanticType.APPLIANCE_USED, 7.8, "kitchen", appliance="coffee"))
    builder.add(event(SemanticType.APPLIANCE_USED, 12.0, "kitchen", appliance="kettle"))
    features = builder.finalize()

    assert features.first_kitchen_activity.strftime("%H:%M") == "07:48"
    assert features.appliances_used == ["coffee", "kettle"]
    assert features.appliance_uses == 2


def test_weekend_flag():
    saturday = datetime.fromisoformat("2026-07-04T09:00:00+02:00")
    builder = FeatureBuilder()
    builder.add(SemanticEvent(type=SemanticType.BED_EXIT, event_class=EventClass.BEHAVIORAL,
                              timestamp=saturday))
    assert builder.finalize().is_weekend is True


def test_day_rollover_returns_previous_day():
    builder = FeatureBuilder(day_start_hour=4)
    assert builder.add(event(SemanticType.BED_EXIT, 8.0)) is None
    finished = builder.add(event(SemanticType.BED_EXIT, 32.0))   # next day 08:00

    assert finished is not None
    assert finished.date.isoformat() == "2026-07-01"
    assert builder.current_day.isoformat() == "2026-07-02"


def test_baseline_fields_are_none_until_phase_three():
    """We do not send an invented value to the cloud or to the AI."""
    builder = FeatureBuilder()
    builder.add(event(SemanticType.BED_EXIT, 7.0))
    features = builder.finalize()

    assert features.usual_wakeup_window is None
    assert features.movement_reduction_percent is None


# ------------------------------------------------------------- data quality

def confirmed_mappings(count: int) -> dict[str, EntityMapping]:
    return {
        f"binary_sensor.room{i}_presence": EntityMapping(
            entity_id=f"binary_sensor.room{i}_presence", role=EntityRole.PRESENCE,
            room=f"room{i}", confirmed=True)
        for i in range(count)
    }


def emit(tracker: DataQualityTracker, entity_id: str, hours) -> None:
    """Alternating on/off reports — how a real binary sensor behaves.

    Sending the same state every time would (correctly) make the consistency
    check report lost events.
    """
    state = "off"
    for hour in hours:
        nxt = "on" if state == "off" else "off"
        tracker.add(RawEvent(entity_id, nxt, state, DAY + timedelta(hours=hour)))
        state = nxt


def test_quality_zero_without_mappings():
    tracker = DataQualityTracker({})
    report = tracker.score(DAY, DAY + timedelta(days=1))
    assert report.score == 0.0
    assert report.usable is False


def test_quality_low_with_few_entities():
    """Few mapped entities make the evaluation unreliable on their own."""
    mappings = confirmed_mappings(2)
    tracker = DataQualityTracker(mappings)
    for entity_id in mappings:
        emit(tracker, entity_id, (7, 12, 19))

    report = tracker.score(DAY, DAY + timedelta(days=1))
    assert report.coverage < 0.6
    assert report.usable is False, "two sensors are not enough to conclude from"


def test_quality_high_with_good_coverage():
    mappings = confirmed_mappings(8)
    tracker = DataQualityTracker(mappings)
    for entity_id in mappings:
        emit(tracker, entity_id, range(6, 22, 2))

    report = tracker.score(DAY, DAY + timedelta(hours=22))
    assert report.coverage == 1.0
    assert report.usable is True, f"good coverage has to be usable: {report}"


def test_silent_entity_is_flagged_but_does_not_block_evaluation():
    """Silence is ambiguous: a quiet day OR a broken sensor.

    Critical rule: silence must not be scored as a data fault. If a motionless
    resident produced few events and that pushed us into 'insufficient data',
    the system would fall silent exactly when it matters most.
    """
    mappings = confirmed_mappings(8)
    tracker = DataQualityTracker(mappings)
    silent = "binary_sensor.room0_presence"
    for entity_id in mappings:
        if entity_id == silent:
            continue
        emit(tracker, entity_id, range(6, 22, 2))

    report = tracker.score(DAY, DAY + timedelta(hours=22))
    assert any(silent in problem for problem in report.problem_entities), "it has to be reported"
    assert report.usable, "but it must NOT block the evaluation — anomaly detection reads silence"


def test_quiet_day_stays_evaluable():
    """A day with a barely moving resident stays evaluable."""
    mappings = confirmed_mappings(8)
    tracker = DataQualityTracker(mappings)
    # Only two sensors report, twice — a very quiet day.
    for entity_id in list(mappings)[:2]:
        emit(tracker, entity_id, (7, 8))

    report = tracker.score(DAY, DAY + timedelta(hours=22))
    assert report.usable, (
        f"a quiet day has to stay evaluable, not be hidden: {report.to_dict()}")


def test_unavailable_sensor_lowers_availability():
    mappings = confirmed_mappings(8)
    tracker = DataQualityTracker(mappings)
    broken = "binary_sensor.room0_presence"

    for entity_id in mappings:
        emit(tracker, entity_id, range(6, 22, 2))
    # One sensor is unreachable for 10 hours.
    tracker.add(RawEvent(broken, "unavailable", "on", DAY + timedelta(hours=8)))
    tracker.add(RawEvent(broken, "on", "unavailable", DAY + timedelta(hours=18)))

    report = tracker.score(DAY, DAY + timedelta(hours=22))
    assert report.availability < 1.0
    assert any(broken in problem for problem in report.problem_entities)


def test_unconfirmed_mapping_not_counted():
    """An unconfirmed mapping does not count towards coverage."""
    mappings = confirmed_mappings(8)
    for mapping in mappings.values():
        mapping.confirmed = False
    tracker = DataQualityTracker(mappings)
    assert tracker.score(DAY, DAY + timedelta(days=1)).score == 0.0


# --------------------------------------------------------- E2E on fixtures

@pytest.mark.asyncio
async def test_normal_day_features_from_fixture():
    """The generated normal day has to produce meaningful features."""
    source = ReplayEventSource(FIXTURES / "normal_day.jsonl", speed=1e9)
    await source.connect()
    normalizer, semantic = Normalizer(), SemanticEngine(build_mappings())
    builder = FeatureBuilder(day_start_hour=4)

    async for raw in source.stream():
        if (event_ := normalizer.process(raw)) is not None:
            for semantic_event in semantic.process(event_):
                builder.add(semantic_event)
    await source.close()

    features = builder.finalize()
    assert features is not None
    assert features.wakeup_time is not None, "the wake-up has to be recognised"
    assert 5 <= features.wakeup_time.hour <= 10
    assert features.appliances_used, "the coffee maker has to show up"
    assert features.rooms_visited, "there have to be rooms"


@pytest.mark.asyncio
async def test_low_data_quality_day_is_marked_unusable():
    """NEGATIVE TEST: on incomplete data, 'insufficient data' — not a conclusion."""
    mappings = build_mappings()
    source = ReplayEventSource(FIXTURES / "low_data_quality.jsonl", speed=1e9)
    await source.connect()
    normalizer, semantic = Normalizer(), SemanticEngine(mappings)
    builder, tracker = FeatureBuilder(), DataQualityTracker(mappings)

    first = last = None
    async for raw in source.stream():
        if (event_ := normalizer.process(raw)) is None:
            continue
        tracker.add(event_)
        first = first or event_.timestamp
        last = event_.timestamp
        for semantic_event in semantic.process(event_):
            builder.add(semantic_event)
    await source.close()

    features = builder.finalize()
    report = tracker.score(first, last)
    day_set = DailyFeatureSet(features=features, quality=report, event_count=0)

    assert not day_set.usable, f"incomplete data must not be concluded from: {report}"
    assert "insufficient_data" in day_set.to_payload("Europe/Budapest")["reasons"]


@pytest.mark.asyncio
async def test_payload_matches_api_contract():
    """The cloud-bound shape matches the contract (docs/05-API-CONTRACT.md §6)."""
    builder = FeatureBuilder()
    builder.add(event(SemanticType.BED_EXIT, 9.3))
    builder.add(event(SemanticType.NIGHT_WAKING, 23.5))
    features = builder.finalize()

    tracker = DataQualityTracker(confirmed_mappings(8))
    for entity_id in confirmed_mappings(8):
        emit(tracker, entity_id, range(6, 22, 2))
    report = tracker.score(DAY, DAY + timedelta(hours=22))

    payload = DailyFeatureSet(features, report, 12).to_payload("Europe/Budapest")

    assert payload["date"] == "2026-07-01"
    assert payload["timezone"] == "Europe/Budapest"
    assert isinstance(payload["data_quality"], float)
    assert payload["features"]["wakeup_time"] == "09:18"
    assert payload["features"]["night_wakings"] == 1
    assert payload["local_anomaly_score"] is None, "no score until phase 3"
