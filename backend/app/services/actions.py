"""Action layer service (Phase 8, PRD FR-7/8/9, architecture section 10).

The safe write path: a pending ``journal_proposals`` row only reaches the
ledger after an explicit human decision, and every decision, post, and
rollback is audited. The functions here are the *only* code that writes
ledger entries outside the dataset seeder:

- ``approve_proposal``   pending -> approved. Re-validates the proposal
  server-side, posts exactly one correction ``LedgerEntry`` through the
  mock ledger (``status='posted'``; the ``LE-MOCK-`` id prefix can never
  collide with the generator's seeded ``LE-3xxx`` sequence), records the
  ``Approval`` with the request's idempotency key and the posted entry id,
  and appends a ``proposal.approve`` audit event. Processing the same key
  twice replays the stored outcome instead of posting a second entry (PRD
  section 14); re-deciding with a different key is refused, so a duplicate
  approval can never double-post.
- ``reject_proposal``    pending -> rejected. Never touches the ledger;
  records the decision and a ``proposal.reject`` audit event.
- ``rollback_proposal`` approved -> rolled_back. The PRD section-15
  rollback path: flips the posted correction entry to ``status='reversed'``
  (append-only — the row stays queryable), marks the proposal
  ``rolled_back``, and appends a ``proposal.rollback`` audit event that
  carries the request's idempotency key so duplicate rollbacks replay.

``simulate_failure=True`` on approve exercises the failure branch of
architecture section 10: the whole write rolls back (no approval row, no
ledger entry, no audit event) and the same idempotency key can be retried.

All failures raise typed exceptions that the route layer maps onto HTTP
status codes, so this module stays unit-testable without FastAPI.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Approval, AuditEvent, JournalProposal, LedgerEntry, Transaction
from app.tools.common import round2

__all__ = [
    "ACTION_APPROVE",
    "ACTION_REJECT",
    "ACTION_ROLLBACK",
    "MOCK_ENTRY_PREFIX",
    "ActionValidationError",
    "IdempotencyConflictError",
    "MockLedgerError",
    "ProposalNotFoundError",
    "ProposalStateError",
    "approve_proposal",
    "reject_proposal",
    "rollback_proposal",
]

# Proposal lifecycle states written by this service.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_ROLLED_BACK = "rolled_back"

# Ledger-entry states written by this service.
ENTRY_POSTED = "posted"
ENTRY_REVERSED = "reversed"

# Audit actions (one per state-changing operation, PRD FR-9).
ACTION_APPROVE = "proposal.approve"
ACTION_REJECT = "proposal.reject"
ACTION_ROLLBACK = "proposal.rollback"

# Mock-posted correction entries carry this id prefix so they can never
# collide with the generator-seeded `LE-3xxx` sequence (see
# `dataset_generator._LE_START`) or any future reseeding.
MOCK_ENTRY_PREFIX = "LE-MOCK-"


class ProposalNotFoundError(Exception):
    """No journal proposal exists for the given id (HTTP 404)."""


class ProposalStateError(Exception):
    """The proposal's lifecycle state forbids this action (HTTP 409)."""


class IdempotencyConflictError(Exception):
    """The idempotency key was already used for a different write (HTTP 409)."""


class ActionValidationError(Exception):
    """Bad request fields or an unpostable proposal (HTTP 422)."""


class MockLedgerError(Exception):
    """The mock ledger post failed; nothing was applied (HTTP 502)."""


def _require_text(value: object, field: str, max_len: int) -> str:
    """Validate one free-text request field (``ActionValidationError`` on bad)."""
    if not isinstance(value, str) or not value.strip():
        raise ActionValidationError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > max_len:
        raise ActionValidationError(f"{field} must be at most {max_len} characters")
    return text


