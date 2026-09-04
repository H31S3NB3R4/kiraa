"""Phase 6 tests: the Gemini tool-calling agent (registry, controller, API).

Mirrors the earlier fixture pattern: generate the dev dataset (seed 42,
100 transactions, 1 exception per type) into a temp SQLite DB, then verify
the whole agent layer with a scripted fake provider (no network, no API
key required):

- the registry exposes the six PRD tools with READ/PROPOSE permission
  classes and provider-agnostic JSON-schema declarations,
- ``dispatch_tool`` happy paths return the tool payloads with
  ``latency_ms`` stamped and (for reconciliation) persisted
  ``exception_id``s attached,
- every failure mode returns a structured error envelope (UNKNOWN_TOOL,
  INVALID_ARGUMENTS, VALIDATION_ERROR, TOOL_FAILURE) that rolls the
  session back and stays usable -- the model is told a tool failed and
  never invents a result,
- ``propose_journal_entry`` drafts a pending proposal (posted=False,
  requires_approval=True), links it to the agent run, deduplicates, and
  picks the debit/credit direction from the exception type,
- the controller loop persists agent_runs + tool_calls rows, feeds error
  envelopes back to the provider, respects the tool-call safety limit,
  and ends provider failures with status model_error,
- ``POST /api/agent/chat`` runs the loop end-to-end with dependency
  overrides, and ``get_provider`` returns 503 without an API key.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from datetime import date, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.controller import (
    STATUS_COMPLETED,
    STATUS_MODEL_ERROR,
    STATUS_TOOL_LIMIT,
    AgentController,
    run_agent,
)

from app.agent.prompts import SYSTEM_PROMPT, build_system_prompt
from app.agent.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    TextMessage,
    ToolCallsMessage,
    ToolResultsMessage,
)
from app.agent.tool_registry import (
    PROPOSE,
    READ,
    TOOL_DECLARATIONS,
    TOOL_PERMISSIONS,
    TOOL_REGISTRY,
    dispatch_tool,
)
from app.api.routes.agent import get_provider
from app.config import Settings
from app.db.session import get_db
from app.main import app
from app.models import AgentRun, JournalProposal, ReconciliationException, ToolCall
from app.services.dataset_generator import generate_dataset, write_dataset
from app.services.db_seed import build_engine, load_dataset_file, seed_database
from app.tools.common import round2
from app.tools.journal import BANK_ACCOUNT, REVENUE_ACCOUNT

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1,
       "customers": 80, "seed": 42}
END = date(2026, 9, 3)  # same anchor day as the Phase 3/4/5 suites

# Record-id shape the controller may surface as evidence.
ID_RE = re.compile(r"^(?:TXN|SET|INV|LE|RFD|FEE|PROP)-[0-9A-Za-z-]+$")

# Independent mirrors of the deterministic business rules under test
# (a test that just re-reads the source proves nothing).
SEVERITY_BY_TYPE = {
    "MISSING_SETTLEMENT": "high",
    "DUPLICATE_TRANSACTION": "high",
    "FAILED_LEDGER_WRITE": "high",
    "FEE_MISMATCH": "medium",
    "REFUND_MISMATCH": "medium",
    "LEDGER_MISMATCH": "medium",
    "GST_MISMATCH": "medium",
    "AMOUNT_MISMATCH": "medium",
    "SETTLEMENT_TIMING_MISMATCH": "low",
}
CONFIDENCE_BY_SEVERITY = {"high": 0.95, "medium": 0.80, "low": 0.60}
# Correction types whose fix debits the bank account (the merchant is
# owed value the books do not reflect); duplicates reverse revenue.
_MERCHANT_OWED_TYPES = {
    "MISSING_SETTLEMENT",
    "FEE_MISMATCH",
    "REFUND_MISMATCH",
    "GST_MISMATCH",
    "SETTLEMENT_TIMING_MISMATCH",
    "FAILED_LEDGER_WRITE",
}


class FakeProvider(LLMProvider):
    """Scripted provider: replays queued rounds (exceptions are raised)."""

    name = "fake"
    model = "fake-model"

    def __init__(self, rounds: list[LLMResponse | Exception]) -> None:
        self.rounds = list(rounds)
        self.calls = 0
        self.seen: list[dict] = []

    def generate(
        self,
        messages,
        tools,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.seen.append(
            {
                "messages": list(messages),
                "tools": list(tools),
                "system_instruction": system_instruction,
                "temperature": temperature,
            }
        )
        response = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate the dev dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase6")
    dataset = generate_dataset(**KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "dataset")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'finance.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    bundle.txns = {t["transaction_id"]: t for t in dataset["transactions"]}
    bundle.labels = {l["transaction_id"]: l for l in dataset["labels"]}
    bundle.invoices = {i["transaction_id"]: i for i in dataset["invoices"]}
    bundle.ledger_rows = dataset["ledger_entries"]
    return bundle


@pytest.fixture
def session(seeded) -> Iterator[Session]:
    """One fresh session on the shared module-scoped database."""
    db = Session(seeded.engine)
    try:
        yield db
    finally:
        db.close()


def _create_run(db: Session, run_id: str) -> AgentRun:
    """Insert one agent run row (PROPOSE tools link proposals to it)."""
    db.add(
        AgentRun(
            run_id=run_id,
            user_query="test",
            status="running",
            started_at=datetime.now(),
        )
    )
    db.commit()
    return db.get(AgentRun, run_id)


def _exception_of_type(db: Session, exception_type: str) -> ReconciliationException:
    """Fetch the persisted exception row of one injected scenario type.

    Runs one (idempotent) reconciliation first so the row is guaranteed
    to exist regardless of test ordering.
    """
    dispatch_tool(db, "run_reconciliation", {})
    return db.execute(
        select(ReconciliationException)
        .where(ReconciliationException.exception_type == exception_type)
        .order_by(ReconciliationException.id)
        .limit(1)
    ).scalars().one()


def _expected_direction(exception_type: str, impact: float) -> tuple[str, str]:
    """Independent mirror of ``journal._correction_direction``."""
    if exception_type in _MERCHANT_OWED_TYPES:
        return BANK_ACCOUNT, REVENUE_ACCOUNT
    if exception_type == "DUPLICATE_TRANSACTION":
        return REVENUE_ACCOUNT, BANK_ACCOUNT
    return (BANK_ACCOUNT, REVENUE_ACCOUNT) if impact < 0 else (REVENUE_ACCOUNT, BANK_ACCOUNT)

# --- system prompt ---------------------------------------------------------


def test_build_system_prompt_embeds_scope_and_anchor() -> None:
    """The prompt carries the rules plus date anchor and merchant scope."""
    with_scope = build_system_prompt(merchant_id="M001", today="2026-09-03")
    assert with_scope.startswith(SYSTEM_PROMPT)
    assert "data anchored around 2026-09-03" in with_scope
    assert "merchant_id=M001" in with_scope
    # Without a scope the merchant id must not leak into the prompt.
    bare = build_system_prompt(today="2026-09-03")
    assert "merchant_id=M001" not in bare
    assert "Analyst scope" not in bare
    # The behavioral contract is embedded verbatim (evidence-first, no writes).
    assert "Never invent financial figures" in with_scope
    assert "propose_journal_entry only drafts a reviewable proposal" in with_scope


# --- registry contract -----------------------------------------------------


def test_registry_exposes_the_six_prd_tools() -> None:
    """Exactly the six model-callable tools, with the right permissions."""
    assert set(TOOL_REGISTRY) == {
        "run_reconciliation",
        "query_ledger",
        "forecast_cashflow",
        "check_gst_match",
        "detect_anomalies",
        "propose_journal_entry",
    }
    assert set(TOOL_PERMISSIONS) == set(TOOL_REGISTRY)
    assert TOOL_PERMISSIONS["propose_journal_entry"] == PROPOSE
    assert {name for name, cls in TOOL_PERMISSIONS.items() if cls == PROPOSE} == {
        "propose_journal_entry"
    }
    assert TOOL_PERMISSIONS["run_reconciliation"] == READ
    assert {READ, PROPOSE} == {cls for cls in TOOL_PERMISSIONS.values()}


def test_declarations_are_provider_agnostic_contracts() -> None:
    """TOOL_DECLARATIONS mirrors the registry specs; no SDK/ORM leakage."""
    assert len(TOOL_DECLARATIONS) == len(TOOL_REGISTRY)
    for declaration in TOOL_DECLARATIONS:
        name = declaration["name"]
        spec = TOOL_REGISTRY[name]
        assert declaration["description"] == spec["description"]
        assert declaration["parameters"] == spec["parameters"]
        assert declaration["parameters"]["type"] == "object"
    # Only the propose tool declares required arguments here.
    required = {
        d["name"]: d["parameters"].get("required", [])
        for d in TOOL_DECLARATIONS
    }
    assert required["check_gst_match"] == ["transaction_id"]
    assert required["propose_journal_entry"] == ["exception_id", "reason"]
    assert required["run_reconciliation"] == []
    assert required["query_ledger"] == []


# --- dispatch_tool: reconciliation -------------------------------------------


def test_dispatch_run_reconciliation_happy_path(session: Session) -> None:
    """Full-scope recon: metrics, persisted exception rows, exception_id
    enrichment, latency stamp, and exception fields matching the payload."""
    result = dispatch_tool(session, "run_reconciliation", {})

    assert result["tool"] == "run_reconciliation"
    assert result["filters"] == {
        "merchant_id": None,
        "start_date": None,
        "end_date": None,
    }
    assert isinstance(result["latency_ms"], float)
    assert result["latency_ms"] >= 0.0

def test_dispatch_run_reconciliation_happy_path(seeded, session: Session) -> None:
    """Full-scope recon: metrics, persisted exception rows, exception_id
    enrichment, latency stamp, and exception fields matching the payload."""
    result = dispatch_tool(session, "run_reconciliation", {})

    assert result["tool"] == "run_reconciliation"
    assert result["filters"] == {
        "merchant_id": None,
        "start_date": None,
        "end_date": None,
    }
    assert isinstance(result["latency_ms"], float)
    assert result["latency_ms"] >= 0.0

    # The persisted rows must match the payload exactly (idempotent upsert
    # was fresh here, so every detected exception was a new row).
    persisted_rows = session.execute(select(ReconciliationException)).scalars().all()
    assert result["persisted"] == {"new": len(persisted_rows), "updated": 0}
    assert len(persisted_rows) == len(result["exceptions"])
    row_by_key = {
        (row.transaction_id, row.exception_type): row for row in persisted_rows
    }
    # Each payload exception is enriched with its persisted exception_id.
    id_by_key = {
        (row.transaction_id, row.exception_type): row.id for row in persisted_rows
    }
    for exception in result["exceptions"]:
        key = (exception["transaction_id"], exception["exception_type"])
        assert key in row_by_key
        assert exception["exception_id"] == id_by_key[key]
        row = row_by_key[key]
        # Payload fields mirror the persisted row (round-trip check).
        assert exception["severity"] == row.severity == SEVERITY_BY_TYPE[key[1]]
        assert exception["financial_impact"] == pytest.approx(
            float(row.financial_impact)
        )
        assert exception["recorded_amount"] == pytest.approx(
            float(row.recorded_amount)
        )

    # Aggregate metrics describe the same population.
    metrics = result["metrics"]
    assert metrics["transactions"] == KWS["transactions"]
    assert metrics["exceptions"] == len(result["exceptions"])
    assert metrics["by_type"] == dict(
        sorted(Counter(e["exception_type"] for e in result["exceptions"]).items())
    )
    txn_ids = {e["transaction_id"] for e in result["exceptions"]}
    assert metrics["exception_transactions"] == len(txn_ids)
    assert metrics["matched"] == KWS["transactions"] - len(txn_ids)
    assert metrics["match_rate_pct"] == pytest.approx(
        100.0 * (KWS["transactions"] - len(txn_ids)) / KWS["transactions"]
    )

    # The engine flags exactly the dataset's recon ground truth (FR-2).
    labels = seeded.labels
    truth = {
        entry["transaction_id"]
        for entry in labels.values()
        if entry["recon_exception"]
    }
    flagged = txn_ids
    assert flagged == truth

def test_dispatch_reconciliation_scope_filters_and_idempotency(seeded, session: Session) -> None:
    """Merchant + date scoping narrow the transaction population, and a
    re-run upserts (updates) instead of duplicating rows."""
    merchant = "M001"
    # Baseline run (fresh or upserting over earlier runs -- both valid).
    first = dispatch_tool(session, "run_reconciliation", {})
    before = session.execute(
        select(func.count()).select_from(ReconciliationException)
    ).scalar_one()

    # Re-running the same scope updates every row, inserts none.
    second = dispatch_tool(session, "run_reconciliation", {})
    assert second["persisted"] == {
        "new": 0,
        "updated": first["persisted"]["new"] + first["persisted"]["updated"],
    }
    after = session.execute(
        select(func.count()).select_from(ReconciliationException)
    ).scalar_one()
    assert after == before

    # A date window keeps only transactions dated within it.
    scoped = dispatch_tool(
        session,
        "run_reconciliation",
        {"merchant_id": merchant, "start_date": "2026-08-30", "end_date": "2026-09-01"},
    )
    txn = seeded.txns
    in_scope = {
        t_id
        for t_id, row in txn.items()
        if row["merchant_id"] == merchant
        and row["timestamp"][:10] >= "2026-08-30"
        and row["timestamp"][:10] <= "2026-09-01"
    }
    assert scoped["filters"]["merchant_id"] == merchant
    assert scoped["filters"]["start_date"] == "2026-08-30"
    assert scoped["filters"]["end_date"] == "2026-09-01"
    assert scoped["metrics"]["transactions"] == len(in_scope)
    flagged_in_scope = {
        e["transaction_id"] for e in scoped["exceptions"]
    }
    assert flagged_in_scope <= in_scope
    # Every flagged row carries the merchant scope.
    assert all(
        seeded.txns[e["transaction_id"]]["merchant_id"] == merchant
        for e in scoped["exceptions"]
    )


# --- dispatch_tool: query_ledger ---------------------------------------------


def test_dispatch_query_ledger_happy_path(seeded, session: Session) -> None:
    """Ledger query: filter shape, row payload, source links, no mutation."""
    merchant = "M001"
    rows_before = session.execute(
        select(func.count()).select_from(JournalProposal)
    ).scalar_one()

    result = dispatch_tool(session, "query_ledger", {"merchant_id": merchant})

    assert result["tool"] == "query_ledger"
    assert result["filters"]["merchant_id"] == merchant
    assert result["count"] == len(result["rows"])
    assert result["truncated"] is False
    assert isinstance(result["latency_ms"], float)
    assert result["rows"], "seeded M001 ledger entries must exist"

    # Rows mirror the seeded ledger entries for the merchant, each linked
    # back to its transaction (settlement/invoice references).
    seeded_rows = [
        entry
        for entry in seeded.ledger_rows
        if entry["merchant_id"] == merchant
    ]
    assert result["count"] == len(seeded_rows)
    assert result["limit"] == 500
    for row in result["rows"]:
        origin = seeded.txns[row["transaction_id"]]
        assert row["merchant_id"] == merchant
        assert row["settlement_id"] == origin["settlement_id"]
        assert row["invoice_id"] == origin["invoice_id"]
        assert row["amount"] == pytest.approx(
            next(
                e["amount"]
                for e in seeded_rows
                if e["entry_id"] == row["entry_id"]
            )
        )

    # READ-class tool: nothing was written.
    assert session.execute(
        select(func.count()).select_from(JournalProposal)
    ).scalar_one() == rows_before

def test_dispatch_query_ledger_limit_truncates(session: Session) -> None:
    """A limit below the population reports truncated=True and caps rows."""
    result = dispatch_tool(session, "query_ledger", {"limit": 1})
    assert result["count"] == 1
    assert result["truncated"] is True
    assert len(result["rows"]) == 1
    assert result["limit"] == 1


# --- dispatch_tool: check_gst_match -------------------------------------------


def test_dispatch_check_gst_match_matched_mismatched_not_found(seeded, session: Session) -> None:
    """GST verdicts on a clean txn, the injected GST mismatch, and a
    missing transaction id."""
    labels = seeded.labels

    # A normal transaction: the invoice decomposition matches exactly.
    normal_id = next(
        t_id for t_id, entry in labels.items() if entry["scenario"] == "NORMAL"
    )
    matched = dispatch_tool(session, "check_gst_match", {"transaction_id": normal_id})
    assert matched["tool"] == "check_gst_match"
    assert matched["status"] == "matched"
    assert matched["difference"] == pytest.approx(0.0)
    invoice = seeded.invoices[normal_id]
    assert matched["invoice_id"] == invoice["invoice_id"]
    assert matched["total_amount"] == pytest.approx(invoice["total_amount"])
    assert matched["recorded_tax"] == pytest.approx(invoice["gst_amount"])
    assert matched["sources"]["invoice_id"] == invoice["invoice_id"]

    # The injected GST_MISMATCH scenario is flagged with the exact delta.
    gst_id = next(
        t_id for t_id, entry in labels.items() if entry["scenario"] == "GST_MISMATCH"
    )
    mismatch = dispatch_tool(session, "check_gst_match", {"transaction_id": gst_id})
    assert mismatch["status"] == "mismatch"
    tampered = seeded.invoices[gst_id]
    expected_tax = round2(
        tampered["total_amount"] * tampered["gst_rate"] / (1.0 + tampered["gst_rate"])
    )
    assert mismatch["difference"] == pytest.approx(
        round2(tampered["gst_amount"] - expected_tax)
    )
    assert abs(mismatch["difference"]) > matched["tolerance"]

    # Unknown transaction: graceful not_found envelope.
    missing = dispatch_tool(
        session, "check_gst_match", {"transaction_id": "TXN-DOES-NOT-EXIST"}
    )
    assert missing["tool"] == "check_gst_match"
    assert missing["status"] == "not_found"
    assert missing["transaction_id"] == "TXN-DOES-NOT-EXIST"
    assert missing["sources"] == {"transaction_id": "TXN-DOES-NOT-EXIST"}
    assert isinstance(missing["latency_ms"], float)


# --- dispatch_tool: forecast + anomalies smoke ---------------------------------


def test_dispatch_forecast_and_anomalies_smoke(session: Session) -> None:
    """Both remaining READ tools execute through dispatch_tool and return
    self-describing ok payloads with latency stamps."""
    forecast = dispatch_tool(session, "forecast_cashflow", {"horizon_days": 3})
    assert forecast["tool"] == "forecast_cashflow"
    assert forecast["status"] == "ok"
    assert forecast["scope"] == "all_merchants"
    assert forecast["horizon_days"] == 3
    assert len(forecast["forecast"]) == 3
    assert forecast["risk"] in {"LOW", "MEDIUM", "HIGH"}
    assert isinstance(forecast["latency_ms"], float)

    anomalies = dispatch_tool(session, "detect_anomalies", {"limit": 5})
    assert anomalies["tool"] == "detect_anomalies"
    assert anomalies["status"] == "ok"
    assert anomalies["model"]["version"]
    assert len(anomalies["scores"]) == 5
    assert anomalies["metrics"]["transactions_scored"] == KWS["transactions"]
    assert isinstance(anomalies["latency_ms"], float)

# --- dispatch_tool: error envelopes ------------------------------------------


def test_dispatch_unknown_tool_returns_error_envelope(session: Session) -> None:
    """An unregistered tool name returns a structured UNKNOWN_TOOL envelope
    (never an exception), stamped with latency."""
    envelope = dispatch_tool(session, "no_such_tool", {})
    assert envelope == {
        "tool": "no_such_tool",
        "status": "error",
        "error_type": "UNKNOWN_TOOL",
        "message": "no tool named 'no_such_tool' is registered",
    } | {"latency_ms": envelope["latency_ms"]}
    assert isinstance(envelope["latency_ms"], float)
    assert envelope["latency_ms"] >= 0.0


def test_dispatch_non_dict_arguments_returns_error_envelope(session: Session) -> None:
    """Arguments that are not a JSON object cannot be dispatched."""
    envelope = dispatch_tool(session, "run_reconciliation", ["not", "a", "dict"])
    assert envelope["status"] == "error"
    assert envelope["error_type"] == "INVALID_ARGUMENTS"
    assert envelope["tool"] == "run_reconciliation"
    assert envelope["message"] == "tool arguments must be a JSON object"
    assert isinstance(envelope["latency_ms"], float)


def test_dispatch_none_arguments_treated_as_empty(session: Session) -> None:
    """Null arguments (the model sent no argument object) dispatch cleanly."""
    result = dispatch_tool(session, "run_reconciliation", None)
    assert result["tool"] == "run_reconciliation"
    assert result["metrics"]["transactions"] == KWS["transactions"]


def test_dispatch_unknown_argument_returns_error_envelope(session: Session) -> None:
    """An argument outside the declared schema is rejected up front."""
    envelope = dispatch_tool(
        session, "query_ledger", {"merchant_id": "M001", "bogus": 1}
    )
    assert envelope["status"] == "error"
    assert envelope["error_type"] == "INVALID_ARGUMENTS"
    assert envelope["tool"] == "query_ledger"
    assert "bogus" in envelope["message"]
    assert "query_ledger" in envelope["message"]
    assert isinstance(envelope["latency_ms"], float)


def test_dispatch_missing_required_argument_returns_error_envelope(session: Session) -> None:
    """Missing required args return INVALID_ARGUMENTS with the arguments
    echoed in details (so the model can retry with a valid call)."""
    envelope = dispatch_tool(session, "check_gst_match", {})
    assert envelope["status"] == "error"
    assert envelope["error_type"] == "INVALID_ARGUMENTS"
    assert envelope["tool"] == "check_gst_match"
    assert "transaction_id" in envelope["message"]
    assert envelope["details"] == {"arguments": {}}

    propose = dispatch_tool(
        session, "propose_journal_entry", {"reason": "justification"}
    )
    assert propose["error_type"] == "INVALID_ARGUMENTS"
    assert propose["tool"] == "propose_journal_entry"
    assert "exception_id" in propose["message"]
    assert propose["details"]["arguments"] == {"reason": "justification"}
    for envelope in (envelope, propose):
        assert isinstance(envelope["latency_ms"], float)


def test_dispatch_value_error_returns_validation_error(session: Session) -> None:
    """A tool-raised ValueError becomes a VALIDATION_ERROR envelope."""
    envelope = dispatch_tool(
        session,
        "run_reconciliation",
        {"start_date": "2026-09-03", "end_date": "2026-08-30"},
    )
    assert envelope["status"] == "error"
    assert envelope["error_type"] == "VALIDATION_ERROR"
    assert envelope["tool"] == "run_reconciliation"
    assert envelope["message"] == "start_date must not be after end_date"
    assert envelope["details"] == {
        "arguments": {
            "start_date": "2026-09-03",
            "end_date": "2026-08-30",
        }
    }
    assert isinstance(envelope["latency_ms"], float)


def test_dispatch_tool_failure_rolls_back_and_recovers(session: Session) -> None:
    """An unexpected exception becomes TOOL_FAILURE, the half-applied state
    is rolled back, and the shared session stays usable afterwards.

    The forced failure is a FK violation: proposing with an agent run id
    that does not exist in agent_runs cannot be committed.
    """
    exception = _exception_of_type(session, "MISSING_SETTLEMENT")
    before = session.execute(
        select(func.count()).select_from(JournalProposal)
    ).scalar_one()

    envelope = dispatch_tool(
        session,
        "propose_journal_entry",
        {"exception_id": exception.id, "reason": "should fail"},
        run_id="RUN-DOES-NOT-EXIST",
    )
    assert envelope["status"] == "error"
    assert envelope["error_type"] == "TOOL_FAILURE"
    assert envelope["tool"] == "propose_journal_entry"
    assert envelope["message"].startswith("IntegrityError:")
    assert envelope["details"]["arguments"]["exception_id"] == exception.id
    assert envelope["details"]["arguments"]["run_id"] == "RUN-DOES-NOT-EXIST"
    assert isinstance(envelope["latency_ms"], float)

    # The rollback removed the pending proposal; nothing was half-applied.
    assert session.execute(
        select(func.count()).select_from(JournalProposal)
    ).scalar_one() == before

    # The same session stays usable for subsequent tool calls.
    recovered = dispatch_tool(session, "query_ledger", {"limit": 3})
    assert recovered["tool"] == "query_ledger"
    assert recovered["count"] == 3

# --- dispatch_tool: propose_journal_entry (PROPOSE class) ---------------------


def test_propose_journal_entry_happy_path_persists_pending_proposal(seeded, session: Session) -> None:
    """Proposing from a verified exception drafts a pending proposal: the
    payload states posted=False / requires_approval=True, and the row
    carries the run link, narrative, and evidence ids."""
    exception = _exception_of_type(session, "MISSING_SETTLEMENT")
    run = _create_run(session, "RUN-TEST-PROPOSE-1")
    run_id = run.run_id
    before = session.execute(
        select(func.count()).select_from(JournalProposal)
    ).scalar_one()

    result = dispatch_tool(
        session,
        "propose_journal_entry",
        {"exception_id": exception.id, "reason": "settlement never received"},
        run_id=run_id,
    )
    assert result["tool"] == "propose_journal_entry"
    assert result["status"] == "ok"
    assert result["posted"] is False
    assert result["requires_approval"] is True
    assert result["deduplicated"] is False
    assert result["sources"] == {
        "exception_id": exception.id,
        "transaction_id": exception.transaction_id,
    }

    # Exactly one new pending proposal row, linked to the run.
    rows = session.execute(select(JournalProposal)).scalars().all()
    assert len(rows) == before + 1
    proposal = rows[-1]
    assert proposal.agent_run_id == run_id
    assert proposal.status == "pending"
    assert proposal.transaction_id == exception.transaction_id
    assert float(proposal.amount) == pytest.approx(
        float(abs(exception.financial_impact))
    )
    assert proposal.narrative == (
        f"MISSING_SETTLEMENT correction for {exception.transaction_id}: "
        "settlement never received"
    )
    # Evidence: the transaction plus its settlement/invoice references.
    txn = seeded.txns[exception.transaction_id]
    expected_evidence = [exception.transaction_id] + [
        ref for ref in (txn["settlement_id"], txn["invoice_id"]) if ref
    ]
    assert list(proposal.evidence_ids) == expected_evidence

    # The payload mirrors the persisted row.
    payload = result["proposal"]
    assert payload["proposal_id"] == proposal.proposal_id
    assert payload["exception_id"] == exception.id
    assert payload["transaction_id"] == exception.transaction_id
    assert payload["merchant_id"] == txn["merchant_id"]
    assert payload["debit_account"] == BANK_ACCOUNT
    assert payload["credit_account"] == REVENUE_ACCOUNT
    assert payload["amount"] == pytest.approx(float(proposal.amount))
    assert payload["narrative"] == proposal.narrative
    assert payload["evidence_ids"] == expected_evidence
    assert payload["confidence"] == pytest.approx(
        CONFIDENCE_BY_SEVERITY[exception.severity]
    )
    assert payload["status"] == "pending"
    # entry_date is stored in a DateTime column, so it serializes as an
    # ISO timestamp at midnight on the exception's date.
    assert payload["entry_date"] == (
        exception.exception_date.isoformat() + "T00:00:00"
    )
    assert isinstance(result["latency_ms"], float)


def test_propose_journal_entry_is_idempotent_for_same_correction(seeded, session: Session) -> None:
    """Re-proposing the same correction returns the existing pending row
    (deduplicated=True) instead of creating a second one."""
    exception = _exception_of_type(session, "FEE_MISMATCH")
    run = _create_run(session, "RUN-TEST-PROPOSE-2")
    before = session.execute(
        select(func.count()).select_from(JournalProposal)
    ).scalar_one()

    first = dispatch_tool(
        session,
        "propose_journal_entry",
        {"exception_id": exception.id, "reason": "processor overcharged the fee"},
        run_id=run.run_id,
    )
    second = dispatch_tool(
        session,
        "propose_journal_entry",
        {"exception_id": exception.id, "reason": "processor overcharged the fee"},
        run_id=run.run_id,
    )
    assert first["status"] == "ok" and second["status"] == "ok"
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["proposal"]["proposal_id"] == first["proposal"]["proposal_id"]
    # One proposal row per unique correction, regardless of earlier tests.
    assert session.execute(
        select(func.count()).select_from(JournalProposal)
    ).scalar_one() == before + 1

def test_propose_journal_entry_direction_confidence_and_graceful_paths(seeded, session: Session) -> None:
    """Direction follows the exception type (duplicates reverse revenue),
    confidence follows severity, and every soft-fail path returns a
    graceful envelope instead of raising."""
    # Direction + confidence across the injected severity spectrum.
    run = _create_run(session, "RUN-TEST-PROPOSE-3")
    for exception_type in ("GST_MISMATCH", "FAILED_LEDGER_WRITE", "DUPLICATE_TRANSACTION"):
        exception = _exception_of_type(session, exception_type)
        result = dispatch_tool(
            session,
            "propose_journal_entry",
            {"exception_id": exception.id, "reason": "verified correction"},
            run_id=run.run_id,
        )
        assert result["status"] == "ok"
        assert result["posted"] is False
        assert result["requires_approval"] is True
        debit, credit = _expected_direction(exception.exception_type, exception.financial_impact)
        assert result["proposal"]["debit_account"] == debit
        assert result["proposal"]["credit_account"] == credit
        assert result["proposal"]["confidence"] == pytest.approx(
            CONFIDENCE_BY_SEVERITY[exception.severity]
        )
        assert result["proposal"]["amount"] == pytest.approx(
            float(abs(exception.financial_impact))
        )

    # A non-numeric exception id is a graceful invalid_exception_id.
    invalid = dispatch_tool(
        session, "propose_journal_entry", {"exception_id": "NaN", "reason": "why"}
    )
    assert invalid["tool"] == "propose_journal_entry"
    assert invalid["status"] == "invalid_exception_id"
    assert invalid["exception_id"] == "NaN"
    assert "numeric" in invalid["message"]
    assert isinstance(invalid["latency_ms"], float)

    # An unknown numeric id is a graceful not_found.
    missing = dispatch_tool(
        session, "propose_journal_entry", {"exception_id": 10**9, "reason": "why"}
    )
    assert missing["status"] == "not_found"
    assert missing["exception_id"] == 10**9
    assert missing["sources"] == {"exception_id": 10**9}
    assert isinstance(missing["latency_ms"], float)

    # An empty reason is a tool-raised ValueError -> VALIDATION_ERROR.
    empty = dispatch_tool(
        session, "propose_journal_entry", {"exception_id": 1, "reason": "   "}
    )
    assert empty["status"] == "error"
    assert empty["error_type"] == "VALIDATION_ERROR"
    assert empty["message"] == "reason must be a non-empty string"
    assert empty["details"]["arguments"]["reason"] == "   "

# --- controller loop ---------------------------------------------------------


def test_controller_completed_run_persists_trace(seeded, session: Session) -> None:
    """A tool-calling round followed by a final answer: agent_runs and
    tool_calls rows are persisted, the message protocol is spoken, and
    the run result carries tools_used and record-id evidence."""
    provider = FakeProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[
                    LLMToolCall(id="call-1", name="run_reconciliation", args={})
                ]
            ),
            LLMResponse(tool_calls=[], text="Investigation complete: exceptions found."),
        ]
    )
    controller = AgentController(provider, session)
    result = controller.run("Investigate exceptions")

    assert result["status"] == STATUS_COMPLETED
    assert result["answer"] == "Investigation complete: exceptions found."
    assert result["tools_used"] == ["run_reconciliation"]
    assert len(result["tool_calls"]) == 1
    assert isinstance(result["run_id"], str) and result["run_id"].startswith("RUN-")
    assert result["total_llm_latency_ms"] >= 0.0

    # The one tool call was executed, traced, and serialized (no raw
    # date/Decimal objects in the JSON-bound trace).
    trace = result["tool_calls"][0]
    assert trace["tool_name"] == "run_reconciliation"
    assert trace["status"] == "ok"
    assert trace["arguments"] == {}
    assert isinstance(trace["latency_ms"], float)
    assert trace["result"]["metrics"]["transactions"] > 0

    # agent_runs row finalized.
    run = session.get(AgentRun, result["run_id"])
    assert run is not None
    assert run.status == STATUS_COMPLETED
    assert run.user_query == "Investigate exceptions"
    assert run.final_response == result["answer"]
    assert run.tool_call_count == 1
    assert run.error is None
    assert run.finished_at is not None

    # tool_calls row persisted with the same trace content.
    calls = session.execute(
        select(ToolCall).where(ToolCall.run_id == run.run_id).order_by(ToolCall.seq)
    ).scalars().all()
    assert len(calls) == 1
    assert calls[0].seq == 1
    assert calls[0].tool_name == "run_reconciliation"
    assert calls[0].status == "ok"
    assert calls[0].arguments == {}
    assert calls[0].result["metrics"] == trace["result"]["metrics"]
    assert calls[0].latency_ms == pytest.approx(trace["latency_ms"])

    # Provider protocol: round 1 saw the user message + the 6 tool
    # declarations; round 2 saw the tool calls echoed and their results.
    assert provider.calls == 2
    first_messages = provider.seen[0]["messages"]
    second_messages = provider.seen[1]["messages"]
    assert len(first_messages) == 1
    assert isinstance(first_messages[0], TextMessage)
    assert first_messages[0].text == "Investigate exceptions"
    assert [tool["name"] for tool in provider.seen[0]["tools"]] == [
        d["name"] for d in TOOL_DECLARATIONS
    ]
    assert len(second_messages) == 3
    assert isinstance(second_messages[1], ToolCallsMessage)
    assert second_messages[1].calls[0].name == "run_reconciliation"
    assert isinstance(second_messages[2], ToolResultsMessage)
    tool_result = second_messages[2].results[0]
    assert tool_result.id == "call-1"
    assert tool_result.name == "run_reconciliation"
    assert tool_result.result["metrics"] == trace["result"]["metrics"]

    # Evidence extraction surfaced the record ids from the recon result.
    assert result["evidence"]
    assert all(ID_RE.match(e) for e in result["evidence"])
    flagged_txn_ids = {
        e["transaction_id"] for e in trace["result"]["exceptions"]
    }
    assert flagged_txn_ids <= set(result["evidence"])

def test_controller_no_tool_round_and_merchant_scope(seeded, session: Session) -> None:
    """A final answer without tool calls produces a clean completed run,
    and the merchant scope lands in the system instruction (not the chat)."""
    provider = FakeProvider([LLMResponse(tool_calls=[], text="How can I help?")])
    tool_calls_before = session.execute(
        select(func.count()).select_from(ToolCall)
    ).scalar_one()
    controller = AgentController(provider, session, merchant_id="M001")
    result = controller.run("hi")

    assert result["status"] == STATUS_COMPLETED
    assert result["answer"] == "How can I help?"
    assert result["tool_calls"] == []
    assert result["tools_used"] == []
    assert result["evidence"] == []
    # A no-tool round persists no tool_calls rows (relative check keeps the
    # test independent of rows written by earlier tests).
    assert session.execute(
        select(func.count()).select_from(ToolCall)
    ).scalar_one() == tool_calls_before

    seen = provider.seen[0]
    assert "merchant_id=M001" in seen["system_instruction"]
    assert all(
        not (isinstance(m, TextMessage) and "M001" in m.text)
        for m in seen["messages"]
    )


def test_controller_feeds_error_envelopes_back_to_provider(seeded, session: Session) -> None:
    """A failing tool call is dispatched into an error envelope, traced as
    status=error, and fed back to the provider (never raised)."""
    provider = FakeProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[
                    LLMToolCall(id="bad-1", name="no_such_tool", args={}),
                    LLMToolCall(
                        id="bad-2",
                        name="run_reconciliation",
                        args={"start_date": "2026-09-03", "end_date": "2026-08-30"},
                    ),
                ]
            ),
            LLMResponse(tool_calls=[], text="Both tools failed; no numbers to report."),
        ]
    )
    controller = AgentController(provider, session)
    result = controller.run("break things")

    assert result["status"] == STATUS_COMPLETED
    assert [call["status"] for call in result["tool_calls"]] == ["error", "error"]
    assert [call["error"] for call in result["tool_calls"]] == [
        "UNKNOWN_TOOL",
        "VALIDATION_ERROR",
    ]
    for call in result["tool_calls"]:
        assert call["result"]["status"] == "error"

    results_msg = provider.seen[1]["messages"][2]
    assert isinstance(results_msg, ToolResultsMessage)
    assert [r.result["error_type"] for r in results_msg.results] == [
        "UNKNOWN_TOOL",
        "VALIDATION_ERROR",
    ]
    assert [r.id for r in results_msg.results] == ["bad-1", "bad-2"]
    # latency_ms is moved from the envelope into the trace entry, so the
    # provider receives results without it.
    for res in results_msg.results:
        assert "latency_ms" not in res.result
    for call in result["tool_calls"]:
        assert isinstance(call["latency_ms"], float)


def test_controller_tool_limit_ends_run_safely(seeded, session: Session) -> None:
    """Hitting the configured tool-call limit ends the run with a
    deterministic, evidence-based summary (status tool_limit)."""
    endless = LLMResponse(
        text=None,
        tool_calls=[
            LLMToolCall(id="loop", name="query_ledger", args={"limit": 1})
        ]
    )
    provider = FakeProvider([endless])
    controller = AgentController(provider, session, max_tool_calls=3)
    result = controller.run("keep calling tools")

    assert result["status"] == STATUS_TOOL_LIMIT
    assert result["answer"].startswith(
        "I stopped after reaching the configured tool-call limit"
    )
    assert result["answer"].endswith("query_ledger(limit=1).")
    assert len(result["tool_calls"]) == 3
    assert result["tool_calls"][-1]["status"] == "ok"
    # The provider saw 4 rounds: three that executed their call, then the
    # round whose excess call tripped the limit (never dispatched).
    assert provider.calls == 4
    # Each round after the first echoes the previous results (1, 3, 5, 7 msgs).
    assert [len(s["messages"]) for s in provider.seen] == [1, 3, 5, 7]


def test_controller_provider_error_ends_run_with_model_error(seeded, session: Session) -> None:
    """A provider failure (LLMProviderError) ends the run with status
    model_error, a safe fallback answer, and the trace persisted."""
    provider = FakeProvider(
        [LLMProviderError("Gemini request failed: quota exceeded")]
    )
    controller = AgentController(provider, session)
    result = controller.run("any question")

    assert result["status"] == STATUS_MODEL_ERROR
    assert result["answer"].startswith("The AI model is temporarily unavailable")
    assert result["run_id"] in result["answer"]
    assert result["tool_calls"] == []
    assert result["tools_used"] == []

    run = session.get(AgentRun, result["run_id"])
    assert run is not None
    assert run.status == STATUS_MODEL_ERROR
    assert "quota exceeded" in run.error
    assert run.final_response is None
    assert run.finished_at is not None


def test_run_agent_wrapper_matches_controller(seeded, session: Session) -> None:
    """The one-call convenience wrapper returns the controller result."""
    provider = FakeProvider([LLMResponse(tool_calls=[], text="done.")])
    result = run_agent(provider, session, "hello", merchant_id="M002")
    assert result["status"] == STATUS_COMPLETED
    assert result["answer"] == "done."
    assert "merchant_id=M002" in provider.seen[0]["system_instruction"]

# --- HTTP API ---------------------------------------------------------------


def test_agent_chat_endpoint_runs_the_loop(seeded) -> None:
    """POST /api/agent/chat with overridden get_db + get_provider runs the
    controller end-to-end and returns the section-13 response shape."""
    provider = FakeProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="query_ledger",
                        args={"merchant_id": "M001", "limit": 3},
                    )
                ]
            ),
            LLMResponse(tool_calls=[], text="M001 ledger looks clean."),
        ]
    )

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
                json={"message": "Show me M001 ledger rows", "merchant_id": "M001"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["answer"] == "M001 ledger looks clean."
    assert body["tools_used"] == ["query_ledger"]
    assert len(body["tool_calls"]) == 1
    assert body["tool_calls"][0]["tool_name"] == "query_ledger"
    assert body["tool_calls"][0]["status"] == "ok"
    assert body["total_llm_latency_ms"] >= 0.0
    assert body["run_id"].startswith("RUN-")

    # The merchant scope reached the system instruction via the route.
    assert "merchant_id=M001" in provider.seen[0]["system_instruction"]


def test_agent_chat_endpoint_model_error_still_serializes(seeded) -> None:
    """A provider failure inside the route returns 200 with status
    model_error and the safe fallback answer (never a 500)."""

    def override_db() -> Iterator[Session]:
        db = Session(seeded.engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_provider] = lambda: FakeProvider(
        [LLMProviderError("Gemini request failed: boom")]
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/chat", json={"message": "hello"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "model_error"
    assert body["answer"].startswith("The AI model is temporarily unavailable")
    assert body["tools_used"] == []
    assert body["tool_calls"] == []


def test_agent_chat_endpoint_validates_request(seeded) -> None:
    """An empty message fails pydantic validation with 422 (never reaches
    the provider)."""

    class ExplodingProvider(LLMProvider):
        name = "exploding"
        model = "x"

        def generate(self, messages, tools, **kwargs):
            raise AssertionError("provider must not be reached")

    app.dependency_overrides[get_provider] = lambda: ExplodingProvider()
    try:
        with TestClient(app) as client:
            response = client.post("/api/agent/chat", json={"message": ""})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_get_provider_503_without_api_key(monkeypatch) -> None:
    """Without GEMINI_API_KEY the dependency raises 503 with a safe,
    actionable message (the deterministic tools never need the key)."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(
        "app.api.routes.agent.get_settings",
        lambda: Settings(gemini_api_key="", gemini_model="gemini-2.5-flash"),
    )
    with pytest.raises(HTTPException) as exc_info:
        get_provider()
    assert exc_info.value.status_code == 503
    assert "Gemini API key is not configured" in exc_info.value.detail
    assert "GEMINI_API_KEY" in exc_info.value.detail


def test_get_provider_builds_configured_gemini_provider(monkeypatch) -> None:
    """With a key configured the dependency returns a GeminiProvider
    wired to the settings' model (constructor only; no network calls)."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setattr(
        "app.api.routes.agent.get_settings",
        lambda: Settings(gemini_api_key="k", gemini_model="gemini-2.5-pro"),
    )
    provider = get_provider()
    assert type(provider).__name__ == "GeminiProvider"
    assert provider.name == "gemini"
    assert provider.model == "gemini-2.5-pro"


def test_get_provider_503_when_sdk_unavailable(monkeypatch) -> None:
    """When the SDK import failed (or the key is empty at construction),
    the dependency surfaces 503 instead of a raw provider error."""
    monkeypatch.setattr(
        "app.api.routes.agent.get_settings",
        lambda: Settings(gemini_api_key="k", gemini_model="gemini-2.5-flash"),
    )
    monkeypatch.setattr(
        "app.api.routes.agent.GeminiProvider",
        lambda **kwargs: (_ for _ in ()).throw(
            LLMProviderError("google-genai SDK is not installed")
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        get_provider()
    assert exc_info.value.status_code == 503
    assert "LLM provider unavailable" in exc_info.value.detail










