"""Phase 10 tests: the dashboard-support API surface.

Mirrors the Phase 9 fixture pattern: the dev dataset (seed 42, 100
transactions, 1 exception per type) is seeded into a temp SQLite DB and
the three new listing endpoints are exercised through ``TestClient`` with
the ``get_db`` override (no network, no API key required):

- **proposals**: lists the journal-proposal queue created through the
  real journal tool, filters by status/merchant/transaction, truncates
  at ``limit``, and rejects bad status values with 422 — mirroring the
  query service exactly,
- **runs**: lists agent-run summaries newest-first with the status
  filter (runs created through the real controller loop); the Phase 9
  detail route keeps its 404 semantics,
- **merchants**: lists the master rows the dashboard scopes by, ordered
  by id, matching the DB exactly.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.controller import run_agent
from app.agent.providers.base import LLMProvider, LLMResponse
from app.agent.tool_registry import dispatch_tool
from app.db.session import get_db
from app.main import app
from app.models import (
    JournalProposal,
    Merchant,
    ReconciliationException,
    Transaction,
)
from app.services.actions import approve_proposal, reject_proposal
from app.services.dataset_generator import generate_dataset, write_dataset
from app.services.db_seed import build_engine, load_dataset_file, seed_database
from app.services.queries import list_proposals, list_runs

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1,
       "customers": 80, "seed": 42}
END = date(2026, 9, 3)  # same anchor day as the Phase 2-9 suites
APPROVER = "phase10-analyst"

_KEY_SEQ = {"next": 0}


def _idem_key(prefix: str) -> str:
    _KEY_SEQ["next"] += 1
    return f"{prefix}-{_KEY_SEQ['next']:04d}"


class FakeProvider(LLMProvider):
    """Scripted provider: replays the queued rounds in order."""

    name = "fake"
    model = "fake-model"

    def __init__(self, rounds: list[LLMResponse]) -> None:
        self.rounds = list(rounds)

    def generate(self, messages, tools, *, system_instruction=None, temperature=0.2):
        return self.rounds.pop(0)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate the dev dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase10")
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


@pytest.fixture
def http(seeded) -> Iterator[TestClient]:
    """TestClient with the module DB behind ``get_db`` (Phase 6-9 pattern)."""
    def override_db() -> Iterator[Session]:
        db = Session(seeded.engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


def _propose_and_decide(seeded, action: str) -> JournalProposal:
    """Create one proposal through the journal tool; optionally decide it.

    Persists reconciliation exceptions first when the DB has none yet
    (the journal tool drafts from *verified* exceptions). Safe to call
    repeatedly: the journal tool deduplicates pending proposals per
    transaction+accounts+amount, and already-decided rows are returned
    as-is.
    """
    with Session(seeded.engine) as db:
        has_exceptions = db.execute(
            select(ReconciliationException.id).limit(1)
        ).scalar_one_or_none()
        if has_exceptions is None:
            dispatch_tool(db, "run_reconciliation", {})

        exception = db.execute(
            select(ReconciliationException)
            .where(ReconciliationException.exception_type == "MISSING_SETTLEMENT")
            .order_by(ReconciliationException.id)
            .limit(1)
        ).scalars().one()
        proposed = dispatch_tool(
            db,
            "propose_journal_entry",
            {"exception_id": exception.id, "reason": "phase 10 test"},
        )
        proposal = db.get(JournalProposal, proposed["proposal"]["proposal_id"])
        if proposal.status != "pending":
            return proposal
        if action == "approve":
            approve_proposal(
                db, proposal.proposal_id, approver=APPROVER,
                idempotency_key=_idem_key("p10-approve"),
            )
        elif action == "reject":
            reject_proposal(
                db, proposal.proposal_id, approver=APPROVER,
                idempotency_key=_idem_key("p10-reject"),
            )
        db.refresh(proposal)
        return proposal

# ---------------------------------------------------------------------------
# Proposals tests
# ---------------------------------------------------------------------------


def test_proposals_lists_the_queue_with_joins(seeded, http) -> None:
    """The queue carries proposals with merchant joins and evidence ids."""
    _propose_and_decide(seeded, "none")  # ensure at least one pending row

    response = http.get("/api/proposals")
    assert response.status_code == 200
    body = response.json()

    assert body["count"] >= 1
    row = body["rows"][0]
    assert set(row) == {
        "proposal_id", "agent_run_id", "transaction_id", "merchant_id",
        "merchant_name", "entry_date", "debit_account", "credit_account",
        "amount", "narrative", "evidence_ids", "confidence", "status",
        "created_at",
    }
    # The join resolves the merchant name from the master table.
    assert row["merchant_name"] is not None
    assert row["transaction_id"] in row["evidence_ids"]


def test_proposals_mirrors_the_query_service(seeded, http) -> None:
    """The HTTP payload equals a direct query-service call."""
    _propose_and_decide(seeded, "none")

    response = http.get("/api/proposals", params={"status": "pending"})
    assert response.status_code == 200
    with Session(seeded.engine) as db:
        expected = list_proposals(db, status="pending", limit=500)
    body = response.json()
    assert body["count"] == expected["count"]
    assert [row["proposal_id"] for row in body["rows"]] == [
        row["proposal_id"] for row in expected["rows"]
    ]
    # Field-level equality on one row (created_at serializes to ISO in JSON).
    http_row, svc_row = body["rows"][0], expected["rows"][0]
    assert http_row["amount"] == svc_row["amount"]
    assert http_row["narrative"] == svc_row["narrative"]
    assert http_row["evidence_ids"] == svc_row["evidence_ids"]
    assert http_row["status"] == svc_row["status"]
    assert http_row["created_at"] == svc_row["created_at"].isoformat()


def test_proposals_status_filter_and_422(seeded, http) -> None:
    """Status filter narrows; an invalid value maps onto 422."""
    approved = _propose_and_decide(seeded, "approve")

    response = http.get("/api/proposals", params={"status": "approved"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    assert all(row["status"] == "approved" for row in body["rows"])
    assert approved.proposal_id in {r["proposal_id"] for r in body["rows"]}

    bad = http.get("/api/proposals", params={"status": "garbage"})
    assert bad.status_code == 422


def test_proposals_merchant_and_transaction_filters(seeded, http) -> None:
    """Merchant scope narrows; unknown ids match nothing (200, not 404)."""
    _propose_and_decide(seeded, "none")

    with Session(seeded.engine) as db:
        proposal = db.execute(
            select(JournalProposal).limit(1)
        ).scalars().first()
        merchant_id = db.execute(
            select(Transaction.merchant_id).where(
                Transaction.transaction_id == proposal.transaction_id
            )
        ).scalar_one()

    scoped = http.get("/api/proposals", params={"merchant_id": merchant_id})
    assert scoped.status_code == 200
    assert scoped.json()["count"] >= 1
    assert all(r["merchant_id"] == merchant_id for r in scoped.json()["rows"])

    empty = http.get("/api/proposals", params={"merchant_id": "M999"})
    assert empty.status_code == 200
    assert empty.json()["count"] == 0

    missing = http.get("/api/proposals", params={"transaction_id": "TXN-NONE"})
    assert missing.status_code == 200
    assert missing.json()["count"] == 0


def test_proposals_limit_truncates(seeded, http) -> None:
    """A limit below the population reports truncated=True and caps rows."""
    _propose_and_decide(seeded, "none")

    response = http.get("/api/proposals", params={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["truncated"] is True

# ---------------------------------------------------------------------------
# Runs history
# ---------------------------------------------------------------------------


def _create_run(seeded) -> str:
    """Create one agent run through the real controller loop."""
    with Session(seeded.engine) as db:
        result = run_agent(
            FakeProvider([LLMResponse(tool_calls=[], text="Checked.")]),
            db,
            "Show me the highest-impact exceptions",
        )
    return result["run_id"]


def test_runs_lists_history_newest_first(seeded, http) -> None:
    """Run summaries appear newest-first with the controller's counters."""
    first = _create_run(seeded)
    second = _create_run(seeded)

    response = http.get("/api/runs")
    assert response.status_code == 200
    body = response.json()

    assert body["count"] >= 2
    ids = [row["run_id"] for row in body["rows"]]
    assert second in ids and first in ids
    assert ids.index(second) < ids.index(first)
    assert set(body["rows"][0]) == {
        "run_id", "user_query", "status", "turn_count", "tool_call_count",
    }


