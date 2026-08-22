"""Tests for the entity note and the send switch.

    The note is the installer's own memo ("behind the TV", "battery powered").
It is stored locally only — it never reaches the cloud.
"""

from __future__ import annotations

import pytest

from app.cloud.analysis_client import RoutineAnalysisClient
from app.ha.entity_discovery import EntityMapping, EntityRole
from app.storage.database import Database
from app.storage.mappings import MappingStore


async def make_store(tmp_path) -> tuple[Database, MappingStore]:
    db = Database(tmp_path)
    await db.connect()
    return db, MappingStore(db)


def mapping(entity_id: str = "binary_sensor.bedroom_presence", **kwargs) -> EntityMapping:
    defaults = {"role": EntityRole.PRESENCE, "room": "bedroom", "confirmed": True}
    return EntityMapping(entity_id=entity_id, **{**defaults, **kwargs})


# ------------------------------------------------------------------- jegyzet

@pytest.mark.asyncio
async def test_note_is_saved_and_reloaded(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(note="Behind the TV, battery powered"))

    loaded = await store.load()
    assert loaded["binary_sensor.bedroom_presence"].note == "Behind the TV, battery powered"
    await db.close()


@pytest.mark.asyncio
async def test_note_survives_restart(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(note="Battery change twice a year"))
    await db.close()

    db2 = Database(tmp_path)
    await db2.connect()
    loaded = await MappingStore(db2).load()

    assert loaded["binary_sensor.bedroom_presence"].note == "Battery change twice a year"
    await db2.close()


@pytest.mark.asyncio
async def test_note_can_be_cleared(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(note="ideiglenes"))
    await store.upsert(mapping(note=None))

    loaded = await store.load()
    assert loaded["binary_sensor.bedroom_presence"].note is None
    await db.close()


@pytest.mark.asyncio
async def test_note_included_in_export(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(note="konyhapult felett"))

    exported = store.export()
    assert exported["mappings"][0]["note"] == "konyhapult felett"
    await db.close()


@pytest.mark.asyncio
async def test_note_survives_export_import(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(note="bedroom, next to the door"))
    exported = store.export()

    db2 = Database(tmp_path / "masodik")
    await db2.connect()
    store2 = MappingStore(db2)
    await store2.import_(exported)

    loaded = await store2.load()
    assert loaded["binary_sensor.bedroom_presence"].note == "bedroom, next to the door"
    await db.close()
    await db2.close()


@pytest.mark.asyncio
async def test_note_never_leaves_the_home(tmp_path):
    """THE POINT: the note must never enter the payload sent to the backend."""
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(note="Grandma's bedroom"))

    client = RoutineAnalysisClient(cloud=None)  # type: ignore[arg-type]
    sensors = await client.collect_sensors(
        [{"entity_id": "binary_sensor.bedroom_presence", "state": "on"}], store.cache)

    assert sensors, "the sensor has to go up"
    assert "note" not in sensors[0]
    assert "Margit" not in str(sensors)
    await db.close()


# --------------------------------------------------------------- send switch

@pytest.mark.asyncio
async def test_disabled_entity_is_not_collected(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping("binary_sensor.a", enabled=True))
    await store.upsert(mapping("binary_sensor.b", enabled=False))

    client = RoutineAnalysisClient(cloud=None)  # type: ignore[arg-type]
    sensors = await client.collect_sensors(
        [{"entity_id": "binary_sensor.a", "state": "on"},
         {"entity_id": "binary_sensor.b", "state": "on"}], store.cache)

    assert [s["entity_id"] for s in sensors] == ["binary_sensor.a"]
    await db.close()


@pytest.mark.asyncio
async def test_disabled_mapping_keeps_its_configuration(tmp_path):
    """Switching it off does not delete the mapping — it can be switched back."""
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(enabled=False, note="off over the winter break"))

    loaded = await store.load()
    entry = loaded["binary_sensor.bedroom_presence"]
    assert entry.enabled is False
    assert entry.room == "bedroom"
    assert entry.note == "off over the winter break"
    await db.close()


# ------------------------------------------------------------- "not needed"

@pytest.mark.asyncio
async def test_ignored_entity_is_not_collected(tmp_path):
    """A set-aside entity's data never goes up — not even when confirmed."""
    db, store = await make_store(tmp_path)
    await store.upsert(mapping("binary_sensor.a"))
    await store.upsert(mapping("binary_sensor.b", ignored=True))

    client = RoutineAnalysisClient(cloud=None)  # type: ignore[arg-type]
    sensors = await client.collect_sensors(
        [{"entity_id": "binary_sensor.a", "state": "on"},
         {"entity_id": "binary_sensor.b", "state": "on"}], store.cache)

    assert [s["entity_id"] for s in sensors] == ["binary_sensor.a"]
    await db.close()


@pytest.mark.asyncio
async def test_ignored_flag_survives_restart(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(ignored=True))
    await db.close()

    db2 = Database(tmp_path)
    await db2.connect()
    loaded = await MappingStore(db2).load()

    assert loaded["binary_sensor.bedroom_presence"].ignored is True
    await db2.close()


@pytest.mark.asyncio
async def test_ignored_survives_export_import(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(ignored=True))
    exported = store.export()

    db2 = Database(tmp_path / "masodik")
    await db2.connect()
    store2 = MappingStore(db2)
    await store2.import_(exported)

    assert (await store2.load())["binary_sensor.bedroom_presence"].ignored is True
    await db.close()
    await db2.close()


# ---------------------------------------------------------------- migration

@pytest.mark.asyncio
async def test_migration_adds_note_column(tmp_path):
    """Upgrading an old database: the note column is added."""
    import aiosqlite

    path = tmp_path / "eldercare.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as legacy:
        await legacy.execute(
            "CREATE TABLE entity_mappings (entity_id TEXT PRIMARY KEY,"
            " semantic_type TEXT NOT NULL, room TEXT, appliance TEXT,"
            " enabled INTEGER NOT NULL DEFAULT 1, confirmed INTEGER NOT NULL DEFAULT 0)")
        await legacy.execute(
            "INSERT INTO entity_mappings (entity_id, semantic_type, room, confirmed)"
            " VALUES ('binary_sensor.regi', 'presence', 'kitchen', 1)")
        await legacy.commit()

    db = Database(tmp_path)
    await db.connect()
    loaded = await MappingStore(db).load()

    assert loaded["binary_sensor.regi"].note is None
    assert loaded["binary_sensor.regi"].ignored is False
    assert loaded["binary_sensor.regi"].room == "kitchen"
    await db.close()
