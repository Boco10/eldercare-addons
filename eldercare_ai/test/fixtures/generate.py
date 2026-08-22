"""Synthetic Home Assistant event stream generator.

The system starts scoring anomalies after 3-4 weeks of observation
(docs/07-ML-BEHAVIOR.md §5). That cannot be waited out during development — this
script produces realistic but synthetic days, replayed at speed by replay mode.

Output: one day = one JSONL file, one Home Assistant `state_changed` per line.
Every anomaly fixture gets an `expected.json` — without it this would be data,

Usage:
    python generate.py                 # generate everything under days/
    python generate.py --scenario late_wakeup
    python generate.py --baseline-days 28 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "days"
TZ_OFFSET = "+02:00"

# --- entities (matching tools/dev-stack/ha-config/configuration.yaml) ---
BEDROOM = "binary_sensor.bedroom_presence"
BATHROOM = "binary_sensor.bathroom_presence"
KITCHEN = "binary_sensor.kitchen_presence"
LIVING = "binary_sensor.livingroom_presence"
BED = "binary_sensor.bed_occupancy"
DOOR = "binary_sensor.front_door"
SMOKE = "binary_sensor.smoke_detector"
COFFEE = "sensor.coffee_machine_power"


@dataclass
class Day:
    """One day's events, in time order."""

    date: datetime
    events: list[dict] = field(default_factory=list)

    def at(self, hour: float, entity: str, state: str, old: str | None = None) -> None:
        ts = self.date + timedelta(hours=hour)
        self.events.append({
            "event_type": "state_changed",
            "time_fired": ts.isoformat() + TZ_OFFSET,
            "data": {
                "entity_id": entity,
                "old_state": {"state": old if old is not None else _flip(state)},
                "new_state": {"state": state, "last_changed": ts.isoformat() + TZ_OFFSET},
            },
        })

    def pulse(self, hour: float, entity: str, duration_min: float) -> None:
        """A short presence: on, then off after `duration` minutes."""
        self.at(hour, entity, "on")
        self.at(hour + duration_min / 60, entity, "off")

    def appliance(self, hour: float, entity: str, watts: int, duration_min: float) -> None:
        self.at(hour, entity, str(watts), old="0")
        self.at(hour + duration_min / 60, entity, "0", old=str(watts))

    def write(self, path: Path) -> None:
        self.events.sort(key=lambda e: e["time_fired"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as f:
            for e in self.events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _flip(state: str) -> str:
    return {"on": "off", "off": "on"}.get(state, "unknown")


def _jitter(rng: random.Random, base: float, spread: float) -> float:
    return base + rng.uniform(-spread, spread)


# --------------------------------------------------------------------- napprofilok

def normal_day(date: datetime, rng: random.Random, *, weekend: bool = False) -> Day:
    """The usual routine: waking, coffee, day, bedtime."""
    d = Day(date)

    # In bed at night, with 0-1 wakings
    # No artificial midnight "in bed" report: a real Home Assistant only sends
    # an event on a state CHANGE, and the resident went to bed the evening before.
    if rng.random() < 0.55:
        wake = _jitter(rng, 2.8, 1.2)
        d.at(wake, BED, "off")
        d.pulse(wake + 0.02, BATHROOM, rng.uniform(3, 7))
        d.at(wake + 0.15, BED, "on")

    # Waking — later at the weekend
    wakeup = _jitter(rng, 7.6 if weekend else 7.0, 0.35)
    d.at(wakeup, BED, "off")
    d.at(wakeup, BEDROOM, "on")
    d.pulse(wakeup + 0.05, BATHROOM, rng.uniform(6, 12))
    d.at(wakeup + 0.3, BEDROOM, "off")

    # Kitchen + coffee
    kitchen = wakeup + _jitter(rng, 0.4, 0.15)
    d.at(kitchen, KITCHEN, "on")
    d.appliance(kitchen + 0.05, COFFEE, rng.randint(800, 1100), rng.uniform(3, 6))
    d.at(kitchen + rng.uniform(0.4, 0.8), KITCHEN, "off")

    # Daytime activity
    for _ in range(rng.randint(6, 10)):
        h = rng.uniform(9, 20)
        d.pulse(h, rng.choice([LIVING, KITCHEN, BATHROOM]), rng.uniform(4, 25))

    # Going out during the day
    if rng.random() < (0.4 if weekend else 0.6):
        out = rng.uniform(10, 15)
        d.pulse(out, DOOR, 0.3)
        d.pulse(out + rng.uniform(1.0, 2.5), DOOR, 0.3)

    # Bedtime
    bedtime = _jitter(rng, 22.3, 0.6)
    d.pulse(bedtime - 0.2, BATHROOM, rng.uniform(5, 10))
    d.at(bedtime, BEDROOM, "on")
    d.at(bedtime + 0.15, BEDROOM, "off")
    d.at(bedtime + 0.15, BED, "on")
    return d


def late_wakeup(date: datetime, rng: random.Random) -> Day:
    """Waking two hours late, no coffee, reduced movement."""
    d = Day(date)
    # No artificial midnight "in bed" report: a real Home Assistant only sends
    # an event on a state CHANGE, and the resident went to bed the evening before.
    for i in range(4):  # many night wakings
        w = 1.5 + i * 1.4
        d.at(w, BED, "off")
        d.pulse(w + 0.02, BATHROOM, rng.uniform(4, 9))
        d.at(w + 0.2, BED, "on")

    wakeup = 9.3
    d.at(wakeup, BED, "off")
    d.at(wakeup, BEDROOM, "on")
    d.at(wakeup + 0.5, BEDROOM, "off")
    # no kitchen, no coffee
    for _ in range(3):  # markedly less movement
        d.pulse(rng.uniform(11, 19), LIVING, rng.uniform(5, 12))
    d.at(21.5, BED, "on")
    return d


def extended_bathroom(date: datetime, rng: random.Random) -> Day:
    d = normal_day(date, rng)
    d.pulse(11.0, BATHROOM, 52)  # 52 minutes — well above the baseline percentile
    return d


def night_wakings(date: datetime, rng: random.Random) -> Day:
    d = normal_day(date, rng)
    for i in range(4):
        w = 1.2 + i * 1.5
        d.at(w, BED, "off")
        d.pulse(w + 0.02, BATHROOM, rng.uniform(4, 8))
        d.at(w + 0.2, BED, "on")
    return d


def low_activity(date: datetime, rng: random.Random) -> Day:
    d = Day(date)
    # No artificial midnight "in bed" report: a real Home Assistant only sends
    # an event on a state CHANGE, and the resident went to bed the evening before.
    d.at(7.2, BED, "off")
    d.pulse(7.3, BATHROOM, 8)
    d.at(7.6, KITCHEN, "on")
    d.at(8.0, KITCHEN, "off")
    for _ in range(2):  # ~60% less movement
        d.pulse(rng.uniform(10, 18), LIVING, rng.uniform(6, 15))
    d.at(22.0, BED, "on")
    return d


def no_activity(date: datetime, rng: random.Random) -> Day:
    """Long inactivity in an unusual place — high level."""
    d = Day(date)
    # No artificial midnight "in bed" report: a real Home Assistant only sends
    # an event on a state CHANGE, and the resident went to bed the evening before.
    d.at(7.0, BED, "off")
    d.pulse(7.1, BATHROOM, 7)
    d.at(7.5, LIVING, "on")
    d.at(7.6, LIVING, "off")
    # Nothing from 7:36 for 9 hours, in the living room
    d.at(16.8, LIVING, "on")
    d.at(17.0, LIVING, "off")
    d.at(22.0, BED, "on")
    return d


def fall_candidate(date: datetime, rng: random.Random) -> Day:
    """Camera fall candidate + SUSTAINED STILLNESS — critical.

    Built by hand on purpose, not from normal_day: the random daytime movement
    would refute the fall candidate, and the fixture would prove the opposite.
    """
    d = Day(date)
    # Megszokott reggel
    # No artificial midnight "in bed" report: a real Home Assistant only sends
    # an event on a state CHANGE, and the resident went to bed the evening before.
    d.at(7.1, BED, "off")
    d.pulse(7.2, BATHROOM, 8)
    d.at(7.6, KITCHEN, "on")
    d.appliance(7.65, COFFEE, 950, 4)
    d.at(8.1, KITCHEN, "off")
    d.pulse(10.0, LIVING, 20)
    d.pulse(12.5, KITCHEN, 25)

    # 14:12 — the vision model sees a person on the floor
    d.events.append({
        "event_type": "state_changed",
        "time_fired": (date + timedelta(hours=14.2)).isoformat() + TZ_OFFSET,
        "data": {
            "entity_id": "sensor.frigate_livingroom_person",
            "old_state": {"state": "standing"},
            "new_state": {
                "state": "on_floor",
                "attributes": {"confidence": 0.91, "camera": "livingroom"},
            },
        },
    })

    # After this, NO movement at all. The next signal arrives much later —
    # the stillness in between is what confirms the candidate.
    d.pulse(15.5, LIVING, 2)
    return d


def home_exit_long(date: datetime, rng: random.Random) -> Day:
    d = normal_day(date, rng)
    d.pulse(10.0, DOOR, 0.3)  # left and did not return until the evening
    d.events = [e for e in d.events if not (10.5 < _hour_of(e, date) < 20.0)]
    d.pulse(20.5, DOOR, 0.3)
    return d


def sensor_failure(date: datetime, rng: random.Random) -> Day:
    """A sensor freezes — SENSOR_FAILURE, NOT a behavioural anomaly."""
    d = normal_day(date, rng)
    d.at(9.0, BATHROOM, "unavailable")  # nothing from this entity afterwards
    d.events = [
        e for e in d.events
        if not (e["data"]["entity_id"] == BATHROOM and _hour_of(e, date) > 9.0
                and e["data"]["new_state"]["state"] != "unavailable")
    ]
    return d


def low_data_quality(date: datetime, rng: random.Random) -> Day:
    """Incomplete data — 'insufficient data', NOT an alert."""
    d = normal_day(date, rng)
    d.events = [e for e in d.events if rng.random() < 0.25]  # 75% data loss
    d.at(6.0, BED, "unavailable")
    d.at(12.0, KITCHEN, "unavailable")
    return d


def _hour_of(event: dict, date: datetime) -> float:
    ts = datetime.fromisoformat(event["time_fired"])
    return (ts.replace(tzinfo=None) - date).total_seconds() / 3600


# ------------------------------------------------------------ expected output

EXPECTED = {
    "normal_day": {
        "expected_alert_level": "info", "expected_reasons": [],
        "min_risk_score": 0.0, "max_risk_score": 0.35, "requires_cloud": False,
    },
    "late_wakeup": {
        "expected_alert_level": "medium",
        "expected_reasons": ["late_wakeup", "no_kitchen_activity", "increased_night_activity"],
        "min_risk_score": 0.65, "max_risk_score": 0.95, "requires_cloud": False,
    },
    "extended_bathroom": {
        "expected_alert_level": "low",
        "expected_reasons": ["extended_bathroom_stay"],
        "min_risk_score": 0.35, "max_risk_score": 0.7, "requires_cloud": False,
    },
    "night_wakings": {
        "expected_alert_level": "low",
        "expected_reasons": ["increased_night_activity"],
        "min_risk_score": 0.3, "max_risk_score": 0.65, "requires_cloud": False,
    },
    "low_activity": {
        "expected_alert_level": "medium",
        "expected_reasons": ["reduced_movement"],
        "min_risk_score": 0.6, "max_risk_score": 0.9, "requires_cloud": False,
    },
    "no_activity": {
        "expected_alert_level": "high",
        "expected_reasons": ["prolonged_inactivity", "inactivity_unusual_location"],
        "min_risk_score": 0.82, "max_risk_score": 1.0, "requires_cloud": False,
    },
    "fall_candidate": {
        "expected_alert_level": "critical",
        "expected_reasons": [],
        "min_risk_score": 0.0, "max_risk_score": 1.0, "requires_cloud": False,
        "note": "Deterministic event — overrides the risk score, alerts without an LLM.",
    },
    "home_exit_long": {
        "expected_alert_level": "low",
        "expected_reasons": ["extended_absence"],
        "min_risk_score": 0.3, "max_risk_score": 0.7, "requires_cloud": False,
    },
    "sensor_failure": {
        "expected_alert_level": "info",
        "expected_reasons": ["sensor_failure"],
        "min_risk_score": 0.0, "max_risk_score": 0.3, "requires_cloud": False,
        "note": "NEGATIVE TEST: a sensor fault cannot be a behavioural anomaly.",
    },
    "low_data_quality": {
        "expected_alert_level": "info",
        "expected_reasons": ["insufficient_data"],
        "min_risk_score": 0.0, "max_risk_score": 0.3, "requires_cloud": False,
        "note": "NEGATIVE TEST: low data quality means 'insufficient data', not a conclusion.",
    },
}

SCENARIOS = {
    "normal_day": normal_day,
    "late_wakeup": late_wakeup,
    "extended_bathroom": extended_bathroom,
    "night_wakings": night_wakings,
    "low_activity": low_activity,
    "no_activity": no_activity,
    "fall_candidate": fall_candidate,
    "home_exit_long": home_exit_long,
    "sensor_failure": sensor_failure,
    "low_data_quality": low_data_quality,
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", help="generate only this one")
    p.add_argument("--baseline-days", type=int, default=28)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--start", default="2026-06-01")
    args = p.parse_args()

    rng = random.Random(args.seed)
    start = datetime.fromisoformat(args.start)

    if not args.scenario:
        # 28 normal days for baseline learning (weekends included)
        base_dir = OUT / "baseline_28d"
        for i in range(args.baseline_days):
            date = start + timedelta(days=i)
            day = normal_day(date, rng, weekend=date.weekday() >= 5)
            day.write(base_dir / f"day_{i + 1:02d}.jsonl")
        print(f"baseline_28d/: {args.baseline_days} nap")

    anomaly_date = start + timedelta(days=args.baseline_days)
    for name, fn in SCENARIOS.items():
        if args.scenario and name != args.scenario:
            continue
        day = fn(anomaly_date, rng)
        day.write(OUT / f"{name}.jsonl")
        expected = {"file": f"{name}.jsonl", "catalog_version": 1, **EXPECTED[name]}
        (OUT / f"{name}.expected.json").write_text(
            json.dumps(expected, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8", newline="\n",
        )
        print(f"{name}.jsonl ({len(day.events)} events) + expected.json")


if __name__ == "__main__":
    main()
