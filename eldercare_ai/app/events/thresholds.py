"""Event-building thresholds.

Not values fixed in code: they can be overridden from the
`/v1/installations/configuration` response (docs/05-API-CONTRACT.md §4), so they
can be tuned per installation to the resident's habits. These are the defaults.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass(slots=True)
class Thresholds:
    # --- appliance use ---
    appliance_on_watts: float = 15.0
    """Above this we consider the power-metering plug switched on."""
    appliance_min_duration_s: float = 30.0
    """A shorter switch-on is noise (a standby spike), not use."""

    # --- room stay ---
    room_transition_window_s: float = 120.0
    """A room change within this counts as a transition, not two separate stays."""
    extended_stay_s: dict[str, float] | None = None
    """Per-room threshold for an extended stay. None -> DEFAULT_EXTENDED_STAY."""

    # --- inactivity ---
    inactivity_threshold_s: float = 4 * 3600
    """This much movement-free time produces an inactivity_period event."""
    inactivity_night_threshold_s: float = 10 * 3600
    """Longer inactivity is normal at night — a separate threshold."""

    # --- night ---
    night_start_hour: int = 22
    night_end_hour: int = 6

    # --- redundant sensors ---
    semantic_dedup_window_s: float = 5.0
    """An event of the same type and room is produced once within this window.
    Two sensors reporting one phenomenon (mmWave + PIR in the same room) would
    otherwise double-count in the daily features and alert twice."""

    # --- leaving home ---
    exit_confirm_window_s: float = 600.0
    """No presence for this long after a door opening -> home_exit."""

    # --- fall validation (docs/09-ALERTS.md §4) ---
    fall_stillness_s: float = 60.0
    """This much stillness is required after a fall candidate to confirm it."""
    fall_min_confidence: float = 0.70
    """Vision confidence below this is ignored."""
    fall_immediate_confidence: float = 0.95
    """At this confidence we confirm at once, without waiting."""

    def __post_init__(self) -> None:
        if self.extended_stay_s is None:
            self.extended_stay_s = dict(DEFAULT_EXTENDED_STAY)

    def extended_stay_for(self, room: str | None) -> float:
        table = self.extended_stay_s or DEFAULT_EXTENDED_STAY
        return table.get(room or "", table.get("_default", 2700.0))

    def is_night(self, hour: int) -> bool:
        if self.night_start_hour > self.night_end_hour:  # wraps past midnight
            return hour >= self.night_start_hour or hour < self.night_end_hour
        return self.night_start_hour <= hour < self.night_end_hour

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Thresholds:
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_EXTENDED_STAY: dict[str, float] = {
    "bathroom": 1800.0,   # 30 perc
    "toilet": 1200.0,     # 20 perc
    "bedroom": 5400.0,    # 90 minutes (a daytime nap is still normal)
    "_default": 2700.0,   # 45 perc
}
