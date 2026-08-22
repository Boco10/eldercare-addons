"""Requesting a routine analysis from the backend (docs/05-API-CONTRACT.md §9).

Per decision A14 the behavioural evaluation runs on the backend. The add-on's job:

  1. collect the sensor states and the recent semantic events,
  2. attach a camera snapshot if the settings allow it,
  3. upload, and show the answer.

What this module does NOT do: it does not evaluate, score or decide. If the
backend is unreachable the caller is told — and the critical alerting path lives on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.cloud.sync_client import CloudSyncClient
from app.config import settings
from app.ha.camera_client import CameraClient, may_upload_image
from app.ha.entity_discovery import EntityMapping

log = logging.getLogger(__name__)

RECENT_EVENT_LIMIT = 40


@dataclass(slots=True)
class AnalysisResult:
    ok: bool
    risk_level: str = "unknown"
    risk_score: float | None = None
    reasons: list[str] = field(default_factory=list)
    summary: str = ""
    recommended_action: str | None = None
    urgency: str | None = None
    data_quality: float | None = None
    image_analyzed: bool = False
    uncertainty_note: str | None = None
    credits_used: int | None = None
    error: str | None = None
    is_mock: bool = False

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> AnalysisResult:
        return cls(
            ok=True,
            risk_level=payload.get("risk_level", "unknown"),
            risk_score=payload.get("risk_score"),
            reasons=list(payload.get("reasons", [])),
            summary=payload.get("summary", ""),
            recommended_action=payload.get("recommended_action"),
            urgency=payload.get("urgency"),
            data_quality=payload.get("data_quality"),
            image_analyzed=bool(payload.get("image_analyzed")),
            uncertainty_note=payload.get("uncertainty_note"),
            credits_used=payload.get("credits_used"),
            is_mock=bool(payload.get("_mock")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "risk_level": self.risk_level, "risk_score": self.risk_score,
            "reasons": self.reasons, "summary": self.summary,
            "recommended_action": self.recommended_action, "urgency": self.urgency,
            "data_quality": self.data_quality, "image_analyzed": self.image_analyzed,
            "uncertainty_note": self.uncertainty_note, "credits_used": self.credits_used,
            "error": self.error, "is_mock": self.is_mock,
        }


class RoutineAnalysisClient:
    def __init__(self, cloud: CloudSyncClient, camera: CameraClient | None = None) -> None:
        self.cloud = cloud
        self.camera = camera
        self.last_result: AnalysisResult | None = None

    async def collect_sensors(self, states: list[dict],
                              mappings: dict[str, EntityMapping]) -> list[dict]:
        """The current state of the confirmed entities.

        Only confirmed mappings go up: a raw, meaningless entity list is neither
        useful to the backend nor defensible on privacy grounds.
        """
        collected = []
        for entity in states:
            entity_id = entity.get("entity_id", "")
            mapping = mappings.get(entity_id)
            if mapping is None or not mapping.active:
                continue
            collected.append({
                "entity_id": entity_id,
                "role": mapping.role.value,
                "room": mapping.room,
                "state": entity.get("state"),
                "last_changed": entity.get("last_changed"),
            })
        return collected

    async def analyze(self, home_id: str, sensors: list[dict],
                      recent_events: list[dict] | None = None,
                      camera_entity: str | None = None,
                      trigger: str = "manual") -> AnalysisResult:
        """Request a routine analysis. The image only goes if the settings allow."""
        payload: dict[str, Any] = {
            "home_id": home_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "trigger": trigger,
            "sensors": sensors,
            "recent_events": (recent_events or [])[-RECENT_EVENT_LIMIT:],
        }

        # --- privacy gate: the image is filtered WHILE BUILDING THE REQUEST ---
        if camera_entity and self.camera is not None:
            if not may_upload_image(trigger):
                log.info("Image not attached (image_upload_mode=%s, trigger=%s).",
                         settings.image_upload_mode, trigger)
            else:
                snapshot = await self.camera.snapshot(camera_entity)
                if snapshot is not None:
                    payload["image"] = snapshot.to_payload()
                    log.info("Snapshot attached: %s (%.0f KB)",
                             snapshot.camera, snapshot.size_kb)

        idempotency = f"{home_id}_routine_{payload['timestamp']}"
        outcome = await self.cloud.post("/v1/analysis/routine", payload,
                                        idempotency_key=idempotency)

        if not outcome.ok:
            result = AnalysisResult(
                ok=False,
                error=outcome.error_code or f"http_{outcome.status}",
                summary=_error_message(outcome.status, outcome.error_code),
            )
            log.warning("Routine analysis failed: %s", result.error)
            self.last_result = result
            return result

        result = AnalysisResult.from_response(outcome.body or {})
        self.last_result = result
        log.info("Routine analysis done: %s (%s), reasons: %s",
                 result.risk_level, result.summary[:60], result.reasons)
        return result


def _error_message(status: int | None, code: str | None) -> str:
    """A readable message for the local UI — a sentence instead of a code."""
    if code == "unreachable" or status is None:
        return ("The backend is currently unreachable. Critical alerts keep "
                "working regardless.")
    if status == 401:
        return "This installation is not paired, or its access has been revoked."
    if status == 402:
        return "Not enough credit for a routine analysis."
    if status == 429:
        return "Too many requests — try again in a few minutes."
    return f"The backend returned an error ({status})."
