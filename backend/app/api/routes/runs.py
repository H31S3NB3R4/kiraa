"""Runs route (Phase 9/10, PRD section 16, architecture section 13).

``GET /api/runs/{run_id}`` returns one conversation run with its tool-call
trace and full transcript — the audit view's run history and tool
sequence. Unknown ids return 404, mirroring the chat route's continuation
semantics (never a silent empty payload).

``GET /api/runs`` (Phase 10) lists run summaries newest-first for the
audit view's run history panel.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.schemas.runs import RunDetailResponse, RunsListResponse
from app.db.session import get_db
from app.services.queries import RunNotFoundError, get_run_detail, list_runs

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", response_model=RunsListResponse)
def runs(
    db: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description="Optional run status, e.g. 'completed'",
        ),
    ] = None,
    limit: int | None = 500,
) -> RunsListResponse:
    """List agent runs, newest first (the audit view's run history)."""
    return RunsListResponse(**list_runs(db, status=status_filter, limit=limit))


@router.get("/{run_id}", response_model=RunDetailResponse)
def run_detail(
    run_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> RunDetailResponse:
    """Fetch one agent run with its tool calls and transcript."""
    try:
        return RunDetailResponse(**get_run_detail(db, run_id))
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
