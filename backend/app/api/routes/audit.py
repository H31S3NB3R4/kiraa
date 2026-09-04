"""Audit route (Phase 9, PRD section 16, architecture section 13).

``GET /api/audit`` reads the append-only ``audit_events`` trail written by
the Phase 8 action service (FR-9): every human decision, post, rollback,
and idempotent replay with before/after states. Filters cover action,
actor, object (proposal), and the linked agent run; newest first.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from sqlalchemy.orm import Session

from app.api.schemas.audit import AuditListResponse
from app.db.session import get_db
from app.services.queries import list_audit_events

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=AuditListResponse)
def audit(
    db: Annotated[Session, Depends(get_db)],
    action: str | None = None,
    actor: str | None = None,
    object_id: str | None = None,
    agent_run_id: str | None = None,
    limit: int | None = 500,
) -> AuditListResponse:
    """List audit events, newest first."""
    return AuditListResponse(
        **list_audit_events(
            db,
            action=action,
            actor=actor,
            object_id=object_id,
            agent_run_id=agent_run_id,
            limit=limit,
        )
    )
