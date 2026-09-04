"""Phase 8 tests: the action layer — approval, rejection, rollback.

Mirrors the earlier fixture pattern: the dev dataset (seed 42, 100
transactions, 1 exception per type) is seeded into a temp SQLite DB.
Proposals are drafted with the real Phase 6 ``propose_journal_entry`` tool
(same path the agent uses), then decided through the Phase 8 service and
HTTP surface (no network, no API key required):

- **approve** posts exactly one ``LE-MOCK-`` correction entry whose fields
  come from the proposal (accounts, round2 amount, linked transaction,
  entry date, merchant scope), records the approval with the request's
  idempotency key and the posted entry id, and appends a
  ``proposal.approve`` audit event with before/after states,
- **idempotency** (PRD section 14): the same approve/reject request
  replayed returns the stored outcome, adds no second ledger entry, no
  second approval, and one replay-marker audit event; re-deciding a
  decided proposal with a *different* key is refused (409), so duplicate
  approvals can never double-post,
- **reject** records the decision + audit event and never touches the
  ledger; approve-after-reject and reject-after-approve are refused,
- **rollback** (PRD section 15) flips the posted entry to ``reversed``
  (append-only: the row stays queryable), marks the proposal
  ``rolled_back``, audits the transition, replays duplicates, and refuses
  keys spent on other writes or proposals,
- **server-side validation** (todo Phase 14): non-positive amounts, missing
  or identical accounts, and missing linked transactions are refused with
  422 — never trusted from the (model-drafted) proposal payload,
- **the failure branch** (architecture section 10): ``simulate_failure``
  applies nothing (no approval, no ledger entry, no audit event) and the
  same idempotency key succeeds on retry,
- **the safety gate**: no natural-language request can reach the ledger —
  the six model-callable tools contain no action verbs, the registry
  carries no WRITE-class callable, and ``POST /api/agent/chat`` with an
  "approve it" message leaves proposals pending and the ledger untouched,
- **HTTP**: approve/reject/rollback round-trip end-to-end through
  ``TestClient`` with the phase 6/7 ``get_db`` override pattern, and the
  pydantic bodies reject missing/short idempotency keys with 422.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.providers.base import (
    LLMProvider,
    LLMResponse,
    LLMToolCall,
)
from app.agent.tool_registry import TOOL_REGISTRY, dispatch_tool
from app.api.routes.agent import get_provider
from app.db.session import get_db
from app.main import app
from app.models import (
    AgentRun,
    Approval,
    AuditEvent,
    JournalProposal,
    LedgerEntry,
    ReconciliationException,
    Transaction,
)
from app.services.actions import (
    ACTION_APPROVE,
    ACTION_REJECT,
    ACTION_ROLLBACK,
    MOCK_ENTRY_PREFIX,
    ActionValidationError,
    IdempotencyConflictError,
    MockLedgerError,
    ProposalNotFoundError,
    ProposalStateError,
    approve_proposal,
    reject_proposal,
    rollback_proposal,
)
from app.services.dataset_generator import generate_dataset, write_dataset
from app.services.db_seed import build_engine, load_dataset_file, seed_database
from app.tools.common import round2

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1,
       "customers": 80, "seed": 42}
END = date(2026, 9, 3)  # same anchor day as the Phase 2-7 suites

APPROVER = "demo-analyst"
KEY_A = "idem-approve-0001"
KEY_R = "idem-reject-0001"
KEY_RB = "idem-rollback-0001"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate the dev dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase8")
    dataset = generate_dataset(**KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "dataset")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'finance.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    return bundle


@pytest.fixture
def session(seeded) -> Iterator[Session]:
    """One fresh session on the shared module-scoped database."""
    db = Session(seeded.engine)
    try:
        yield db
    finally:
        db.close()


def _create_run(db: Session) -> AgentRun:
    """Insert one agent run row (PROPOSE tools link proposals to it)."""
    run = AgentRun(
        run_id="run-phase8", user_query="test", status="running",
        started_at=datetime.now(),
    )
    db.add(run)
    db.commit()
    return run


def _pending_proposal(db: Session, exception_type: str = "MISSING_SETTLEMENT") -> JournalProposal:
    """Draft one pending proposal through the real Phase 6 tool path."""
    if db.get(AgentRun, "run-phase8") is None:
        _create_run(db)
    result = dispatch_tool(db, "run_reconciliation", {})
    assert result["tool"] == "run_reconciliation"
    assert result["status"] != "error" if "status" in result else True
    exception = db.execute(
        select(ReconciliationException)
        .where(ReconciliationException.exception_type == exception_type)
        .order_by(ReconciliationException.id)
        .limit(1)
    ).scalars().one()
    proposed = dispatch_tool(
        db, "propose_journal_entry",
        {"exception_id": exception.id, "reason": "phase 8 test correction"},
    )
    assert proposed.get("posted") is False, proposed
    assert proposed.get("requires_approval") is True, proposed
    return db.get(JournalProposal, proposed["proposal"]["proposal_id"])


# ---------------------------------------------------------------------------
# Service: approve — the safe write path end to end
# ---------------------------------------------------------------------------


def test_approve_posts_one_correction_entry_and_audits(seeded, session: Session) -> None:
    """Approval writes exactly one posted LE-MOCK- entry whose fields mirror
    the proposal, one approval row, one audit event, and flips the proposal."""
    ledger_before = session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one()
    proposal = _pending_proposal(session)
    txn = session.get(Transaction, proposal.transaction_id)

    result = approve_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_A
    )

    assert result["status"] == "approved"
    assert result["decision"] == "approved"
    assert result["idempotent_replay"] is False
    entry_id = result["ledger_entry_id"]
    assert entry_id.startswith(MOCK_ENTRY_PREFIX)

    entries = session.execute(
        select(LedgerEntry).where(LedgerEntry.entry_id == entry_id)
    ).scalars().all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.transaction_id == proposal.transaction_id
    assert entry.merchant_id == txn.merchant_id
    assert entry.debit_account == proposal.debit_account
    assert entry.credit_account == proposal.credit_account
    assert float(entry.amount) == pytest.approx(round2(proposal.amount))
    assert entry.status == "posted"
    expected_date = proposal.entry_date
    if isinstance(expected_date, datetime):
        expected_date = expected_date.date()
    assert entry.entry_date == expected_date
    assert proposal.transaction_id in entry.description
    # Exactly one new ledger row — no double-post.
    ledger_after = session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one()
    assert ledger_after == ledger_before + 1

    session.refresh(proposal)
    assert proposal.status == "approved"

    approval = session.execute(
        select(Approval).where(Approval.proposal_id == proposal.proposal_id)
    ).scalars().one()
    assert approval.decision == "approved"
    assert approval.approver == APPROVER
    assert approval.idempotency_key == KEY_A
    assert approval.ledger_entry_id == entry_id

    event = session.get(AuditEvent, result["audit_event_id"])
    assert event is not None
    assert event.action == ACTION_APPROVE
    assert event.actor == APPROVER
    assert event.object_type == "journal_proposal"
    assert event.object_id == proposal.proposal_id
    assert event.agent_run_id == proposal.agent_run_id
    assert event.before_state == {"status": "pending"}
    assert event.after_state["status"] == "approved"


# ---------------------------------------------------------------------------
# Service: idempotency (PRD section 14)
# ---------------------------------------------------------------------------


def test_duplicate_approval_replays_and_never_double_posts(seeded, session: Session) -> None:
    """The same approve request processed twice posts once; the replay echoes
    the stored outcome, adds no rows, and leaves a replay-marker event."""
    proposal = _pending_proposal(session, "FEE_MISMATCH")
    first = approve_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_A + "-fee"
    )
    assert first["idempotent_replay"] is False
    ledger_after_first = session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one()
    approvals_after_first = session.execute(
        select(func.count()).select_from(Approval)
    ).scalar_one()

    second = approve_proposal(
        session, proposal.proposal_id, approver=APPROVER,
        idempotency_key=KEY_A + "-fee",
    )

    assert second["idempotent_replay"] is True
    assert second["ledger_entry_id"] == first["ledger_entry_id"]
    assert second["audit_event_id"] != first["audit_event_id"]
    assert "no second post" in second["message"]
    # Nothing new: one ledger entry, one approval, and the entry untouched.
    assert session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one() == ledger_after_first
    assert session.execute(
        select(func.count()).select_from(Approval)
    ).scalar_one() == approvals_after_first
    entry = session.get(LedgerEntry, first["ledger_entry_id"])
    assert entry.status == "posted"

    replay_event = session.get(AuditEvent, second["audit_event_id"])
    assert replay_event.action == ACTION_APPROVE
    assert replay_event.after_state["idempotent_replay"] is True
    assert replay_event.after_state["idempotency_key"] == KEY_A + "-fee"


def test_redeciding_with_a_different_key_is_refused(seeded, session: Session) -> None:
    """A decided proposal cannot be re-decided under a new key (409): the
    duplicate-approval double-post hole is closed from the other side."""
    proposal = _pending_proposal(session, "REFUND_MISMATCH")
    approve_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_A + "-rm"
    )
    with pytest.raises(ProposalStateError):
        approve_proposal(
            session, proposal.proposal_id, approver=APPROVER,
            idempotency_key="different-key-entirely",
        )
    with pytest.raises(IdempotencyConflictError):
        # The old key can also not be spent on a different proposal.
        other = _pending_proposal(session, "GST_MISMATCH")
        approve_proposal(
            session, other.proposal_id, approver=APPROVER, idempotency_key=KEY_A + "-rm"
        )


# ---------------------------------------------------------------------------
# Service: reject
# ---------------------------------------------------------------------------


def test_reject_records_decision_and_never_touches_the_ledger(
    seeded, session: Session
) -> None:
    """Rejection stores the decision + audit event; the ledger is unchanged."""
    ledger_before = session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one()
    proposal = _pending_proposal(session, "GST_MISMATCH")

    result = reject_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_R,
        note="not material enough",
    )

    assert result["status"] == "rejected"
    assert result["decision"] == "rejected"
    assert result["ledger_entry_id"] is None
    assert result["idempotent_replay"] is False

    session.refresh(proposal)
    assert proposal.status == "rejected"
    assert session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one() == ledger_before

    approval = session.execute(
        select(Approval).where(Approval.proposal_id == proposal.proposal_id)
    ).scalars().one()
    assert approval.decision == "rejected"
    assert approval.note == "not material enough"
    assert approval.ledger_entry_id is None

    event = session.get(AuditEvent, result["audit_event_id"])
    assert event.action == ACTION_REJECT
    assert event.after_state["status"] == "rejected"
    assert event.after_state["note"] == "not material enough"

    # Idempotent replay of the same rejection.
    replay = reject_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_R
    )
    assert replay["idempotent_replay"] is True

    # State machine: decided proposals accept no further decisions.
    with pytest.raises(ProposalStateError):
        approve_proposal(
            session, proposal.proposal_id, approver=APPROVER,
            idempotency_key="another-new-key-2",
        )


def test_approve_after_reject_is_refused(seeded, session: Session) -> None:
    """A rejected proposal cannot be approved afterwards."""
    proposal = _pending_proposal(session, "LEDGER_MISMATCH")
    ledger_before = session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one()
    reject_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_R + "-lm"
    )
    with pytest.raises(ProposalStateError):
        approve_proposal(
            session, proposal.proposal_id, approver=APPROVER,
            idempotency_key="fresh-key-for-approve",
        )
    assert session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one() == ledger_before


# ---------------------------------------------------------------------------
# Service: rollback (PRD section 15)
# ---------------------------------------------------------------------------


def test_rollback_reverses_the_posted_entry_and_audits(seeded, session: Session) -> None:
    """Rollback flips the posted entry to 'reversed' (append-only), marks the
    proposal rolled_back, and records the transition in the audit trail."""
    proposal = _pending_proposal(session, "DUPLICATE_TRANSACTION")
    approved = approve_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_A + "-dup"
    )
    entry_id = approved["ledger_entry_id"]

    result = rollback_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_RB,
        note="posted against the wrong transaction",
    )

    assert result["status"] == "rolled_back"
    assert result["ledger_entry_id"] == entry_id
    assert result["idempotent_replay"] is False

    session.refresh(proposal)
    assert proposal.status == "rolled_back"
    # Append-only: the row is still there, now reversed (not deleted).
    entry = session.get(LedgerEntry, entry_id)
    assert entry is not None
    assert entry.status == "reversed"
    assert float(entry.amount) == pytest.approx(round2(proposal.amount))

    event = session.get(AuditEvent, result["audit_event_id"])
    assert event.action == ACTION_ROLLBACK
    assert event.before_state == {
        "status": "approved", "ledger_entry_id": entry_id, "entry_status": "posted",
    }
    assert event.after_state["entry_status"] == "reversed"
    assert event.after_state["status"] == "rolled_back"


def test_duplicate_rollback_replays_without_a_second_reversal(
    seeded, session: Session
) -> None:
    """A repeated rollback request replays the stored outcome and never
    writes a second reversal audit (the idempotency key lives on the
    rollback's own audit event, since no Approval row is created)."""
    proposal = _pending_proposal(session, "FAILED_LEDGER_WRITE")
    approved = approve_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_A + "-am"
    )
    first = rollback_proposal(
        session, proposal.proposal_id, approver=APPROVER, idempotency_key=KEY_RB + "-am"
    )
    rollbacks_after_first = session.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == ACTION_ROLLBACK
        )
    ).scalar_one()

    second = rollback_proposal(
        session, proposal.proposal_id, approver=APPROVER,
        idempotency_key=KEY_RB + "-am",
    )

    assert second["idempotent_replay"] is True
    assert second["ledger_entry_id"] == approved["ledger_entry_id"]
    # One extra audit row: the replay marker. Nothing else changed.
    assert session.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == ACTION_ROLLBACK
        )
    ).scalar_one() == rollbacks_after_first + 1
    entry = session.get(LedgerEntry, approved["ledger_entry_id"])
    assert entry.status == "reversed"


