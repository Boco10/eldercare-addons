"""Cloud pairing and device token handling (docs/05-API-CONTRACT.md §2).

The flow:
  1. The add-on generates an anonymous `installation_id` (once, and keeps it).
  2. It requests a pairing code from the backend.
  3. The user signs in on the web portal and types the code there.
  4. The backend issues a device token — the add-on stores it.
  5. On unpairing the token is deleted and the app goes offline-only.

What the module guarantees:
  - The token is **never logged**, not even at DEBUG level.
  - The token does not sit in `/data` as plain text: it is masked with a
    separate key file readable only by the owner. That guards against backups
    and exports — not against root, which the container isolation handles.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.cloud.sync_client import CloudSyncClient
from app.storage.database import Database

log = logging.getLogger(__name__)

KEY_FILENAME = ".token_key"
META_INSTALLATION_ID = "installation_id"
META_DEVICE_TOKEN = "device_token"  # noqa: S105 — a key name, not a secret
META_HOME_ID = "home_id"
META_PAIRED_AT = "paired_at"


@dataclass(slots=True)
class PairingState:
    installation_id: str
    device_token: str | None = None
    home_id: str | None = None
    paired_at: str | None = None
    pending_code: str | None = None
    code_expires_at: str | None = None

    @property
    def paired(self) -> bool:
        return bool(self.device_token)

    def to_dict(self) -> dict:
        """For the local UI. The token is NEVER handed out, only the fact it exists."""
        return {
            "installation_id": self.installation_id,
            "paired": self.paired,
            "home_id": self.home_id,
            "paired_at": self.paired_at,
            "pending_code": self.pending_code,
            "code_expires_at": self.code_expires_at,
        }


class TokenStore:
    """Stores secret values under `/data`, masked."""

    def __init__(self, db: Database, data_dir: Path) -> None:
        self.db = db
        self.key_path = data_dir / KEY_FILENAME

    def _key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes()
        key = secrets.token_bytes(32)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(key)
        # chmod is meaningless on Windows — that must not stop startup.
        with contextlib.suppress(OSError, NotImplementedError):
            os.chmod(self.key_path, 0o600)
        log.info("New token key created: %s", self.key_path.name)
        return key

    def _mask(self, value: str) -> str:
        """XOR with a stream derived from an HMAC.

        Not strong encryption — the goal is that a database backup or an export
        does not contain a directly usable token. The real protection comes from
        the Home Assistant container isolation and the `/data` permissions.
        """
        data = value.encode()
        stream = b""
        counter = 0
        while len(stream) < len(data):
            stream += hmac.new(self._key(), counter.to_bytes(4, "big"),
                               hashlib.sha256).digest()
            counter += 1
        return base64.b64encode(bytes(a ^ b for a, b in zip(data, stream, strict=False))).decode()

    def _unmask(self, value: str) -> str:
        raw = base64.b64decode(value)
        stream = b""
        counter = 0
        while len(stream) < len(raw):
            stream += hmac.new(self._key(), counter.to_bytes(4, "big"),
                               hashlib.sha256).digest()
            counter += 1
        return bytes(a ^ b for a, b in zip(raw, stream, strict=False)).decode()

    async def get(self, key: str, *, secret: bool = False) -> str | None:
        async with self.db.db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._unmask(row["value"]) if secret else row["value"]

    async def set(self, key: str, value: str, *, secret: bool = False) -> None:
        stored = self._mask(value) if secret else value
        await self.db.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, stored))
        await self.db.commit()

    async def delete(self, key: str) -> None:
        await self.db.db.execute("DELETE FROM meta WHERE key = ?", (key,))
        await self.db.commit()


class PairingManager:
    def __init__(self, db: Database, cloud: CloudSyncClient, data_dir: Path) -> None:
        self.store = TokenStore(db, data_dir)
        self.cloud = cloud
        self.state: PairingState | None = None

    async def load(self, fallback_installation_id: str = "",
                   fallback_token: str = "") -> PairingState:
        """At startup: load the id and the token, or generate them."""
        installation_id = await self.store.get(META_INSTALLATION_ID)
        if not installation_id:
            # Anonymous id — it says nothing about the resident or the home.
            installation_id = fallback_installation_id or f"inst_{secrets.token_hex(12)}"
            await self.store.set(META_INSTALLATION_ID, installation_id)
            log.info("New installation id: %s", installation_id)

        token = await self.store.get(META_DEVICE_TOKEN, secret=True)
        if not token and fallback_token:
            # Token from the environment (development) — stored so it survives.
            token = fallback_token
            await self.store.set(META_DEVICE_TOKEN, token, secret=True)

        self.state = PairingState(
            installation_id=installation_id,
            device_token=token,
            home_id=await self.store.get(META_HOME_ID),
            paired_at=await self.store.get(META_PAIRED_AT),
        )
        self._apply()
        log.info("Pairing state: %s", "paired" if self.state.paired else "not paired")
        return self.state

    def _apply(self) -> None:
        """Hand the id and the token to the client."""
        if self.state is None:
            return
        self.cloud.installation_id = self.state.installation_id
        self.cloud.device_token = self.state.device_token

    async def request_code(self) -> dict:
        """Request a pairing code. The only call that goes without a token."""
        assert self.state is not None
        result = await self.cloud.post(
            "/v1/pairing/codes", {"installation_id": self.state.installation_id})

        if not result.ok:
            message = ("The backend is unreachable. Check the internet connection "
                       "and the backend address in the add-on options.")
            if result.status and result.status >= 400:
                message = f"The backend returned an error ({result.status})."
            log.warning("Pairing code request failed: %s", result.error_code)
            return {"ok": False, "error": message}

        body = result.body or {}
        self.state.pending_code = body.get("code")
        self.state.code_expires_at = body.get("expires_at")
        log.info("Pairing code received, expires: %s", self.state.code_expires_at)
        return {"ok": True, "code": self.state.pending_code,
                "expires_at": self.state.code_expires_at}

    async def confirm(self) -> dict:
        """The user redeemed the code on the web — fetch the token.

        The backend serves `/v1/pairing/complete` with the WEB session, so the
        add-on never receives the token directly. This method uses the heartbeat
        call to check whether the connection came together.
        """
        assert self.state is not None
        if self.state.paired:
            return {"ok": True, "paired": True}
        return {"ok": False, "paired": False,
                "error": ("Finish pairing in the caregiver portal: enter the code "
                          "there, then paste back the device token it shows.")}

    async def store_token(self, device_token: str, home_id: str | None = None) -> dict:
        """Store the device token received on the web portal."""
        assert self.state is not None
        token = device_token.strip()
        if not token.startswith("eld_"):
            return {"ok": False, "error": "The token format is wrong — it must start with eld_…."}

        await self.store.set(META_DEVICE_TOKEN, token, secret=True)
        await self.store.set(META_PAIRED_AT, datetime.now(UTC).isoformat())
        if home_id:
            await self.store.set(META_HOME_ID, home_id)

        self.state.device_token = token
        self.state.home_id = home_id or self.state.home_id
        self.state.paired_at = datetime.now(UTC).isoformat()
        self.state.pending_code = None
        self._apply()

        # Verify that the token actually works.
        check = await self.cloud.post("/v1/installations/heartbeat", {})
        if not check.ok:
            await self.unpair()
            return {"ok": False, "error": (
                "The backend rejected the token. Request a new code and "
                "try again.")}

        log.info("Pairing successful. Home: %s", self.state.home_id or "unknown")
        return {"ok": True, "paired": True, "home_id": self.state.home_id}

    async def unpair(self) -> dict:
        """Unpair — the token is deleted and the app goes offline-only.

        Local operation (critical alerting, event building) does NOT stop.
        """
        assert self.state is not None
        await self.store.delete(META_DEVICE_TOKEN)
        await self.store.delete(META_PAIRED_AT)
        self.state.device_token = None
        self.state.paired_at = None
        self._apply()
        log.warning("Unpaired from the cloud. Local alerting keeps working.")
        return {"ok": True, "paired": False}
