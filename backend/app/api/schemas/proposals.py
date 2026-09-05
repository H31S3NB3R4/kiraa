"""Response schemas for the journal-proposal listing endpoint (Phase 10).

``GET /api/proposals`` feeds the dashboard's Action view: the pending
queue awaiting human review plus the already-decided history, joined to
their transaction and merchant for scoping/labels. The approve/reject/
rollback writes stay on the Phase 8 action routes — this listing is
strictly read-only.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class ProposalRow(BaseModel):
    """One journal proposal with its source references."""

    proposal_id: str
    agent_run_id: str | None = None
    transaction_id: str | None = None
    merchant_id: str | None = None
    merchant_name: str | None = None
    entry_date: date | None = None
    debit_account: str
    credit_account: str
    amount: float
    narrative: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float
    status: str
    created_at: datetime


class ProposalsListResponse(BaseModel):
    """``GET /api/proposals`` response body (list envelope convention)."""

    count: int = 0
    limit: int | None = None
    truncated: bool = False
    filters: dict[str, Any] = Field(default_factory=dict)
    rows: list[ProposalRow] = Field(default_factory=list)