def _load_proposal(db: Session, proposal_id: str) -> JournalProposal:
    """Fetch the proposal or raise the typed 404 error."""
    proposal = db.get(JournalProposal, proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(f"no journal proposal {proposal_id!r} exists")
    return proposal


def _prior_decision(db: Session, idempotency_key: str) -> Approval | None:
    """Return the decision already recorded under ``idempotency_key``, if any."""
    return (
        db.execute(
            select(Approval).where(Approval.idempotency_key == idempotency_key)
        )
        .scalars()
        .first()
    )


def _entry_date(proposal: JournalProposal, txn: Transaction) -> date:
    """Resolve the correction entry's posting date (datetime first: subclass)."""
    value = proposal.entry_date
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is not None:
        return date.fromisoformat(str(value))
    return txn.timestamp.date()


def _audit_event(
    db: Session,
    *,
    actor: str,
    action: str,
    proposal: JournalProposal,
    before_state: dict[str, Any],
    after_state: dict[str, Any],
) -> AuditEvent:
    """Append one audit row describing a state-changing action (PRD FR-9)."""
    event = AuditEvent(
        event_id=str(uuid4()),
        actor=actor,
        action=action,
        object_type="journal_proposal",
        object_id=proposal.proposal_id,
        agent_run_id=proposal.agent_run_id,
        before_state=before_state,
        after_state=after_state,
    )
    db.add(event)
    return event


def _replay_event(
    db: Session,
    *,
    action: str,
    approver: str,
    proposal: JournalProposal,
    idempotency_key: str,
    prior: Approval,
) -> AuditEvent:
    """Audit marker for a duplicate request answered from its stored outcome.

    No approval row is added and the ledger is untouched; the event exists
    so the trail still shows who retried the write and when.
    """
    return _audit_event(
        db,
        actor=approver,
        action=action,
        proposal=proposal,
        before_state={},
        after_state={
            "idempotent_replay": True,
            "idempotency_key": idempotency_key,
            "recorded_decision": prior.decision,
            "ledger_entry_id": prior.ledger_entry_id,
        },
    )


def _replay_result(
    proposal_id: str,
    status: str,
    decision: str | None,
    ledger_entry_id: str | None,
    audit_event_id: str,
    message: str,
) -> dict[str, Any]:
    """Build the response for a duplicate request (stored outcome echoed)."""
    return {
        "proposal_id": proposal_id,
        "status": status,
        "decision": decision,
        "ledger_entry_id": ledger_entry_id,
        "idempotent_replay": True,
        "audit_event_id": audit_event_id,
        "message": message,
    }


def _commit_or_conflict(db: Session, idempotency_key: str) -> None:
    """Commit one atomic action write; a unique-key race becomes a 409."""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise IdempotencyConflictError(
            f"idempotency key {idempotency_key!r} was already used by another write"
        ) from None


def approve_proposal(
    db: Session,
    proposal_id: str,
    *,
    approver: str,
    idempotency_key: str,
    note: str | None = None,
    simulate_failure: bool = False,
) -> dict[str, Any]:
    """Human approval: post the correction to the mock ledger (architecture 10).

    One atomic transaction writes the ``LedgerEntry`` (``status='posted'``),
    the ``Approval`` row carrying the request's idempotency key, the
    proposal's new ``approved`` status, and the ``proposal.approve`` audit
    event. A duplicate request with the same key replays the stored
    outcome; a different key on a decided proposal is refused, so a
    duplicate approval can never double-post (PRD section 14).
    """
    proposal_id = _require_text(proposal_id, "proposal_id", 36)
    approver = _require_text(approver, "approver", 128)
    idempotency_key = _require_text(idempotency_key, "idempotency_key", 64)

    prior = _prior_decision(db, idempotency_key)
    if prior is not None:
        if prior.proposal_id != proposal_id or prior.decision != "approved":
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} was already used for the "
                f"{prior.decision} of proposal {prior.proposal_id!r}"
            )
        proposal = _load_proposal(db, proposal_id)
        event = _replay_event(
            db, action=ACTION_APPROVE, approver=approver, proposal=proposal,
            idempotency_key=idempotency_key, prior=prior,
        )
        db.commit()
        return _replay_result(
            proposal_id, STATUS_APPROVED, "approved", prior.ledger_entry_id,
            event.event_id,
            f"Idempotent replay: proposal {proposal_id} was already approved "
            f"(ledger entry {prior.ledger_entry_id}); no second post was made.",
        )

    proposal = _load_proposal(db, proposal_id)
    if proposal.status != STATUS_PENDING:
        raise ProposalStateError(
            f"proposal {proposal_id} is already {proposal.status}; "
            "only pending proposals can be approved"
        )

    # Server-side payload validation (todo Phase 14): amount and accounts are
    # re-checked here — never trusted from a client or a model payload.
    amount = round2(proposal.amount)
    if amount <= 0:
        raise ActionValidationError(
            f"proposal {proposal_id} has a non-positive amount {amount}; refusing to post"
        )
    debit = (proposal.debit_account or "").strip()
    credit = (proposal.credit_account or "").strip()
    if not debit or not credit:
        raise ActionValidationError(
            "proposal must name both a debit and a credit account"
        )
    if debit == credit:
        raise ActionValidationError("debit and credit accounts must differ")
    txn = db.get(Transaction, proposal.transaction_id) if proposal.transaction_id else None
    if txn is None:
        raise ActionValidationError(
            "proposal has no linked transaction; the merchant scope of the "
            "ledger post cannot be determined"
        )

    if simulate_failure:
        # Architecture section 10, failure branch: raise before anything is
        # written so nothing is applied and the same key can be retried.
        raise MockLedgerError(
            f"simulated mock-ledger failure for proposal {proposal_id}: "
            "nothing was applied; retry with the same idempotency key"
        )

    entry = LedgerEntry(
        entry_id=f"{MOCK_ENTRY_PREFIX}{uuid4().hex[:12]}",
        transaction_id=txn.transaction_id,
        merchant_id=txn.merchant_id,
        entry_date=_entry_date(proposal, txn),
        debit_account=debit,
        credit_account=credit,
        amount=amount,
        status=ENTRY_POSTED,
        description=(
            f"Correction for {txn.transaction_id}: "
            f"{proposal.narrative or 'approved journal proposal'}"
        )[:256],
    )
    db.add(entry)

    db.add(
        Approval(
            proposal_id=proposal.proposal_id,
            decision="approved",
            approver=approver,
            decided_at=datetime.now(),
            note=note,
            idempotency_key=idempotency_key,
            ledger_entry_id=entry.entry_id,
        )
    )

    proposal.status = STATUS_APPROVED

    event = _audit_event(
        db, actor=approver, action=ACTION_APPROVE, proposal=proposal,
        before_state={"status": STATUS_PENDING},
        after_state={
            "status": STATUS_APPROVED,
            "idempotency_key": idempotency_key,
            "ledger_entry_id": entry.entry_id,
            "amount": amount,
            "debit_account": debit,
            "credit_account": credit,
        },
    )
    _commit_or_conflict(db, idempotency_key)

    return {
        "proposal_id": proposal_id,
        "status": STATUS_APPROVED,
        "decision": "approved",
        "ledger_entry_id": entry.entry_id,
        "idempotent_replay": False,
        "audit_event_id": event.event_id,
        "message": (
            f"Approved proposal {proposal_id}: posted {amount:,.2f} "
            f"from {debit} to {credit} to the mock ledger ({entry.entry_id})."
        ),
    }


