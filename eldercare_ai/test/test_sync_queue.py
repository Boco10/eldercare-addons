"""Tests for the offline upload queue.

The failure branches are the point, not the happy path: the system promises
that during an outage data queues up and then goes up idempotently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.cloud.sync_client import SyncResult
from app.cloud.sync_queue import MAX_ATTEMPTS, SyncQueue
from app.storage.database import Database


class FakeClient:
    """A controllable cloud client: it answers every call with a preset result."""

    def __init__(self, device_token: str | None = "token", outcome: SyncResult | None = None):
        self.device_token = device_token
        self.outcome = outcome or SyncResult(ok=True, status=202)
        self.calls: list[tuple[str, dict, str | None]] = []

    async def post(self, path, payload, idempotency_key=None):
        self.calls.append((path, payload, idempotency_key))
        return self.outcome


async def make_queue(tmp_path, client: FakeClient | None = None) -> tuple[Database, SyncQueue]:
    db = Database(tmp_path)
    await db.connect()
    return db, SyncQueue(db, client or FakeClient())


# ------------------------------------------------------------------- write

@pytest.mark.asyncio
async def test_enqueue_then_drain(tmp_path):
    db, queue = await make_queue(tmp_path)
    await queue.enqueue("/v1/daily-features", {"date": "2026-07-01"}, "home_df_2026-07-01")

    assert await queue.pending_count() == 1
    result = await queue.drain()

    assert result.sent == 1
    assert await queue.pending_count() == 0
    await db.close()


@pytest.mark.asyncio
async def test_duplicate_key_not_queued_twice(tmp_path):
    db, queue = await make_queue(tmp_path)
    payload = {"date": "2026-07-01"}
    assert await queue.enqueue("/v1/daily-features", payload, "same-key") is True
    assert await queue.enqueue("/v1/daily-features", payload, "same-key") is False
    assert await queue.pending_count() == 1
    await db.close()


@pytest.mark.asyncio
async def test_idempotency_key_sent_with_request(tmp_path):
    client = FakeClient()
    db, queue = await make_queue(tmp_path, client)
    await queue.enqueue("/v1/daily-features", {"date": "2026-07-01"}, "home_df_2026-07-01")
    await queue.drain()

    _, _, key = client.calls[0]
    assert key == "home_df_2026-07-01", "resending is only safe with an idempotency key"
    await db.close()


@pytest.mark.asyncio
async def test_daily_features_respect_privacy_switch(tmp_path, monkeypatch):
    """What must not go up does NOT even enter the queue."""
    from app.config import settings

    monkeypatch.setattr(settings, "send_daily_features", False)
    db, queue = await make_queue(tmp_path)

    assert await queue.enqueue_daily_features({"date": "2026-07-01"}, "home") is False
    assert await queue.pending_count() == 0
    assert queue.stats["skipped_privacy"] == 1
    await db.close()


# --------------------------------------------------------- offline operation

@pytest.mark.asyncio
async def test_unreachable_cloud_keeps_items(tmp_path):
    """THE MOST IMPORTANT ONE: no data may be lost during an outage."""
    client = FakeClient(outcome=SyncResult(ok=False, error_code="unreachable"))
    db, queue = await make_queue(tmp_path, client)
    for day in range(3):
        await queue.enqueue("/v1/daily-features", {"date": f"2026-07-0{day}"}, f"k{day}")

    result = await queue.drain()

    assert result.sent == 0
    assert result.paused is True, "an unreachable cloud stops the round"
    assert await queue.pending_count() == 3, "every item stays in the queue"
    await db.close()


@pytest.mark.asyncio
async def test_queue_drains_after_connection_returns(tmp_path):
    client = FakeClient(outcome=SyncResult(ok=False, error_code="unreachable"))
    db, queue = await make_queue(tmp_path, client)
    await queue.enqueue("/v1/daily-features", {"date": "2026-07-01"}, "k1")
    await queue.drain()
    assert await queue.pending_count() == 1

    # The connection returns and the hold has expired.
    client.outcome = SyncResult(ok=True, status=202)
    await db.db.execute("UPDATE sync_queue SET next_attempt_at = ?",
                        ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
    await db.commit()

    result = await queue.drain()
    assert result.sent == 1
    assert await queue.pending_count() == 0
    await db.close()


@pytest.mark.asyncio
async def test_no_device_token_pauses_upload(tmp_path):
    """Without pairing we do not send — but we collect, and later it all goes up."""
    client = FakeClient(device_token=None)
    db, queue = await make_queue(tmp_path, client)
    await queue.enqueue("/v1/daily-features", {"date": "2026-07-01"}, "k1")

    result = await queue.drain()

    assert result.paused is True
    assert client.calls == [], "no network call starts without a token"
    assert await queue.pending_count() == 1
    await db.close()


# ------------------------------------------------------- error classification

@pytest.mark.asyncio
async def test_permanent_error_drops_item(tmp_path):
    """409 idempotency collision: retrying does not help."""
    client = FakeClient(outcome=SyncResult(ok=False, status=409,
                                           error_code="idempotency_conflict", permanent=True))
    db, queue = await make_queue(tmp_path, client)
    await queue.enqueue("/v1/daily-features", {"date": "2026-07-01"}, "k1")

    result = await queue.drain()

    assert result.dropped == 1
    assert await queue.pending_count() == 0
    await db.close()


@pytest.mark.asyncio
async def test_rate_limit_pauses_and_respects_retry_after(tmp_path):
    client = FakeClient(outcome=SyncResult(ok=False, status=429, retry_after=45.0))
    db, queue = await make_queue(tmp_path, client)
    for i in range(3):
        await queue.enqueue("/v1/events/batch", {"n": i}, f"k{i}")

    result = await queue.drain()

    assert result.paused is True
    assert len(client.calls) == 1, "a rate limit stops the round"
    async with db.db.execute("SELECT next_attempt_at FROM sync_queue ORDER BY id") as c:
        row = await c.fetchone()
    delay = datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(UTC)
    assert 30 < delay.total_seconds() <= 46, "we wait as long as Retry-After says"
    await db.close()


@pytest.mark.asyncio
async def test_server_error_backs_off_but_keeps_item(tmp_path):
    client = FakeClient(outcome=SyncResult(ok=False, status=503))
    db, queue = await make_queue(tmp_path, client)
    await queue.enqueue("/v1/events/batch", {"n": 1}, "k1")

    await queue.drain()

    assert await queue.pending_count() == 1
    async with db.db.execute("SELECT attempts, last_error FROM sync_queue") as c:
        row = await c.fetchone()
    assert row["attempts"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_item_dropped_after_max_attempts(tmp_path):
    """Retrying cannot run forever, but giving up has to be logged."""
    client = FakeClient(outcome=SyncResult(ok=False, status=503))
    db, queue = await make_queue(tmp_path, client)
    await queue.enqueue("/v1/events/batch", {"n": 1}, "k1")

    for _ in range(MAX_ATTEMPTS + 1):
        await db.db.execute("UPDATE sync_queue SET next_attempt_at = ?",
                            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
        await db.commit()
        await queue.drain()

    assert await queue.pending_count() == 0
    assert queue.stats["dropped"] >= 1
    await db.close()


@pytest.mark.asyncio
async def test_expired_items_are_dropped(tmp_path):
    db, queue = await make_queue(tmp_path)
    await queue.enqueue("/v1/events/batch", {"n": 1}, "old")
    await db.db.execute("UPDATE sync_queue SET created_at = ?",
                        ((datetime.now(UTC) - timedelta(days=30)).isoformat(),))
    await db.commit()

    await queue.drain()
    assert await queue.pending_count() == 0
    await db.close()


# ---------------------------------------------------------------- batching

@pytest.mark.asyncio
async def test_events_are_batched_not_sent_individually(tmp_path):
    """One batch per event would mean hundreds of HTTP requests over 28 days."""
    from app.cloud.sync_queue import EVENT_BATCH_SIZE

    db, queue = await make_queue(tmp_path)
    for i in range(EVENT_BATCH_SIZE - 1):
        await queue.add_event({"type": "room_occupancy", "class": "behavioral",
                               "timestamp": f"2026-07-01T10:{i:02d}:00+02:00"}, "home")

    assert await queue.pending_count() == 0, "the buffer is not full yet"
    assert queue.buffered_events == EVENT_BATCH_SIZE - 1

    await queue.add_event({"type": "bed_exit", "class": "behavioral",
                           "timestamp": "2026-07-01T11:00:00+02:00"}, "home")

    assert await queue.pending_count() == 1, "one batch, not 50 separate items"
    assert queue.buffered_events == 0
    await db.close()


@pytest.mark.asyncio
async def test_critical_event_flushes_immediately(tmp_path):
    """The cloud must not fall behind on a critical event — no waiting for a full buffer."""
    db, queue = await make_queue(tmp_path)
    await queue.add_event({"type": "room_occupancy", "class": "behavioral",
                           "timestamp": "2026-07-01T10:00:00+02:00"}, "home")
    await queue.add_event({"type": "sos_triggered", "class": "critical",
                           "timestamp": "2026-07-01T10:01:00+02:00"}, "home")

    assert await queue.pending_count() == 1
    assert queue.buffered_events == 0
    await db.close()


@pytest.mark.asyncio
async def test_flush_is_idempotent_on_empty_buffer(tmp_path):
    db, queue = await make_queue(tmp_path)
    assert await queue.flush_events() is False
    assert await queue.pending_count() == 0
    await db.close()


# ------------------------------------------------------------------ sorrend

@pytest.mark.asyncio
async def test_items_sent_in_chronological_order(tmp_path):
    """The cloud has to get a consistent picture — the order must not scramble."""
    client = FakeClient()
    db, queue = await make_queue(tmp_path, client)
    for day in (1, 2, 3):
        await queue.enqueue("/v1/daily-features", {"date": f"2026-07-0{day}"}, f"k{day}")
        await db.db.execute(
            "UPDATE sync_queue SET created_at = ? WHERE idempotency_key = ?",
            ((datetime.now(UTC) + timedelta(seconds=day)).isoformat(), f"k{day}"))
    await db.commit()

    await queue.drain()

    dates = [payload["date"] for _, payload, _ in client.calls]
    assert dates == ["2026-07-01", "2026-07-02", "2026-07-03"]
    await db.close()


@pytest.mark.asyncio
async def test_oldest_age_reported_for_diagnostics(tmp_path):
    db, queue = await make_queue(tmp_path)
    assert await queue.oldest_age_seconds() is None

    await queue.enqueue("/v1/events/batch", {"n": 1}, "k1")
    await db.db.execute("UPDATE sync_queue SET created_at = ?",
                        ((datetime.now(UTC) - timedelta(hours=5)).isoformat(),))
    await db.commit()

    age = await queue.oldest_age_seconds()
    assert 4.9 * 3600 < age < 5.1 * 3600
    await db.close()
