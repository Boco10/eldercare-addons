"""Tests for the critical offline alerting engine.

The project's ground rule: a critical alert reaches the caregiver without
internet, cloud or LLM. These tests prove that — they do not assume it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.alerts.local_engine import MAX_DELIVERY_ATTEMPTS, LocalAlertEngine
from app.events.models import AlertLevel, EventClass, SemanticEvent, SemanticType
from app.events.normalizer import Normalizer
from app.events.semantic_engine import SemanticEngine
from app.ha.replay_client import ReplayEventSource
from app.storage.database import Database

from .test_semantic_engine import build_mappings

# The fixtures live next to the tests, so the package runs on its own.
# They used to expect a `/fixtures` mount: without it 14 tests failed for a
# reason the error message never revealed — a missing mount, not a bug.
FIXTURES = Path(os.getenv("FIXTURES_DIR", str(Path(__file__).parent / "fixtures"))) / "days"


class FakeServiceClient:
    """Records Home Assistant service calls. `available=False` = HA is unreachable too."""

    def __init__(self, available: bool = True, notify_services: list[str] | None = None) -> None:
        self.available = available
        self._notify = notify_services if notify_services is not None else ["mobile_app_phone"]
        self.calls: list[tuple[str, str, dict]] = []

    async def notify_all(self, title: str, message: str, data: dict | None = None) -> dict:
        self.calls.append(("notify", title, {"message": message, "data": data}))
        targets = ["persistent_notification.create"] + [f"notify.{s}" for s in self._notify]
        return dict.fromkeys(targets, self.available)

    async def turn_on(self, entity_id: str) -> bool:
        self.calls.append(("turn_on", entity_id, {}))
        return self.available

    @property
    def notify_services(self) -> list[str]:
        return self._notify


async def make_db(tmp_path) -> Database:
    db = Database(tmp_path)
    await db.connect()
    return db


def critical_event(type_: SemanticType, when: datetime, room: str | None = None,
                   **fields) -> SemanticEvent:
    return SemanticEvent(type=type_, event_class=EventClass.CRITICAL, timestamp=when,
                         room=room, fields=fields)


BASE = datetime.fromisoformat("2026-07-01T14:00:00+02:00")


# ------------------------------------------------------------ basic delivery

@pytest.mark.asyncio
async def test_sos_triggers_immediate_alert(tmp_path):
    db = await make_db(tmp_path)
    services = FakeServiceClient()
    engine = LocalAlertEngine(db, services)

    alerts = await engine.handle([critical_event(SemanticType.SOS_TRIGGERED, BASE)])

    assert len(alerts) == 1
    assert alerts[0].level is AlertLevel.CRITICAL
    assert alerts[0].delivered
    assert services.calls, "the notification has to go out"
    await db.close()


@pytest.mark.asyncio
async def test_alert_message_contains_explanation(tmp_path):
    """An alert without an explanation must not go out (explainability principle)."""
    db = await make_db(tmp_path)
    services = FakeServiceClient()
    engine = LocalAlertEngine(db, services)

    await engine.handle([critical_event(
        SemanticType.CONFIRMED_FALL, BASE, room="livingroom",
        evidence=["person_on_floor", "no_movement_60s"])])

    _, _, payload = services.calls[0]
    message = payload["message"]
    assert "livingroom" in message
    assert "person_on_floor" in message, "it has to name the trigger"
    assert "14:00" in message
    await db.close()


@pytest.mark.asyncio
async def test_alert_persisted_before_delivery(tmp_path):
    """We have to know what happened even after a crash."""
    db = await make_db(tmp_path)
    engine = LocalAlertEngine(db, FakeServiceClient())

    await engine.handle([critical_event(SemanticType.SMOKE_DETECTED, BASE, room="kitchen")])

    async with db.db.execute("SELECT level, type, state, notified_locally FROM local_alerts") as c:
        rows = await c.fetchall()
    assert len(rows) == 1
    assert rows[0]["level"] == "critical"
    assert rows[0]["state"] == "NOTIFIED"
    assert rows[0]["notified_locally"] == 1
    await db.close()


# ------------------------------------------------------------------ cooldown

@pytest.mark.asyncio
async def test_repeated_event_is_suppressed(tmp_path):
    db = await make_db(tmp_path)
    engine = LocalAlertEngine(db, FakeServiceClient(), cooldown_s=300)

    first = await engine.handle([critical_event(SemanticType.SOS_TRIGGERED, BASE)])
    second = await engine.handle([
        critical_event(SemanticType.SOS_TRIGGERED, BASE + timedelta(seconds=30))])

    assert len(first) == 1
    assert second == [], "the same event must not alert again within 5 minutes"
    assert engine.stats["suppressed"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_alert_fires_again_after_cooldown(tmp_path):
    db = await make_db(tmp_path)
    engine = LocalAlertEngine(db, FakeServiceClient(), cooldown_s=300)

    await engine.handle([critical_event(SemanticType.SOS_TRIGGERED, BASE)])
    later = await engine.handle([
        critical_event(SemanticType.SOS_TRIGGERED, BASE + timedelta(seconds=400))])

    assert len(later) == 1, "after the cooldown it has to alert again"
    await db.close()


@pytest.mark.asyncio
async def test_different_rooms_alert_separately(tmp_path):
    db = await make_db(tmp_path)
    engine = LocalAlertEngine(db, FakeServiceClient())

    kitchen = await engine.handle([
        critical_event(SemanticType.SMOKE_DETECTED, BASE, room="kitchen")])
    bedroom = await engine.handle([
        critical_event(SemanticType.SMOKE_DETECTED, BASE + timedelta(seconds=10),
                       room="bedroom")])

    assert len(kitchen) == 1 and len(bedroom) == 1, "two rooms, two separate alerts"
    await db.close()


# -------------------------------------------------- delivery failure handling

@pytest.mark.asyncio
async def test_failed_delivery_is_retried(tmp_path):
    """When no channel is reachable, the alert must not be lost."""
    db = await make_db(tmp_path)
    services = FakeServiceClient(available=False)
    engine = LocalAlertEngine(db, services)

    alerts = await engine.handle([critical_event(SemanticType.SOS_TRIGGERED, BASE)])
    assert not alerts[0].delivered
    assert engine.pending_count == 1
    assert engine.stats["failed"] == 1

    services.available = True          # Home Assistant comes back
    await engine.retry_pending()
    assert engine.pending_count == 0
    assert engine.stats["delivered"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_attempts(tmp_path):
    """Retrying cannot run forever — but giving up has to be logged."""
    db = await make_db(tmp_path)
    engine = LocalAlertEngine(db, FakeServiceClient(available=False))

    await engine.handle([critical_event(SemanticType.SOS_TRIGGERED, BASE)])
    for _ in range(MAX_DELIVERY_ATTEMPTS + 2):
        await engine.retry_pending()

    assert engine.pending_count == 0
    await db.close()


@pytest.mark.asyncio
async def test_partial_channel_failure_still_delivers(tmp_path):
    """One broken channel must not block the others."""
    db = await make_db(tmp_path)

    class PartialClient(FakeServiceClient):
        async def notify_all(self, title, message, data=None):
            self.calls.append(("notify", title, {}))
            return {"persistent_notification.create": True, "notify.broken": False}

    engine = LocalAlertEngine(db, PartialClient())
    alerts = await engine.handle([critical_event(SemanticType.CO_DETECTED, BASE)])

    assert alerts[0].delivered, "one working channel is enough to deliver"
    await db.close()


# ------------------------------------------- MUST NOT alert: behavioural

@pytest.mark.asyncio
async def test_behavioral_events_do_not_alert_locally(tmp_path):
    """No guessing without a baseline — behavioural alerts arrive in phase 3."""
    db = await make_db(tmp_path)
    engine = LocalAlertEngine(db, FakeServiceClient())

    alerts = await engine.handle([SemanticEvent(
        type=SemanticType.EXTENDED_ROOM_STAY, event_class=EventClass.BEHAVIORAL,
        timestamp=BASE, room="bathroom", fields={"duration": 3600})])

    assert alerts == []
    await db.close()


@pytest.mark.asyncio
async def test_sensor_failure_does_not_alert(tmp_path):
    """NEGATIVE TEST: a sensor fault is a system state, not an emergency."""
    db = await make_db(tmp_path)
    engine = LocalAlertEngine(db, FakeServiceClient())

    alerts = await engine.handle([SemanticEvent(
        type=SemanticType.SENSOR_UNAVAILABLE, event_class=EventClass.SYSTEM,
        timestamp=BASE, fields={"entity_id": "binary_sensor.bedroom_presence"})])

    assert alerts == []
    await db.close()


# ------------------------------------------------------- E2E: OFFLINE PATH

@pytest.mark.asyncio
async def test_fall_alert_delivered_with_cloud_unreachable(tmp_path):
    """A LEGFONTOSABB TESZT.

    The whole chain runs — fixture → normalizer → semantic engine → alert —
    with no cloud client anywhere in the picture. If this fails, the product's
    core promise is broken.
    """
    db = await make_db(tmp_path)
    services = FakeServiceClient()
    alert_engine = LocalAlertEngine(db, services)

    source = ReplayEventSource(FIXTURES / "fall_candidate.jsonl", speed=1e9)
    await source.connect()
    normalizer = Normalizer()
    semantic = SemanticEngine(build_mappings())

    delivered = []
    async for raw in source.stream():
        event = normalizer.process(raw)
        if event is None:
            continue
        delivered.extend(await alert_engine.handle(semantic.process(event)))
    await source.close()

    assert delivered, "the fall has to raise an alert"
    fall = [a for a in delivered if a.type is SemanticType.CONFIRMED_FALL]
    assert fall, "the confirmed fall is missing"
    assert fall[0].delivered, "the alert has to go out without a cloud too"
    assert fall[0].level is AlertLevel.CRITICAL
    await db.close()


@pytest.mark.asyncio
async def test_test_alert_uses_real_delivery_path(tmp_path):
    """The test button does not simulate — it uses the real channels."""
    db = await make_db(tmp_path)
    services = FakeServiceClient()
    engine = LocalAlertEngine(db, services)

    channels = await engine.send_test_alert()

    assert any(channels.values())
    assert services.calls, "the test alert makes a real call"
    await db.close()


@pytest.mark.asyncio
async def test_normal_day_produces_no_alerts(tmp_path):
    """Control: an ordinary day must not alert."""
    db = await make_db(tmp_path)
    alert_engine = LocalAlertEngine(db, FakeServiceClient())

    source = ReplayEventSource(FIXTURES / "normal_day.jsonl", speed=1e9)
    await source.connect()
    normalizer = Normalizer()
    semantic = SemanticEngine(build_mappings())

    raised = []
    async for raw in source.stream():
        event = normalizer.process(raw)
        if event:
            raised.extend(await alert_engine.handle(semantic.process(event)))
    await source.close()

    assert raised == [], f"a normal day must not raise an alert: {raised}"
    await db.close()
