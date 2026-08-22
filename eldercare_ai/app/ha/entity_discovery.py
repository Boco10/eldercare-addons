"""Entity discovery and semantic type suggestion.

The biggest onboarding friction is mapping 10+ entities by hand
(docs/11-ROADMAP.md). This module suggests a meaning from the `device_class` and
the entity name — the user confirms or overrides it in the local UI. A
suggestion NEVER takes effect unconfirmed: a wrong meaning causes a wrong alert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class EntityRole(StrEnum):
    """What an entity means physically. Not the same as the event type."""

    PRESENCE = "presence"
    BED_OCCUPANCY = "bed_occupancy"
    DOOR_ENTRY = "door_entry"
    APPLIANCE_POWER = "appliance_power"
    SMOKE = "smoke"
    CO = "co"
    SOS = "sos"
    VISION_PERSON = "vision_person"
    UNKNOWN = "unknown"


KNOWN_ROOMS = (
    "bedroom", "bathroom", "kitchen", "livingroom", "living_room", "living",
    "hallway", "hall", "corridor", "toilet", "wc", "diningroom", "dining_room",
    "office", "garage", "balcony", "entrance",
)

ROOM_ALIASES = {"living": "livingroom", "living_room": "livingroom", "hall": "hallway",
                "wc": "toilet", "dining_room": "diningroom"}

APPLIANCE_WORDS = ("coffee", "kettle", "microwave", "toaster", "oven", "washing",
                   "tv", "television", "fridge", "stove", "cooker")

# We only make automatic suggestions for these domains.
AUTO_SUGGEST_DOMAINS = frozenset({"binary_sensor", "sensor", "device_tracker", "person"})


@dataclass(slots=True)
class EntityMapping:
    """The semantic meaning of a Home Assistant entity (docs/07-ML-BEHAVIOR.md §1)."""

    entity_id: str
    role: EntityRole
    room: str | None = None
    appliance: str | None = None
    enabled: bool = True
    confirmed: bool = False
    """False = only a suggestion. The pipeline uses confirmed mappings only."""
    note: str | None = None
    """The installer's note. Stored locally only — it never goes to the cloud."""
    ignored: bool = False
    """"Not needed": the user set it aside. It moves to its own tab, and the
    pipeline never processes it — not even if someone later confirms it."""

    @property
    def active(self) -> bool:
        """The whole pipeline uses this one gate.

        Three conditions — confirmed, enabled, not set aside — kept in one place:
        scattered around, a new filter would easily be missed in one branch, and a
        set-aside sensor would still produce an alert.
        """
        return self.confirmed and self.enabled and not self.ignored


def suggest(entity_id: str, attributes: dict | None = None) -> EntityMapping:
    """A heuristic suggestion. The user has to confirm it."""
    attributes = attributes or {}
    device_class = str(attributes.get("device_class", "")).lower()
    name = entity_id.split(".", 1)[-1].lower()
    domain = entity_id.split(".", 1)[0]

    room = _extract_room(name)

    # We do NOT suggest for helper domains (input_boolean, input_number,
    # automation…). Those are typically controls or mirrors of a real sensor — if
    # both were processed, every physical event would be produced twice and the
    # daily features would double-count. Manual mapping is still possible.
    if domain not in AUTO_SUGGEST_DOMAINS:
        return EntityMapping(entity_id, EntityRole.UNKNOWN, room)

    # device_class is the more reliable signal — when present, it decides.
    if device_class in ("smoke",):
        return EntityMapping(entity_id, EntityRole.SMOKE, room)
    if device_class in ("gas", "carbon_monoxide"):
        return EntityMapping(entity_id, EntityRole.CO, room)
    if device_class in ("occupancy", "motion", "presence"):
        role = EntityRole.BED_OCCUPANCY if _is_bed(name) else EntityRole.PRESENCE
        return EntityMapping(entity_id, role, room)
    if device_class == "door":
        is_entry = any(w in name for w in ("front", "entrance", "main", "bejarat"))
        role = EntityRole.DOOR_ENTRY if is_entry else EntityRole.UNKNOWN
        return EntityMapping(entity_id, role, room)
    if device_class == "power" or (domain == "sensor" and "power" in name):
        return EntityMapping(entity_id, EntityRole.APPLIANCE_POWER, room,
                             appliance=_extract_appliance(name))

    # Without a device_class we fall back to the name — a weaker signal, but
    # better than nothing.
    if _is_bed(name):
        return EntityMapping(entity_id, EntityRole.BED_OCCUPANCY, room)
    if any(w in name for w in ("sos", "panic", "emergency", "help")):
        return EntityMapping(entity_id, EntityRole.SOS, room)
    if "smoke" in name:
        return EntityMapping(entity_id, EntityRole.SMOKE, room)
    if any(w in name for w in ("carbon_monoxide", "_co_", "co_detector")):
        return EntityMapping(entity_id, EntityRole.CO, room)
    if "person" in name and any(w in name for w in ("frigate", "camera", "vision", "detect")):
        return EntityMapping(entity_id, EntityRole.VISION_PERSON, room)
    if any(w in name for w in ("presence", "motion", "occupancy")):
        return EntityMapping(entity_id, EntityRole.PRESENCE, room)
    if "door" in name:
        is_entry = any(w in name for w in ("front", "entrance", "main"))
        role = EntityRole.DOOR_ENTRY if is_entry else EntityRole.UNKNOWN
        return EntityMapping(entity_id, role, room)

    return EntityMapping(entity_id, EntityRole.UNKNOWN, room)


def _is_bed(name: str) -> bool:
    """Match on word boundaries: "bedroom_presence" is NOT a bed sensor, "bed_occupancy" is."""
    return bool(re.search(r"(^|_)bed(_|$)", name))


def _extract_room(name: str) -> str | None:
    for room in sorted(KNOWN_ROOMS, key=len, reverse=True):
        if re.search(rf"(^|_){room}(_|$)", name):
            return ROOM_ALIASES.get(room, room)
    return None


def _extract_appliance(name: str) -> str | None:
    for word in APPLIANCE_WORDS:
        if word in name:
            return {"television": "tv", "cooker": "stove"}.get(word, word)
    # "sensor.coffee_machine_power" -> "coffee_machine"
    cleaned = re.sub(r"_(power|energy|watt|consumption)$", "", name)
    return cleaned or None
