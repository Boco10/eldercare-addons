"""Tests for the routine analysis and the camera snapshot.

Per decision A14 the evaluation runs on the backend. The add-on is responsible
for collecting, privacy filtering and displaying — that is what is tested here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.cloud.analysis_client import AnalysisResult, RoutineAnalysisClient
from app.cloud.sync_client import SyncResult
from app.ha.camera_client import Snapshot, may_upload_image
from app.ha.entity_discovery import EntityMapping, EntityRole


class FakeCloud:
    def __init__(self, outcome: SyncResult | None = None):
        self.device_token = "token"
        self.outcome = outcome or SyncResult(ok=True, status=200, body={
            "analysis_id": "an_1", "risk_level": "medium", "risk_score": 0.72,
            "reasons": ["late_wakeup"], "summary": "Late wake-up.",
            "recommended_action": "contact_resident", "urgency": "within_30_minutes",
            "data_quality": 0.9, "image_analyzed": False, "credits_used": 30,
            "_mock": True,
        })
        self.calls: list[tuple[str, dict]] = []

    async def post(self, path, payload, idempotency_key=None):
        self.calls.append((path, payload))
        return self.outcome


class FakeCamera:
    def __init__(self, available: bool = True):
        self.available = available
        self.snapshot_calls: list[str] = []

    async def snapshot(self, entity_id: str):
        self.snapshot_calls.append(entity_id)
        if not self.available:
            return None
        return Snapshot(camera=entity_id, captured_at=datetime.now(UTC), data=b"\xff\xd8fake")


def mappings() -> dict[str, EntityMapping]:
    return {
        "binary_sensor.bedroom_presence": EntityMapping(
            "binary_sensor.bedroom_presence", EntityRole.PRESENCE, "bedroom", confirmed=True),
        "sensor.coffee_machine_power": EntityMapping(
            "sensor.coffee_machine_power", EntityRole.APPLIANCE_POWER, "kitchen",
            appliance="coffee", confirmed=True),
        "binary_sensor.unconfirmed": EntityMapping(
            "binary_sensor.unconfirmed", EntityRole.PRESENCE, "hall", confirmed=False),
    }


STATES = [
    {"entity_id": "binary_sensor.bedroom_presence", "state": "off",
     "last_changed": "2026-07-29T07:02:00+02:00"},
    {"entity_id": "sensor.coffee_machine_power", "state": "0",
     "last_changed": "2026-07-29T07:31:00+02:00"},
    {"entity_id": "binary_sensor.unconfirmed", "state": "on",
     "last_changed": "2026-07-29T08:00:00+02:00"},
    {"entity_id": "sun.sun", "state": "above_horizon"},
]


# ---------------------------------------------------------------- collecting

@pytest.mark.asyncio
async def test_only_confirmed_sensors_are_sent():
    """An unconfirmed or unmapped entity never leaves the home."""
    client = RoutineAnalysisClient(FakeCloud())
    sensors = await client.collect_sensors(STATES, mappings())

    ids = {s["entity_id"] for s in sensors}
    assert ids == {"binary_sensor.bedroom_presence", "sensor.coffee_machine_power"}
    assert "binary_sensor.unconfirmed" not in ids
    assert "sun.sun" not in ids


@pytest.mark.asyncio
async def test_sensor_payload_carries_semantics():
    client = RoutineAnalysisClient(FakeCloud())
    sensors = await client.collect_sensors(STATES, mappings())
    coffee = next(s for s in sensors if s["entity_id"] == "sensor.coffee_machine_power")

    assert coffee["role"] == "appliance_power"
    assert coffee["room"] == "kitchen"
    assert coffee["state"] == "0"


# -------------------------------------------------------------- privacy gate

@pytest.mark.parametrize(("mode", "trigger", "expected"), [
    ("never", "manual", False),
    ("never", "critical", False),
    ("critical_only", "manual", False),
    ("critical_only", "critical", True),
    ("critical_only", "anomaly_candidate", True),
    ("on_request", "manual", True),
    ("on_request", "scheduled", False),
    ("always", "scheduled", True),
])
def test_image_upload_gate(monkeypatch, mode, trigger, expected):
    from app.config import settings
    monkeypatch.setattr(settings, "image_upload_mode", mode)
    assert may_upload_image(trigger) is expected


@pytest.mark.asyncio
async def test_image_not_attached_when_mode_forbids(monkeypatch):
    """The image is filtered WHILE BUILDING THE REQUEST — the server does not decide."""
    from app.config import settings
    monkeypatch.setattr(settings, "image_upload_mode", "critical_only")

    cloud, camera = FakeCloud(), FakeCamera()
    client = RoutineAnalysisClient(cloud, camera)
    await client.analyze("home", [{"entity_id": "x"}], camera_entity="camera.living",
                         trigger="manual")

    _, payload = cloud.calls[0]
    assert "image" not in payload, "manual trigger + critical_only -> no image"
    assert camera.snapshot_calls == [], "the image is not even fetched"


@pytest.mark.asyncio
async def test_image_attached_when_allowed(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "image_upload_mode", "on_request")

    cloud, camera = FakeCloud(), FakeCamera()
    client = RoutineAnalysisClient(cloud, camera)
    await client.analyze("home", [{"entity_id": "x"}], camera_entity="camera.living",
                         trigger="manual")

    _, payload = cloud.calls[0]
    assert payload["image"]["camera"] == "camera.living"
    assert payload["image"]["data_base64"]


@pytest.mark.asyncio
async def test_no_camera_requested_means_no_image():
    cloud, camera = FakeCloud(), FakeCamera()
    client = RoutineAnalysisClient(cloud, camera)
    await client.analyze("home", [{"entity_id": "x"}], trigger="manual")

    _, payload = cloud.calls[0]
    assert "image" not in payload
    assert camera.snapshot_calls == []


@pytest.mark.asyncio
async def test_failed_snapshot_does_not_break_analysis(monkeypatch):
    """A broken camera must not prevent the routine analysis."""
    from app.config import settings
    monkeypatch.setattr(settings, "image_upload_mode", "always")

    cloud = FakeCloud()
    client = RoutineAnalysisClient(cloud, FakeCamera(available=False))
    result = await client.analyze("home", [{"entity_id": "x"}],
                                  camera_entity="camera.broken", trigger="manual")

    assert result.ok
    _, payload = cloud.calls[0]
    assert "image" not in payload


# ------------------------------------------------------------- request shape

@pytest.mark.asyncio
async def test_request_matches_contract():
    cloud = RoutineAnalysisClient(FakeCloud())
    fake = cloud.cloud
    await cloud.analyze("home_123", [{"entity_id": "x"}],
                        recent_events=[{"type": "bed_exit"}], trigger="manual")

    path, payload = fake.calls[0]
    assert path == "/v1/analysis/routine"
    assert payload["home_id"] == "home_123"
    assert payload["trigger"] == "manual"
    assert "timestamp" in payload
    assert payload["sensors"] and payload["recent_events"]


@pytest.mark.asyncio
async def test_recent_events_are_capped():
    """We never upload an unbounded history in one request."""
    from app.cloud.analysis_client import RECENT_EVENT_LIMIT

    cloud = FakeCloud()
    client = RoutineAnalysisClient(cloud)
    await client.analyze("home", [{"entity_id": "x"}],
                         recent_events=[{"n": i} for i in range(200)])

    _, payload = cloud.calls[0]
    assert len(payload["recent_events"]) == RECENT_EVENT_LIMIT


# ----------------------------------------------------------- handling replies

@pytest.mark.asyncio
async def test_successful_response_parsed():
    client = RoutineAnalysisClient(FakeCloud())
    result = await client.analyze("home", [{"entity_id": "x"}])

    assert result.ok
    assert result.risk_level == "medium"
    assert result.reasons == ["late_wakeup"]
    assert result.is_mock is True, "a mock reply has to be marked as such"
    assert client.last_result is result


@pytest.mark.asyncio
async def test_unreachable_backend_returns_readable_message():
    """The local UI should show a sentence, not an error code."""
    cloud = FakeCloud(SyncResult(ok=False, error_code="unreachable"))
    result = await RoutineAnalysisClient(cloud).analyze("home", [{"entity_id": "x"}])

    assert result.ok is False
    assert "unreachable" in result.summary
    assert "critical alerts" in result.summary.lower(), \
        "it has to reassure: the critical path lives on regardless"


@pytest.mark.asyncio
async def test_insufficient_credit_message():
    cloud = FakeCloud(SyncResult(ok=False, status=402, error_code="insufficient_credit",
                                 permanent=True))
    result = await RoutineAnalysisClient(cloud).analyze("home", [{"entity_id": "x"}])

    assert result.ok is False
    assert "credit" in result.summary


@pytest.mark.asyncio
async def test_unknown_level_when_backend_cannot_evaluate():
    """When the backend cannot evaluate, we do not display a guess."""
    cloud = FakeCloud(SyncResult(ok=True, status=200, body={
        "risk_level": "unknown", "risk_score": None,
        "reasons": ["insufficient_data"], "summary": "Insufficient data.",
    }))
    result = await RoutineAnalysisClient(cloud).analyze("home", [])

    assert result.risk_level == "unknown"
    assert result.risk_score is None
    assert result.reasons == ["insufficient_data"]


def test_result_serialises_for_ui():
    payload = AnalysisResult(ok=True, risk_level="high", reasons=["prolonged_inactivity"],
                             summary="…").to_dict()
    assert payload["risk_level"] == "high"
    assert payload["reasons"] == ["prolonged_inactivity"]
