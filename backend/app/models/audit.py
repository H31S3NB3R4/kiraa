"""Action/audit models: journal proposals, approvals, audit events.

These tables enforce the human-approval safety gate: a proposal only
mutates the ledger after an approval record exists, and every state change
is captured in `audit_events`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.master_data import Money


class JournalProposal(TimestampMixin, Base):
    """Agent-proposed journal entry awaiting human review (Phase 10)."""

    __tablename__ = "journal_proposals"
    __table_args__ = (
        Index("ix_journal_proposals_status_created", "status", "created_at"),
    )

    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.run_id"), index=True
    )
    transaction_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("transactions.transaction_id"), index=True
    )
    entry_date: Mapped[datetime | None] = mapped_column(DateTime)
    debit_account: Mapped[str] = mapped_column(String(64), nullable=False)
    credit_account: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[float] = mapped_column(Money, nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    def __repr__(self) -> str:
        return f"<JournalProposal {self.proposal_id} {self.amount} {self.status}>"


class Approval(TimestampMixin, Base):
    """Human decision on a journal proposal (Phase 11)."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("journal_proposals.proposal_id"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # approved | rejected
    approver: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Approval {self.proposal_id} {self.decision}>"


class AuditEvent(TimestampMixin, Base):
    """Append-only record of every significant controller action (Phase 11)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_created", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.run_id"), index=True
    )
    before_state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    after_state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    def __repr__(self) -> str:
        return f"<AuditEvent {self.event_id} {self.action}>"
