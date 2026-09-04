"""Request/response schemas for the action endpoints (Phase 8).

Mirrors the PRD section-16 / architecture section-13 contracts. Every
write request carries an ``idempotency_key`` (PRD section 14: the same
request processed twice must never create duplicate ledger entries) plus
the acting analyst identifier, which lands on the decision row and the
audit event (PRD FR-9). The approve body can additionally ask the mock
ledger to simulate a posting failure so the failure branch of the safe
write path (architecture section 10) is demonstrable offline.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ActionRequest(BaseModel):
    """Shared body for the ``POST /api/actions/{proposal_id}/...`` writes."""

    idempotency_key: str = Field(
        min_length=8,
        max_length=64,
        description=(
            "Client-generated write key; processing it twice replays the "
            "stored outcome instead of posting again (PRD section 14)."
        ),
    )
    approver: str = Field(
        min_length=1,
        max_length=128,
        description="Acting analyst identifier recorded on the decision and audit event.",
    )
    note: str | None = Field(
        default=None, max_length=1024, description="Optional reviewer note."
    )


class ApproveRequest(ActionRequest):
    """``POST /api/actions/{proposal_id}/approve`` request body."""

    simulate_failure: bool = Field(
        default=False,
        description=(
            "Demo-only hook for the architecture section-10 failure branch: "
            "the post fails, nothing is applied, and the same idempotency "
            "key can be retried."
        ),
    )


class RejectRequest(ActionRequest):
    """``POST /api/actions/{proposal_id}/reject`` request body."""


class RollbackRequest(ActionRequest):
    """``POST /api/actions/{proposal_id}/rollback`` request body."""


class ActionResponse(BaseModel):
    """``POST /api/actions/{proposal_id}/...`` response body.

    ``status`` is the proposal's new lifecycle state (approved / rejected /
    rolled_back); ``decision`` echoes the recorded human decision;
    ``ledger_entry_id`` points at the posted (or reversed) correction entry
    when one exists. ``idempotent_replay`` is true when a duplicate request
    was answered from the stored outcome without posting again — in that
    case ``status``/``ledger_entry_id`` echo what the original request did,
    not the proposal's current lifecycle state.
    """

    proposal_id: str
    status: str
    decision: str | None = None
    ledger_entry_id: str | None = None
    idempotent_replay: bool = False
    audit_event_id: str
    message: str
