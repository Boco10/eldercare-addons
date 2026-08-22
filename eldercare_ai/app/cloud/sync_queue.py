"""Offline upload queue.

The system is local-first: during an outage events queue up, and when the
connection returns they go up idempotently, in order (docs/02-ADDON.md §6).

What this module guarantees:

  1. **No data loss.** The payload reaches the database before any network
     call starts. A power cut delays the send at worst; it never erases it.
  2. **Idempotency.** Every item gets a deterministic key, so a resend never
     creates a duplicate in the cloud.
  3. **Privacy filtering on write.** What the settings forbid never enters the
     queue at all — it is not decided at send time.
  4. **The critical alert does not depend on this.** The local notification
     went out long before a row lands here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.cloud.sync_client import CloudSyncClient
from app.storage.database import Database

log = logging.getLogger(__name__)

# The plan asks for at least 72 hours of capacity; we are more generous, so an
# outage during a longer holiday does not lose data either.
MAX_AGE_DAYS = 14
MAX_ATTEMPTS = 12
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 3600.0
DRAIN_BATCH = 25
EVENT_BATCH_SIZE = 50
"""The buffer flushes into one batch after this many semantic events."""


@dataclass(slots=True)
class DrainResult:
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    paused: bool = False
    """True when the round stopped on a rate limit or an unreachable cloud."""


class SyncQueue:
    """Persistent, idempotent upload queue."""

    def __init__(self, db: Database, client: CloudSyncClient) -> None:
        self.db = db
        self.client = client
        self.stats = {"enqueued": 0, "sent": 0, "dropped": 0, "skipped_privacy": 0}
        self._buffer: list[dict] = []
        self._buffer_home: str = "home_local"

    # ------------------------------------------------------------------- write

    async def enqueue(self, endpoint: str, payload: dict, idempotency_key: str) -> bool:
        """Enqueue. False when the same key is already present."""
        try:
            await self.db.db.execute(
                "INSERT INTO sync_queue (endpoint, payload, idempotency_key, created_at,"
                " next_attempt_at) VALUES (?, ?, ?, ?, ?)",
                (endpoint, json.dumps(payload, ensure_ascii=False, default=str),
                 idempotency_key, datetime.now(UTC).isoformat(),
                 datetime.now(UTC).isoformat()),
            )
        except Exception as exc:  # aiosqlite.IntegrityError -> UNIQUE collision
            if "UNIQUE" not in str(exc):
                raise
            log.debug("Already queued: %s", idempotency_key)
            return False

        await self.db.commit()
        self.stats["enqueued"] += 1
        log.debug("Queued: %s (%s)", endpoint, idempotency_key)
        return True

    async def enqueue_daily_features(self, payload: dict, home_id: str) -> bool:
        """Daily features. The privacy switch decides before anything is queued."""
        from app.config import settings

        if not settings.send_daily_features:
            self.stats["skipped_privacy"] += 1
            log.info("Daily summary upload is off — not queued.")
            return False
        return await self.enqueue(
            "/v1/daily-features", payload,
            f"{home_id}_daily-features_{payload['date']}",
        )

    async def enqueue_events(self, events: list[dict], home_id: str, batch_key: str) -> bool:
        """Semantic events, batched.

        Raw events are NEVER sent unless explicitly enabled
        (docs/10-SECURITY-PRIVACY.md §1) — by default the cloud sees only semantic events.
        """
        if not events:
            return False
        return await self.enqueue(
            "/v1/events/batch", {"home_id": home_id, "events": events},
            f"{home_id}_events_{batch_key}",
        )

    async def enqueue_alert(self, payload: dict, home_id: str, alert_key: str) -> bool:
        return await self.enqueue(
            "/v1/alerts", payload, f"{home_id}_alerts_{alert_key}")

    # ---------------------------------------------------------------- batching

    async def add_event(self, event_payload: dict, home_id: str) -> None:
        """Buffer a semantic event.

        One batch per event would mean ~550 HTTP requests over 28 days for a few
        hundred events. The buffer cuts that by an order of magnitude.

        The loss window is small and handled: buffered events are already in the
        `semantic_events` table, so a crash does not lose them.
        A critical event flushes at once, so the cloud does not fall behind on it.
        """
        self._buffer.append(event_payload)
        self._buffer_home = home_id
        if (len(self._buffer) >= EVENT_BATCH_SIZE
                or event_payload.get("class") == "critical"):
            await self.flush_events()

    async def flush_events(self) -> bool:
        """Enqueue the buffer. Call it on a day change and on shutdown too."""
        if not self._buffer:
            return False
        batch, self._buffer = self._buffer, []
        # The key is the last event's timestamp plus the size: deterministic, so a
        # repeated replay produces the very same key.
        batch_key = f"{batch[-1]['timestamp']}_{len(batch)}"
        return await self.enqueue_events(batch, self._buffer_home, batch_key)

    @property
    def buffered_events(self) -> int:
        return len(self._buffer)

    # ------------------------------------------------------------------ flush

    async def drain(self, limit: int = DRAIN_BATCH) -> DrainResult:
        """One upload round. In time order, so the cloud gets a consistent picture."""
        result = DrainResult()

        if not self.client.device_token:
            # Without pairing there is nothing to send — the queue keeps growing,
            # and everything goes up once pairing happens.
            log.debug("No device token — uploading is paused.")
            result.paused = True
            return result

        await self._drop_expired()
        now = datetime.now(UTC)

        async with self.db.db.execute(
            "SELECT id, endpoint, payload, idempotency_key, attempts FROM sync_queue"
            " WHERE next_attempt_at IS NULL OR next_attempt_at <= ?"
            " ORDER BY created_at LIMIT ?",
            (now.isoformat(), limit),
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            outcome = await self.client.post(
                row["endpoint"], json.loads(row["payload"]),
                idempotency_key=row["idempotency_key"],
            )

            if outcome.ok:
                await self._delete(row["id"])
                result.sent += 1
                self.stats["sent"] += 1
                continue

            # Rate limit or unreachable cloud: stop the whole round. There is no
            # point trying the remaining items in the same second.
            if outcome.status == 429 or outcome.error_code == "unreachable":
                delay = outcome.retry_after or BACKOFF_BASE_S
                await self._postpone(row["id"], row["attempts"], delay, outcome.error_code)
                result.paused = True
                result.failed += 1
                break

            if outcome.permanent:
                # 401/402/409/410 — retrying does not help.
                log.warning("Permanent error (%s), item dropped: %s -> %s",
                            outcome.status, row["endpoint"], outcome.error_code)
                await self._delete(row["id"])
                result.dropped += 1
                self.stats["dropped"] += 1
                continue

            await self._postpone(row["id"], row["attempts"], None, outcome.error_code)
            result.failed += 1

        if result.sent:
            log.info("Uploaded %d items, %d left in the queue.",
                     result.sent, await self.pending_count())
        return result

    async def _postpone(self, row_id: int, attempts: int, delay: float | None,
                        error: str | None) -> None:
        attempts += 1
        if attempts >= MAX_ATTEMPTS:
            log.error("Item given up after %d attempts (%s) — DROPPED.", attempts, error)
            await self._delete(row_id)
            self.stats["dropped"] += 1
            return

        wait = delay if delay is not None else min(
            BACKOFF_BASE_S * (2 ** (attempts - 1)), BACKOFF_CAP_S)
        next_at = datetime.now(UTC) + timedelta(seconds=wait)
        await self.db.db.execute(
            "UPDATE sync_queue SET attempts = ?, last_error = ?, next_attempt_at = ?"
            " WHERE id = ?",
            (attempts, error, next_at.isoformat(), row_id),
        )
        await self.db.commit()

    async def _delete(self, row_id: int) -> None:
        await self.db.db.execute("DELETE FROM sync_queue WHERE id = ?", (row_id,))
        await self.db.commit()

    async def _drop_expired(self) -> int:
        """Drop items that are too old — a daily summary from weeks ago is no
        longer useful, but it would grow the queue without bound."""
        cutoff = (datetime.now(UTC) - timedelta(days=MAX_AGE_DAYS)).isoformat()
        cursor = await self.db.db.execute(
            "DELETE FROM sync_queue WHERE created_at < ?", (cutoff,))
        await self.db.commit()
        if cursor.rowcount:
            log.warning("Dropped %d items from the queue (older than %d days).",
                        cursor.rowcount, MAX_AGE_DAYS)
            self.stats["dropped"] += cursor.rowcount
        return cursor.rowcount

    # ------------------------------------------------------------------ status

    async def pending_count(self) -> int:
        return await self.db.count("sync_queue")

    async def oldest_age_seconds(self) -> float | None:
        """Age of the oldest queued item — for the diagnostics panel."""
        async with self.db.db.execute(
            "SELECT created_at FROM sync_queue ORDER BY created_at LIMIT 1") as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        created = datetime.fromisoformat(row["created_at"])
        return (datetime.now(UTC) - created).total_seconds()
