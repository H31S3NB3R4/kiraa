"""Shared model building blocks: declarative base, timestamp mixin, type aliases.

Every ORM model inherits from `Base` and the domain modules in this package
register their tables on import. `app.models.__init__` imports the domain
modules so `Base.metadata` always contains the full 18-table schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy ORM models."""


class TimestampMixin:
    """Adds database-managed creation/update timestamps to a model."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
