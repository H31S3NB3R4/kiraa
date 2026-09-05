"""FastAPI application entry point for the AI Finance Controller."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    actions,
    agent,
    anomalies,
    audit,
    exceptions,
    forecast,
    health,
    ledger,
    merchants,
    metrics,
    proposals,
    reconciliation,
    runs,
)
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

    # Routers are registered here as phases land.
    app.include_router(health.router, tags=["health"])
    app.include_router(agent.router, tags=["agent"])
    app.include_router(actions.router, tags=["actions"])
    app.include_router(reconciliation.router, tags=["reconciliation"])
    app.include_router(ledger.router, tags=["ledger"])
    app.include_router(forecast.router, tags=["forecast"])
    app.include_router(anomalies.router, tags=["anomalies"])
    app.include_router(exceptions.router, tags=["exceptions"])
    app.include_router(runs.router, tags=["runs"])
    app.include_router(audit.router, tags=["audit"])
    app.include_router(metrics.router, tags=["metrics"])
    app.include_router(proposals.router, tags=["proposals"])
    app.include_router(merchants.router, tags=["merchants"])

    return app


app = create_app()