def test_rollback_key_conflicts_and_state_guards(seeded, session: Session) -> None:
    """Rollback keys cannot be reused on other proposals, spent decision keys
    are refused, and non-approved proposals cannot be rolled back."""
    # Two separate approved proposals.
    p1 = _pending_proposal(session, "SETTLEMENT_TIMING_MISMATCH")
    approve_proposal(session, p1.proposal_id, approver=APPROVER, idempotency_key=KEY_A + "-st")
    p2 = _pending_proposal(session, "FAILED_LEDGER_WRITE")
    approve_proposal(session, p2.proposal_id, approver=APPROVER, idempotency_key=KEY_A + "-fl")

    # A rollback key spent on p1 cannot be reused for p2's rollback.
    rollback_proposal(session, p1.proposal_id, approver=APPROVER, idempotency_key=KEY_RB + "-st")
    with pytest.raises(IdempotencyConflictError):
        rollback_proposal(session, p2.proposal_id, approver=APPROVER, idempotency_key=KEY_RB + "-st")

    # A decision key cannot be recycled as a rollback key.
    with pytest.raises(IdempotencyConflictError):
        rollback_proposal(session, p2.proposal_id, approver=APPROVER, idempotency_key=KEY_A + "-fl")

    # Pending proposals cannot be rolled back.
    pending = _pending_proposal(session, "MISSING_SETTLEMENT")
    with pytest.raises(ProposalStateError):
        rollback_proposal(session, pending.proposal_id, approver=APPROVER, idempotency_key=KEY_RB + "-x")

    # Rolled-back proposals cannot be rolled back again with a fresh key.
    with pytest.raises(ProposalStateError):
        rollback_proposal(session, p1.proposal_id, approver=APPROVER, idempotency_key=KEY_RB + "-again")



