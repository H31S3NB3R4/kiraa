"""Reconciliation route (Phase 9, PRD section 16, architecture section 13).

``POST /api/reconciliation/run`` runs the deterministic Phase 3 engine for
a scope and returns aggregate metrics plus record-level exceptions with
financial impact and evidence. The result is enriched with the persisted
``exception_id``s (the same shared helper the agent registry uses) so
clients can chain straight into journal proposals and the human-approval
actions.

The route is a thin wrapper over the tool (the agent layer wraps the very
same callable): ``ValueError`` from bad filter combinations maps onto 422.
``persist=True`` (the default) upserts the findings into
``reconciliation_exceptions`` idempotently — the same semantics the agent
and the audit views rely on.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agent.tool_registry import enrich_reconciliation_result
from app.api.schemas.reconciliation import (
    ReconciliationRunRequest,
    ReconciliationRunResponse,
)
from app.db.session import get_db
from app.tools import run_reconciliation

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


@router.post("/run", response_model=ReconciliationRunResponse)
def run(
    db: Annotated[Session, Depends(get_db)],
    request: ReconciliationRunRequest = ReconciliationRunRequest(),
) -> ReconciliationRunResponse:
    """Reconcile a scope against settlements/ledger/invoices (FR-2)."""
    try:
        result = run_reconciliation(
            db,
            merchant_id=request.merchant_id,
            start_date=request.start_date,
            end_date=request.end_date,
            persist=request.persist,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return ReconciliationRunResponse(**enrich_reconciliation_result(result, db))
