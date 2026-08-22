"""Grouping the entity list and shortening the state.

A typical Home Assistant has hundreds of sensors (updates, backups, sunrise).
Listed together with the presence sensors, the installer cannot find what
actually needs configuring.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.local_ui import _short_state, create_app
from app.config import settings
from app.ha.entity_discovery import EntityMapping, EntityRole
from app.storage.database import Database
from app.storage.mappings import MappingStore


class FakeServices:
    def __init__(self, states):
        self._states = states

    async def get_states(self):
        return self._states


def entity(entity_id: str, state: str = "on", **attributes) -> dict:
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


@pytest.fixture
def dev_mode():
    """The middleware only allows the Ingress IP — tests switch to dev mode."""
    original = settings.eldercare_dev_mode
    settings.eldercare_dev_mode = True
    yield
    settings.eldercare_dev_mode = original


async def make_client(tmp_path, states, mappings=(), extra_state=None
                      ) -> tuple[Database, TestClient]:
    db = Database(tmp_path)
    await db.connect()
    store = MappingStore(db)
    for mapping in mappings:
        await store.upsert(mapping)
    app = create_app({"mappings": store, "services": FakeServices(states),
                      **(extra_state or {})})
    return db, TestClient(app)


# ------------------------------------------------------- shortening the state

def test_iso_timestamp_is_shortened():
    """The sunrise sensor's 25-character ISO time would squeeze the column flat."""
    assert _short_state("2026-08-11T02:59:11+00:00") == "2026-08-11 02:59"


def test_iso_timestamp_with_space_separator():
    assert _short_state("2026-08-11 02:59:11+00:00") == "2026-08-11 02:59"


def test_short_state_is_untouched():
    assert _short_state("on") == "on"
    assert _short_state("unknown") == "unknown"


def test_long_state_is_truncated_with_ellipsis():
    result = _short_state("a" * 60)
    assert result.endswith("…")
    assert len(result) == 24


def test_none_becomes_empty_string():
    assert _short_state(None) == ""


def test_numeric_state_survives():
    assert _short_state(21.5) == "21.5"


def test_version_like_state_is_not_mistaken_for_a_date():
    """Not every text with dashes at positions 4 and 10 is a timestamp."""
    assert _short_state("2026-08-11") == "2026-08-11"


# ------------------------------------------------------------------ csoportok

@pytest.mark.asyncio
async def test_entities_are_split_into_three_groups(tmp_path, dev_mode):
    db, client = await make_client(
        tmp_path,
        [entity("binary_sensor.bedroom_motion", device_class="motion"),
         entity("sensor.backup_backup_manager_state", "idle"),
         entity("binary_sensor.kitchen_door", device_class="door")],
        [EntityMapping(entity_id="binary_sensor.kitchen_door", role=EntityRole.DOOR_ENTRY,
                       room="kitchen", confirmed=True)],
    )

    data = client.get("/api/entities").json()
    groups = {e["entity_id"]: e["group"] for e in data["entities"]}

    assert groups["binary_sensor.kitchen_door"] == "confirmed"
    assert groups["binary_sensor.bedroom_motion"] == "suggested"
    assert groups["sensor.backup_backup_manager_state"] == "other"
    await db.close()


@pytest.mark.asyncio
async def test_counts_match_the_groups(tmp_path, dev_mode):
    db, client = await make_client(
        tmp_path,
        [entity("binary_sensor.hall_motion", device_class="motion"),
         entity("sensor.sun_next_dawn", "2026-08-11T02:59:11+00:00"),
         entity("sensor.backup_last_successful_automatic_backup", "unknown")],
    )

    data = client.get("/api/entities").json()

    assert data["counts"] == {"confirmed": 0, "suggested": 1, "other": 2, "ignored": 0}
    await db.close()


# -------------------------------------------------------- tabs by entity type

@pytest.mark.asyncio
async def test_tabs_are_built_per_entity_type(tmp_path, dev_mode):
    db, client = await make_client(
        tmp_path,
        [entity("binary_sensor.hall_motion", device_class="motion"),
         entity("binary_sensor.kitchen_door", device_class="door"),
         entity("sensor.backup_backup_manager_state", "idle"),
         entity("person.jozsef", "home")],
    )

    domains = client.get("/api/entities").json()["domains"]

    assert {d["domain"]: d["total"] for d in domains} == {
        "binary_sensor": 2, "sensor": 1, "person": 1}
    await db.close()


@pytest.mark.asyncio
async def test_tab_shows_how_many_entities_wait_for_setup(tmp_path, dev_mode):
    """The `pending` count leads the installer to the tab that needs work."""
    db, client = await make_client(
        tmp_path,
        [entity("binary_sensor.hall_motion", device_class="motion"),
         entity("binary_sensor.kitchen_door", device_class="door"),
         entity("sensor.backup_backup_manager_state", "idle")],
        [EntityMapping(entity_id="binary_sensor.kitchen_door", role=EntityRole.DOOR_ENTRY,
                       room="kitchen", confirmed=True)],
    )

    domains = {d["domain"]: d for d in client.get("/api/entities").json()["domains"]}

    assert domains["binary_sensor"]["pending"] == 1  # only the motion sensor waits
    assert domains["sensor"]["pending"] == 0  # a system sensor is not a task
    await db.close()


