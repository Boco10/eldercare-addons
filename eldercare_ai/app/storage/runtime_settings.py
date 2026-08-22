"""Privacy settings that can be changed at runtime.

The add-on options (`config.yaml` → `options.json`) only change with a restart,
and they live on a separate Home Assistant Configuration tab. The privacy
switches belong where their effect is visible — next to the camera panel — and
they have to take effect at once.

So the settings live in two places, in a clear order:

  1. **Runtime override** (`meta` table) — what the user sets in the local UI.
  2. **Add-on option** — the default, when there is no override.

Under a Supervisor, saving also updates the add-on options, so the Home
Assistant Configuration tab does not show a stale value.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.storage.database import Database

log = logging.getLogger(__name__)

META_KEY = "privacy_overrides"

IMAGE_MODES = ("never", "critical_only", "on_request", "always")

# Only these can be written from the local UI. The thresholds and the cloud
# address are deliberately absent: those are install-time settings, not daily decisions.
EDITABLE = ("image_upload_mode", "send_daily_features", "send_raw_events",
            "local_raw_retention_days")


@dataclass(slots=True)
class PrivacySettings:
    image_upload_mode: str
    send_daily_features: bool
    send_raw_events: bool
    local_raw_retention_days: int

    @classmethod
    def current(cls) -> PrivacySettings:
        return cls(
            image_upload_mode=settings.image_upload_mode,
            send_daily_features=settings.send_daily_features,
            send_raw_events=settings.send_raw_events,
            local_raw_retention_days=settings.local_raw_retention_days,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_upload_mode": self.image_upload_mode,
            "send_daily_features": self.send_daily_features,
            "send_raw_events": self.send_raw_events,
            "local_raw_retention_days": self.local_raw_retention_days,
        }


def validate(values: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Only valid and permitted fields get through."""
    cleaned: dict[str, Any] = {}

    for key, value in values.items():
        if key not in EDITABLE or value is None:
            continue
        if key == "image_upload_mode":
            if value not in IMAGE_MODES:
                return {}, f"Unknown image upload mode: {value}"
            cleaned[key] = value
        elif key == "local_raw_retention_days":
            try:
                days = int(value)
            except (TypeError, ValueError):
                return {}, "Retention must be a whole number."
            if not 1 <= days <= 365:
                return {}, "Retention must be between 1 and 365 days."
            cleaned[key] = days
        else:
            cleaned[key] = bool(value)

    return cleaned, None


async def load(db: Database) -> PrivacySettings:
    """At startup: apply the stored overrides to the settings."""
    async with db.db.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)) as cursor:
        row = await cursor.fetchone()

    if row is not None:
        try:
            stored = json.loads(row["value"])
        except (ValueError, TypeError):
            log.warning("Corrupt privacy override in the database — falling back to the default.")
            stored = {}
        cleaned, error = validate(stored)
        if error:
            log.warning("Invalid stored setting (%s) — skipped.", error)
        for key, value in cleaned.items():
            setattr(settings, key, value)
        if cleaned:
            log.info("Privacy overrides loaded: %s", cleaned)

    current = PrivacySettings.current()
    log.info("Privacy: image upload=%s, daily summary=%s, raw events=%s",
             current.image_upload_mode, current.send_daily_features,
             current.send_raw_events)
    return current


async def save(db: Database, values: dict[str, Any],
               services=None) -> tuple[PrivacySettings | None, str | None]:
    """Save, apply at once, and — where possible — update the add-on option."""
    cleaned, error = validate(values)
    if error:
        return None, error
    if not cleaned:
        return PrivacySettings.current(), None

    for key, value in cleaned.items():
        setattr(settings, key, value)

    # We store the full current state, not just the difference: this way a later
    # change of a default cannot silently override the user's decision.
    current = PrivacySettings.current()
    await db.db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (META_KEY, json.dumps(current.to_dict(), ensure_ascii=False)))
    await db.commit()

    log.info("Privacy setting changed: %s", cleaned)

    # Under a Supervisor we also update the add-on options, so the Configuration
    # tab does not keep a stale value. A failure here does not affect the save.
    if services is not None:
        await services.update_addon_options(current.to_dict())

    return current, None
