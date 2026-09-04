"""Request/response schemas for the reconciliation endpoints (Phase 9).

``POST /api/reconciliation/run`` mirrors the PRD section-16 API surface:
the request carries the engine's optional scope filters (merchant, date
range) plus the ``persist`` control, and the response mirrors the dict
returned by ``app.tools.reconciliation.run_reconciliation`` — enriched
with the persisted ``exception_id``s so clients can chain straight into
journal proposals and the human-approval actions.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class ReconciliationRunRequest(BaseModel):
    """``POST /api/reconciliation/run`` request body (all fields optional)."""

    merchant_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    persist: bool = True


class ExceptionRecord(BaseModel):
    """One classified exception: taxonomy, amounts, impact, and evidence."""

    transaction_id: str
    merchant_id: str
    exception_type: str
    exception_date: date
    severity: str
    expected_amount: float
    recorded_amount: float
    financial_impact: float
    description: str
    status: str = "open"
    sources: dict[str, Any] = Field(default_factory=dict)
    # Persisted row id (attached by the shared enrichment helper); None
    # when the run did not persist and no earlier row exists.
    exception_id: int | None = None


class ReconciliationRunResponse(BaseModel):
    """Serialization contract for ``run_reconciliation`` results (FR-2)."""

    tool: str = "run_reconciliation"
    filters: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    persisted: dict[str, int] | None = None
