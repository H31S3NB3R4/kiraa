"""Phase 0 smoke tests: application factory, /health endpoint, DB wiring."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.session import engine
from app.main import app

client = TestClient(app)


def test_health_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "AI Finance Controller"


def test_database_engine_connects() -> None:
    # Verifies the SQLite wiring (directory creation + engine) without
    # requiring any tables yet.
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT 1").scalar() == 1
