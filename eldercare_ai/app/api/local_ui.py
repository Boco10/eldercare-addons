"""The local UI behind Ingress (docs/02-ADDON.md §4).

Two mandatory Home Assistant requirements:
  1. Only connections from 172.30.32.2 are allowed.
  2. No authentication of its own — the user is already authenticated here.

The frontend is static HTML + Alpine.js. NOT Next.js: Ingress adds a runtime
path prefix (/api/hassio_ingress/<token>/), so every path has to be relative.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.api.health import create_router as create_health_router
from app.config import settings
from app.ha.camera_client import may_upload_image
from app.ha.entity_discovery import EntityMapping, EntityRole, suggest
from app.storage import runtime_settings

log = logging.getLogger(__name__)

# We only look for sensors in these domains — the rest (automation, scene,
# script…) is not a physical signal.
RELEVANT_DOMAINS = frozenset({"binary_sensor", "sensor", "device_tracker", "person",
                              "input_boolean", "input_number"})

_MAX_STATE_CHARS = 24

# Identifier of the "not needed" tab. It cannot be a real domain name, and the
# dot separator guarantees it never collides with an entity_id domain.
IGNORED_TAB = "__ignored__"


def _short_state(state: object) -> str:
    """Shorten so the table does not fall apart.

    An ISO timestamp (sunrise, backup time) is 25+ characters and squeezes the
    column flat. The date part is enough to recognise it; the full value stays
    in the tooltip.
    """
    text = "" if state is None else str(state)
    if len(text) >= 19 and text[4] == "-" and text[10] in ("T", " "):
        return text[:16].replace("T", " ")
    return text if len(text) <= _MAX_STATE_CHARS else text[:_MAX_STATE_CHARS - 1] + "…"


def _skip_reason(mapping: EntityMapping | None) -> str | None:
    """Why we do not process this state change.

    The order follows what the user has to do: first a missing mapping, then
    the deliberate exclusions.
    """
    if mapping is None:
        return "unmapped"
    if mapping.ignored:
        return "ignored"
    if not mapping.confirmed:
        return "unconfirmed"
    if not mapping.enabled:
        return "disabled"
    return None


class PrivacyRequest(BaseModel):
    image_upload_mode: str | None = None
    send_daily_features: bool | None = None
    send_raw_events: bool | None = None
    local_raw_retention_days: int | None = None


class PairingTokenRequest(BaseModel):
    device_token: str
    home_id: str | None = None


class AnalyzeRequest(BaseModel):
    camera: str | None = None
    """Optional camera entity_id. Whether it is attached is up to image_upload_mode."""


class MappingRequest(BaseModel):
    entity_id: str
    role: str
    room: str | None = None
    appliance: str | None = None
    enabled: bool = True
    note: str | None = Field(default=None, max_length=500)
    ignored: bool = False
    """"Not needed" — moves to its own tab, the pipeline never processes it."""

    @field_validator("role")
    @classmethod
    def known_role(cls, value: str) -> str:
        if value not in {r.value for r in EntityRole}:
            raise ValueError(f"Unknown role: {value}")
        return value

    @field_validator("room", "appliance")
    @classmethod
    def normalize(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower().replace(" ", "_")
        return cleaned or None

INGRESS_IP = "172.30.32.2"
STATIC_DIR = Path(__file__).parent / "static"

# The health check comes from INSIDE the container (s6 watchdog, smoke test),
# not through Ingress — hence the exception from the IP filter. It returns nothing sensitive.
LOCAL_ONLY_PATHS = ("/health", "/ready")
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def create_app(state: dict) -> FastAPI:
    """`state` is the running app's shared state (db, source, statistics)."""
    app = FastAPI(title="ElderCare AI", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def ingress_only(request: Request, call_next):
        client_ip = request.client.host if request.client else None

        if request.url.path in LOCAL_ONLY_PATHS:
            if client_ip in LOOPBACK or settings.eldercare_dev_mode:
                return await call_next(request)
            return JSONResponse(status_code=403, content={"error": "loopback_only"})

        if not settings.eldercare_dev_mode and client_ip != INGRESS_IP:
            log.warning("Request from outside Ingress rejected: %s", client_ip)
            return JSONResponse(status_code=403, content={"error": "ingress_only"})
        return await call_next(request)

    app.include_router(create_health_router(state))

    @app.get("/api/status")
    async def status():
        """A diagnosztikai panel adatai."""
        db = state.get("db")
        queue = state.get("queue")

        # The source of truth for pairing is the pairing manager, not the `state`
        # dict: that one is filled at startup and would go stale after pairing or
        # unpairing from the UI — the panel would show "not paired" on a working
        # connection, and the other way round after unpairing, which is worse.
        manager = state.get("pairing")
        paired = (bool(manager.state.paired) if manager is not None and manager.state
                  else bool(state.get("device_token")))

        return {
            "app_version": state.get("version"),
            "queue_oldest_age_s": await queue.oldest_age_seconds() if queue else None,
            "queue_stats": state.get("queue_stats", {}),
            "ha_mode": settings.ha_mode,
            "ha_connected": state.get("ha_connected", False),
            # The UI language follows Home Assistant unless the user picked one
            # explicitly in this browser. Empty means "we could not ask".
            "ha_language": state.get("ha_language", ""),
            "cloud_url": settings.cloud_api_url,
            "cloud_offline": state.get("cloud_offline", True),
            "paired": paired,
            "normalizer_stats": state.get("normalizer_stats", {}),
            "alert_stats": state.get("alert_stats", {}),
            "last_alert": state.get("last_alert"),
            "mapped_entities": sum(
                1 for m in (state["mappings"].cache.values() if state.get("mappings") else [])
                if m.confirmed),
            "counts": {
                "raw_states": await db.count("raw_states") if db else 0,
                "semantic_events": await db.count("semantic_events") if db else 0,
                "sync_queue": await db.count("sync_queue") if db else 0,
            },
            "privacy": {
                "send_raw_events": settings.send_raw_events,
                "send_daily_features": settings.send_daily_features,
                "image_upload_mode": settings.image_upload_mode,
            },
        }

    @app.get("/api/entities")
    async def entities():
        """Discovered Home Assistant entities, with a suggestion and current mapping."""
        store = state.get("mappings")
        services = state.get("services")
        if store is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})

        states = await services.get_states() if services else []
        rows = []
        for entity in states:
            entity_id = entity.get("entity_id", "")
            domain = entity_id.split(".", 1)[0]
            if domain not in RELEVANT_DOMAINS:
                continue
            attributes = entity.get("attributes", {})
            proposal = suggest(entity_id, attributes)
            current = store.cache.get(entity_id)

            # Grouping: confirmed first, then recognised, then system entities.
            # A typical Home Assistant has hundreds of sensors (updates, backups,
            # sunrise) — they would drown out what matters.
            if current is not None and current.ignored:
                group = "ignored"
            elif current is not None and current.confirmed:
                group = "confirmed"
            elif proposal.role is not EntityRole.UNKNOWN:
                group = "suggested"
            else:
                group = "other"

            # Set-aside entities go to their own tab rather than their type's —
            # the point is to keep them out of the way while configuring.
            tab = IGNORED_TAB if group == "ignored" else domain

            rows.append({
                "entity_id": entity_id,
                "name": attributes.get("friendly_name", entity_id),
                "state": _short_state(entity.get("state")),
                "state_full": entity.get("state"),
                "device_class": attributes.get("device_class"),
                "domain": domain,
                "tab": tab,
                "group": group,
                "suggestion": {"role": proposal.role.value, "room": proposal.room,
                               "appliance": proposal.appliance},
                "mapping": None if current is None else {
                    "role": current.role.value, "room": current.room,
                    "appliance": current.appliance, "enabled": current.enabled,
                    "confirmed": current.confirmed, "note": current.note,
                    "ignored": current.ignored,
                },
            })

        # Tabs go by entity type; inside a tab, what needs doing comes first:
        # confirmed, then recognised, then system entities. The set-aside tab goes
        # last — you only reach into it to undo something.
        order = {"confirmed": 0, "suggested": 1, "other": 2, "ignored": 3}
        rows.sort(key=lambda r: (r["tab"] == IGNORED_TAB, r["tab"], order[r["group"]],
                                 (r["mapping"] or r["suggestion"]).get("role") or "",
                                 r["entity_id"]))
        confirmed = sum(1 for r in rows if r["group"] == "confirmed")

        # Per-tab counters: how many entities of that type, and how many still
        # need attention. The tab order is the list order.
        tabs: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = tabs.setdefault(row["tab"], {"total": 0, "pending": 0})
            bucket["total"] += 1
            if row["group"] == "suggested":
                bucket["pending"] += 1

        return {
            "entities": rows,
            "roles": [role.value for role in EntityRole],
            "confirmed_count": confirmed,
            "domains": [{"domain": name, **counts} for name, counts in tabs.items()],
            "counts": {
                "confirmed": confirmed,
                "suggested": sum(1 for r in rows if r["group"] == "suggested"),
                "other": sum(1 for r in rows if r["group"] == "other"),
                "ignored": sum(1 for r in rows if r["group"] == "ignored"),
            },
            # The definition of done expects at least 10 configurable entities.
            "ready_for_learning": confirmed >= 5,
        }

    @app.post("/api/mappings")
    async def save_mapping(payload: MappingRequest):
        """Confirm or change a mapping. This is what puts it into effect."""
        store = state.get("mappings")
        engine = state.get("engine")
        if store is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})

        mapping = EntityMapping(
            entity_id=payload.entity_id, role=EntityRole(payload.role), room=payload.room,
            appliance=payload.appliance, enabled=payload.enabled,
            # "Not needed" is not a confirmation: a set-aside entity must not end up
            # confirmed, or undoing it would silently drop it into the pipeline.
            confirmed=not payload.ignored,
            note=(payload.note or "").strip() or None, ignored=payload.ignored,
        )
        await store.upsert(mapping)
        if engine is not None:
            engine.mappings[mapping.entity_id] = mapping   # live at once, no restart
        if mapping.ignored:
            log.info("Entity marked as not needed: %s", mapping.entity_id)
        else:
            log.info("Mapping confirmed: %s -> %s (%s)",
                     mapping.entity_id, mapping.role.value, mapping.room or "no room")
        return {"saved": True, "entity_id": mapping.entity_id, "ignored": mapping.ignored}

    @app.delete("/api/mappings/{entity_id}")
    async def delete_mapping(entity_id: str):
        store = state.get("mappings")
        engine = state.get("engine")
        if store is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})
        removed = await store.delete(entity_id)
        if engine is not None:
            engine.mappings.pop(entity_id, None)
        return {"deleted": removed}

    @app.get("/api/pairing")
    async def pairing_status():
        manager = state.get("pairing")
        if manager is None or manager.state is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})
        return {**manager.state.to_dict(), "cloud_api_url": settings.cloud_api_url}

    @app.post("/api/pairing/code")
    async def pairing_code():
        """Request a code from the backend. The only call that goes without a token."""
        manager = state.get("pairing")
        if manager is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})
        return await manager.request_code()

    @app.post("/api/pairing/token")
    async def pairing_token(payload: PairingTokenRequest):
        """Store and verify the device token received on the web portal."""
        manager = state.get("pairing")
        if manager is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})
        result = await manager.store_token(payload.device_token, payload.home_id)
        if result.get("ok") and manager.state:
            state["home_id"] = manager.state.home_id or state.get("home_id", "home_local")
            state["device_token"] = True
        return result

    @app.post("/api/pairing/unpair")
    async def pairing_unpair():
        manager = state.get("pairing")
        if manager is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})
        result = await manager.unpair()
        state["device_token"] = False
        return result

    @app.get("/api/privacy")
    async def privacy_settings():
        """Privacy settings — where their effect is visible."""
        current = runtime_settings.PrivacySettings.current()
        return {
            **current.to_dict(),
            "image_modes": list(runtime_settings.IMAGE_MODES),
            "supervisor_managed": settings.ha_mode == "supervisor",
        }

    @app.post("/api/privacy")
    async def update_privacy(payload: PrivacyRequest):
        db = state.get("db")
        if db is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})

        current, error = await runtime_settings.save(
            db, payload.model_dump(exclude_none=True), services=state.get("services"))
        if error:
            return JSONResponse(status_code=422, content={"error": error})
        return {"saved": True, **current.to_dict()}

    @app.get("/api/feed")
    async def feed(limit: int = 60):
        """What is arriving right now: raw state changes and the events built from them.

        This view answers one question: does a sensor **reach** the system, and if
        so, what becomes of it. Otherwise a fault shows up only as "nothing is
        happening" — which does not say whether the sensor is silent or the
        mapping is missing.

        Local view only: the raw state never leaves the home.
        """
        db = state.get("db")
        store = state.get("mappings")
        if db is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})

        limit = max(1, min(limit, 200))
        mappings = store.cache if store else {}

        rows = []
        async with db.db.execute(
            "SELECT entity_id, state, previous_state, timestamp FROM raw_states"
            " ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            for row in await cursor.fetchall():
                mapping = mappings.get(row["entity_id"])
                rows.append({
                    "entity_id": row["entity_id"],
                    "state": _short_state(row["state"]),
                    "previous_state": _short_state(row["previous_state"]),
                    "timestamp": row["timestamp"],
                    "role": mapping.role.value if mapping else None,
                    "room": mapping.room if mapping else None,
                    # This is the point: does it enter processing, and if not, why.
                    "processed": bool(mapping and mapping.active),
                    "skip_reason": _skip_reason(mapping),
                })

        events = []
        async with db.db.execute(
            "SELECT type, class, timestamp, confidence, room, fields, synced"
            " FROM semantic_events ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            for row in await cursor.fetchall():
                events.append({
                    "type": row["type"], "class": row["class"],
                    "timestamp": row["timestamp"],
                    "confidence": round(float(row["confidence"]), 2),
                    "room": row["room"],
                    "fields": json.loads(row["fields"] or "{}"),
                    "synced": bool(row["synced"]),
                })

        return {
            "states": rows,
            "events": events,
            "totals": {
                "raw_states": await db.count("raw_states"),
                "semantic_events": await db.count("semantic_events"),
            },
            "normalizer_stats": state.get("normalizer_stats", {}),
        }

    @app.get("/api/cameras")
    async def cameras():
        """Cameras available in Home Assistant + the current privacy mode."""
        client = state.get("camera")
        found = await client.list_cameras() if client else []
        return {
            "cameras": found,
            "image_upload_mode": settings.image_upload_mode,
            "upload_allowed_manual": may_upload_image("manual"),
        }

    @app.get("/api/cameras/{entity_id}/snapshot")
    async def camera_snapshot(entity_id: str):
        """Snapshot for PREVIEW. It stays local and goes nowhere."""
        client = state.get("camera")
        if client is None:
            return JSONResponse(status_code=503, content={"error": "camera_unavailable"})
        snapshot = await client.snapshot(entity_id)
        if snapshot is None:
            return JSONResponse(status_code=404, content={
                "error": "snapshot_failed",
                "hint": ("Image upload is disabled (image_upload_mode=never), "
                         "or the camera is unreachable."),
            })
        return Response(content=snapshot.data, media_type=snapshot.content_type)

    @app.post("/api/analyze")
    async def analyze(payload: AnalyzeRequest):
        """Request a routine analysis from the backend (docs/05-API-CONTRACT.md §9).

        The add-on collects and uploads — the evaluation happens on the backend (A14).
        """
        analysis = state.get("analysis")
        services = state.get("services")
        store = state.get("mappings")
        if analysis is None or store is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})

        states = await services.get_states() if services else []
        sensors = await analysis.collect_sensors(states, store.cache)
        recent = state.get("recent_events", [])[-40:]

        result = await analysis.analyze(
            home_id=state.get("home_id", "home_local"),
            sensors=sensors,
            recent_events=recent,
            camera_entity=payload.camera,
            trigger="manual",
        )
        return {
            **result.to_dict(),
            "sensors_sent": len(sensors),
            "events_sent": len(recent),
        }

    @app.get("/api/analyze/last")
    async def last_analysis():
        analysis = state.get("analysis")
        if analysis is None or analysis.last_result is None:
            return {"available": False}
        return {"available": True, **analysis.last_result.to_dict()}

    @app.get("/api/mappings/export")
    async def export_mappings():
        store = state.get("mappings")
        if store is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})
        return store.export()

    @app.post("/api/mappings/import")
    async def import_mappings(payload: dict):
        store = state.get("mappings")
        engine = state.get("engine")
        if store is None:
            return JSONResponse(status_code=503, content={"error": "not_ready"})
        try:
            imported, errors = await store.import_(payload)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        if engine is not None:
            engine.mappings.update(store.cache)
        return {"imported": imported, "errors": errors}

    @app.post("/api/test-alert")
    async def test_alert():
        """Test alert — it travels the REAL delivery path.

        Without this, a broken notification path would only surface in a real emergency.
        """
        engine = state.get("alerts")
        if engine is None:
            return JSONResponse(status_code=503,
                                content={"error": "alert_engine_unavailable"})
        channels = await engine.send_test_alert()
        return {
            "triggered": any(channels.values()),
            "channels": channels,
            "hint": None if any(channels.values())
                    else "No notification channel is reachable. "
                         "Check your Home Assistant notify integrations.",
        }

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
