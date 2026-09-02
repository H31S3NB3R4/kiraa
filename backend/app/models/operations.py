"""Operations models: ground-truth labels, reconciliation exceptions, anomaly scores.

- `DatasetLabel` preserves the Phase 1 ground truth inside the database so
  the Phase 12 evaluation harness can score engine output against it.
- `ReconciliationException` and `AnomalyScore` are written by the Phase 3/5
  engines; the seed only creates the tables.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.master_data import Money


class DatasetLabel(TimestampMixin, Base):
    """Ground-truth scenario label for a synthetic transaction (seeded)."""

    __tablename__ = "dataset_labels"
    __table_args__ = (
        Index("ix_dataset_labels_scenario", "scenario"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    scenario: Mapped[str] = mapped_column(String(48), nullable=False)
    recon_exception: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    anomaly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class ReconciliationException(TimestampMixin, Base):
    """A mismatch found by the Phase 3 reconciliation engine."""

    __tablename__ = "reconciliation_exceptions"
    __table_args__ = (
        Index("ix_recon_exceptions_txn", "transaction_id"),
        Index("ix_recon_exceptions_date_type", "exception_date", "exception_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    exception_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    exception_type: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    expected_amount: Mapped[float] = mapped_column(Money, nullable=False, default=0)
    recorded_amount: Mapped[float] = mapped_column(Money, nullable=False, default=0)
    financial_impact: Mapped[float] = mapped_column(Money, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")

    def __repr__(self) -> str:
        return f"<ReconciliationException {self.exception_type} {self.transaction_id}>"


class AnomalyScore(TimestampMixin, Base):
    """Isolation-Forest score attached by the Phase 5 ML layer."""

    __tablename__ = "anomaly_scores"
    __table_args__ = (
        Index("ix_anomaly_scores_txn_score", "transaction_id", "is_anomaly"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    scored_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    anomaly_score: Mapped[float] = mapped_column(Float, nullable=False)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reasons: Mapped[dict | list] = mapped_column(JSON, nullable=False, default=dict)
