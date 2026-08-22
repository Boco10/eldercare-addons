"""Storing and loading the entity mappings.

The semantic mapping (entity_id → room + physical meaning) is one of the
system's most sensitive settings: a wrong meaning makes the system draw a wrong
conclusion and raise a wrong alert. Therefore:

  - An automatic suggestion is stored with `confirmed=False`.
  - The pipeline uses confirmed mappings ONLY.
  - Every change is timestamped, so it can be traced back.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.ha.entity_discovery import EntityMapping, EntityRole
from app.storage.database import Database

log = logging.getLogger(__name__)


class MappingStore:
    """Handles the entity_mappings table."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[str, EntityMapping] = {}

    async def load(self) -> dict[str, EntityMapping]:
        """Load at startup. The cache is the pipeline's live view."""
        self._cache.clear()
        async with self.db.db.execute(
            "SELECT entity_id, semantic_type, room, appliance, enabled, confirmed, note,"
            " ignored FROM entity_mappings"
        ) as cursor:
            for row in await cursor.fetchall():
                try:
                    role = EntityRole(row["semantic_type"])
                except ValueError:
                    log.warning("Unknown semantic type in the database: %s (%s) — skipped",
                                row["semantic_type"], row["entity_id"])
                    continue
                self._cache[row["entity_id"]] = EntityMapping(
                    entity_id=row["entity_id"], role=role, room=row["room"],
                    appliance=row["appliance"], enabled=bool(row["enabled"]),
                    confirmed=bool(row["confirmed"]), note=row["note"],
                    ignored=bool(row["ignored"]),
                )

        confirmed = sum(1 for m in self._cache.values() if m.confirmed)
        log.info("Mappings loaded: %d entries (%d confirmed)", len(self._cache), confirmed)
        return self._cache

    @property
    def cache(self) -> dict[str, EntityMapping]:
        return self._cache

    async def upsert(self, mapping: EntityMapping) -> None:
        await self.db.db.execute(
            "INSERT INTO entity_mappings"
            " (entity_id, semantic_type, room, appliance, enabled, confirmed, note,"
            "  ignored, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(entity_id) DO UPDATE SET"
            "   semantic_type=excluded.semantic_type, room=excluded.room,"
            "   appliance=excluded.appliance, enabled=excluded.enabled,"
            "   confirmed=excluded.confirmed, note=excluded.note,"
            "   ignored=excluded.ignored, updated_at=excluded.updated_at",
            (mapping.entity_id, mapping.role.value, mapping.room, mapping.appliance,
             int(mapping.enabled), int(mapping.confirmed), mapping.note,
             int(mapping.ignored), datetime.now(UTC).isoformat()),
        )
        await self.db.commit()
        self._cache[mapping.entity_id] = mapping

    async def delete(self, entity_id: str) -> bool:
        cursor = await self.db.db.execute(
            "DELETE FROM entity_mappings WHERE entity_id = ?", (entity_id,))
        await self.db.commit()
        self._cache.pop(entity_id, None)
        return cursor.rowcount > 0

    async def remember_suggestion(self, mapping: EntityMapping) -> None:
        """Remember a suggestion WITHOUT confirming it.

        It never overwrites an entry the user already confirmed — a machine
        suggestion must not override a human decision.
        """
        existing = self._cache.get(mapping.entity_id)
        if existing is not None and existing.confirmed:
            return
        mapping.confirmed = False
        await self.upsert(mapping)

    # ------------------------------------------------------------ export/import

    def export(self) -> dict[str, Any]:
        return {
            "version": 1,
            "exported_at": datetime.now(UTC).isoformat(),
            "mappings": [
                {"entity_id": m.entity_id, "role": m.role.value, "room": m.room,
                 "appliance": m.appliance, "enabled": m.enabled,
                 "confirmed": m.confirmed, "note": m.note, "ignored": m.ignored}
                for m in sorted(self._cache.values(), key=lambda m: m.entity_id)
            ],
        }

    async def import_(self, payload: dict[str, Any]) -> tuple[int, list[str]]:
        """Returns (imported count, error messages).

        One bad row does not abort the import — the rest takes effect, and the
        caller sees exactly what was left out.
        """
        if payload.get("version") != 1:
            raise ValueError(f"Unsupported export version: {payload.get('version')}")

        imported, errors = 0, []
        for entry in payload.get("mappings", []):
            entity_id = entry.get("entity_id")
            try:
                role = EntityRole(entry["role"])
            except (KeyError, ValueError):
                errors.append(f"{entity_id}: unknown role ({entry.get('role')})")
                continue
            if not entity_id or "." not in entity_id:
                errors.append(f"Invalid entity_id: {entity_id!r}")
                continue
            await self.upsert(EntityMapping(
                entity_id=entity_id, role=role, room=entry.get("room"),
                appliance=entry.get("appliance"), enabled=bool(entry.get("enabled", True)),
                confirmed=bool(entry.get("confirmed", True)), note=entry.get("note"),
                ignored=bool(entry.get("ignored", False)),
            ))
            imported += 1

        log.info("Mapping import: %d entries, %d errors", imported, len(errors))
        return imported, errors

    def export_json(self) -> str:
        return json.dumps(self.export(), indent=2, ensure_ascii=False)
