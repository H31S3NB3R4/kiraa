"""Master-data models: merchants and customers."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# INR amounts can be large; 14 digits + 2 paise covers any realistic value.
Money = Numeric(14, 2)
# Fee/tax rates are fractions (e.g. 0.02); six decimal places are ample.
Rate = Numeric(8, 6)


class Merchant(TimestampMixin, Base):
    __tablename__ = "merchants"

    merchant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    fee_rate: Mapped[float] = mapped_column(Rate, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    opening_balance: Mapped[float] = mapped_column(Money, nullable=False)
    current_balance: Mapped[float | None] = mapped_column(Money)

    def __repr__(self) -> str:
        return f"<Merchant {self.merchant_id} {self.name!r}>"


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    city: Mapped[str] = mapped_column(String(64), nullable=False)
    signup_date: Mapped[date] = mapped_column(Date, nullable=False)

    def __repr__(self) -> str:
        return f"<Customer {self.customer_id} {self.name!r}>"
