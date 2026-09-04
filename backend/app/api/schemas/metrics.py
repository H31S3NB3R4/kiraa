"""Response schemas for the dashboard metrics endpoint (Phase 9).

``GET /api/metrics`` serves the Phase 10 KPI cards: total cash,
reconciliation match rate, exception count, and financial impact at risk —
plus the pending-proposal count awaiting human review.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class ReconciliationMetrics(BaseModel):
    """Aggregate reconciliation counters (the engine's metrics dict)."""

    transactions: int = 0
    matched: int = 0
    exception_transactions: int = 0
    exceptions: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    total_financial_impact: float = 0.0
    match_rate_pct: float = 100.0


class MetricsResponse(BaseModel):
    """``GET /api/metrics`` response body.

    ``total_cash``/``cash_as_of_date`` pool the closing balances recorded
    on the latest cash-flow day (``None`` when the scope has no rows).
    The reconciliation fields come from a fresh *read-only* engine run —
    deterministic and never persisted — so the dashboard can never show
    stale or invented numbers.
    """

    merchant_id: str | None = None
    total_cash: float | None = None
    cash_as_of_date: date | None = None
    reconciliation: ReconciliationMetrics
    # KPI-card conveniences (mirrors of the reconciliation block above).
    exception_count: int = 0
    exception_transactions: int = 0
    financial_impact_at_risk: float = 0.0
    match_rate_pct: float = 100.0
    pending_proposals: int = 0
