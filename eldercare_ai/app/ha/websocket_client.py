"""Home Assistant Core WebSocket client.

Two connection modes (docs/02-ADDON.md §3):
  supervisor -> ws://supervisor/core/websocket, SUPERVISOR_TOKEN
  live       -> ws://<ha>/api/websocket, long-lived access token

The protocol is the same; only the URL and the token source differ.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Callable
from datetime import datetime

import websockets

from app.events.models import RawEvent

log = logging.getLogger(__name__)

INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0
# No message for this long forces a reconnect.
WATCHDOG_TIMEOUT = 120.0


class HomeAssistantWebSocket:
    def __init__(self, url: str, token: str | Callable[[], str]) -> None:
        self.url = url
        # May also be a callable: every reconnect then reads a fresh token, and a
        # token that appeared meanwhile takes effect (see config.ha_token_file).
        self._token = token
        self._ws: websockets.ClientConnection | None = None
        self._msg_id = 0
        self._closing = False

    async def connect(self) -> None:
        self._closing = False

    async def close(self) -> None:
        self._closing = True
        if self._ws:
            await self._ws.close()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def stream(self) -> AsyncIterator[RawEvent]:
        """Event stream with automatic reconnect."""
        backoff = INITIAL_BACKOFF

        while not self._closing:
            try:
                async with websockets.connect(self.url, max_size=8 * 1024 * 1024) as ws:
                    self._ws = ws
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    log.info("WebSocket connected: %s", self.url)
                    backoff = INITIAL_BACKOFF  # connected -> reset the backoff

                    # TODO: full get_states resync after a reconnect, then dedup
                    # on the normalizer's dedup_key.

                    while not self._closing:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WATCHDOG_TIMEOUT)
                        message = json.loads(raw)
                        if message.get("type") != "event":
                            continue
                        event = self._parse(message.get("event", {}))
                        if event:
                            yield event

            except TimeoutError:
                log.warning("Watchdog: no message for %.0f s, reconnecting.", WATCHDOG_TIMEOUT)
            except (websockets.ConnectionClosed, OSError) as exc:
                log.warning("WebSocket connection lost: %s", exc)
            except AuthenticationError:
                log.error("Home Assistant authentication failed — the token is invalid or expired.")
                raise
            finally:
                self._ws = None

            if self._closing:
                break

            # Exponential backoff with jitter, so replicas do not synchronise.
            sleep_for = min(backoff, MAX_BACKOFF) * (0.5 + random.random())  # noqa: S311
            log.info("Reconnecting in %.1f s…", sleep_for)
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _authenticate(self, ws) -> None:
        greeting = json.loads(await ws.recv())
        if greeting.get("type") != "auth_required":
            raise AuthenticationError(f"Unexpected greeting message: {greeting.get('type')}")

        token = self._token() if callable(self._token) else self._token
        if not token:
            raise AuthenticationError("No Home Assistant token.")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        result = json.loads(await ws.recv())
        if result.get("type") != "auth_ok":
            raise AuthenticationError(result.get("message", "auth_invalid"))
        log.debug("Home Assistant authentication OK (version: %s)", result.get("ha_version"))

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({
            "id": self._next_id(),
            "type": "subscribe_events",
            "event_type": "state_changed",
        }))
        result = json.loads(await ws.recv())
        if not result.get("success"):
            raise RuntimeError(f"Subscription failed: {result}")

    @staticmethod
    def _parse(event: dict) -> RawEvent | None:
        data = event.get("data", {})
        new_state = data.get("new_state")
        if not new_state or not data.get("entity_id"):
            return None
        old_state = data.get("old_state") or {}
        return RawEvent(
            entity_id=data["entity_id"],
            state=new_state.get("state", "unknown"),
            previous_state=old_state.get("state"),
            timestamp=datetime.fromisoformat(event["time_fired"]),
            attributes=new_state.get("attributes", {}),
        )


class AuthenticationError(Exception):
    """The Home Assistant token is invalid — retrying does not help."""
