"""Payments-domain models: transactions, settlements, refunds, fees, invoices.

All money columns use `Numeric` so values round-trip exactly (paise-level
precision); SQLAlchemy returns `Decimal` objects for these columns.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.master_data import Merchant, Money, Rate


class Transaction(TimestampMixin, Base):
    """Merchant system-of-record payment record (source of the reconciliation)."""

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_timestamp", "timestamp"),
        Index("ix_transactions_merchant_ts", "merchant_id", "timestamp"),
    )

    transaction_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("merchants.merchant_id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    amount: Mapped[float] = mapped_column(Money, nullable=False)
    fee: Mapped[float] = mapped_column(Money, nullable=False)
    refund_amount: Mapped[float] = mapped_column(Money, nullable=False, default=0)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Soft references: settlements/invoices are inserted after the transaction
    # row itself, so these stay as indexed columns without hard FKs.
    settlement_id: Mapped[str | None] = mapped_column(String(32), index=True)
    invoice_id: Mapped[str | None] = mapped_column(String(32), index=True)

    merchant: Mapped[Merchant] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<Transaction {self.transaction_id} {self.amount}>"


class Settlement(TimestampMixin, Base):
    """Processor-side settlement record (the other half of reconciliation)."""

    __tablename__ = "settlements"
    __table_args__ = (
        Index("ix_settlements_date_id", "settlement_date", "settlement_id"),
        Index("ix_settlements_merchant_date", "merchant_id", "settlement_date"),
    )

    settlement_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    merchant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("merchants.merchant_id"), nullable=False
    )
    settlement_date: Mapped[date] = mapped_column(Date, nullable=False)
    gross_amount: Mapped[float] = mapped_column(Money, nullable=False)
    fee_amount: Mapped[float] = mapped_column(Money, nullable=False)
    refund_amount: Mapped[float] = mapped_column(Money, nullable=False, default=0)
    net_amount: Mapped[float] = mapped_column(Money, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    def __repr__(self) -> str:
        return f"<Settlement {self.settlement_id} net={self.net_amount}>"


class Refund(TimestampMixin, Base):
    __tablename__ = "refunds"
    __table_args__ = (
        Index("ix_refunds_txn_date", "transaction_id", "processed_date"),
    )

    refund_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    merchant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("merchants.merchant_id"), nullable=False
    )
    initiated_date: Mapped[date] = mapped_column(Date, nullable=False)
    processed_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    expected_amount: Mapped[float] = mapped_column(Money, nullable=False)
    recorded_amount: Mapped[float] = mapped_column(Money, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    def __repr__(self) -> str:
        return f"<Refund {self.refund_id} {self.recorded_amount}>"


class Fee(TimestampMixin, Base):
    __tablename__ = "fees"
    __table_args__ = (
        Index("ix_fees_txn_date", "transaction_id", "fee_date"),
    )

    fee_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    merchant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("merchants.merchant_id"), nullable=False
    )
    fee_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_amount: Mapped[float] = mapped_column(Money, nullable=False)
    recorded_amount: Mapped[float] = mapped_column(Money, nullable=False)

    def __repr__(self) -> str:
        return f"<Fee {self.fee_id} expected={self.expected_amount}>"


class Invoice(TimestampMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoices_issue_date", "issue_date"),
    )

    invoice_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    merchant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("merchants.merchant_id"), nullable=False
    )
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    taxable_value: Mapped[float] = mapped_column(Money, nullable=False)
    gst_rate: Mapped[float] = mapped_column(Rate, nullable=False)
    gst_amount: Mapped[float] = mapped_column(Money, nullable=False)
    total_amount: Mapped[float] = mapped_column(Money, nullable=False)

    def __repr__(self) -> str:
        return f"<Invoice {self.invoice_id} total={self.total_amount}>"

