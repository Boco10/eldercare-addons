"""Health and readiness endpoints.

The smoke test and the Supervisor watchdog call these. They are NOT subject to
the Ingress IP filter, because they arrive from inside the container (127.0.0.1).
"""

from __future__ import annotations

from fastapi import APIRouter


def create_router(state: dict) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict:
        """Is the process alive. Always 200 while the event loop runs."""
        return {"status": "ok", "version": state.get("version")}

    @router.get("/ready")
    async def ready() -> dict:
        """Ready for work: the database is up and there is an event source.

        An unreachable cloud does NOT make it not-ready — local operation lives on
        (docs/00-PROJECT.md §4, graceful degradation).
        """
        return {
            "ready": state.get("db") is not None,
            "ha_connected": state.get("ha_connected", False),
            "cloud_offline": state.get("cloud_offline", True),
        }

    return router
