"""FastAPI application entry point for the AI Finance Controller."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import health
from app.config import get_settings


def create_app() -> FastAPI:
    """Application factory (separate from the module-level `app` for tests)."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Agentic finance-operations controller: reconciliation, ledger, "
            "forecast, GST matching, anomaly detection, and safe (human-"
            "approved) journal actions."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers are registered here as phases land
    # (agent, reconciliation, ledger, forecast, anomalies, actions, audit,
    # metrics).
    app.include_router(health.router, tags=["health"])

    return app


app = create_app()
