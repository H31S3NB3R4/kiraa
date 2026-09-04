"""Phase 9 tests: the read/report API surface.

Mirrors the Phase 8 fixture pattern: the dev dataset (seed 42, 100
transactions, 1 exception per type) is seeded into a temp SQLite DB and
every endpoint is exercised through ``TestClient`` with the ``get_db``
override (no network, no API key required):

- **reconciliation run**: the HTTP payload mirrors the engine exactly
  (schema-level equality against a direct tool call), persists
  idempotently (new/updated upsert counts, never duplicates), stays
  read-only with ``persist=false``, and maps bad ranges onto 422,
- **ledger query**: mirrors ``query_ledger`` exactly, honors the
  merchant/transaction/status filters, truncates at ``limit``, and
  rejects inverted date ranges,
- **forecast**: mirrors ``forecast_cashflow`` exactly (anchor, series,
  risk), returns the ``unknown_merchant`` guard envelope with 200, and
  rejects out-of-range horizons with 422,
- **anomalies**: mirrors ``detect_anomalies`` (read-only — the
  ``anomaly_scores`` table is never written by a GET), preserves the
  flagship PASS+HIGH case, filters by merchant/transaction, and rejects
  ``limit=0``,
- **exceptions**: lists the *persisted* rows with filters, newest-first
  ordering, and truncation, mirroring the query service exactly,
- **runs**: returns one run with its tool calls and transcript (created
  through the real controller loop), 404 on unknown ids,
- **audit**: lists decision events with action/actor filters and
  newest-first ordering (seeded through the real action service),
- **metrics**: KPI cards mirror an independent cash sum and a direct
  read-only engine run; 404 on unknown merchants.

The module also pins the full PRD section-16 surface in the OpenAPI
schema.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.controller import run_agent
from app.agent.providers.base import LLMProvider, LLMResponse, LLMToolCall
from app.agent.tool_registry import enrich_reconciliation_result
from app.api.schemas.anomalies import AnomalyResponse
from app.api.schemas.forecast import ForecastResponse
from app.api.schemas.ledger import LedgerQueryResponse
from app.api.schemas.reconciliation import ReconciliationRunResponse
from app.db.session import get_db
from app.main import app
from app.models import (
    AnomalyScore,
    CashFlow,
    JournalProposal,
    ReconciliationException,
    Transaction,
)
from app.services.actions import approve_proposal, reject_proposal
from app.services.dataset_generator import generate_dataset, write_dataset
from app.services.db_seed import build_engine, load_dataset_file, seed_database
from app.services.metrics import dashboard_metrics
from app.services.queries import list_audit_events, list_exceptions
from app.tools import (
    detect_anomalies,
    forecast_cashflow,
    query_ledger,
    run_reconciliation,
)
from app.tools.common import round2

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1,
       "customers": 80, "seed": 42}
END = date(2026, 9, 3)  # same anchor day as the Phase 2-8 suites

APPROVER = "phase9-analyst"


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
    out_dir = tmp_path_factory.mktemp("phase9")
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
    """TestClient with the module DB behind ``get_db`` (Phase 6-8 pattern)."""
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
# OpenAPI surface
# ---------------------------------------------------------------------------


def test_openapi_exposes_the_prd_section_16_surface() -> None:
    """The app registers exactly the PRD section-16 API plus /health."""
    paths = set(app.openapi()["paths"])
    expected = {
        "/health",
        "/api/agent/chat",
        "/api/reconciliation/run",
        "/api/ledger/query",
        "/api/forecast",
        "/api/anomalies",
        "/api/exceptions",
        "/api/runs/{run_id}",
        "/api/actions/{proposal_id}/approve",
        "/api/actions/{proposal_id}/reject",
        "/api/actions/{proposal_id}/rollback",
        "/api/audit",
        "/api/metrics",
    }
    assert paths == expected


# ---------------------------------------------------------------------------
# POST /api/reconciliation/run
# ---------------------------------------------------------------------------


def test_reconciliation_run_mirrors_the_engine(seeded, http) -> None:
    """The HTTP payload equals the enriched direct tool call, schema-level."""
    with Session(seeded.engine) as db:
        expected = ReconciliationRunResponse(
            **enrich_reconciliation_result(run_reconciliation(db, persist=False), db)
        )

    response = http.post("/api/reconciliation/run", json={"persist": False})

    assert response.status_code == 200
    assert ReconciliationRunResponse(**response.json()) == expected
    body = response.json()
    metrics = body["metrics"]
    assert metrics["transactions"] == KWS["transactions"]
    assert metrics["exception_transactions"] == 9
    assert metrics["by_type"]["DUPLICATE_TRANSACTION"] == 2
    assert body["persisted"] is None  # read-only run: nothing upserted


def test_reconciliation_run_persists_idempotently(seeded, http, session) -> None:
    """Two persisted runs upsert the same rows — never duplicates."""
    first = http.post("/api/reconciliation/run", json={})
    assert first.status_code == 200
    persisted_first = first.json()["persisted"]
    assert persisted_first is not None
    assert persisted_first["new"] > 0
    assert persisted_first["updated"] == 0

    second = http.post("/api/reconciliation/run", json={})
    assert second.status_code == 200
    persisted_second = second.json()["persisted"]
    assert persisted_second["new"] == 0
    assert persisted_second["updated"] > 0

    assert session.execute(
        select(func.count()).select_from(ReconciliationException)
    ).scalar_one() == sum(first.json()["metrics"]["by_type"].values())
    # Every exception carries its persisted id — the propose/action chain.
    assert all(row["exception_id"] for row in second.json()["exceptions"])


def test_reconciliation_run_scopes_by_merchant(seeded, http) -> None:
    """Scope filters shrink the scan to the merchant's own transactions."""
    with Session(seeded.engine) as db:
        merchant_txns = db.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.merchant_id == "M001")
        ).scalar_one()

    response = http.post(
        "/api/reconciliation/run",
        json={"merchant_id": "M001", "persist": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filters"]["merchant_id"] == "M001"
    assert body["metrics"]["transactions"] == merchant_txns
    assert all(row["merchant_id"] == "M001" for row in body["exceptions"])


def test_reconciliation_run_rejects_inverted_date_range(http) -> None:
    """An inverted range is a 422, never a 500."""
    response = http.post(
        "/api/reconciliation/run",
        json={"start_date": "2026-09-03", "end_date": "2026-08-01"},
    )
    assert response.status_code == 422
    assert "must not be after" in response.json()["detail"]



# ---------------------------------------------------------------------------
# GET /api/ledger/query
# ---------------------------------------------------------------------------


def test_ledger_query_mirrors_the_tool(seeded, http) -> None:
    """The HTTP payload equals the direct tool call, schema-level."""
    with Session(seeded.engine) as db:
        expected = LedgerQueryResponse(**query_ledger(db, merchant_id="M001"))

    response = http.get("/api/ledger/query", params={"merchant_id": "M001"})
    assert response.status_code == 200
    assert LedgerQueryResponse(**response.json()) == expected
    body = response.json()
    assert body["count"] > 0
    row = body["rows"][0]
    # Source links back to the transaction (FR-3).
    assert {"settlement_id", "invoice_id"} <= set(row)


def test_ledger_query_filters_and_limit(seeded, http) -> None:
    """Status filters select slices; limit truncates; transaction_id scopes."""
    posted = http.get("/api/ledger/query", params={"status": "posted", "limit": 5})
    assert posted.status_code == 200
    posted_body = posted.json()
    assert 0 < posted_body["count"] <= 5
    assert all(row["status"] == "posted" for row in posted_body["rows"])
    assert posted_body["truncated"] is (posted_body["count"] == 5)

    failed = http.get("/api/ledger/query", params={"status": "failed"})
    assert failed.status_code == 200
    assert all(row["status"] == "failed" for row in failed.json()["rows"])

    txn = posted_body["rows"][0]["transaction_id"]
    single = http.get("/api/ledger/query", params={"transaction_id": txn})
    assert single.status_code == 200
    assert single.json()["count"] >= 1
    assert all(row["transaction_id"] == txn for row in single.json()["rows"])


def test_ledger_query_rejects_inverted_date_range(http) -> None:
    response = http.get(
        "/api/ledger/query",
        params={"start_date": "2026-09-03", "end_date": "2026-08-01"},
    )
    assert response.status_code == 422
    assert "must not be after" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/forecast
# ---------------------------------------------------------------------------


def test_forecast_mirrors_the_tool(seeded, http) -> None:
    """The HTTP payload equals the direct tool call, schema-level."""
    with Session(seeded.engine) as db:
        expected = ForecastResponse(**forecast_cashflow(db, merchant_id="M001"))

    response = http.get("/api/forecast", params={"merchant_id": "M001"})
    assert response.status_code == 200
    assert ForecastResponse(**response.json()) == expected
    body = response.json()
    assert body["status"] == "ok"
    assert body["scope"] == "merchant"
    assert len(body["forecast"]) == 7  # default horizon
    assert body["risk"] in {"LOW", "MEDIUM", "HIGH"}
    assert body["risk_reason"]


def test_forecast_pooled_scope_and_custom_horizon(seeded, http) -> None:
    """merchant_id=null pools every merchant; horizon_days changes the series."""
    with Session(seeded.engine) as db:
        expected = ForecastResponse(
            **forecast_cashflow(db, horizon_days=3, history_days=14)
        )

    response = http.get(
        "/api/forecast", params={"horizon_days": 3, "history_days": 14}
    )
    assert response.status_code == 200
    body = response.json()
    assert ForecastResponse(**body) == expected
    assert body["scope"] == "all_merchants"
    assert len(body["forecast"]) == 3
    assert [point["day_offset"] for point in body["forecast"]] == [1, 2, 3]


def test_forecast_guard_envelope_returns_200(http) -> None:
    """Unknown merchants answer a 200 guard envelope, not an error."""
    unknown = http.get("/api/forecast", params={"merchant_id": "M999"})
    assert unknown.status_code == 200
    assert unknown.json()["status"] == "unknown_merchant"


def test_forecast_rejects_out_of_range_horizon(http) -> None:
    response = http.get("/api/forecast", params={"horizon_days": 0})
    assert response.status_code == 422
    assert "horizon_days" in response.json()["detail"]



# ---------------------------------------------------------------------------
# GET /api/anomalies
# ---------------------------------------------------------------------------


def test_anomalies_mirrors_the_tool_and_stays_read_only(seeded, http, session) -> None:
    """The HTTP payload equals a direct read-only scoring run; the GET never
    writes into ``anomaly_scores`` (persisting belongs to the agent tool)."""
    before = session.execute(
        select(func.count()).select_from(AnomalyScore)
    ).scalar_one()

    with Session(seeded.engine) as db:
        expected = AnomalyResponse(**detect_anomalies(db, persist=False))

    response = http.get("/api/anomalies")
    assert response.status_code == 200
    assert AnomalyResponse(**response.json()) == expected

    body = response.json()
    assert body["status"] == "ok"
    assert body["metrics"]["transactions_scored"] == KWS["transactions"]
    # The flagship demo case survives the HTTP hop: books consistent...
    assert body["scores"][0]["is_anomaly"] is True
    assert body["scores"][0]["reconciliation_pass"] is True
    assert body["ground_truth"]["precision"] == 1.0
    assert body["ground_truth"]["recall"] == 1.0

    after = session.execute(
        select(func.count()).select_from(AnomalyScore)
    ).scalar_one()
    assert after == before


def test_anomalies_filters_by_merchant(seeded, http) -> None:
    with Session(seeded.engine) as db:
        expected = AnomalyResponse(
            **detect_anomalies(db, merchant_id="M002", persist=False)
        )

    response = http.get("/api/anomalies", params={"merchant_id": "M002"})
    assert response.status_code == 200
    assert AnomalyResponse(**response.json()) == expected
    assert all(row["merchant_id"] == "M002" for row in response.json()["scores"])


def test_anomalies_transaction_id_filter(seeded, http) -> None:
    """``transaction_id`` scores exactly that transaction."""
    all_rows = http.get("/api/anomalies").json()["scores"]
    target = all_rows[0]["transaction_id"]
    response = http.get("/api/anomalies", params={"transaction_id": target})
    assert response.status_code == 200
    scores = response.json()["scores"]
    assert [row["transaction_id"] for row in scores] == [target]


def test_anomalies_guard_envelopes(http) -> None:
    unknown = http.get("/api/anomalies", params={"merchant_id": "M999"})
    assert unknown.status_code == 200
    assert unknown.json()["status"] == "unknown_merchant"

    empty = http.get("/api/anomalies", params={"transaction_id": "TXN-NONE"})
    assert empty.status_code == 200
    assert empty.json()["status"] == "no_transactions"
    assert empty.json()["scores"] == []


def test_anomalies_rejects_bad_limit(http) -> None:
    response = http.get("/api/anomalies", params={"limit": 0})
    assert response.status_code == 422
    assert "limit" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/exceptions
# ---------------------------------------------------------------------------


def _persist_exceptions(seeded) -> None:
    """Run the engine once (persisted) — shared module state, idempotent."""
    with Session(seeded.engine) as db:
        run_reconciliation(db)


def test_exceptions_lists_persisted_rows(seeded, http) -> None:
    """The endpoint lists the engine's upserted rows, newest first."""
    with Session(seeded.engine) as db:
        engine_result = run_reconciliation(db)
        expected = list_exceptions(db)

    response = http.get("/api/exceptions")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == expected["count"]
    assert [row["exception_id"] for row in body["rows"]] == [
        row["exception_id"] for row in expected["rows"]
    ]
    dates = [row["exception_date"] for row in body["rows"]]
    assert dates == sorted(dates, reverse=True)
    # Counts mirror the engine taxonomy (the duplicate pair spans 2 rows).
    by_type = engine_result["metrics"]["by_type"]
    assert body["count"] == engine_result["metrics"]["exceptions"]
    assert body["count"] == sum(by_type.values())
    assert len({row["exception_type"] for row in body["rows"]}) == len(by_type)
    assert by_type["DUPLICATE_TRANSACTION"] == 2


def test_exceptions_filters(seeded, http) -> None:
    _persist_exceptions(seeded)

    high = http.get("/api/exceptions", params={"severity": "high"})
    assert high.status_code == 200
    assert high.json()["count"] > 0
    assert all(row["severity"] == "high" for row in high.json()["rows"])

    one_type = http.get("/api/exceptions", params={"exception_type": "FEE_MISMATCH"})
    assert one_type.status_code == 200
    assert one_type.json()["count"] == 1
    assert one_type.json()["rows"][0]["exception_type"] == "FEE_MISMATCH"

    txn = one_type.json()["rows"][0]["transaction_id"]
    by_txn = http.get("/api/exceptions", params={"transaction_id": txn})
    assert by_txn.status_code == 200
    assert all(row["transaction_id"] == txn for row in by_txn.json()["rows"])


def test_exceptions_limit_truncates(seeded, http) -> None:
    _persist_exceptions(seeded)

    response = http.get("/api/exceptions", params={"limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["truncated"] is True
    assert body["limit"] == 3


def test_exceptions_rejects_inverted_date_range(http) -> None:
    response = http.get(
        "/api/exceptions",
        params={"start_date": "2026-09-03", "end_date": "2026-08-01"},
    )
    assert response.status_code == 422
    assert "must not be after" in response.json()["detail"]



# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}
# ---------------------------------------------------------------------------


def _make_run(seeded, provider: FakeProvider, message: str) -> dict:
    """Run the real controller loop against the module DB."""
    with Session(seeded.engine) as db:
        return run_agent(provider, db, message)


def test_run_detail_returns_trace_and_transcript(seeded, http) -> None:
    """A run created through the real loop serializes with calls + transcript."""
    provider = FakeProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[
                    LLMToolCall(id="c1", name="query_ledger", args={"limit": 2})
                ],
            ),
            LLMResponse(text="Found two rows.", tool_calls=[]),
        ]
    )
    result = _make_run(seeded, provider, "Show me two ledger rows.")

    response = http.get(f"/api/runs/{result['run_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == result["run_id"]
    assert body["status"] == "completed"
    assert body["user_query"] == "Show me two ledger rows."
    assert body["final_response"] == "Found two rows."
    assert body["tool_call_count"] == 1
    assert body["tool_calls"][0]["tool_name"] == "query_ledger"
    assert body["tool_calls"][0]["status"] == "ok"
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"]["text"] == "Show me two ledger rows."
    roles = [message["role"] for message in body["messages"]]
    assert roles == ["user", "model", "tool", "model"]


def test_run_detail_unknown_run_returns_404(http) -> None:
    response = http.get("/api/runs/RUN-DOES-NOT-EXIST")
    assert response.status_code == 404
    assert "does not exist" in response.json()["detail"]



# ---------------------------------------------------------------------------
# GET /api/audit
# ---------------------------------------------------------------------------

# Idempotency keys must be unique per write; tests share the module DB, so
# every seeding call draws the next counter value.
_KEY_SEQ = {"next": 0}


def _idem_key(prefix: str) -> str:
    _KEY_SEQ["next"] += 1
    return f"{prefix}-{_KEY_SEQ['next']:04d}"


def _seed_action_history(seeded) -> None:
    """Approve two proposals + reject one, through the real service path.

    Safe to call repeatedly: each call draws fresh idempotency keys, and
    proposals already decided by an earlier call are skipped (the journal
    tool deduplicates pending proposals per transaction+accounts+amount).
    """
    from app.agent.tool_registry import dispatch_tool

    with Session(seeded.engine) as db:
        for exception_type, action in (
            ("MISSING_SETTLEMENT", "approve"),
            ("FEE_MISMATCH", "approve"),
            ("GST_MISMATCH", "reject"),
        ):
            exception = db.execute(
                select(ReconciliationException)
                .where(ReconciliationException.exception_type == exception_type)
                .order_by(ReconciliationException.id)
                .limit(1)
            ).scalars().one()
            proposed = dispatch_tool(
                db,
                "propose_journal_entry",
                {"exception_id": exception.id, "reason": "phase 9 audit test"},
            )
            proposal = db.get(JournalProposal, proposed["proposal"]["proposal_id"])
            if proposal.status != "pending":
                continue  # already decided by an earlier seeding call
            if action == "approve":
                approve_proposal(
                    db, proposal.proposal_id, approver=APPROVER,
                    idempotency_key=_idem_key("phase9-approve"),
                )
            else:
                reject_proposal(
                    db, proposal.proposal_id, approver=APPROVER,
                    idempotency_key=_idem_key("phase9-reject"),
                )


def test_audit_lists_decision_events_newest_first(seeded, http) -> None:
    """The trail carries every decision with before/after states."""
    _seed_action_history(seeded)

    response = http.get("/api/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 3
    assert {row["action"] for row in body["rows"]} >= {
        "proposal.approve",
        "proposal.reject",
    }
    # Newest first.
    created = [row["created_at"] for row in body["rows"]]
    assert created == sorted(created, reverse=True)
    approve_row = next(
        row for row in body["rows"] if row["action"] == "proposal.approve"
    )
    assert approve_row["actor"] == APPROVER
    assert approve_row["object_type"] == "journal_proposal"
    assert approve_row["before_state"]["status"] == "pending"
    assert approve_row["after_state"]["status"] == "approved"


def test_audit_filters(seeded, http) -> None:
    _seed_action_history(seeded)

    rejects = http.get("/api/audit", params={"action": "proposal.reject"})
    assert rejects.status_code == 200
    assert rejects.json()["count"] >= 1
    assert all(row["action"] == "proposal.reject" for row in rejects.json()["rows"])

    by_actor = http.get("/api/audit", params={"actor": APPROVER})
    assert by_actor.status_code == 200
    assert all(row["actor"] == APPROVER for row in by_actor.json()["rows"])

    # Mirrors the query service exactly.
    with Session(seeded.engine) as db:
        expected = list_audit_events(db, action="proposal.approve", limit=500)
    approved = http.get("/api/audit", params={"action": "proposal.approve"})
    assert approved.status_code == 200
    assert [row["event_id"] for row in approved.json()["rows"]] == [
        row["event_id"] for row in expected["rows"]
    ]


def test_audit_limit_truncates(seeded, http) -> None:
    _seed_action_history(seeded)

    response = http.get("/api/audit", params={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["truncated"] is True



# ---------------------------------------------------------------------------
# GET /api/metrics
# ---------------------------------------------------------------------------


def test_metrics_kpi_cards_mirror_independent_aggregates(seeded, http) -> None:
    """Total cash mirrors an independent pooled-balance sum; the reconciliation
    block mirrors a direct read-only engine run."""
    with Session(seeded.engine) as db:
        anchor = db.execute(select(func.max(CashFlow.date))).scalar_one()
        expected_cash = round2(
            db.execute(
                select(func.sum(CashFlow.closing_balance)).where(
                    CashFlow.date == anchor
                )
            ).scalar()
        )
        expected_metrics = run_reconciliation(db, persist=False)["metrics"]

    response = http.get("/api/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["merchant_id"] is None
    assert body["total_cash"] == pytest.approx(expected_cash)
    assert body["cash_as_of_date"] == anchor.isoformat()
    assert body["reconciliation"]["transactions"] == expected_metrics["transactions"]
    assert body["exception_count"] == expected_metrics["exceptions"]
    assert body["exception_transactions"] == expected_metrics["exception_transactions"]
    assert body["financial_impact_at_risk"] == pytest.approx(
        expected_metrics["total_financial_impact"]
    )
    assert body["match_rate_pct"] == pytest.approx(expected_metrics["match_rate_pct"])


def test_metrics_scopes_by_merchant(seeded, http) -> None:
    with Session(seeded.engine) as db:
        expected = dashboard_metrics(db, merchant_id="M001")

    response = http.get("/api/metrics", params={"merchant_id": "M001"})
    assert response.status_code == 200
    body = response.json()
    assert body["merchant_id"] == "M001"
    assert body["total_cash"] == pytest.approx(expected["total_cash"])
    assert body["exception_count"] == expected["reconciliation"]["exceptions"]
    assert body["pending_proposals"] >= 0


def test_metrics_unknown_merchant_returns_404(http) -> None:
    response = http.get("/api/metrics", params={"merchant_id": "M999"})
    assert response.status_code == 404
    assert "does not exist" in response.json()["detail"]

