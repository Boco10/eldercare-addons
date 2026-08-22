"""ElderCare AI Local App — entry point.

Wires the pipeline together (docs/01-ARCHITECTURE.md §4):
    event source -> normalizer -> semantic engine -> storage -> cloud sync

The critical alerting path branches off before the cloud: it must never
wait for the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sys
from datetime import UTC, datetime

import uvicorn

from app.alerts.local_engine import LocalAlertEngine
from app.api.local_ui import create_app
from app.cloud.analysis_client import RoutineAnalysisClient
from app.cloud.pairing import PairingManager
from app.cloud.sync_client import CloudSyncClient
from app.cloud.sync_queue import SyncQueue
from app.config import settings
from app.events.data_quality import DataQualityTracker, day_bounds
from app.events.feature_builder import DailyFeatureSet, FeatureBuilder
from app.events.normalizer import Normalizer
from app.events.semantic_engine import SemanticEngine
from app.ha.camera_client import CameraClient
from app.ha.entity_discovery import EntityRole, suggest
from app.ha.replay_client import ReplayEventSource
from app.ha.service_client import HomeAssistantServiceClient
from app.ha.websocket_client import HomeAssistantWebSocket
from app.storage import runtime_settings
from app.storage.database import Database
from app.storage.mappings import MappingStore

VERSION = "0.1.5"

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("eldercare")

state: dict = {"version": VERSION, "ha_connected": False, "cloud_offline": True}


def build_source():
    """The source is the only place where the run mode matters."""
    if settings.ha_mode == "replay":
        if not settings.ha_replay_file:
            raise ValueError("replay mode requires HA_REPLAY_FILE")
        return ReplayEventSource(settings.ha_replay_file, settings.ha_replay_speed)
    # Passed as a callable: every reconnect reads a fresh token.
    return HomeAssistantWebSocket(settings.ws_url, lambda: settings.ha_access_token)


async def resolve_mapping(entity_id: str, attributes: dict, mappings: dict,
                          store: MappingStore | None = None) -> None:
    """Remember a suggestion for an unknown entity.

    A suggestion is ONLY a suggestion: it lands in the database with
    `confirmed=False`, and the semantic engine does NOT use it until the user
    approves it in the local UI. A wrong meaning causes a wrong alert — that is
    not left to a heuristic.

    In developer mode we confirm automatically, so the pipeline can run without
    the UI (replay tests, CI).
    """
    if entity_id in mappings:
        return
    mapping = suggest(entity_id, attributes)
    if mapping.role is EntityRole.UNKNOWN:
        return

    if settings.eldercare_auto_confirm_mappings:
        mapping.confirmed = True
        log.warning("AUTO-CONFIRM: %s -> %s (%s) — this must NOT be on in production",
                    entity_id, mapping.role.value, mapping.room or "no room")
        mappings[entity_id] = mapping
        if store is not None:
            await store.upsert(mapping)
        return

    log.info("New entity with a suggestion: %s -> %s (waiting for confirmation in the local UI)",
             entity_id, mapping.role.value)
    mappings[entity_id] = mapping
    if store is not None:
        await store.remember_suggestion(mapping)


async def handle_semantic(db: Database, events: list,
                          alerts: LocalAlertEngine | None = None,
                          queue: SyncQueue | None = None) -> None:
    """Store semantic events.

    The commit happens IMMEDIATELY rather than with the 200-row raw batch:
    semantic events are rare and valuable, and a critical alert must not sit in
    an uncommitted transaction — a power cut or a crash would take it.
    """
    recent = state.setdefault("recent_events", [])
    for event in events:
        payload = event.to_payload()
        # Short ring buffer for the routine analysis context — the full history
        # lives in the database.
        recent.append(payload)
        if len(recent) > 100:
            del recent[:-100]
        await db.db.execute(
            "INSERT INTO semantic_events (type, class, timestamp, confidence, source, room, fields)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.type.value, event.event_class.value, event.timestamp.isoformat(),
             event.confidence, event.source, event.room,
             json.dumps(event.fields, ensure_ascii=False, default=str)),
        )
        if event.is_critical:
            state.setdefault("critical_events", []).append(payload)

    if events:
        await db.commit()

    # CRITICAL PATH: local notification, without the cloud and without an LLM.
    # Queueing happens ONLY afterwards — notifying the caregiver must not wait
    # on the network.
    if alerts is not None:
        raised = await alerts.handle(events)
        if raised:
            state["last_alert"] = raised[-1].timestamp.isoformat()
            state["alert_stats"] = alerts.stats
            if queue is not None:
                home_id = state.get("home_id", "home_local")
                for alert in raised:
                    await queue.enqueue_alert(
                        {"level": alert.level.value, "type": alert.type.value,
                         "timestamp": alert.timestamp.isoformat(), "room": alert.room,
                         "reasons": alert.reasons, "delivered_locally": alert.delivered},
                        home_id,
                        f"{alert.type.value}_{alert.timestamp.isoformat()}",
                    )

    # Semantic events go to a buffer; the queue batches them before sending.
    if queue is not None:
        home_id = state.get("home_id", "home_local")
        for event in events:
            await queue.add_event(event.to_payload(), home_id)


async def persist_daily(db: Database, features, quality, event_count: int,
                        queue: SyncQueue | None = None) -> None:
    """Store the daily features. `synced=0` marks what has not reached the cloud yet."""
    day_set = DailyFeatureSet(features=features, quality=quality, event_count=event_count)
    payload = day_set.to_payload(settings.timezone)
    await db.db.execute(
        "INSERT INTO daily_features (date, features, data_quality, anomaly_score, reasons, synced)"
        " VALUES (?, ?, ?, ?, ?, 0)"
        " ON CONFLICT(date) DO UPDATE SET features=excluded.features,"
        "   data_quality=excluded.data_quality, reasons=excluded.reasons, synced=0",
        (payload["date"], json.dumps(payload["features"], ensure_ascii=False),
         payload["data_quality"], None, json.dumps(payload["reasons"], ensure_ascii=False)),
    )
    await db.commit()

    # Queue for upload. It is already written to the database, so from here on
    # it cannot be lost.
    if queue is not None:
        await queue.enqueue_daily_features(payload, state.get("home_id", "home_local"))

    if day_set.usable:
        log.info("Day closed: %s — data quality %.2f, %d events",
                 payload["date"], quality.score, event_count)
    else:
        # On low data quality we do NOT draw conclusions (docs/07-ML-BEHAVIOR.md §4).
        log.warning("Day closed: %s — data quality %.2f is LOW, "
                    "reporting 'insufficient data'. Problems: %s",
                    payload["date"], quality.score,
                    ", ".join(quality.problem_entities[:3]) or "coverage")


async def pipeline(db: Database, normalizer: Normalizer, engine: SemanticEngine,
                   alerts: LocalAlertEngine | None = None,
                   store: MappingStore | None = None,
                   features: FeatureBuilder | None = None,
                   quality: DataQualityTracker | None = None,
                   queue: SyncQueue | None = None) -> None:
    """Process the event stream. The wall clock is NEVER read here."""
    source = build_source()
    await source.connect()
    state["ha_connected"] = True

    processed = 0
    semantic_count = 0
    day_start: datetime | None = None
    try:
        async for raw in source.stream():
            event = normalizer.process(raw)
            state["normalizer_stats"] = normalizer.stats
            if event is None:
                continue

            await db.store_raw(
                event.entity_id, event.state, event.previous_state,
                event.timestamp, json.dumps(event.attributes, ensure_ascii=False),
            )

            await resolve_mapping(event.entity_id, event.attributes, engine.mappings, store)

            if quality is not None:
                quality.add(event)
            if day_start is None:
                day_start, _ = day_bounds(event.timestamp, settings.day_start_hour)

            semantic = engine.process(event)
            if semantic:
                await handle_semantic(db, semantic, alerts, queue)
                semantic_count += len(semantic)

            # Daily features: the day rolls over on the event timestamp.
            if features is not None:
                for semantic_event in semantic:
                    finished = features.add(semantic_event)
                    if finished is None:
                        continue
                    report = (quality.score(day_start, event.timestamp)
                              if quality and day_start else None)
                    if report is not None:
                        # The buffer flushes with the day, so the day's events
                        # travel together with its summary.
                        if queue is not None:
                            await queue.flush_events()
                        await persist_daily(db, finished, report, semantic_count, queue)
                        quality.reset()
                    day_start, _ = day_bounds(event.timestamp, settings.day_start_hour)

            # TODO(phase 3): baseline + anomaly score from the daily features

            processed += 1
            state["last_event_ts"] = event.timestamp
            if processed % 200 == 0:
                await db.commit()
                log.info("Processed: %d raw, %d semantic %s",
                         processed, semantic_count, normalizer.stats)
    finally:
        # An unfinished day still has to be closed — otherwise the last day's
        # data would be lost on a restart.
        if queue is not None:
            await queue.flush_events()
        if features is not None and (last_day := features.finalize()) is not None:
            report = (quality.score(day_start, state.get("last_event_ts") or day_start)
                      if quality and day_start else None)
            if report is not None:
                await persist_daily(db, last_day, report, semantic_count, queue)
        await db.commit()
        state["ha_connected"] = False
        await source.close()
        log.info("Pipeline stopped. %d raw -> %d semantic events. %s",
                 processed, semantic_count, normalizer.stats)


async def ticker(db: Database, engine: SemanticEngine, stop: asyncio.Event,
                 alerts: LocalAlertEngine | None = None) -> None:
    """Periodic, time-based check.

    SAFETY COMPONENT: next to an unconscious person, what happens is that
    nothing happens. Without an incoming event a fall candidate would never be
    confirmed and the alert would never fire. Not used in replay mode — there
    the event timestamps drive the clock.
    """
    if settings.ha_mode == "replay":
        # In replay mode the event timestamps drive the clock, not real time.
        # IMPORTANT: do not return immediately here — the caller waits on
        # FIRST_COMPLETED, and an early return would kill the pipeline.
        await stop.wait()
        return

    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        if stop.is_set():
            break
        # In production the real clock is the right source — no replay here.
        events = engine.tick(datetime.now(UTC))
        if events:
            await handle_semantic(db, events, alerts)
            await db.commit()

        # Retry undelivered alerts — in case Home Assistant restarted or the
        # notification integration was briefly unresponsive.
        if alerts is not None and alerts.pending_count:
            await alerts.retry_pending()


async def uploader(queue: SyncQueue, stop: asyncio.Event, interval: float = 30.0) -> None:
    """The upload round, on a timer.

    A separate task so that a slow network NEVER holds up event processing or
    the alerting path. With no connection the queue simply keeps growing.
    """
    first = True
    while not stop.is_set():
        # Start the first round early: without this a short-lived run (a test, a
        # replay, a quick restart) would never upload a single item.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=3.0 if first else interval)
        first = False
        if stop.is_set():
            break
        try:
            result = await queue.drain()
            state["cloud_offline"] = result.paused and result.sent == 0
            state["queue_stats"] = queue.stats
        except Exception:
            # An upload failure must not stop the app — local operation is what matters.
            log.exception("Error in the upload round — local operation continues.")


async def heartbeat(cloud: CloudSyncClient, stop: asyncio.Event,
                    interval: float = 120.0) -> None:
    """Heartbeat to the backend.

    This is how the caregiver portal knows the installation is alive: it
    refreshes `last_heartbeat_at` on the `installations` row, and the portal
    shows offline after five minutes of silence. Without it a perfectly
    healthy add-on would look dead, which is exactly what destroys trust.

    Not a critical path: on failure we only log, and local operation continues.
    """
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        if stop.is_set():
            break
        if not cloud.device_token:
            continue  # not paired — nobody to report to
        try:
            result = await cloud.heartbeat({
                "app_version": state.get("version"),
                "ha_connected": state.get("ha_connected", False),
                "queue_depth": state.get("queue_stats", {}).get("enqueued", 0),
                "mapped_entities": sum(
                    1 for m in (state["mappings"].cache.values()
                                if state.get("mappings") else []) if m.active),
            })
            if not result.ok:
                log.debug("Heartbeat did not go through (%s).", result.status)
        except Exception:
            log.exception("Error while sending the heartbeat — local operation continues.")


async def serve_ui(db: Database, stop: asyncio.Event) -> None:
    """Stop uvicorn through its own should_exit flag rather than cancelling the
    task — otherwise the lifespan raises CancelledError on shutdown."""
    state["db"] = db
    config = uvicorn.Config(
        create_app(state),
        host="0.0.0.0",  # noqa: S104 — the Ingress IP filter guards it, see local_ui.py
        port=settings.ingress_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    async def _shutdown_on_signal() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(_shutdown_on_signal(), name="ui-watchdog")
    try:
        await server.serve()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def main() -> None:
    log.info("ElderCare AI %s starting (mode: %s)", VERSION, settings.ha_mode)

    db = Database(settings.data_dir)
    await db.connect()

    cloud = CloudSyncClient(
        base_url=settings.cloud_api_url,
        app_version=VERSION,
        installation_id=settings.installation_id,
        device_token=settings.device_token or None,
    )
    await cloud.connect()

    # Pairing: the id and the token come from /data, not from the environment.
    # The environment is only a developer crutch, adopted once into storage.
    pairing = PairingManager(db, cloud, settings.data_dir)
    pairing_state = await pairing.load(settings.installation_id, settings.device_token)
    state["pairing"] = pairing
    state["installation_id"] = pairing_state.installation_id
    state["device_token"] = bool(pairing_state.device_token)
    if pairing_state.home_id:
        state["home_id"] = pairing_state.home_id

    state["cloud_offline"] = cloud.offline
    queue = SyncQueue(db, cloud)
    state["queue"] = queue
    if not cloud.device_token:
        log.warning("No device token — uploading is paused and the queue grows. "
                    "Pair on the local interface.")

    stop = asyncio.Event()

    def request_stop(*_: object) -> None:
        log.info("Shutdown signal — graceful shutdown.")
        stop.set()

    with contextlib.suppress(NotImplementedError):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, request_stop)

    # Mappings come from the database: what the user confirmed survives a
    # restart, and the pipeline uses nothing else.
    # Privacy overrides: what the user set in the local UI wins over the
    # add-on options.
    await runtime_settings.load(db)

    store = MappingStore(db)
    await store.load()
    engine = SemanticEngine(dict(store.cache))
    state["engine"] = engine
    state["mappings"] = store

    # Critical alerting path. It uses Home Assistant's own notification system,
    # which runs locally — so it works even when the cloud or the internet is gone.
    services = HomeAssistantServiceClient()
    await services.connect()
    if settings.ha_mode != "replay":
        await services.discover_notify_services()
        # Ask Home Assistant for the timezone: the day boundaries of the daily
        # features depend on it, and guessing would silently shift wake-up and
        # bedtime in the learned routine.
        ha_config = await services.get_config()
        if ha_config.get("time_zone"):
            settings.timezone = ha_config["time_zone"]
            log.info("Timezone from Home Assistant: %s", settings.timezone)

        # The interface follows Home Assistant's own language. English is the
        # default, but a Hungarian install should not force its caregiver to
        # read English — the person using this may not speak it.
        if ha_config.get("language"):
            state["ha_language"] = str(ha_config["language"])
            log.info("Interface language from Home Assistant: %s",
                     state["ha_language"])
    alerts = LocalAlertEngine(db, services)
    state["alerts"] = alerts
    state["services"] = services

    # Camera and routine analysis. The evaluation runs on the backend (A14) —
    # the add-on collects, uploads and displays.
    camera = CameraClient()
    await camera.connect()
    state["camera"] = camera
    state["analysis"] = RoutineAnalysisClient(cloud, camera)

    # The pipeline is the "main" work: when it ends (or we ask to stop), we close.
    # The UI and the ticker are serving tasks — they cannot trigger a shutdown.
    features = FeatureBuilder(settings.day_start_hour)
    quality = DataQualityTracker(engine.mappings)
    state["features"] = features

    pipeline_task = asyncio.create_task(
        pipeline(db, Normalizer(), engine, alerts, store, features, quality, queue),
        name="pipeline")
    ui_task = asyncio.create_task(serve_ui(db, stop), name="ui")
    ticker_task = asyncio.create_task(ticker(db, engine, stop, alerts), name="ticker")
    uploader_task = asyncio.create_task(uploader(queue, stop), name="uploader")
    heartbeat_task = asyncio.create_task(heartbeat(cloud, stop), name="heartbeat")
    stop_task = asyncio.create_task(stop.wait(), name="stop")

    done, _ = await asyncio.wait([pipeline_task, stop_task],
                                return_when=asyncio.FIRST_COMPLETED)

    for task in done:
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("Error in task %s: %s", task.get_name(), exc, exc_info=exc)

    # Signal first — uvicorn and the ticker stop cleanly that way — and only then cancel.
    stop.set()
    service_tasks = [ui_task, ticker_task, uploader_task, heartbeat_task]
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.gather(*service_tasks, return_exceptions=True),
                               timeout=5.0)

    for task in (*service_tasks, pipeline_task, stop_task):
        if not task.done():
            task.cancel()
    await asyncio.gather(*service_tasks, pipeline_task, stop_task, return_exceptions=True)

    # One last upload round before stopping: whatever is already queued still
    # gets a chance. Time-boxed, so shutdown cannot hang.
    await queue.flush_events()
    with contextlib.suppress(TimeoutError, Exception):
        await asyncio.wait_for(queue.drain(limit=200), timeout=20.0)

    await camera.close()
    await services.close()
    await cloud.close()
    await db.close()
    log.info("Stopped.")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