# ---------------------------------------------------------------------------
# Service: server-side validation (todo Phase 14) and the failure branch
# ---------------------------------------------------------------------------


def test_unpostable_proposals_are_refused_with_validation_errors(
    seeded, session: Session
) -> None:
    """Amount/accounts are re-checked server-side — a model-drafted payload
    can never push a malformed entry into the ledger."""
    proposal = _pending_proposal(session, "FEE_MISMATCH")

    def mock_count() -> int:
        """Mock entries present right now (earlier tests left their own)."""
        return session.execute(
            select(func.count()).select_from(LedgerEntry).where(
                LedgerEntry.entry_id.like(MOCK_ENTRY_PREFIX + "%")
            )
        ).scalar_one()

    mock_before = mock_count()
    # Non-positive amount.
    proposal.amount = 0
    session.commit()
    with pytest.raises(ActionValidationError):
        approve_proposal(
            session, proposal.proposal_id, approver=APPROVER, idempotency_key="key-zero-amount"
        )
    # Missing accounts.
    proposal.amount = 42.50
    proposal.debit_account = "  "
    session.commit()
    with pytest.raises(ActionValidationError):
        approve_proposal(
            session, proposal.proposal_id, approver=APPROVER, idempotency_key="key-no-debit"
        )
    # Identical accounts.
    proposal.debit_account = "Sales Revenue"
    proposal.credit_account = "Sales Revenue"
    session.commit()
    with pytest.raises(ActionValidationError):
        approve_proposal(
            session, proposal.proposal_id, approver=APPROVER, idempotency_key="key-same-accounts"
        )
    # No linked transaction (nullable column — no fake FK id needed).
    proposal.credit_account = "Bank - Settlement Account"
    proposal.transaction_id = None
    session.commit()
    with pytest.raises(ActionValidationError):
        approve_proposal(
            session, proposal.proposal_id, approver=APPROVER, idempotency_key="key-no-txn"
        )
    # Nothing was posted by any refused attempt (scoped to this test: the
    # count of mock entries is unchanged, the proposal is still pending).
    assert mock_count() == mock_before
    session.refresh(proposal)
    assert proposal.status == "pending"