def test_runs_status_filter_mirrors_query_service(seeded, http) -> None:
    """The status filter narrows and mirrors the service call exactly."""
    _create_run(seeded)

    response = http.get("/api/runs", params={"status": "completed"})
    assert response.status_code == 200
    with Session(seeded.engine) as db:
        expected = list_runs(db, status="completed", limit=500)
    body = response.json()
    assert body["count"] == expected["count"] >= 1
    assert [row["run_id"] for row in body["rows"]] == [
        row["run_id"] for row in expected["rows"]
    ]


def test_runs_detail_still_404s(seeded, http) -> None:
    """The Phase 9 detail route keeps its 404 semantics."""
    response = http.get("/api/runs/RUN-NONE")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------


def test_merchants_lists_the_master_rows(seeded, http) -> None:
    """The selector lists every merchant, ordered by id, mirroring the DB."""
    response = http.get("/api/merchants")
    assert response.status_code == 200
    body = response.json()

    with Session(seeded.engine) as db:
        expected = [
            (m.merchant_id, m.name, m.category, m.currency)
            for m in db.execute(
                select(Merchant).order_by(Merchant.merchant_id)
            ).scalars()
        ]

    assert body["count"] == len(expected) == 5
    assert [
        (r["merchant_id"], r["name"], r["category"], r["currency"])
        for r in body["rows"]
    ] == expected
    assert all(r["currency"] == "INR" for r in body["rows"])