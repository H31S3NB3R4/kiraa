"""Read-only ledger query tool (Phase 3).

``query_ledger`` filters ledger entries by merchant, transaction, date
range, status, account, or merchant category and returns source-linked
rows: every entry is joined back to its transaction (for settlement and
invoice references) and its merchant. The tool never mutates state
(PRD FR-3: "support investigative queries without allowing mutation").
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.models import LedgerEntry, Merchant, Transaction
from app.tools.common import coerce_date, round2

DEFAULT_LIMIT = 500


def query_ledger(
    db: Session,
    merchant_id: str | None = None,
    transaction_id: str | None = None,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    status: str | None = None,
    account: str | None = None,
    category: str | None = None,
    limit: int | None = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Run a read-only, source-linked query over ``ledger_entries``.

    ``limit`` caps the number of rows returned (fetching one extra row to
    report ``truncated`` accurately); ``None`` disables the cap.
    """
    start = coerce_date(start_date)
    end = coerce_date(end_date)
    if start is not None and end is not None and start > end:
        raise ValueError("start_date must not be after end_date")

    stmt: Select = (
        select(LedgerEntry, Transaction, Merchant)
        .join(Transaction, LedgerEntry.transaction_id == Transaction.transaction_id)
        .join(Merchant, Transaction.merchant_id == Merchant.merchant_id)
    )
    if merchant_id is not None:
        stmt = stmt.where(LedgerEntry.merchant_id == merchant_id)
    if transaction_id is not None:
        stmt = stmt.where(LedgerEntry.transaction_id == transaction_id)
    if start is not None:
        stmt = stmt.where(LedgerEntry.entry_date >= start)
    if end is not None:
        stmt = stmt.where(LedgerEntry.entry_date <= end)
    if status is not None:
        stmt = stmt.where(LedgerEntry.status == status)
    if account is not None:
        stmt = stmt.where(
            or_(
                LedgerEntry.debit_account == account,
                LedgerEntry.credit_account == account,
            )
        )
    if category is not None:
        stmt = stmt.where(Merchant.category == category)

    stmt = stmt.order_by(LedgerEntry.entry_date, LedgerEntry.entry_id)
    if limit is not None:
        stmt = stmt.limit(limit + 1)

    rows = [_serialize(entry, txn, merchant) for entry, txn, merchant in db.execute(stmt)]
    truncated = limit is not None and len(rows) > limit
    if truncated:
        rows = rows[:limit]

    return {
        "tool": "query_ledger",
        "filters": {
            "merchant_id": merchant_id,
            "transaction_id": transaction_id,
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "status": status,
            "account": account,
            "category": category,
        },
        "count": len(rows),
        "limit": limit,
        "truncated": truncated,
        "rows": rows,
    }


def _serialize(
    entry: LedgerEntry, txn: Transaction, merchant: Merchant
) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "transaction_id": entry.transaction_id,
        "merchant_id": entry.merchant_id,
        "merchant_name": merchant.name,
        "merchant_category": merchant.category,
        "entry_date": entry.entry_date.isoformat(),
        "debit_account": entry.debit_account,
        "credit_account": entry.credit_account,
        "amount": round2(entry.amount),
        "status": entry.status,
        "description": entry.description,
        # Source references (FR-3): link the entry back to its records.
        "settlement_id": txn.settlement_id,
        "invoice_id": txn.invoice_id,
    }