def test_simulated_failure_applies_nothing_and_retry_succeeds(
    seeded, session: Session
) -> None:
    """The architecture section-10 failure branch: a failed post writes no
    approval, no ledger entry, no audit event — and the same idempotency key
    succeeds when retried (the key was never spent)."""
    proposal = _pending_proposal(session, "REFUND_MISMATCH")
    ledger_before = session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one()
    approvals_before = session.execute(
        select(func.count()).select_from(Approval)
    ).scalar_one()
    events_before = session.execute(
        select(func.count()).select_from(AuditEvent)
    ).scalar_one()

    with pytest.raises(MockLedgerError):
        approve_proposal(
            session, proposal.proposal_id, approver=APPROVER,
            idempotency_key="key-fails-first", simulate_failure=True,
        )

    # Nothing was applied by the failed attempt.
    assert session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one() == ledger_before
    assert session.execute(
        select(func.count()).select_from(Approval)
    ).scalar_one() == approvals_before
    assert session.execute(
        select(func.count()).select_from(AuditEvent)
    ).scalar_one() == events_before
    session.refresh(proposal)
    assert proposal.status == "pending"

    # Recovery: the same key succeeds on retry.
    retried = approve_proposal(
        session, proposal.proposal_id, approver=APPROVER,
        idempotency_key="key-fails-first",
    )
    assert retried["idempotent_replay"] is False
    assert retried["status"] == "approved"
    assert session.execute(
        select(func.count()).select_from(LedgerEntry)
    ).scalar_one() == ledger_before + 1


