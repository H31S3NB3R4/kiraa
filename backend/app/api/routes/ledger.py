"""Ledger route (Phase 9, PRD section 16, architecture section 13).

``GET /api/ledger/query`` is the HTTP twin of the agent's ``query_ledger``
READ tool: read-only, source-linked rows with the full filter surface —
merchant, transaction, date range, status, account, and merchant
category. It never mutates state (FR-3: "support investigative queries
without allowing mutation"); bad date ranges map onto 422.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.schemas.ledger import LedgerQueryResponse
from app.db.session import get_db
from app.tools import query_ledger

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


@router.get("/query", response_model=LedgerQueryResponse)
def query(
    db: Annotated[Session, Depends(get_db)],
    merchant_id: str | None = None,
    transaction_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description="Optional ledger status, e.g. 'posted'/'failed'",
        ),
    ] = None,
    account: str | None = None,
    category: str | None = None,
    limit: int | None = 500,
) -> LedgerQueryResponse:
    """Run a read-only, source-linked ledger query (FR-3)."""
    try:
        result = query_ledger(
            db,
            merchant_id=merchant_id,
            transaction_id=transaction_id,
            start_date=start_date,
            end_date=end_date,
            status=status_filter,
            account=account,
            category=category,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return LedgerQueryResponse(**result)
