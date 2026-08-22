"""Local SQLite storage — everything under /data (docs/06-DATA-MODEL.md §3)."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_states (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id     TEXT NOT NULL,
    state         TEXT NOT NULL,
    previous_state TEXT,
    timestamp     TEXT NOT NULL,
    attributes    TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_states_ts ON raw_states(timestamp);
CREATE INDEX IF NOT EXISTS idx_raw_states_entity ON raw_states(entity_id, timestamp);

CREATE TABLE IF NOT EXISTS semantic_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,
    class         TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 1.0,
    source        TEXT NOT NULL DEFAULT 'sensor',
    room          TEXT,
    fields        TEXT NOT NULL DEFAULT '{}',
    synced        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_semantic_ts ON semantic_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_semantic_unsynced ON semantic_events(synced, timestamp);

CREATE TABLE IF NOT EXISTS entity_mappings (
    entity_id     TEXT PRIMARY KEY,
    semantic_type TEXT NOT NULL,
    room          TEXT,
    appliance     TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    -- 0 = automatic suggestion only; the pipeline does NOT use it.
    -- A wrong meaning causes a wrong alert, so confirmation is required.
    confirmed     INTEGER NOT NULL DEFAULT 0,
    -- The installer's own note: "in the living room, behind the TV", "battery,
    -- replace twice a year". Stored locally only; it NEVER goes to the cloud.
    note          TEXT,
    -- 1 = "not needed": the user set it aside. It shows on a separate tab, and
    -- the pipeline never processes it.
    ignored       INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS daily_features (
    date          TEXT PRIMARY KEY,
    features      TEXT NOT NULL,
    data_quality  REAL NOT NULL,
    anomaly_score REAL,
    reasons       TEXT NOT NULL DEFAULT '[]',
    synced        INTEGER NOT NULL DEFAULT 0
);

-- Offline queue: at least 72 hours of capacity, with an idempotency key (docs/02-ADDON.md §6)
CREATE TABLE IF NOT EXISTS sync_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint        TEXT NOT NULL,
    payload         TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_created ON sync_queue(created_at);
CREATE INDEX IF NOT EXISTS idx_queue_next ON sync_queue(next_attempt_at);

CREATE TABLE IF NOT EXISTS local_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       TEXT NOT NULL,
    type        TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    reasons     TEXT NOT NULL DEFAULT '[]',
    state       TEXT NOT NULL DEFAULT 'DETECTED',
    notified_locally INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "eldercare.db"
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()
        log.info("Database ready: %s", self.path)

    async def _migrate(self) -> None:
        """Add missing columns to existing databases.

        `CREATE TABLE IF NOT EXISTS` does not touch an existing table, so after an
        upgrade the old schema would remain — and the insert would fail silently.
        """
        for table, column, ddl in (
            ("entity_mappings", "confirmed", "INTEGER NOT NULL DEFAULT 0"),
            ("entity_mappings", "updated_at", "TEXT"),
            ("entity_mappings", "note", "TEXT"),
            ("entity_mappings", "ignored", "INTEGER NOT NULL DEFAULT 0"),
            ("sync_queue", "next_attempt_at", "TEXT"),
        ):
            async with self.db.execute(f"PRAGMA table_info({table})") as cursor:
                existing = {row["name"] for row in await cursor.fetchall()}
            if column not in existing:
                log.info("Migration: adding column %s.%s", table, column)
                await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("The database is not connected.")
        return self._db

    async def store_raw(self, entity_id: str, state: str, previous: str | None,
                        timestamp: datetime, attributes: str = "{}") -> None:
        await self.db.execute(
            "INSERT INTO raw_states (entity_id, state, previous_state, timestamp, attributes)"
            " VALUES (?, ?, ?, ?, ?)",
            (entity_id, state, previous, timestamp.isoformat(), attributes),
        )

    async def commit(self) -> None:
        await self.db.commit()

    async def count(self, table: str) -> int:
        # A table name cannot be parameterised, so we allow-list it.
        allowed = {"raw_states", "semantic_events", "sync_queue", "daily_features", "local_alerts"}
        if table not in allowed:
            raise ValueError(f"Unknown table: {table}")
        async with self.db.execute(f"SELECT COUNT(*) AS c FROM {table}") as cur:  # noqa: S608
            row = await cur.fetchone()
            return int(row["c"]) if row else 0

    async def purge_old_raw(self, before: datetime) -> int:
        """Retention — raw states live shorter (docs/06-DATA-MODEL.md §3)."""
        cur = await self.db.execute(
            "DELETE FROM raw_states WHERE timestamp < ?", (before.isoformat(),)
        )
        await self.db.commit()
        return cur.rowcount