def test_unknown_proposal_and_bad_fields_raise_typed_errors(
    seeded, session: Session
) -> None:
    """404 for unknown ids; 422-class validation for empty approver/key."""
    with pytest.raises(ProposalNotFoundError):
        approve_proposal(
            session, "PROP-does-not-exist", approver=APPROVER, idempotency_key="key-404-test"
        )
    with pytest.raises(ActionValidationError):
        reject_proposal(
            session, "PROP-does-not-exist", approver="  ", idempotency_key="key-bad-approver"
        )
    with pytest.raises(ActionValidationError):
        reject_proposal(
            session, "PROP-does-not-exist", approver=APPROVER, idempotency_key="  "
        )



# ---------------------------------------------------------------------------
# The critical rule: no natural-language path can mutate the ledger
# ---------------------------------------------------------------------------


def test_no_model_callable_tool_can_mutate_the_ledger() -> None:
    """The registry exposes only the six READ/PROPOSE tools: no action verb,
    no WRITE-class callable — the approve/reject/rollback functions are
    reachable only through their HTTP endpoints (architecture section 5)."""
    forbidden = {"approve", "reject", "rollback", "post_journal_entry"}
    names = set(TOOL_REGISTRY)
    assert not (names & forbidden)
    for spec in TOOL_REGISTRY.values():
        assert spec["permission"] in {"READ", "PROPOSE"}
    # And the agent layer never imports the action service: its modules
    # expose no approve/reject/rollback callable for the model to reach.
    import sys

    import app.agent.controller  # noqa: F401  (forces every agent module in)
    import app.services.actions as actions_service

    agent_modules = {name: sys.modules[name] for name in sys.modules if name.startswith("app.agent")}
    assert agent_modules, "agent modules must be imported for the scan"
    for module_name, module in agent_modules.items():
        leaked = {"approve_proposal", "reject_proposal", "rollback_proposal"} & set(dir(module))
        assert not leaked, f"{module_name} exposes action callables: {leaked}"
    assert actions_service not in agent_modules.values()


