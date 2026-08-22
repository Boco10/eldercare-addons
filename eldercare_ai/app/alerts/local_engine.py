"""Critical offline alerting engine.

**The most important component in the project.** The ground rule is that a
critical alert (SOS, smoke, CO, confirmed fall) reaches the caregiver without
(docs/00-PROJECT.md §4, docs/09-ALERTS.md §1).

What this module guarantees:
  1. The alert goes out immediately — it waits for no cloud reply and no AI text.
  2. It tries every channel; if ALL of them fail, that is an emergency of its
     own and is logged as such.
  3. The same event does not alert twice (cooldown), but the critical level
     keeps retrying until at least one channel succeeds.
  4. The alert is written to the local database before it goes out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.events.models import AlertLevel, EventClass, SemanticEvent, SemanticType
from app.ha.service_client import HomeAssistantServiceClient
from app.storage.database import Database

log = logging.getLogger(__name__)

# Deterministic rules: these events alert on their own, with no baseline,
# no score and no AI.
CRITICAL_RULES: dict[SemanticType, str] = {
    SemanticType.SOS_TRIGGERED: "Emergency button pressed",
    SemanticType.SMOKE_DETECTED: "Smoke detected",
    SemanticType.CO_DETECTED: "Carbon monoxide detected",
    SemanticType.CONFIRMED_FALL: "Fall confirmed",
}

# The same alert type does not repeat for this long.
DEFAULT_COOLDOWN_S = 300.0
# When no channel succeeded, retry this many times from tick().
MAX_DELIVERY_ATTEMPTS = 5


@dataclass(slots=True)
class Alert:
    level: AlertLevel
    type: SemanticType
    timestamp: datetime
    title: str
    message: str
    room: str | None = None
    reasons: list[str] = field(default_factory=list)
    delivered: bool = False
    attempts: int = 0
    channels: dict[str, bool] = field(default_factory=dict)

    @property
    def dedup_key(self) -> tuple[SemanticType, str | None]:
        return (self.type, self.room)


class LocalAlertEngine:
    """Raising and delivering local alerts."""

    def __init__(self, db: Database, services: HomeAssistantServiceClient,
                 cooldown_s: float = DEFAULT_COOLDOWN_S,
                 siren_entity: str | None = None) -> None:
        self.db = db
        self.services = services
        self.cooldown_s = cooldown_s
        self.siren_entity = siren_entity
        self._last_alert: dict[tuple[SemanticType, str | None], datetime] = {}
        self._pending: list[Alert] = []
        self.stats = {"raised": 0, "suppressed": 0, "delivered": 0, "failed": 0}

    # ------------------------------------------------------------------- entry

    async def handle(self, events: list[SemanticEvent]) -> list[Alert]:
        """Alerts from semantic events. The critical branch NEVER waits on the network."""
        alerts: list[Alert] = []
        for event in events:
            alert = self._evaluate(event)
            if alert is None:
                continue
            await self._persist(alert)
            await self._deliver(alert)
            alerts.append(alert)
        return alerts

    def _evaluate(self, event: SemanticEvent) -> Alert | None:
        """Decide the alert level.

        For now ONLY the deterministic critical rules alert. Behavioural anomalies
        join in phase 3 from the risk score — until then not alerting is more
        honest than guessing without a baseline.
        """
        if event.event_class is not EventClass.CRITICAL:
            return None

        rule = CRITICAL_RULES.get(event.type)
        if rule is None:
            log.error("Critical-class event with no rule: %s", event.type.value)
            return None

        if self._in_cooldown(event):
            self.stats["suppressed"] += 1
            log.info("Alert suppressed (cooldown): %s", event.type.value)
            return None

        self._last_alert[(event.type, event.room)] = event.timestamp
        self.stats["raised"] += 1

        location = f" — {event.room}" if event.room else ""
        return Alert(
            level=AlertLevel.CRITICAL,
            type=event.type,
            timestamp=event.timestamp,
            title=f"ElderCare: {rule}",
            message=self._compose_message(event, rule, location),
            room=event.room,
            reasons=list(event.fields.get("evidence", [])),
        )

    def _in_cooldown(self, event: SemanticEvent) -> bool:
        previous = self._last_alert.get((event.type, event.room))
        if previous is None:
            return False
        return event.timestamp - previous < timedelta(seconds=self.cooldown_s)

    @staticmethod
    def _compose_message(event: SemanticEvent, rule: str, location: str) -> str:
        """With an explanation — an alert without a score and a reason never goes
        (docs/00-PROJECT.md §4, explainability)."""
        when = event.timestamp.strftime("%H:%M")
        parts = [f"{rule}{location}, {when}."]

        if evidence := event.fields.get("evidence"):
            parts.append("Corroborating signals: " + ", ".join(str(e) for e in evidence) + ".")
        if event.source == "vision":
            parts.append(f"Camera signal, confidence: {event.confidence:.0%}.")
        if device := event.fields.get("device"):
            parts.append(f"Device: {device}.")

        parts.append("Please check on the resident.")
        return " ".join(parts)

    # -------------------------------------------------------------- delivery

    async def _persist(self, alert: Alert) -> None:
        """Store BEFORE delivering — so a crash still leaves a record of what happened."""
        import json

        await self.db.db.execute(
            "INSERT INTO local_alerts (level, type, timestamp, reasons, state, notified_locally)"
            " VALUES (?, ?, ?, ?, ?, 0)",
            (alert.level.value, alert.type.value, alert.timestamp.isoformat(),
             json.dumps(alert.reasons, ensure_ascii=False), "DETECTED"),
        )
        await self.db.commit()

    async def _deliver(self, alert: Alert) -> None:
        alert.attempts += 1
        alert.channels = await self.services.notify_all(
            alert.title, alert.message,
            data={"id": f"eldercare_{alert.type.value}", "importance": "high"},
        )

        if self.siren_entity and alert.level is AlertLevel.CRITICAL:
            alert.channels[self.siren_entity] = await self.services.turn_on(self.siren_entity)

        succeeded = [name for name, ok in alert.channels.items() if ok]

        if succeeded:
            alert.delivered = True
            self.stats["delivered"] += 1
            self._pending = [a for a in self._pending if a is not alert]
            log.critical("ALERT DELIVERED (%s): %s -> %s",
                         alert.level.value, alert.type.value, ", ".join(succeeded))
            await self.db.db.execute(
                "UPDATE local_alerts SET state = ?, notified_locally = 1"
                " WHERE timestamp = ? AND type = ?",
                ("NOTIFIED", alert.timestamp.isoformat(), alert.type.value),
            )
            await self.db.commit()
            return

        # Not a single channel succeeded. This is the worst failure state in the
        # system: something happened, and the caregiver does not know.
        self.stats["failed"] += 1
        log.critical("ALERT DELIVERY FAILED (attempt %d): %s — "
                     "NOT A SINGLE channel is reachable!", alert.attempts, alert.type.value)
        if alert.attempts < MAX_DELIVERY_ATTEMPTS and alert not in self._pending:
            self._pending.append(alert)

    async def retry_pending(self) -> None:
        """Called from tick(): retry the undelivered alerts."""
        for alert in list(self._pending):
            if alert.attempts >= MAX_DELIVERY_ATTEMPTS:
                log.error("Alert abandoned after %d attempts: %s",
                          alert.attempts, alert.type.value)
                self._pending.remove(alert)
                continue
            log.info("Retrying alert (attempt %d): %s",
                     alert.attempts + 1, alert.type.value)
            await self._deliver(alert)

    # ------------------------------------------------------------------- teszt

    async def send_test_alert(self) -> dict[str, bool]:
        """The test alert button in the local UI.

        It travels the REAL delivery path, so the installer can see that the alert
        actually arrives. Otherwise a broken path would surface only after handover.
        """
        channels = await self.services.notify_all(
            "ElderCare: test alert",
            "This is a test. If you can read this, the notification channel works.",
            data={"id": "eldercare_test"},
        )
        log.info("Test alert result: %s", channels)
        return channels

    @property
    def pending_count(self) -> int:
        return len(self._pending)
