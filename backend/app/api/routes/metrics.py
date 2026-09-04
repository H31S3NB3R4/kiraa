"""Metrics route (Phase 9, PRD section 16, architecture section 13).

``GET /api/metrics`` serves the Phase 10 KPI cards: total cash (pooled
closing balances on the latest cash-flow day), a fresh read-only
reconciliation pass (match rate, exception count, financial impact at
risk), and the pending-proposal count awaiting human review. Unknown
merchant scopes return 404 (the guard convention for scoped reads).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.schemas.metrics import MetricsResponse
from app.db.session import get_db
from app.services.metrics import MerchantNotFoundError, dashboard_metrics

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("", response_model=MetricsResponse)
def metrics(
    db: Annotated[Session, Depends(get_db)],
    merchant_id: str | None = None,
) -> MetricsResponse:
    """Aggregate the dashboard KPI cards for a scope (read-only)."""
    try:
        return MetricsResponse(**dashboard_metrics(db, merchant_id=merchant_id))
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