def test_agent_cannot_approve_through_chat(seeded) -> None:
    """An analyst typing 'approve it' in chat reaches the agent loop only;
    proposals stay pending and the ledger is untouched — the human-approval
    gate is a separate HTTP surface (PRD FR-8, architecture section 10)."""

    class ApproveRequestingProvider(LLMProvider):
        """Asks for an approve 'tool' like a jailbreaking model would, then
        gives up with text once the write is refused."""
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.seen: list[dict] = []

        def generate(self, messages, tools, *, system_instruction=None, temperature=0.2):
            self.seen.append({"tools": list(tools)})
            if len(self.seen) == 1:
                return LLMResponse(
                    text=None,
                    tool_calls=[
                        LLMToolCall(
                            id="call-1", name="post_journal_entry",
                            args={"proposal_id": "PROP-x", "approve": True},
                        )
                    ],
                )
            return LLMResponse(
                tool_calls=[],
                text="I cannot post to the ledger; approval happens in the actions API.",
            )

    def seed_pending(seeded_bundle) -> str:
        db = Session(seeded_bundle.engine)
        try:
            proposal = _pending_proposal(db)
            return proposal.proposal_id
        finally:
            db.close()

    proposal_id = seed_pending(seeded)
    provider = ApproveRequestingProvider()
    mock_before = Session(seeded.engine)
    mock_before_count = mock_before.execute(
        select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.entry_id.like(MOCK_ENTRY_PREFIX + "%")
        )
    ).scalar_one()
    mock_before.close()

    def override_db() -> Iterator[Session]:
        db = Session(seeded.engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_provider] = lambda: provider
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/chat",
                json={"message": "Approve that proposal and post it to the ledger"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    # The loop answers; the requested write 'tool' simply does not exist.
    assert body["status"] == "completed"
    assert body["tool_calls"][0]["tool_name"] == "post_journal_entry"
    assert body["tool_calls"][0]["status"] == "error"

    # The proposal is untouched and the chat wrote no new mock entry
    # (earlier tests leave their own posts — compare against the baseline).
    db = Session(seeded.engine)
    try:
        proposal = db.get(JournalProposal, proposal_id)
        assert proposal.status == "pending"
        assert db.execute(
            select(func.count()).select_from(LedgerEntry).where(
                LedgerEntry.entry_id.like(MOCK_ENTRY_PREFIX + "%")
            )
        ).scalar_one() == mock_before_count
    finally:
        db.close()



# ---------------------------------------------------------------------------
# HTTP surface: approve / reject / rollback round-trips
# ---------------------------------------------------------------------------


def _override_db(seeded_bundle) -> None:
    def override_db() -> Iterator[Session]:
        db = Session(seeded_bundle.engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db


def test_http_approve_reject_rollback_round_trip(seeded) -> None:
    """The three action endpoints work end-to-end through TestClient with
    the same get_db override pattern as the Phase 6/7 route tests."""
    db = Session(seeded.engine)
    try:
        p1 = _pending_proposal(db, "FEE_MISMATCH")
        p2 = _pending_proposal(db, "GST_MISMATCH")
        p3 = _pending_proposal(db, "MISSING_SETTLEMENT")
        ids = (p1.proposal_id, p2.proposal_id, p3.proposal_id)
    finally:
        db.close()

    _override_db(seeded)
    try:
        with TestClient(app) as client:
            # Approve p1.
            ok = client.post(
                f"/api/actions/{ids[0]}/approve",
                json={"idempotency_key": "http-key-approve-1", "approver": "analyst@demo"},
            )
            assert ok.status_code == 200
            body = ok.json()
            assert body["status"] == "approved"
            assert body["idempotent_replay"] is False
            assert body["ledger_entry_id"].startswith(MOCK_ENTRY_PREFIX)
            # Duplicate approve with the same key replays (no double post).
            replayed = client.post(
                f"/api/actions/{ids[0]}/approve",
                json={"idempotency_key": "http-key-approve-1", "approver": "analyst@demo"},
            )
            assert replayed.status_code == 200
            assert replayed.json()["idempotent_replay"] is True
            assert replayed.json()["ledger_entry_id"] == body["ledger_entry_id"]

            # Reject p2.
            rejected = client.post(
                f"/api/actions/{ids[1]}/reject",
                json={"idempotency_key": "http-key-reject-1", "approver": "analyst@demo"},
            )
            assert rejected.status_code == 200
            assert rejected.json()["status"] == "rejected"
            assert rejected.json()["ledger_entry_id"] is None

            # Rollback p1 (it was approved).
            rolled = client.post(
                f"/api/actions/{ids[0]}/rollback",
                json={"idempotency_key": "http-key-rollback-1", "approver": "analyst@demo"},
            )
            assert rolled.status_code == 200
            assert rolled.json()["status"] == "rolled_back"
            assert rolled.json()["ledger_entry_id"] == body["ledger_entry_id"]

            # Errors map to status codes: 404, 409, 422, 502.
            missing = client.post(
                "/api/actions/PROP-nope/approve",
                json={"idempotency_key": "http-key-404", "approver": "analyst@demo"},
            )
            assert missing.status_code == 404
            decided = client.post(
                f"/api/actions/{ids[1]}/approve",
                json={"idempotency_key": "http-key-conflict", "approver": "analyst@demo"},
            )
            assert decided.status_code == 409
            failed = client.post(
                f"/api/actions/{ids[2]}/approve",
                json={
                    "idempotency_key": "http-key-fail",
                    "approver": "analyst@demo",
                    "simulate_failure": True,
                },
            )
            assert failed.status_code == 502
            short_key = client.post(
                f"/api/actions/{ids[2]}/approve",
                json={"idempotency_key": "short", "approver": "analyst@demo"},
            )
            assert short_key.status_code == 422
    finally:
        app.dependency_overrides.clear()

    # Persisted state matches the HTTP story.
    db = Session(seeded.engine)
    try:
        p1, p2, p3 = (db.get(JournalProposal, pid) for pid in ids)
        assert p1.status == "rolled_back"
        assert p2.status == "rejected"
        assert p3.status == "pending"  # the failed+short-key attempts landed nothing
        events = db.execute(
            select(AuditEvent).where(
                AuditEvent.object_id.in_(ids)
            ).order_by(AuditEvent.created_at, AuditEvent.event_id)
        ).scalars().all()
        actions = [event.action for event in events]
        assert ACTION_APPROVE in actions
        assert ACTION_REJECT in actions
        assert ACTION_ROLLBACK in actions
    finally:
        db.close()
