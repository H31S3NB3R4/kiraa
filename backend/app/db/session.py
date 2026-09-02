"""Database engine and session management.

The local prototype runs on SQLite (no external services required); the
same code path supports PostgreSQL by changing `DATABASE_URL` (see
`.env.example`). Import `app.models` (done below) so the engine session is
created with the full ORM metadata registered.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  (registers all tables on Base.metadata)
from app.config import get_settings
from app.models.base import Base

_settings = get_settings()


def _prepare_sqlite_dir(url: str) -> None:
    """Ensure the parent directory of a SQLite file database exists."""
    if not url.startswith("sqlite:///") or url in {"sqlite:///:memory:", "sqlite://"}:
        return
    db_path = url.removeprefix("sqlite:///")
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def create_engine_for(url: str) -> Engine:
    """Create an engine for ``url`` with prototype-appropriate options.

    SQLite engines enable per-connection ``PRAGMA foreign_keys=ON`` so the
    declared foreign keys are actually enforced (SQLite defaults to OFF).
    """
    if url.startswith("sqlite"):
        _prepare_sqlite_dir(url)
        sqlite_engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(sqlite_engine, "connect")
        def _enable_sqlite_fk(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return sqlite_engine
    return create_engine(url, pool_pre_ping=True)


engine = create_engine_for(_settings.database_url)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a request-scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    """Create every table that does not exist yet (idempotent)."""
    Base.metadata.create_all(engine)


def drop_all() -> None:
    """Drop every table (used by the seed script's --recreate flag)."""
    Base.metadata.drop_all(engine)

