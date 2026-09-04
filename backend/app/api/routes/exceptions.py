"""Exceptions route (Phase 9, PRD section 16, architecture section 13).

``GET /api/exceptions`` lists the *persisted* reconciliation exceptions —
the Phase 3 engine's upserted rows, not a fresh run — so the dashboard's
exception list stays stable between runs. Rows are joined to their
transaction for the merchant scope; bad date ranges map onto 422.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.schemas.exceptions import ExceptionsListResponse
from app.db.session import get_db
from app.services.queries import list_exceptions

router = APIRouter(prefix="/api/exceptions", tags=["exceptions"])


@router.get("", response_model=ExceptionsListResponse)
def exceptions(
    db: Annotated[Session, Depends(get_db)],
    merchant_id: str | None = None,
    exception_type: str | None = None,
    severity: str | None = None,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description="Optional exception status, e.g. 'open'",
        ),
    ] = None,
    transaction_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = 500,
) -> ExceptionsListResponse:
    """List persisted reconciliation exceptions, newest first."""
    try:
        return ExceptionsListResponse(
            **list_exceptions(
                db,
                merchant_id=merchant_id,
                exception_type=exception_type,
                severity=severity,
                status=status_filter,
                transaction_id=transaction_id,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
