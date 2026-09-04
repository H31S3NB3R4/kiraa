"""Action routes (Phase 8, PRD section 16, architecture section 13).

``POST /api/actions/{proposal_id}/approve|reject|rollback`` are the only
HTTP surface that can mutate ledger state. They are deliberately absent
from the agent tool registry: no natural-language request can reach them —
only an explicit human action carrying an idempotency key (architecture
section 5: the WRITE permission class is never model-callable; PRD
section 15: human approval gates every ledger mutation).

The routes stay thin — validation, posting, and audit live in the
service layer (``app/services/actions.py``) so they stay unit-testable
without FastAPI. Typed service errors map onto status codes:

- ``ProposalNotFoundError``      404  unknown proposal id
- ``ProposalStateError``         409  wrong lifecycle state (already decided)
- ``IdempotencyConflictError``   409  idempotency key reused for another action
- ``ActionValidationError``      422  bad request fields / unpostable proposal
- ``MockLedgerError``            502  failed ledger post (nothing was applied)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.schemas.actions import (
    ActionResponse,
    ApproveRequest,
    RejectRequest,
    RollbackRequest,
)
from app.db.session import get_db
from app.services.actions import (
    ActionValidationError,
    IdempotencyConflictError,
    MockLedgerError,
    ProposalNotFoundError,
    ProposalStateError,
    approve_proposal,
    reject_proposal,
    rollback_proposal,
)

router = APIRouter(prefix="/api/actions", tags=["actions"])

_STATUS_BY_ERROR: tuple[tuple[type[Exception], int], ...] = (
    (ProposalNotFoundError, status.HTTP_404_NOT_FOUND),
    (ProposalStateError, status.HTTP_409_CONFLICT),
    (IdempotencyConflictError, status.HTTP_409_CONFLICT),
    (ActionValidationError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (MockLedgerError, status.HTTP_502_BAD_GATEWAY),
)

_ACTION_ERRORS = tuple(cls for cls, _code in _STATUS_BY_ERROR)


def _as_http(exc: Exception) -> HTTPException:
    """Translate one typed service error into its HTTP counterpart."""
    for cls, code in _STATUS_BY_ERROR:
        if isinstance(exc, cls):
            return HTTPException(status_code=code, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/{proposal_id}/approve", response_model=ActionResponse)
def approve(
    proposal_id: str,
    request: ApproveRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ActionResponse:
    """Human approval: post the proposed correction to the mock ledger."""
    try:
        result = approve_proposal(
            db,
            proposal_id,
            approver=request.approver,
            idempotency_key=request.idempotency_key,
            note=request.note,
            simulate_failure=request.simulate_failure,
        )
    except _ACTION_ERRORS as exc:
        raise _as_http(exc) from exc
    return ActionResponse(**result)


@router.post("/{proposal_id}/reject", response_model=ActionResponse)
def reject(
    proposal_id: str,
    request: RejectRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ActionResponse:
    """Human rejection: record the decision; the ledger is never touched."""
    try:
        result = reject_proposal(
            db,
            proposal_id,
            approver=request.approver,
            idempotency_key=request.idempotency_key,
            note=request.note,
        )
    except _ACTION_ERRORS as exc:
        raise _as_http(exc) from exc
    return ActionResponse(**result)


@router.post("/{proposal_id}/rollback", response_model=ActionResponse)
def rollback(
    proposal_id: str,
    request: RollbackRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ActionResponse:
    """Roll back an approved post: the entry flips to 'reversed'."""
    try:
        result = rollback_proposal(
            db,
            proposal_id,
            approver=request.approver,
            idempotency_key=request.idempotency_key,
            note=request.note,
        )
    except _ACTION_ERRORS as exc:
        raise _as_http(exc) from exc
    return ActionResponse(**result)
