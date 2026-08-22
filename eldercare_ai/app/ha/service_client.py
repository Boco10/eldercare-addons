"""Home Assistant service calls (notify, snapshot, switches).

This module is part of the CRITICAL ALERTING PATH: the notification for an SOS,
smoke, CO or a confirmed fall goes out through it. Therefore:

  - It calls no cloud and no LLM.
  - It uses short timeouts: one slow channel must not block the rest.
  - It tries every channel; a broken one does not stop the others.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# The outcome of a notification must not hang on one slow integration.
SERVICE_TIMEOUT = 8.0
DISCOVERY_TIMEOUT = 10.0

# These `notify.*` services do NOT take the classic title/message shape:
#   send_message            -> entity based, `entity_id` is required (notify entity)
#   persistent_notification -> handled separately, always called
# Putting them in the broadcast call would return 400 on every single alert.
# TODO(phase 1): discover notify entities and call them with send_message.
INCOMPATIBLE_NOTIFY_SERVICES = frozenset({"send_message", "persistent_notification"})


class HomeAssistantServiceClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.rest_url).rstrip("/")
        self.token = token if token is not None else settings.ha_access_token
        self._client: httpx.AsyncClient | None = None
        self._notify_services: list[str] = []

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(SERVICE_TIMEOUT, connect=3.0),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def call_service(self, domain: str, service: str,
                           data: dict[str, Any] | None = None) -> bool:
        """True when the call succeeded. Raises NOTHING — the caller tries more channels."""
        if self._client is None:
            log.error("The service client is not connected.")
            return False
        try:
            response = await self._client.post(
                f"{self.base_url}/services/{domain}/{service}", json=data or {},
            )
            if response.status_code >= 400:
                log.warning("Home Assistant service error %s.%s -> %s: %s",
                            domain, service, response.status_code, response.text[:200])
                return False
            return True
        except (httpx.HTTPError, OSError) as exc:
            log.warning("Home Assistant service unreachable %s.%s: %s", domain, service, exc)
            return False

    async def discover_notify_services(self) -> list[str]:
        """Discover the available `notify.*` services.

        The mobile app notification appears as `notify.mobile_app_<device>`, which
        differs per installation — so it has to be discovered, not hardcoded.
        """
        if self._client is None:
            return []
        try:
            response = await self._client.get(f"{self.base_url}/services",
                                              timeout=DISCOVERY_TIMEOUT)
            response.raise_for_status()
            domains = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.warning("Service discovery failed: %s", exc)
            return []

        found: list[str] = []
        for domain in domains:
            if domain.get("domain") != "notify":
                continue
            for name in domain.get("services", {}):
                if name in INCOMPATIBLE_NOTIFY_SERVICES:
                    continue
                found.append(name)

        self._notify_services = found
        log.info("Discovered notification channels: %s", found or "none")
        return found

    @property
    def notify_services(self) -> list[str]:
        return self._notify_services

    async def notify_all(self, title: str, message: str,
                         data: dict | None = None) -> dict[str, bool]:
        """Notify EVERY available channel, in parallel.

        It uses Home Assistant's own notification system, which runs locally — so
        it works without an internet connection (push delivery, of course, does not).
        """
        payload: dict[str, Any] = {"title": title, "message": message}
        if data:
            payload["data"] = data

        # persistent_notification is always available and shows up in the Home
        # Assistant interface right away.
        targets: list[tuple[str, str, dict]] = [
            ("persistent_notification", "create",
             {"title": title, "message": message, "notification_id": (data or {}).get("id")}),
        ]
        targets += [("notify", service, payload) for service in self._notify_services]

        results = await asyncio.gather(
            *(self.call_service(domain, service, body) for domain, service, body in targets),
            return_exceptions=True,
        )

        outcome: dict[str, bool] = {}
        for (domain, service, _), result in zip(targets, results, strict=True):
            outcome[f"{domain}.{service}"] = result is True
        return outcome

    async def get_config(self) -> dict[str, Any]:
        """The Home Assistant base configuration — timezone, country, version.

        The timezone is never guessed: the day boundaries of the daily features
        depend on it.
        """
        if self._client is None:
            return {}
        try:
            response = await self._client.get(f"{self.base_url}/config",
                                              timeout=DISCOVERY_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.warning("Fetching the Home Assistant configuration failed: %s", exc)
            return {}

    async def get_states(self) -> list[dict[str, Any]]:
        """The current state of every Home Assistant entity — for entity discovery."""
        if self._client is None:
            return []
        try:
            response = await self._client.get(f"{self.base_url}/states",
                                              timeout=DISCOVERY_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.warning("Fetching entities failed: %s", exc)
            return []

    async def update_addon_options(self, options: dict[str, Any]) -> bool:
        """Update our own add-on options through the Supervisor API.

        This way the Home Assistant Configuration tab shows the same thing the
        user set in the local UI. Without a Supervisor (development, compose) it
        is unavailable — then only the local override remains, which is fine.
        """
        if settings.ha_mode != "supervisor":
            log.debug("No Supervisor — add-on options will not be updated.")
            return False
        if self._client is None:
            return False

        try:
            response = await self._client.post(
                "http://supervisor/addons/self/options",
                json={"options": options},
                headers={"Authorization": f"Bearer {settings.supervisor_token}"})
            if response.status_code >= 400:
                log.warning("Updating add-on options failed (%s): %s",
                            response.status_code, response.text[:200])
                return False
        except (httpx.HTTPError, OSError) as exc:
            log.warning("The Supervisor is unreachable for the options update: %s", exc)
            return False

        log.info("Add-on options updated in the Supervisor.")
        return True

    async def turn_on(self, entity_id: str) -> bool:
        """Local sound or light signal (siren, lamp)."""
        domain = entity_id.split(".", 1)[0]
        return await self.call_service(domain, "turn_on", {"entity_id": entity_id})
