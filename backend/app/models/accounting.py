"""Accounting models: ledger entries and daily merchant cash flows."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.master_data import Money


class LedgerEntry(TimestampMixin, Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_entries_date", "entry_date"),
        Index("ix_ledger_entries_merchant_date", "merchant_id", "entry_date"),
    )

    entry_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    merchant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("merchants.merchant_id"), nullable=False
    )
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    debit_account: Mapped[str] = mapped_column(String(64), nullable=False)
    credit_account: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[float] = mapped_column(Money, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=False)

    def __repr__(self) -> str:
        return f"<LedgerEntry {self.entry_id} {self.amount} {self.status}>"


class CashFlow(TimestampMixin, Base):
    """Daily per-merchant cash aggregate (inflow/outflow/net/closing)."""

    __tablename__ = "cash_flows"
    __table_args__ = (
        # Enforces one row per (merchant, day) — the forecasting module
        # (Phase 4) relies on this uniqueness.
        Index("uq_cash_flows_merchant_date", "merchant_id", "date", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("merchants.merchant_id"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    inflow: Mapped[float] = mapped_column(Money, nullable=False)
    outflow: Mapped[float] = mapped_column(Money, nullable=False)
    net_amount: Mapped[float] = mapped_column(Money, nullable=False)
    closing_balance: Mapped[float] = mapped_column(Money, nullable=False)

    def __repr__(self) -> str:
        return f"<CashFlow {self.merchant_id} {self.date} net={self.net_amount}>"
