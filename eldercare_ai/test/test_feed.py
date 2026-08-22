"""The live sensor view endpoint.

This view exists so that debugging reveals one thing: does the sensor reach the
system, and if so, why no event comes of it. The "why not" is the point —
without it, all you see is that nothing happens.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.local_ui import create_app
from app.config import settings
from app.ha.entity_discovery import EntityMapping, EntityRole
from app.storage.database import Database
from app.storage.mappings import MappingStore


@pytest.fixture
def dev_mode():
    original = settings.eldercare_dev_mode
    settings.eldercare_dev_mode = True
    yield
    settings.eldercare_dev_mode = original


async def make_client(tmp_path, mappings=()) -> tuple[Database, MappingStore, TestClient]:
    db = Database(tmp_path)
    await db.connect()
    store = MappingStore(db)
    for mapping in mappings:
        await store.upsert(mapping)
    app = create_app({"db": db, "mappings": store})
    return db, store, TestClient(app)


async def add_state(db: Database, entity_id: str, state: str = "on",
                    previous: str | None = "off") -> None:
    await db.store_raw(entity_id, state, previous, datetime.now(UTC), "{}")
    await db.commit()


async def add_event(db: Database, kind: str = "presence_detected", room: str = "kitchen",
                    synced: int = 0) -> None:
    await db.db.execute(
        "INSERT INTO semantic_events (type, class, timestamp, confidence, source, room,"
        " fields, synced) VALUES (?, 'behavioral', ?, 1.0, 'sensor', ?, '{}', ?)",
        (kind, datetime.now(UTC).isoformat(), room, synced))
    await db.commit()


def confirmed(entity_id: str, **kwargs) -> EntityMapping:
    defaults = {"role": EntityRole.PRESENCE, "room": "kitchen", "confirmed": True}
    return EntityMapping(entity_id=entity_id, **{**defaults, **kwargs})


# --------------------------------------------------------- incoming state

@pytest.mark.asyncio
async def test_incoming_state_is_listed(tmp_path, dev_mode):
    db, _, client = await make_client(tmp_path, [confirmed("binary_sensor.kitchen")])
    await add_state(db, "binary_sensor.kitchen")

    row = client.get("/api/feed").json()["states"][0]

    assert row["entity_id"] == "binary_sensor.kitchen"
    assert row["state"] == "on"
    assert row["previous_state"] == "off"
    assert row["processed"] is True
    assert row["skip_reason"] is None
    await db.close()


@pytest.mark.asyncio
async def test_newest_state_comes_first(tmp_path, dev_mode):
    db, _, client = await make_client(tmp_path)
    await add_state(db, "binary_sensor.regi")
    await add_state(db, "binary_sensor.uj")

    states = client.get("/api/feed").json()["states"]

    assert states[0]["entity_id"] == "binary_sensor.uj"
    await db.close()


@pytest.mark.asyncio
async def test_long_state_is_shortened(tmp_path, dev_mode):
    db, _, client = await make_client(tmp_path)
    await add_state(db, "sensor.sun_next_dawn", "2026-08-11T02:59:11+00:00", None)

    row = client.get("/api/feed").json()["states"][0]

    assert row["state"] == "2026-08-11 02:59"
    await db.close()


# ------------------------------------------------------ why it is not processed

@pytest.mark.asyncio
async def test_unmapped_entity_is_explained(tmp_path, dev_mode):
    db, _, client = await make_client(tmp_path)
    await add_state(db, "binary_sensor.ismeretlen")

    row = client.get("/api/feed").json()["states"][0]

    assert row["processed"] is False
    assert row["skip_reason"] == "unmapped"
    await db.close()


@pytest.mark.asyncio
async def test_unconfirmed_mapping_is_explained(tmp_path, dev_mode):
    db, _, client = await make_client(
        tmp_path, [confirmed("binary_sensor.kitchen", confirmed=False)])
    await add_state(db, "binary_sensor.kitchen")

    assert client.get("/api/feed").json()["states"][0]["skip_reason"] == "unconfirmed"
    await db.close()


@pytest.mark.asyncio
async def test_disabled_mapping_is_explained(tmp_path, dev_mode):
    db, _, client = await make_client(
        tmp_path, [confirmed("binary_sensor.kitchen", enabled=False)])
    await add_state(db, "binary_sensor.kitchen")

    assert client.get("/api/feed").json()["states"][0]["skip_reason"] == "disabled"
    await db.close()


@pytest.mark.asyncio
async def test_ignored_mapping_is_explained(tmp_path, dev_mode):
    db, _, client = await make_client(
        tmp_path, [confirmed("binary_sensor.kitchen", ignored=True)])
    await add_state(db, "binary_sensor.kitchen")

    assert client.get("/api/feed").json()["states"][0]["skip_reason"] == "ignored"
    await db.close()


@pytest.mark.asyncio
async def test_missing_mapping_wins_over_other_reasons(tmp_path, dev_mode):
    """The order of what to do: the mapping first, then deliberate exclusions."""
    db, _, client = await make_client(tmp_path)
    await add_state(db, "binary_sensor.unmapped_one")

    assert client.get("/api/feed").json()["states"][0]["skip_reason"] == "unmapped"
    await db.close()


# ------------------------------------------------------------------- events

@pytest.mark.asyncio
async def test_semantic_events_are_listed(tmp_path, dev_mode):
    db, _, client = await make_client(tmp_path)
    await add_event(db, "presence_detected", "bathroom", synced=1)

    event = client.get("/api/feed").json()["events"][0]

    assert event["type"] == "presence_detected"
    assert event["room"] == "bathroom"
    assert event["synced"] is True
    await db.close()


@pytest.mark.asyncio
async def test_unsynced_event_is_marked_local(tmp_path, dev_mode):
    db, _, client = await make_client(tmp_path)
    await add_event(db, synced=0)

    assert client.get("/api/feed").json()["events"][0]["synced"] is False
    await db.close()


# ------------------------------------------------------------------- limits

@pytest.mark.asyncio
async def test_limit_is_respected(tmp_path, dev_mode):
    db, _, client = await make_client(tmp_path)
    for i in range(10):
        await add_state(db, f"binary_sensor.s{i}")

    assert len(client.get("/api/feed?limit=3").json()["states"]) == 3
    await db.close()


@pytest.mark.asyncio
async def test_limit_is_capped(tmp_path, dev_mode):
    """A large limit must not overload the UI — the server truncates."""
    db, _, client = await make_client(tmp_path)
    for i in range(5):
        await add_state(db, f"binary_sensor.s{i}")

    response = client.get("/api/feed?limit=100000")

    assert response.status_code == 200
    assert len(response.json()["states"]) == 5
    await db.close()


@pytest.mark.asyncio
async def test_totals_report_the_whole_database(tmp_path, dev_mode):
    """The list is short, but the counter shows every stored row."""
    db, _, client = await make_client(tmp_path)
    for i in range(7):
        await add_state(db, f"binary_sensor.s{i}")
    await add_event(db)

    totals = client.get("/api/feed?limit=2").json()["totals"]

    assert totals == {"raw_states": 7, "semantic_events": 1}
    await db.close()


@pytest.mark.asyncio
async def test_feed_is_not_ready_without_database(dev_mode):
    client = TestClient(create_app({}))

    assert client.get("/api/feed").status_code == 503
