"""Response schemas for the persisted-exceptions endpoint (Phase 9).

``GET /api/exceptions`` reads the ``reconciliation_exceptions`` rows the
Phase 3 engine upserts (not a fresh run) — the dashboard's exception
list. Rows are joined back to their transaction for the merchant scope.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class ExceptionRow(BaseModel):
    """One persisted reconciliation exception."""

    exception_id: int
    transaction_id: str
    merchant_id: str
    exception_date: date
    exception_type: str
    severity: str
    expected_amount: float
    recorded_amount: float
    financial_impact: float
    description: str
    status: str


class ExceptionsListResponse(BaseModel):
    """``GET /api/exceptions`` response body (list envelope convention)."""

    count: int = 0
    limit: int | None = None
    truncated: bool = False
    filters: dict[str, Any] = Field(default_factory=dict)
    rows: list[ExceptionRow] = Field(default_factory=list)
