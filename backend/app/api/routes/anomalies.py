"""Anomalies route (Phase 9, PRD section 16, architecture section 13).

``GET /api/anomalies`` is the HTTP twin of the agent's ``detect_anomalies``
READ tool (FR-6): Isolation-Forest scores with severity bands, reason
metadata, deterministic cross-links, and ground-truth metrics. Because a
GET must never write, the endpoint pins ``persist=False`` — scores are
computed fresh and returned, never upserted (the agent tool and the Phase
12 evaluation harness own the persisted ``anomaly_scores`` path). Guard
envelopes (``unknown_merchant`` / ``no_transactions``) return HTTP 200.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.schemas.anomalies import AnomalyResponse
from app.db.session import get_db
from app.tools import detect_anomalies

router = APIRouter(prefix="/api/anomalies", tags=["anomalies"])


@router.get("", response_model=AnomalyResponse)
def anomalies(
    db: Annotated[Session, Depends(get_db)],
    merchant_id: str | None = None,
    transaction_id: str | None = None,
    limit: int | None = 500,
) -> AnomalyResponse:
    """Score transactions for statistical unusualness (FR-6, read-only)."""
    transaction_ids = None
    if transaction_id is not None:
        transaction_ids = [transaction_id]
    try:
        result = detect_anomalies(
            db,
            merchant_id=merchant_id,
            transaction_ids=transaction_ids,
            limit=limit,
            persist=False,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return AnomalyResponse(**result)
