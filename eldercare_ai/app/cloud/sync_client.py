"""Cloud sync client — offline queue, idempotency, contract-conform error handling.

What the error codes mean and what to do: docs/05-API-CONTRACT.md §7.
The client NEVER blocks the critical alerting path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncResult:
    ok: bool
    status: int | None = None
    error_code: str | None = None
    retry_after: float | None = None
    permanent: bool = False
    body: dict | None = None
    """The response body. The upload queue does not need it; the analysis does."""


class CloudSyncClient:
    """Idempotent upload. On failure the caller keeps the payload in the queue."""

    def __init__(self, base_url: str, app_version: str,
                 installation_id: str, device_token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.app_version = app_version
        self.installation_id = installation_id
        self.device_token = device_token
        self._client: httpx.AsyncClient | None = None
        self.offline = False

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self, idempotency_key: str | None) -> dict[str, str]:
        headers = {
            "X-Installation-Id": self.installation_id,
            "X-App-Version": self.app_version,
            "Content-Type": "application/json",
        }
        if self.device_token:
            headers["Authorization"] = f"Bearer {self.device_token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def post(self, path: str, payload: dict,
                   idempotency_key: str | None = None) -> SyncResult:
        if self._client is None:
            raise RuntimeError("The client is not connected.")

        try:
            response = await self._client.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=self._headers(idempotency_key),
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            # No internet or the cloud is unreachable -> offline mode.
            # Local operation does NOT stop (docs/02-ADDON.md §6).
            self.offline = True
            log.warning("Cloud unreachable: %s: %s (%s) — continuing offline.",
                        type(exc).__name__, exc, f"{self.base_url}{path}")
            return SyncResult(ok=False, error_code="unreachable")

        self.offline = False
        return self._interpret(response)

    @staticmethod
    def _interpret(response: httpx.Response) -> SyncResult:
        status = response.status_code

        if 200 <= status < 300:
            body = None
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                parsed = response.json()
                body = parsed if isinstance(parsed, dict) else None
            return SyncResult(ok=True, status=status, body=body)

        error_code = None
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            error_code = response.json().get("error", {}).get("code")

        # 401: token revoked -> offline mode, re-pair in the UI. No retry.
        # 402: out of credit -> the AI is skipped, local operation continues. No retry.
        # 409: idempotency collision -> log and drop, do NOT retry.
        # 410: unsupported version -> only the heartbeat remains.
        if status in (401, 402, 409, 410):
            return SyncResult(ok=False, status=status, error_code=error_code, permanent=True)

        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "30"))
            return SyncResult(ok=False, status=status,
                              error_code=error_code, retry_after=retry_after)

        # 5xx and everything else: stays in the queue, exponential backoff.
        return SyncResult(ok=False, status=status, error_code=error_code)

    # --- convenience methods for the contract endpoints ---

    async def upload_events(self, events: list[dict], home_id: str, batch_key: str) -> SyncResult:
        return await self.post(
            "/v1/events/batch",
            {"home_id": home_id, "events": events},
            idempotency_key=f"{home_id}_events_{batch_key}",
        )

    async def upload_daily_features(self, home_id: str, payload: dict) -> SyncResult:
        return await self.post(
            "/v1/daily-features",
            payload,
            idempotency_key=f"{home_id}_daily-features_{payload['date']}",
        )

    async def heartbeat(self, status: dict) -> SyncResult:
        return await self.post("/v1/installations/heartbeat", status)


async def backoff_sleep(attempt: int, base: float = 2.0, cap: float = 300.0) -> None:
    await asyncio.sleep(min(base ** attempt, cap))


def idempotency_key(home_id: str, resource: str, natural_key: str | datetime) -> str:
    """Deterministic key — the same after a restart (docs/05-API-CONTRACT.md §5)."""
    if isinstance(natural_key, datetime):
        natural_key = natural_key.date().isoformat()
    return f"{home_id}_{resource}_{natural_key}"
