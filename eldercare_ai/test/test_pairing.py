"""Tests for pairing and device token storage."""

from __future__ import annotations

import pytest

from app.cloud.pairing import META_DEVICE_TOKEN, PairingManager, TokenStore
from app.cloud.sync_client import SyncResult
from app.storage.database import Database


class FakeCloud:
    def __init__(self, outcomes: dict[str, SyncResult] | None = None):
        self.installation_id = ""
        self.device_token: str | None = None
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, dict]] = []

    async def post(self, path, payload, idempotency_key=None):
        self.calls.append((path, payload))
        return self.outcomes.get(path, SyncResult(ok=True, status=200, body={}))


async def make_manager(tmp_path, cloud: FakeCloud | None = None):
    db = Database(tmp_path)
    await db.connect()
    return db, PairingManager(db, cloud or FakeCloud(), tmp_path)


# ------------------------------------------------------------- installation id

@pytest.mark.asyncio
async def test_installation_id_generated_once(tmp_path):
    db, manager = await make_manager(tmp_path)
    first = (await manager.load()).installation_id
    assert first.startswith("inst_")

    # Restart: the same id.
    db2 = Database(tmp_path)
    await db2.connect()
    second = (await PairingManager(db2, FakeCloud(), tmp_path).load()).installation_id
    assert second == first
    await db.close()
    await db2.close()


@pytest.mark.asyncio
async def test_installation_id_is_anonymous(tmp_path):
    """The id must say nothing about the resident or the home."""
    db, manager = await make_manager(tmp_path)
    state = await manager.load()
    assert state.installation_id.startswith("inst_")
    assert len(state.installation_id) > 12
    await db.close()


# ------------------------------------------------------------- token storage

@pytest.mark.asyncio
async def test_token_not_stored_in_plaintext(tmp_path):
    """A database backup must not contain a directly usable token."""
    db, manager = await make_manager(tmp_path)
    await manager.load()
    await manager.store_token("eld_supersecrettoken12345")

    async with db.db.execute(
        "SELECT value FROM meta WHERE key = ?", (META_DEVICE_TOKEN,)) as cursor:
        row = await cursor.fetchone()
    assert "eld_supersecrettoken12345" not in row["value"]
    await db.close()


@pytest.mark.asyncio
async def test_token_roundtrip(tmp_path):
    db, manager = await make_manager(tmp_path)
    await manager.load()
    await manager.store_token("eld_roundtrip_token_value")

    store = TokenStore(db, tmp_path)
    assert await store.get(META_DEVICE_TOKEN, secret=True) == "eld_roundtrip_token_value"
    await db.close()


@pytest.mark.asyncio
async def test_token_survives_restart(tmp_path):
    db, manager = await make_manager(tmp_path)
    await manager.load()
    await manager.store_token("eld_persisted_token")
    await db.close()

    db2 = Database(tmp_path)
    await db2.connect()
    cloud = FakeCloud()
    state = await PairingManager(db2, cloud, tmp_path).load()

    assert state.paired
    assert cloud.device_token == "eld_persisted_token", "the client has to be set too"
    await db2.close()


@pytest.mark.asyncio
async def test_ui_payload_never_exposes_token(tmp_path):
    """The local UI may only learn THAT there is a token — never the token."""
    db, manager = await make_manager(tmp_path)
    await manager.load()
    await manager.store_token("eld_never_show_this")

    payload = manager.state.to_dict()
    assert payload["paired"] is True
    assert "eld_never_show_this" not in str(payload)
    assert "device_token" not in payload
    await db.close()


# ------------------------------------------------------------ requesting a code

@pytest.mark.asyncio
async def test_request_code_success(tmp_path):
    cloud = FakeCloud({"/v1/pairing/codes": SyncResult(
        ok=True, status=201, body={"code": "ELDER-1234",
                                   "expires_at": "2026-07-30T10:00:00+00:00"})})
    db, manager = await make_manager(tmp_path, cloud)
    await manager.load()

    result = await manager.request_code()

    assert result["ok"] and result["code"] == "ELDER-1234"
    assert manager.state.pending_code == "ELDER-1234"
    # Requesting a code is the ONLY call that goes without a token.
    assert cloud.calls[0][0] == "/v1/pairing/codes"
    await db.close()


@pytest.mark.asyncio
async def test_request_code_offline_gives_readable_message(tmp_path):
    cloud = FakeCloud({"/v1/pairing/codes": SyncResult(ok=False, error_code="unreachable")})
    db, manager = await make_manager(tmp_path, cloud)
    await manager.load()

    result = await manager.request_code()

    assert result["ok"] is False
    assert "unreachable" in result["error"]
    await db.close()


# ----------------------------------------------------------- token validation

@pytest.mark.asyncio
async def test_malformed_token_rejected(tmp_path):
    db, manager = await make_manager(tmp_path)
    await manager.load()

    result = await manager.store_token("nem-jo-formatum")

    assert result["ok"] is False
    assert manager.state.paired is False
    await db.close()


@pytest.mark.asyncio
async def test_token_rejected_by_backend_is_rolled_back(tmp_path):
    """If the backend rejects it, no half-finished pairing may remain."""
    cloud = FakeCloud({"/v1/installations/heartbeat": SyncResult(
        ok=False, status=401, error_code="token_revoked", permanent=True)})
    db, manager = await make_manager(tmp_path, cloud)
    await manager.load()

    result = await manager.store_token("eld_invalid_token_value")

    assert result["ok"] is False
    assert manager.state.paired is False
    assert cloud.device_token is None
    await db.close()


# ------------------------------------------------------------------ unpairing

@pytest.mark.asyncio
async def test_unpair_clears_token(tmp_path):
    db, manager = await make_manager(tmp_path)
    await manager.load()
    await manager.store_token("eld_to_be_removed")
    assert manager.state.paired

    result = await manager.unpair()

    assert result["paired"] is False
    assert manager.cloud.device_token is None
    store = TokenStore(db, tmp_path)
    assert await store.get(META_DEVICE_TOKEN, secret=True) is None
    await db.close()


@pytest.mark.asyncio
async def test_env_token_migrated_to_store(tmp_path):
    """The developer env token is stored, so it survives a restart."""
    db, manager = await make_manager(tmp_path)
    state = await manager.load(fallback_token="eld_from_environment")

    assert state.paired
    store = TokenStore(db, tmp_path)
    assert await store.get(META_DEVICE_TOKEN, secret=True) == "eld_from_environment"
    await db.close()
