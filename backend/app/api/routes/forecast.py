"""Forecast route (Phase 9, PRD section 16, architecture section 13).

``GET /api/forecast`` is the HTTP twin of the agent's ``forecast_cashflow``
READ tool (FR-4): pooled or per-merchant horizon forecasts with the
LOW/MEDIUM/HIGH risk classification, drivers, and a chart-ready series
(the Phase 4 ``ForecastResponse`` schema). Unknown merchants and empty
history return guard envelopes with HTTP 200 (GST-tool convention), so
dashboards can render a safe empty state instead of an error.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.schemas.forecast import ForecastResponse
from app.db.session import get_db
from app.tools import forecast_cashflow

router = APIRouter(prefix="/api/forecast", tags=["forecast"])


@router.get("", response_model=ForecastResponse)
def forecast(
    db: Annotated[Session, Depends(get_db)],
    merchant_id: str | None = None,
    horizon_days: int = 7,
    history_days: int = 28,
    operating_threshold: float | None = None,
) -> ForecastResponse:
    """Produce a deterministic cash-flow forecast (FR-4)."""
    try:
        result = forecast_cashflow(
            db,
            merchant_id=merchant_id,
            horizon_days=horizon_days,
            history_days=history_days,
            operating_threshold=operating_threshold,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return ForecastResponse(**result)