@pytest.mark.asyncio
async def test_ignored_entities_get_their_own_tab(tmp_path, dev_mode):
    db, client = await make_client(
        tmp_path,
        [entity("binary_sensor.hall_motion", device_class="motion"),
         entity("sensor.backup_backup_manager_state", "idle")],
        [EntityMapping(entity_id="sensor.backup_backup_manager_state",
                       role=EntityRole.UNKNOWN, ignored=True)],
    )

    data = client.get("/api/entities").json()
    tabs = {d["domain"]: d["total"] for d in data["domains"]}

    assert tabs == {"binary_sensor": 1, "__ignored__": 1}
    assert "sensor" not in tabs, "a set-aside entity leaves its type tab"
    await db.close()


@pytest.mark.asyncio
async def test_ignored_tab_comes_last(tmp_path, dev_mode):
    """You only reach in there to undo — so it must not be the first tab."""
    db, client = await make_client(
        tmp_path,
        [entity("binary_sensor.hall_motion", device_class="motion"),
         entity("person.jozsef", "home")],
        [EntityMapping(entity_id="person.jozsef", role=EntityRole.UNKNOWN, ignored=True)],
    )

    tabs = [d["domain"] for d in client.get("/api/entities").json()["domains"]]

    assert tabs[-1] == "__ignored__"
    await db.close()


@pytest.mark.asyncio
async def test_ignoring_does_not_confirm_the_entity(tmp_path, dev_mode):
    """"Not needed" is not a confirmation — undoing must not add it silently."""
    db, client = await make_client(
        tmp_path, [entity("binary_sensor.hall_motion", device_class="motion")])

    client.post("/api/mappings", json={
        "entity_id": "binary_sensor.hall_motion", "role": "presence",
        "room": "hallway", "ignored": True})

    row = client.get("/api/entities").json()["entities"][0]
    assert row["group"] == "ignored"
    assert row["mapping"]["confirmed"] is False
    assert row["mapping"]["ignored"] is True
    await db.close()


@pytest.mark.asyncio
async def test_ignored_entity_is_not_active(tmp_path, dev_mode):
    """THE POINT: no branch of the pipeline processes a set-aside entity."""
    ignored = EntityMapping(entity_id="binary_sensor.hall_motion",
                            role=EntityRole.PRESENCE, room="hallway",
                            confirmed=True, enabled=True, ignored=True)

    assert ignored.active is False


@pytest.mark.asyncio
async def test_within_a_tab_confirmed_comes_first(tmp_path, dev_mode):
    """Inside a tab: what needs doing first, system entities last."""
    db, client = await make_client(
        tmp_path,
        [entity("binary_sensor.zzz_system", "off"),
         entity("binary_sensor.mmm_motion", device_class="motion"),
         entity("binary_sensor.aaa_bed", device_class="occupancy")],
        [EntityMapping(entity_id="binary_sensor.aaa_bed", role=EntityRole.BED_OCCUPANCY,
                       room="bedroom", confirmed=True)],
    )

    rows = client.get("/api/entities").json()["entities"]

    assert [e["group"] for e in rows] == ["confirmed", "suggested", "other"]
    await db.close()


@pytest.mark.asyncio
async def test_long_state_keeps_its_full_value_for_the_tooltip(tmp_path, dev_mode):
    db, client = await make_client(
        tmp_path, [entity("sensor.sun_next_dawn", "2026-08-11T02:59:11+00:00")])

    row = client.get("/api/entities").json()["entities"][0]

    assert row["state"] == "2026-08-11 02:59"
    assert row["state_full"] == "2026-08-11T02:59:11+00:00"
    await db.close()


# ------------------------------------------------------ pairing state in the UI

class FakePairingManager:
    """The part of the pairing manager the status endpoint uses."""

    def __init__(self, token: str | None = None):
        self.state = SimpleNamespace(device_token=token, paired=bool(token))


@pytest.mark.asyncio
async def test_status_reports_pairing_from_the_manager(tmp_path, dev_mode):
    """After pairing from the UI the panel must show paired at once."""
    db, client = await make_client(
        tmp_path, [], extra_state={"pairing": FakePairingManager("eld_abc"),
                                   "device_token": False})

    assert client.get("/api/status").json()["paired"] is True
    await db.close()


@pytest.mark.asyncio
async def test_status_reports_unpaired_after_disconnect(tmp_path, dev_mode):
    """The worse direction: after unpairing it must NOT show paired."""
    db, client = await make_client(
        tmp_path, [], extra_state={"pairing": FakePairingManager(None),
                                   "device_token": True})

    assert client.get("/api/status").json()["paired"] is False
    await db.close()


@pytest.mark.asyncio
async def test_status_falls_back_when_manager_missing(tmp_path, dev_mode):
    """During startup there is no manager yet — then `state` is the source."""
    db, client = await make_client(tmp_path, [], extra_state={"device_token": True})

    assert client.get("/api/status").json()["paired"] is True
    await db.close()
