"""Database engine and session management.

The local prototype runs on SQLite (no external services required); the
same code path supports PostgreSQL by changing `DATABASE_URL` (see
`.env.example`). ORM models and tables arrive in Phase 2.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

_settings = get_settings()


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy ORM models (Phase 2)."""


def _prepare_sqlite_dir(url: str) -> None:
    """Ensure the parent directory of a SQLite file database exists."""
    if not url.startswith("sqlite:///") or url in {"sqlite:///:memory:", "sqlite://"}:
        return
    db_path = url.removeprefix("sqlite:///")
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def _make_engine():
    url = _settings.database_url
    if url.startswith("sqlite"):
        # `check_same_thread=False` allows the session to be used across
        # FastAPI's threadpool; the SQLite prototype is single-process.
        _prepare_sqlite_dir(url)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


engine = _make_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a request-scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
