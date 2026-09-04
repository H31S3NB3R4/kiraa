"""Response schemas for the audit-trail endpoint (Phase 9).

``GET /api/audit`` reads the append-only ``audit_events`` trail written by
the Phase 8 action service — every human decision, post, and rollback,
with before/after states (FR-9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditEventRow(BaseModel):
    """One append-only audit event."""

    event_id: str
    actor: str
    action: str
    object_type: str
    object_id: str
    agent_run_id: str | None = None
    before_state: dict[str, Any] = Field(default_factory=dict)
    after_state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AuditListResponse(BaseModel):
    """``GET /api/audit`` response body (list envelope convention)."""

    count: int = 0
    limit: int | None = None
    truncated: bool = False
    filters: dict[str, Any] = Field(default_factory=dict)
    rows: list[AuditEventRow] = Field(default_factory=list)
