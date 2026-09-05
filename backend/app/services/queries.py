"""Read-only query services for the Phase 9/10 API surface.

These functions exist so the routes stay thin (the Phase 8 convention):
filters, ordering, and serialization live here, unit-testable without
FastAPI. Everything in this module is strictly read-only.

- ``list_exceptions``    persisted ``reconciliation_exceptions`` rows,
                         joined to their transaction (merchant scope)
- ``get_run_detail``    one agent run with its tool calls and transcript
- ``list_audit_events`` the append-only ``audit_events`` trail
- ``list_proposals``    journal proposals pending/decided (Action view)
- ``list_runs``         agent run history (Audit view)
- ``list_merchants``    merchant master rows (dashboard selector)

Row caps mirror the ledger-tool convention: fetch ``limit + 1`` rows and
report ``truncated`` when the extra row existed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AgentMessage,
    AgentRun,
    AuditEvent,
    JournalProposal,
    Merchant,
    ReconciliationException,
    ToolCall,
    Transaction,
)
from app.tools.common import coerce_date, round2

__all__ = [
    "DEFAULT_LIMIT",
    "RunNotFoundError",
    "get_run_detail",
    "list_audit_events",
    "list_exceptions",
    "list_merchants",
    "list_proposals",
    "list_runs",
]

DEFAULT_LIMIT = 500


class RunNotFoundError(LookupError):
    """No agent run exists for the given run id (HTTP 404)."""


def list_exceptions(
    db: Session,
    *,
    merchant_id: str | None = None,
    exception_type: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    transaction_id: str | None = None,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    limit: int | None = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List persisted reconciliation exceptions, newest first.

    ``merchant_id`` scopes through the linked transaction (the row itself
    carries no merchant column); the other filters map onto the row's own
    columns. ``limit`` caps the rows (``None`` disables the cap).
    """
    start = coerce_date(start_date)
    end = coerce_date(end_date)
    if start is not None and end is not None and start > end:
        raise ValueError("start_date must not be after end_date")

    stmt = select(ReconciliationException, Transaction).join(
        Transaction,
        ReconciliationException.transaction_id == Transaction.transaction_id,
    )
    if merchant_id is not None:
        stmt = stmt.where(Transaction.merchant_id == merchant_id)
    if exception_type is not None:
        stmt = stmt.where(ReconciliationException.exception_type == exception_type)
    if severity is not None:
        stmt = stmt.where(ReconciliationException.severity == severity)
    if status is not None:
        stmt = stmt.where(ReconciliationException.status == status)
    if transaction_id is not None:
        stmt = stmt.where(ReconciliationException.transaction_id == transaction_id)
    if start is not None:
        stmt = stmt.where(ReconciliationException.exception_date >= start)
    if end is not None:
        stmt = stmt.where(ReconciliationException.exception_date <= end)

    # Newest divergence first (the dashboard's exception list).
    stmt = stmt.order_by(
        ReconciliationException.exception_date.desc(),
        ReconciliationException.id.desc(),
    )
    if limit is not None:
        stmt = stmt.limit(limit + 1)

    rows = [_exception_row(exc, txn) for exc, txn in db.execute(stmt)]
    truncated = limit is not None and len(rows) > limit
    if truncated:
        rows = rows[:limit]

    return {
        "count": len(rows),
        "limit": limit,
        "truncated": truncated,
        "filters": {
            "merchant_id": merchant_id,
            "exception_type": exception_type,
            "severity": severity,
            "status": status,
            "transaction_id": transaction_id,
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
        },
        "rows": rows,
    }


def _exception_row(
    exc: ReconciliationException, txn: Transaction
) -> dict[str, Any]:
    """Serialize one persisted exception with its merchant scope."""
    return {
        "exception_id": exc.id,
        "transaction_id": exc.transaction_id,
        "merchant_id": txn.merchant_id,
        "exception_date": exc.exception_date,
        "exception_type": exc.exception_type,
        "severity": exc.severity,
        "expected_amount": round2(exc.expected_amount),
        "recorded_amount": round2(exc.recorded_amount),
        "financial_impact": round2(exc.financial_impact),
        "description": exc.description,
        "status": exc.status,
    }


def get_run_detail(db: Session, run_id: str) -> dict[str, Any]:
    """Fetch one agent run with its tool calls and transcript.

    Raises ``RunNotFoundError`` when no run row exists — the route maps
    that onto 404, mirroring the chat route's continuation semantics.
    """
    run = db.get(AgentRun, run_id)
    if run is None:
        raise RunNotFoundError(f"run_id {run_id!r} does not exist")

    tool_calls = db.execute(
        select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.seq)
    ).scalars().all()
    messages = db.execute(
        select(AgentMessage)
        .where(AgentMessage.run_id == run_id)
        .order_by(AgentMessage.seq)
    ).scalars().all()

    return {
        "run_id": run.run_id,
        "user_query": run.user_query,
        "status": run.status,
        "turn_count": run.turn_count,
        "tool_call_count": run.tool_call_count,
        "total_llm_latency_ms": float(run.total_llm_latency_ms),
        "error": run.error,
        "final_response": run.final_response,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "tool_calls": [
            {
                "seq": call.seq,
                "tool_name": call.tool_name,
                "arguments": dict(call.arguments),
                "result": dict(call.result),
                "status": call.status,
                "error": call.error,
                "latency_ms": float(call.latency_ms),
            }
            for call in tool_calls
        ],
        "messages": [
            {"seq": message.seq, "role": message.role, "content": dict(message.content)}
            for message in messages
        ],
    }


