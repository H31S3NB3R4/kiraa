"""Journal-proposal tool (Phase 6, PRD FR-7 — PROPOSE permission class).

``propose_journal_entry`` drafts a structured correction for a *verified*
reconciliation exception. It is the only model-callable tool that creates
state, and what it creates is a pending ``journal_proposals`` row — the
human-approval gate (Phase 8/11) is the only path that can post it. The
tool therefore never mutates the ledger (architecture section 5: PROPOSE,
not WRITE), and its payload always states ``posted=False`` and
``requires_approval=True`` so the model can never claim money moved.

Correction semantics (deterministic, two-account demo convention):

- ``merchant_owed``  the merchant is owed value the books do not reflect:
  debit ``Bank - Settlement Account`` / credit ``Sales Revenue``.
  Applies to MISSING_SETTLEMENT, FEE_MISMATCH, REFUND_MISMATCH,
  GST_MISMATCH, SETTLEMENT_TIMING_MISMATCH, and FAILED_LEDGER_WRITE.
- ``reversal``       recorded value should not stand:
  debit ``Sales Revenue`` / credit ``Bank - Settlement Account``.
  Applies to DUPLICATE_TRANSACTION (the duplicate charge is owed back).
- sign-based         AMOUNT_MISMATCH and LEDGER_MISMATCH follow the sign of
  ``financial_impact`` (negative -> merchant_owed, positive -> reversal).

The proposal amount is ``abs(financial_impact)`` (round2), confidence is
derived deterministically from the exception severity, and evidence ids
reference the transaction and its settlement/invoice records. Re-proposing
the same correction while a pending proposal exists returns the existing
row (idempotency, PRD section 14) instead of duplicating it.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JournalProposal, ReconciliationException, Transaction
from app.tools.common import round2

__all__ = ["propose_journal_entry"]

BANK_ACCOUNT = "Bank - Settlement Account"
REVENUE_ACCOUNT = "Sales Revenue"

_SEVERITY_CONFIDENCE: dict[str, float] = {"high": 0.95, "medium": 0.80, "low": 0.60}

_DIRECTION_BY_TYPE: dict[str, str] = {
    "MISSING_SETTLEMENT": "merchant_owed",
    "FEE_MISMATCH": "merchant_owed",
    "REFUND_MISMATCH": "merchant_owed",
    "GST_MISMATCH": "merchant_owed",
    "SETTLEMENT_TIMING_MISMATCH": "merchant_owed",
    "FAILED_LEDGER_WRITE": "merchant_owed",
    "DUPLICATE_TRANSACTION": "reversal",
}


def _correction_direction(exception_type: str, impact: float) -> str:
    """Decide debit/credit direction for one exception type deterministically."""
    rule = _DIRECTION_BY_TYPE.get(exception_type, "by_sign")
    if rule == "by_sign":
        return "merchant_owed" if impact < 0 else "reversal"
    return rule


def _envelope(status: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool": "propose_journal_entry", "status": status}
    payload.update(extra)
    return payload
def propose_journal_entry(
    db: Session,
    exception_id: int | str,
    reason: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Draft a pending journal-entry proposal from a verified exception (FR-7)."""
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason:
        raise ValueError("reason must be a non-empty string")

    try:
        exc_id = int(str(exception_id).strip())
    except (TypeError, ValueError):
        return _envelope(
            "invalid_exception_id",
            exception_id=exception_id,
            message="exception_id must be the numeric id of a persisted reconciliation exception",
        )

    exception = db.get(ReconciliationException, exc_id)
    if exception is None:
        return _envelope(
            "not_found",
            exception_id=exception_id,
            sources={"exception_id": exception_id},
        )

    impact = round2(exception.financial_impact)
    amount = abs(impact)
    if amount == 0:
        return _envelope(
            "no_financial_impact",
            exception_id=exc_id,
            transaction_id=exception.transaction_id,
            financial_impact=impact,
            message="exception carries zero financial impact; nothing to propose",
        )

    direction = _correction_direction(exception.exception_type, impact)
    debit, credit = (
        (BANK_ACCOUNT, REVENUE_ACCOUNT)
        if direction == "merchant_owed"
        else (REVENUE_ACCOUNT, BANK_ACCOUNT)
    )

    transaction = db.get(Transaction, exception.transaction_id)
    merchant_id = transaction.merchant_id if transaction is not None else None

    # Idempotency (PRD section 14): one pending proposal per correction.
    existing = db.execute(
        select(JournalProposal).where(
            JournalProposal.transaction_id == exception.transaction_id,
            JournalProposal.debit_account == debit,
            JournalProposal.credit_account == credit,
            JournalProposal.amount == amount,
            JournalProposal.status == "pending",
        )
    ).scalars().first()
    if existing is not None:
        return _envelope(
            "ok",
            proposal=_proposal_payload(existing, exception, merchant_id),
            posted=False,
            requires_approval=True,
            deduplicated=True,
            sources={"exception_id": exc_id, "transaction_id": exception.transaction_id},
        )

    evidence_ids = [exception.transaction_id]
    if transaction is not None:
        evidence_ids.extend(
            ref for ref in (transaction.settlement_id, transaction.invoice_id) if ref
        )

    proposal = JournalProposal(
        proposal_id=str(uuid4()),
        agent_run_id=run_id,
        transaction_id=exception.transaction_id,
        entry_date=exception.exception_date,
        debit_account=debit,
        credit_account=credit,
        amount=amount,
        narrative=(
            f"{exception.exception_type} correction for "
            f"{exception.transaction_id}: {reason}"
        ),
        evidence_ids=evidence_ids,
        confidence=_SEVERITY_CONFIDENCE.get(exception.severity, 0.50),
        status="pending",
    )
    db.add(proposal)
    db.commit()

    return _envelope(
        "ok",
        proposal=_proposal_payload(proposal, exception, merchant_id),
        posted=False,
        requires_approval=True,
        deduplicated=False,
        sources={"exception_id": exc_id, "transaction_id": exception.transaction_id},
    )


def _proposal_payload(
    proposal: JournalProposal,
    exception: ReconciliationException,
    merchant_id: str | None,
) -> dict[str, Any]:
    """Serialize one proposal row (plus its source exception) for the model."""
    return {
        "proposal_id": proposal.proposal_id,
        "exception_id": exception.id,
        "exception_type": exception.exception_type,
        "transaction_id": exception.transaction_id,
        "merchant_id": merchant_id,
        "entry_date": proposal.entry_date.isoformat(),
        "debit_account": proposal.debit_account,
        "credit_account": proposal.credit_account,
        "amount": round2(proposal.amount),
        "financial_impact": round2(exception.financial_impact),
        "narrative": proposal.narrative,
        "evidence_ids": list(proposal.evidence_ids),
        "confidence": float(proposal.confidence),
        "status": proposal.status,
    }