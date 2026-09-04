"""Dashboard metrics aggregation (Phase 9, PRD section 16, todo Phase 10).

``dashboard_metrics`` serves the KPI cards behind ``GET /api/metrics``:

- **total cash**: closing balances pooled on the latest cash-flow day of
  the scope (the same anchoring the forecast tool uses),
- **reconciliation match rate / exception count / financial impact at
  risk**: a fresh *read-only* engine run — deterministic, never
  persisted — so the dashboard can never show stale or invented numbers,
- **pending proposals**: journal corrections awaiting human review.

Strictly read-only; unknown merchant scopes raise ``MerchantNotFoundError``
(the route maps it onto 404).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CashFlow, JournalProposal, Merchant, Transaction
from app.tools import run_reconciliation
from app.tools.common import round2

__all__ = ["MerchantNotFoundError", "dashboard_metrics"]


class MerchantNotFoundError(LookupError):
    """No merchant exists for the given id (HTTP 404)."""


def dashboard_metrics(db: Session, *, merchant_id: str | None = None) -> dict[str, Any]:
    """Aggregate the dashboard KPI cards for a scope (read-only)."""
    if merchant_id is not None and db.get(Merchant, merchant_id) is None:
        raise MerchantNotFoundError(f"merchant_id {merchant_id!r} does not exist")

    scope = [CashFlow.merchant_id == merchant_id] if merchant_id is not None else []

    total_cash: float | None = None
    cash_as_of = db.execute(select(func.max(CashFlow.date)).where(*scope)).scalar()
    if cash_as_of is not None:
        total_cash = round2(
            db.execute(
                select(func.sum(CashFlow.closing_balance))
                .where(CashFlow.date == cash_as_of)
                .where(*scope)
            ).scalar()
            or 0.0
        )

    # Fresh deterministic numbers — the same engine the agent uses, without
    # persisting anything (a GET must never write).
    metrics = run_reconciliation(db, merchant_id, persist=False)["metrics"]

    pending_stmt = (
        select(func.count())
        .select_from(JournalProposal)
        .where(JournalProposal.status == "pending")
    )
    if merchant_id is not None:
        pending_stmt = pending_stmt.join(
            Transaction, JournalProposal.transaction_id == Transaction.transaction_id
        ).where(Transaction.merchant_id == merchant_id)
    pending = db.execute(pending_stmt).scalar_one()

    return {
        "merchant_id": merchant_id,
        "total_cash": total_cash,
        "cash_as_of_date": cash_as_of,
        "reconciliation": metrics,
        "exception_count": metrics["exceptions"],
        "exception_transactions": metrics["exception_transactions"],
        "financial_impact_at_risk": metrics["total_financial_impact"],
        "match_rate_pct": metrics["match_rate_pct"],
        "pending_proposals": pending,
    }
