"""Camera snapshots from Home Assistant.

Home Assistant's `camera_proxy` endpoint returns the camera's current image as
JPEG. That happens locally, inside the home — the image only leaves it when
`image_upload_mode` explicitly allows it (docs/10-SECURITY-PRIVACY.md §1).

Privacy filtering happens here, at the source: in `never` mode we do not even fetch it.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.config import settings

log = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT = 15.0
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


@dataclass(slots=True)
class Snapshot:
    camera: str
    captured_at: datetime
    data: bytes
    content_type: str = "image/jpeg"

    @property
    def size_kb(self) -> float:
        return len(self.data) / 1024

    def to_payload(self) -> dict:
        return {
            "camera": self.camera,
            "captured_at": self.captured_at.isoformat(),
            "format": "jpeg",
            "data_base64": base64.b64encode(self.data).decode("ascii"),
        }


class CameraClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.rest_url).rstrip("/")
        self.token = token if token is not None else settings.ha_access_token
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(SNAPSHOT_TIMEOUT, connect=5.0),
            headers={"Authorization": f"Bearer {self.token}"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def list_cameras(self) -> list[dict]:
        """The cameras available in Home Assistant."""
        if self._client is None:
            return []
        try:
            response = await self._client.get(f"{self.base_url}/states")
            response.raise_for_status()
            states = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.warning("Listing cameras failed: %s", exc)
            return []

        return [
            {
                "entity_id": entity["entity_id"],
                "name": entity.get("attributes", {}).get("friendly_name", entity["entity_id"]),
                "state": entity.get("state"),
                "available": entity.get("state") not in ("unavailable", "unknown"),
            }
            for entity in states
            if entity.get("entity_id", "").startswith("camera.")
        ]

    async def snapshot(self, entity_id: str) -> Snapshot | None:
        """A snapshot from one camera.

        With `image_upload_mode: never` the image is **not even fetched** — so a
        later coding mistake cannot accidentally forward it.
        """
        if settings.image_upload_mode == "never":
            log.info("Image upload disabled (image_upload_mode=never) — no snapshot taken.")
            return None
        if self._client is None:
            log.error("The camera client is not connected.")
            return None
        if not entity_id.startswith("camera."):
            log.warning("Not a camera entity: %s", entity_id)
            return None

        try:
            response = await self._client.get(f"{self.base_url}/camera_proxy/{entity_id}")
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            log.warning("Snapshot failed (%s): %s", entity_id, exc)
            return None

        data = response.content
        if not data:
            log.warning("Empty snapshot: %s", entity_id)
            return None
        if len(data) > MAX_SNAPSHOT_BYTES:
            log.warning("Snapshot too large (%.1f MB), dropped: %s",
                        len(data) / 1024 / 1024, entity_id)
            return None

        snapshot = Snapshot(camera=entity_id, captured_at=datetime.now(UTC), data=data)
        log.info("Snapshot ready: %s (%.0f KB)", entity_id, snapshot.size_kb)
        return snapshot


def may_upload_image(trigger: str) -> bool:
    """May the image be uploaded for this trigger (docs/09-ALERTS.md §5)."""
    mode = settings.image_upload_mode
    if mode == "never":
        return False
    if mode == "always":
        return True
    if mode == "on_request":
        # Only for a request the user started.
        return trigger == "manual"
    # critical_only (the default)
    return trigger in ("critical", "anomaly_candidate")