def reject_proposal(
    db: Session,
    proposal_id: str,
    *,
    approver: str,
    idempotency_key: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Human rejection: record the decision; the ledger is never touched."""
    proposal_id = _require_text(proposal_id, "proposal_id", 36)
    approver = _require_text(approver, "approver", 128)
    idempotency_key = _require_text(idempotency_key, "idempotency_key", 64)

    prior = _prior_decision(db, idempotency_key)
    if prior is not None:
        if prior.proposal_id != proposal_id or prior.decision != "rejected":
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} was already used for the "
                f"{prior.decision} of proposal {prior.proposal_id!r}"
            )
        proposal = _load_proposal(db, proposal_id)
        event = _replay_event(
            db, action=ACTION_REJECT, approver=approver, proposal=proposal,
            idempotency_key=idempotency_key, prior=prior,
        )
        db.commit()
        return _replay_result(
            proposal_id, STATUS_REJECTED, "rejected", None, event.event_id,
            f"Idempotent replay: proposal {proposal_id} was already rejected.",
        )

    proposal = _load_proposal(db, proposal_id)
    if proposal.status != STATUS_PENDING:
        raise ProposalStateError(
            f"proposal {proposal_id} is already {proposal.status}; "
            "only pending proposals can be rejected"
        )

    db.add(
        Approval(
            proposal_id=proposal.proposal_id,
            decision="rejected",
            approver=approver,
            decided_at=datetime.now(),
            note=note,
            idempotency_key=idempotency_key,
        )
    )
    proposal.status = STATUS_REJECTED

    event = _audit_event(
        db, actor=approver, action=ACTION_REJECT, proposal=proposal,
        before_state={"status": STATUS_PENDING},
        after_state={
            "status": STATUS_REJECTED,
            "idempotency_key": idempotency_key,
            **({"note": note} if note else {}),
        },
    )
    _commit_or_conflict(db, idempotency_key)

    return {
        "proposal_id": proposal_id,
        "status": STATUS_REJECTED,
        "decision": "rejected",
        "ledger_entry_id": None,
        "idempotent_replay": False,
        "audit_event_id": event.event_id,
        "message": f"Rejected proposal {proposal_id}; the ledger was not modified.",
    }


def _prior_rollback_event(
    db: Session, idempotency_key: str
) -> AuditEvent | None:
    """Return the rollback audit event recorded under ``idempotency_key``.

    Rollbacks record no ``Approval`` row (the human decision was the
    earlier approval); their idempotency keys live on the audit event.
    The lookup is global — like approve/reject, one key may only ever
    cover a single write across all proposals.
    """
    events = (
        db.execute(
            select(AuditEvent).where(AuditEvent.action == ACTION_ROLLBACK)
        )
        .scalars()
        .all()
    )
    for event in events:
        if event.after_state.get("idempotency_key") == idempotency_key:
            return event
    return None


def rollback_proposal(
    db: Session,
    proposal_id: str,
    *,
    approver: str,
    idempotency_key: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Roll back an approved post (PRD section 15 rollback path).

    The posted correction entry flips to ``status='reversed'`` (append-only:
    the row stays queryable), the proposal becomes ``rolled_back``, and a
    ``proposal.rollback`` audit event records both sides. Duplicate requests
    replay; any other action on a terminal proposal is refused.
    """
    proposal_id = _require_text(proposal_id, "proposal_id", 36)
    approver = _require_text(approver, "approver", 128)
    idempotency_key = _require_text(idempotency_key, "idempotency_key", 64)

    prior = _prior_decision(db, idempotency_key)
    if prior is not None:
        # The key was spent on a decision (approve/reject), never on a
        # rollback — a rollback is a distinct write and must carry its own.
        raise IdempotencyConflictError(
            f"idempotency key {idempotency_key!r} was already used for the "
            f"{prior.decision} of proposal {prior.proposal_id!r}"
        )

    proposal = _load_proposal(db, proposal_id)
    prior_rollback = _prior_rollback_event(db, idempotency_key)
    if prior_rollback is not None:
        if prior_rollback.object_id != proposal_id:
            # The key already covered another proposal's rollback; it must
            # never be reused for a second write (PRD section 14).
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} was already used for the "
                f"rollback of proposal {prior_rollback.object_id!r}"
            )
        # Duplicate rollback: replay the stored outcome; the entry stays
        # reversed and nothing is written a second time (PRD section 14).
        event = _audit_event(
            db, actor=approver, action=ACTION_ROLLBACK, proposal=proposal,
            before_state={},
            after_state={
                "idempotent_replay": True,
                "idempotency_key": idempotency_key,
                "ledger_entry_id": prior_rollback.after_state.get("ledger_entry_id"),
            },
        )
        db.commit()
        return _replay_result(
            proposal_id, STATUS_ROLLED_BACK, None,
            prior_rollback.after_state.get("ledger_entry_id"),
            event.event_id,
            f"Idempotent replay: proposal {proposal_id} was already rolled "
            "back; the correction entry remains reversed.",
        )

    if proposal.status == STATUS_ROLLED_BACK:
        raise ProposalStateError(
            f"proposal {proposal_id} is already rolled_back"
        )
    if proposal.status != STATUS_APPROVED:
        raise ProposalStateError(
            f"only approved proposals can be rolled back "
            f"(proposal {proposal_id} is {proposal.status})"
        )

    approval = db.execute(
        select(Approval).where(
            Approval.proposal_id == proposal_id,
            Approval.decision == "approved",
        )
    ).scalars().one_or_none()
    if approval is None or approval.ledger_entry_id is None:
        raise ProposalStateError(
            f"proposal {proposal_id} has no posted ledger entry to roll back"
        )
    entry = db.get(LedgerEntry, approval.ledger_entry_id)
    if entry is None:
        raise ProposalStateError(
            f"ledger entry {approval.ledger_entry_id} for proposal "
            f"{proposal_id} is missing"
        )

    entry.status = ENTRY_REVERSED
    proposal.status = STATUS_ROLLED_BACK

    event = _audit_event(
        db, actor=approver, action=ACTION_ROLLBACK, proposal=proposal,
        before_state={
            "status": STATUS_APPROVED,
            "ledger_entry_id": entry.entry_id,
            "entry_status": ENTRY_POSTED,
        },
        after_state={
            "status": STATUS_ROLLED_BACK,
            "ledger_entry_id": entry.entry_id,
            "entry_status": ENTRY_REVERSED,
            "idempotency_key": idempotency_key,
            **({"note": note} if note else {}),
        },
    )
    _commit_or_conflict(db, idempotency_key)

    return {
        "proposal_id": proposal_id,
        "status": STATUS_ROLLED_BACK,
        "decision": None,
        "ledger_entry_id": entry.entry_id,
        "idempotent_replay": False,
        "audit_event_id": event.event_id,
        "message": (
            f"Rolled back proposal {proposal_id}: ledger entry "
            f"{entry.entry_id} is marked reversed (append-only; the row "
            "remains queryable)."
        ),
    }





