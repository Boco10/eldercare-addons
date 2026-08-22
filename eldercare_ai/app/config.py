"""Configuration — everything comes from environment variables.

Production: config.yaml options -> bashio -> env (rootfs/etc/services.d/eldercare/run).
Development: straight from the environment (docs/13-DEV-SETUP.md §3).

No threshold is hardcoded in the code. A new option has to be added in four
places: config.yaml (options+schema), the run script, this file, and a
test/scenarios fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- logging and storage ---
    # bashio reserves the name `LOG_LEVEL`, so `ELDERCARE_LOG_LEVEL` is the
    # primary one; plain `LOG_LEVEL` stays for backwards compatibility.
    log_level: str = Field(default="info", validation_alias=AliasChoices(
        "ELDERCARE_LOG_LEVEL", "log_level"))
    data_dir: Path = Path("/data")

    # --- cloud ---
    cloud_api_url: str = "http://localhost:8080"

    installation_id: str = "inst_dev_local"
    """Anonymous installation id. Pairing generates and stores it."""

    device_token: str = ""
    """Empty means uploading is paused and the queue grows — pairing starts it."""

    # The learning period, the minimum data quality and the anomaly thresholds
    # have been backend decisions since the A14 change (services/baseline.py;
    # what counts as unusual is defined by the caregiver's rulebook). Here they
    # were only ever read, with no effect — which is why they were removed from
    # this file and from the add-on's configuration page.

    # --- privacy (docs/10-SECURITY-PRIVACY.md) ---
    send_raw_events: bool = False
    send_daily_features: bool = True
    image_upload_mode: Literal["never", "critical_only", "on_request", "always"] = "critical_only"
    local_raw_retention_days: int = 30

    # --- Home Assistant connection ---
    # supervisor: production, through the Supervisor proxy (docs/02-ADDON.md §3)
    # live:       development, straight to a HA Container with a long-lived token
    # replay:     development, replaying a JSONL fixture without Home Assistant
    ha_mode: Literal["supervisor", "live", "replay"] = "supervisor"
    supervisor_token: str = ""
    ha_url: str = "http://localhost:8123"
    ha_token: str = ""
    ha_replay_file: Path | None = None
    ha_replay_speed: float = Field(
        default=1.0, description="1.0 = real time, 3600 = 1 hour per second")

    # --- local UI ---
    ingress_port: int = 8099

    # --- daily features ---
    timezone: str = "Europe/Budapest"
    """Overwritten from the Home Assistant config at startup — never guessed."""

    day_start_hour: int = 4
    """The observation day does NOT roll over at midnight: night wakings and the
    morning wake-up belong to one logical day. The rollover is before dawn."""

    # --- developer mode (docs/13-DEV-SETUP.md §6) ---
    # Only relaxes the Ingress IP filter and allows replay acceleration.
    # NOT set in the production image; CI checks that.
    eldercare_dev_mode: bool = False

    # Auto-confirming semantic suggestions — a SEPARATE switch.
    # Deliberately not part of dev_mode: the confirmation flow has to be
    # testable even where the IP filter is relaxed.
    # NEVER enable it in production: a wrong meaning causes a wrong alert.
    eldercare_auto_confirm_mappings: bool = False

    @property
    def ws_url(self) -> str:
        if self.ha_mode == "supervisor":
            return "ws://supervisor/core/websocket"
        base = self.ha_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/api/websocket"

    @property
    def rest_url(self) -> str:
        """Home Assistant REST API — for service calls (notify, snapshot)."""
        if self.ha_mode == "supervisor":
            return "http://supervisor/core/api"
        return f"{self.ha_url.rstrip('/')}/api"

    ha_token_file: str = ""
    """File to read the Home Assistant token from.

    In a development environment a setup script produces the token, and it may
    run AFTER the add-on starts. That is why the token is read fresh on every
    call: the container start order does not matter, and the WebSocket client
    picks up a token that appeared in the meantime when it reconnects.
    """

    @property
    def ha_access_token(self) -> str:
        if self.ha_mode == "supervisor":
            return self.supervisor_token
        if self.ha_token_file:
            path = Path(self.ha_token_file)
            if path.exists():
                token = path.read_text(encoding="utf-8").strip()
                if token:
                    return token
        return self.ha_token


settings = Settings()
