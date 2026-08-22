"""Tests for storing entity mappings and for the confirmation rule.

The central rule: a machine suggestion NEVER takes effect without human
confirmation, because a wrong meaning causes a wrong alert.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.events.models import RawEvent, SemanticType
from app.events.semantic_engine import SemanticEngine
from app.ha.entity_discovery import EntityMapping, EntityRole, suggest
from app.storage.database import Database
from app.storage.mappings import MappingStore

BASE = datetime.fromisoformat("2026-07-01T09:00:00+02:00")


async def make_store(tmp_path) -> tuple[Database, MappingStore]:
    db = Database(tmp_path)
    await db.connect()
    return db, MappingStore(db)


def mapping(entity_id: str = "binary_sensor.bedroom_presence", *, confirmed: bool = True,
            role: EntityRole = EntityRole.PRESENCE, room: str | None = "bedroom") -> EntityMapping:
    return EntityMapping(entity_id=entity_id, role=role, room=room, confirmed=confirmed)


# ---------------------------------------------------------------- perzisztencia

@pytest.mark.asyncio
async def test_mapping_survives_restart(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping())
    await db.close()

    db2 = Database(tmp_path)
    await db2.connect()
    reloaded = await MappingStore(db2).load()

    assert "binary_sensor.bedroom_presence" in reloaded
    assert reloaded["binary_sensor.bedroom_presence"].room == "bedroom"
    assert reloaded["binary_sensor.bedroom_presence"].confirmed
    await db2.close()


@pytest.mark.asyncio
async def test_upsert_overwrites(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(room="bedroom"))
    await store.upsert(mapping(room="livingroom"))

    loaded = await store.load()
    assert len(loaded) == 1
    assert loaded["binary_sensor.bedroom_presence"].room == "livingroom"
    await db.close()


@pytest.mark.asyncio
async def test_delete_removes_from_cache_and_db(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping())
    assert await store.delete("binary_sensor.bedroom_presence") is True
    assert await store.load() == {}
    await db.close()


@pytest.mark.asyncio
async def test_unknown_role_in_db_is_skipped_not_crash(tmp_path):
    """An old or corrupt row must not prevent startup."""
    db, store = await make_store(tmp_path)
    await db.db.execute(
        "INSERT INTO entity_mappings (entity_id, semantic_type, confirmed)"
        " VALUES ('sensor.legacy', 'nonexistent_role', 1)")
    await db.commit()

    loaded = await store.load()
    assert "sensor.legacy" not in loaded
    await db.close()


# --------------------------------------------------------- confirmation rule

@pytest.mark.asyncio
async def test_suggestion_stored_unconfirmed(tmp_path):
    db, store = await make_store(tmp_path)
    proposal = suggest("binary_sensor.kitchen_presence", {"device_class": "occupancy"})
    await store.remember_suggestion(proposal)

    loaded = await store.load()
    assert loaded["binary_sensor.kitchen_presence"].confirmed is False
    await db.close()


@pytest.mark.asyncio
async def test_suggestion_never_overwrites_confirmed(tmp_path):
    """The machine must not override the human."""
    db, store = await make_store(tmp_path)
    await store.upsert(mapping(role=EntityRole.PRESENCE, room="hallway"))

    proposal = suggest("binary_sensor.bedroom_presence", {"device_class": "occupancy"})
    await store.remember_suggestion(proposal)   # ez "bedroom"-ot javasolna

    loaded = await store.load()
    assert loaded["binary_sensor.bedroom_presence"].room == "hallway"
    assert loaded["binary_sensor.bedroom_presence"].confirmed is True
    await db.close()


def test_engine_ignores_unconfirmed_mapping():
    """THE MOST IMPORTANT RULE: an unconfirmed mapping produces no events."""
    unconfirmed = {"binary_sensor.bed_occupancy":
                   mapping("binary_sensor.bed_occupancy", confirmed=False,
                           role=EntityRole.BED_OCCUPANCY, room="bedroom")}
    engine = SemanticEngine(unconfirmed)
    events = engine.process(RawEvent("binary_sensor.bed_occupancy", "on", "off", BASE))
    assert events == [], "an unconfirmed mapping must not take effect"


def test_engine_uses_confirmed_mapping():
    confirmed = {"binary_sensor.bed_occupancy":
                 mapping("binary_sensor.bed_occupancy", confirmed=True,
                         role=EntityRole.BED_OCCUPANCY, room="bedroom")}
    engine = SemanticEngine(confirmed)
    events = engine.process(RawEvent("binary_sensor.bed_occupancy", "on", "off", BASE))
    assert [e.type for e in events] == [SemanticType.BED_ENTRY]


def test_engine_ignores_disabled_mapping():
    disabled = mapping("binary_sensor.bed_occupancy", confirmed=True,
                       role=EntityRole.BED_OCCUPANCY)
    disabled.enabled = False
    engine = SemanticEngine({"binary_sensor.bed_occupancy": disabled})
    assert engine.process(RawEvent("binary_sensor.bed_occupancy", "on", "off", BASE)) == []


# ------------------------------------------------------------- export/import

@pytest.mark.asyncio
async def test_export_import_roundtrip(tmp_path):
    db, store = await make_store(tmp_path)
    await store.upsert(mapping("binary_sensor.bedroom_presence", room="bedroom"))
    await store.upsert(mapping("binary_sensor.kitchen_presence", room="kitchen"))
    exported = store.export()

    db2 = Database(tmp_path / "second")
    await db2.connect()
    store2 = MappingStore(db2)
    imported, errors = await store2.import_(exported)

    assert imported == 2
    assert errors == []
    assert (await store2.load()).keys() == {"binary_sensor.bedroom_presence",
                                            "binary_sensor.kitchen_presence"}
    await db.close()
    await db2.close()


@pytest.mark.asyncio
async def test_import_rejects_unknown_version(tmp_path):
    db, store = await make_store(tmp_path)
    with pytest.raises(ValueError, match="version"):
        await store.import_({"version": 99, "mappings": []})
    await db.close()


@pytest.mark.asyncio
async def test_import_continues_after_bad_row(tmp_path):
    """One bad row must not abort the import — the rest takes effect."""
    db, store = await make_store(tmp_path)
    imported, errors = await store.import_({"version": 1, "mappings": [
        {"entity_id": "binary_sensor.good", "role": "presence", "room": "kitchen"},
        {"entity_id": "binary_sensor.bad", "role": "teleporter"},
        {"entity_id": "nodot", "role": "presence"},
    ]})

    assert imported == 1
    assert len(errors) == 2
    assert "binary_sensor.good" in (await store.load())
    await db.close()


# ---------------------------------------------------------------- migration

@pytest.mark.asyncio
async def test_migration_adds_missing_column(tmp_path):
    """Upgrading an old schema: without it the insert would fail silently."""
    import aiosqlite

    path = tmp_path / "eldercare.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as legacy:
        await legacy.execute(
            "CREATE TABLE entity_mappings (entity_id TEXT PRIMARY KEY, semantic_type TEXT"
            " NOT NULL, room TEXT, appliance TEXT, enabled INTEGER NOT NULL DEFAULT 1)")
        await legacy.execute(
            "INSERT INTO entity_mappings (entity_id, semantic_type, room)"
            " VALUES ('binary_sensor.old', 'presence', 'bedroom')")
        await legacy.commit()

    db = Database(tmp_path)
    await db.connect()
    loaded = await MappingStore(db).load()

    assert "binary_sensor.old" in loaded
    assert loaded["binary_sensor.old"].confirmed is False, "an old row cannot go live by itself"
    await db.close()
