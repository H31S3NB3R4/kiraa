"""Proposals route (Phase 10, PRD section 16, architecture section 13).

``GET /api/proposals`` lists journal proposals — pending ones awaiting
human review plus decided history — for the dashboard's Action view.
Strictly read-only: the approve/reject/rollback writes stay on the Phase
8 action routes (``/api/actions/{id}/...``), which remain the only
ledger-mutating surface.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.schemas.proposals import ProposalsListResponse
from app.db.session import get_db
from app.services.queries import list_proposals

router = APIRouter(prefix="/api/proposals", tags=["proposals"])

_PROPOSAL_STATUSES = ("pending", "approved", "rejected", "rolled_back")


@router.get("", response_model=ProposalsListResponse)
def proposals(
    db: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description=f"Optional lifecycle state: one of {_PROPOSAL_STATUSES}",
            pattern="^(pending|approved|rejected|rolled_back)$",
        ),
    ] = None,
    merchant_id: str | None = None,
    transaction_id: str | None = None,
    limit: int | None = 500,
) -> ProposalsListResponse:
    """List journal proposals, newest first (the Action view's queue)."""
    return ProposalsListResponse(
        **list_proposals(
            db,
            status=status_filter,
            merchant_id=merchant_id,
            transaction_id=transaction_id,
            limit=limit,
        )
    )