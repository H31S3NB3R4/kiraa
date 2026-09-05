"""Health/liveness endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """Liveness probe.

    Intentionally cheap: no database, LLM, or network access so that
    deployment checks never depend on downstream services.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
        # Phase 14: every record is seeded synthetic demo data.
        "data": "synthetic",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