def list_audit_events(
    db: Session,
    *,
    action: str | None = None,
    actor: str | None = None,
    object_id: str | None = None,
    agent_run_id: str | None = None,
    limit: int | None = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List audit events, newest first (append-only trail, FR-9)."""
    stmt = select(AuditEvent)
    if action is not None:
        stmt = stmt.where(AuditEvent.action == action)
    if actor is not None:
        stmt = stmt.where(AuditEvent.actor == actor)
    if object_id is not None:
        stmt = stmt.where(AuditEvent.object_id == object_id)
    if agent_run_id is not None:
        stmt = stmt.where(AuditEvent.agent_run_id == agent_run_id)

    stmt = stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.event_id.desc())
    if limit is not None:
        stmt = stmt.limit(limit + 1)

    events = [_audit_row(event) for event in db.execute(stmt).scalars()]
    truncated = limit is not None and len(events) > limit
    if truncated:
        events = events[:limit]

    return {
        "count": len(events),
        "limit": limit,
        "truncated": truncated,
        "filters": {
            "action": action,
            "actor": actor,
            "object_id": object_id,
            "agent_run_id": agent_run_id,
        },
        "rows": events,
    }


def _audit_row(event: AuditEvent) -> dict[str, Any]:
    """Serialize one audit event with its before/after states."""
    return {
        "event_id": event.event_id,
        "actor": event.actor,
        "action": event.action,
        "object_type": event.object_type,
        "object_id": event.object_id,
        "agent_run_id": event.agent_run_id,
        "before_state": dict(event.before_state),
        "after_state": dict(event.after_state),
        "created_at": event.created_at,
    }


def list_proposals(
    db: Session,
    *,
    status: str | None = None,
    merchant_id: str | None = None,
    transaction_id: str | None = None,
    limit: int | None = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List journal proposals, newest first (the Action view's queue).

    ``merchant_id`` scopes through the linked transaction. Status filter
    accepts the proposal lifecycle states (``pending`` / ``approved`` /
    ``rejected`` / ``rolled_back``); an unknown status simply matches no
    rows (the route validates the enum instead).
    """
    stmt = select(JournalProposal, Transaction, Merchant).join(
        Transaction,
        JournalProposal.transaction_id == Transaction.transaction_id,
    ).join(
        Merchant,
        Transaction.merchant_id == Merchant.merchant_id,
    )
    if status is not None:
        stmt = stmt.where(JournalProposal.status == status)
    if merchant_id is not None:
        stmt = stmt.where(Transaction.merchant_id == merchant_id)
    if transaction_id is not None:
        stmt = stmt.where(JournalProposal.transaction_id == transaction_id)

    stmt = stmt.order_by(
        JournalProposal.created_at.desc(), JournalProposal.proposal_id.desc()
    )
    if limit is not None:
        stmt = stmt.limit(limit + 1)

    rows = [_proposal_row(p, t, m) for p, t, m in db.execute(stmt)]
    truncated = limit is not None and len(rows) > limit
    if truncated:
        rows = rows[:limit]

    return {
        "count": len(rows),
        "limit": limit,
        "truncated": truncated,
        "filters": {
            "status": status,
            "merchant_id": merchant_id,
            "transaction_id": transaction_id,
        },
        "rows": rows,
    }


def _proposal_row(
    proposal: JournalProposal,
    transaction: Transaction,
    merchant: Merchant,
) -> dict[str, Any]:
    """Serialize one proposal joined to its transaction and merchant."""
    return {
        "proposal_id": proposal.proposal_id,
        "agent_run_id": proposal.agent_run_id,
        "transaction_id": proposal.transaction_id,
        "merchant_id": transaction.merchant_id,
        "merchant_name": merchant.name,
        "entry_date": proposal.entry_date,
        "debit_account": proposal.debit_account,
        "credit_account": proposal.credit_account,
        "amount": round2(proposal.amount),
        "narrative": proposal.narrative,
        "evidence_ids": list(proposal.evidence_ids),
        "confidence": float(proposal.confidence),
        "status": proposal.status,
        "created_at": proposal.created_at,
    }


def list_runs(
    db: Session,
    *,
    status: str | None = None,
    limit: int | None = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List agent runs, newest first (the Audit view's run history)."""
    stmt = select(AgentRun)
    if status is not None:
        stmt = stmt.where(AgentRun.status == status)
    stmt = stmt.order_by(AgentRun.started_at.desc(), AgentRun.run_id.desc())
    if limit is not None:
        stmt = stmt.limit(limit + 1)

    runs = [_run_row(run) for run in db.execute(stmt).scalars()]
    truncated = limit is not None and len(runs) > limit
    if truncated:
        runs = runs[:limit]

    return {
        "count": len(runs),
        "limit": limit,
        "truncated": truncated,
        "filters": {"status": status},
        "rows": runs,
    }


def _run_row(run: AgentRun) -> dict[str, Any]:
    """Serialize one agent-run summary row (no transcript payload)."""
    return {
        "run_id": run.run_id,
        "user_query": run.user_query,
        "status": run.status,
        "turn_count": run.turn_count,
        "tool_call_count": run.tool_call_count,
    }


def list_merchants(db: Session) -> dict[str, Any]:
    """List merchants (the dashboard's selector); ordered by merchant id."""
    stmt = select(Merchant).order_by(Merchant.merchant_id)
    merchants = [
        {
            "merchant_id": m.merchant_id,
            "name": m.name,
            "category": m.category,
            "currency": m.currency,
        }
        for m in db.execute(stmt).scalars()
    ]
    return {"count": len(merchants), "rows": merchants}

