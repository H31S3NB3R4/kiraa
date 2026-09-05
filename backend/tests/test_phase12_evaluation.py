"""Phase 12 tests: the evaluation harness.

Mirrors the earlier fixture patterns: the dev dataset (seed 42, 100
transactions, 1 exception per type) and the benchmark-shaped dataset
(500 transactions, 5 exceptions per type) are seeded into temp SQLite
DBs, then the harness is validated end to end:

- the reconciliation block scores a read-only engine pass against the
  dataset labels (match accuracy, exception precision/recall, exact-type
  accuracy) — verified against an *independent* recomputation from a
  direct tool call, not the harness's own arithmetic,
- the anomaly block carries the tool's ground-truth metrics (precision,
  recall, FPR); the 500-record benchmark scores 100/100/0,
- ``evaluation_report`` stays strictly read-only on engine tables (no
  ``reconciliation_exceptions`` / ``anomaly_scores`` rows ever appear)
  and is deterministic across two full runs,
- the scripted agent benchmark exercises the real controller loop,
- ``benchmark_table`` renders the fixed todo Phase 12 table shape with
  the report's real numbers,
- the CLI script seeds the fixed benchmark, writes a machine-readable
  JSON report, and prints the table,
- ``GET /api/evaluation`` serves the same report (engine scores only,
  never starting an agent run from a GET) plus the stored agent run
  history, and returns the ``no_labels`` guard envelope (HTTP 200) on an
  unlabelled database.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.controller import run_agent
from app.api.schemas.evaluation import EvaluationResponse
from app.db.session import get_db
from app.main import app
from app.models import AgentRun, AnomalyScore, ReconciliationException
from app.services.dataset_generator import generate_dataset, write_dataset
from app.services.db_seed import build_engine, load_dataset_file, seed_database
from app.services.evaluation import (
    EvaluationError,
    ScriptedEvalProvider,
    benchmark_table,
    evaluate_engines,
    evaluation_report,
)
from app.tools import run_reconciliation

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1,
       "customers": 80, "seed": 42}
BENCH_KWS = {"transactions": 500, "window_days": 56, "exceptions_per_type": 5,
             "customers": 200, "seed": 42}
END = date(2026, 9, 3)  # same anchor day as the Phase 2-11 suites

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_evaluation.py"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate the dev dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase12")
    dataset = generate_dataset(**KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "dataset")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'finance.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    bundle.labels = {l["transaction_id"]: l for l in dataset["labels"]}
    return bundle


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    """Benchmark-shaped dataset (500 txns, 5 exceptions per type) -> temp DB."""
    out_dir = tmp_path_factory.mktemp("phase12_bench")
    dataset = generate_dataset(**BENCH_KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "benchmark")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'benchmark.db'}")
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

# ---------------------------------------------------------------------------
# Engine scoring
# ---------------------------------------------------------------------------


def test_reconciliation_block_matches_independent_recomputation(seeded) -> None:
    """The recon block scores a read-only pass; every counter is verified
    against an independent recount from a direct tool call."""
    with Session(seeded.engine) as db:
        report = evaluate_engines(db)["reconciliation"]
        direct = run_reconciliation(db, persist=False)

    flagged = {e["transaction_id"] for e in direct["exceptions"]}
    truth_pos = {t for t, l in seeded.labels.items() if l["recon_exception"]}
    truth_neg = {t for t, l in seeded.labels.items() if not l["recon_exception"]}
    tp = len(flagged & truth_pos)
    fp = len(flagged & truth_neg)
    fn = len(truth_pos - flagged)

    assert report["records"] == KWS["transactions"] == 100
    assert report["true_positives"] == tp == 9
    assert report["false_positives"] == fp == 0
    assert report["false_negatives"] == fn == 0
    assert report["ground_truth_exceptions"] == 9
    assert report["match_accuracy_pct"] == 100.0
    assert report["exception_precision_pct"] == 100.0
    assert report["exception_recall_pct"] == 100.0
    assert report["exception_type_accuracy_pct"] == 100.0
    assert report["unresolved_exception_transactions"] == []
    assert report["financial_impact_at_risk"] == pytest.approx(
        direct["metrics"]["total_financial_impact"]
    )
    assert isinstance(report["latency_ms"], float)
    assert report["latency_ms"] >= 0.0


def test_benchmark_dataset_scores_perfectly(bench) -> None:
    """The 500-record fixed benchmark: all 45 injected recon exceptions and
    all 5 hidden anomalies are found with zero false positives."""
    with Session(bench.engine) as db:
        report = evaluation_report(db, include_agent=False)

    recon = report["reconciliation"]
    anomaly = report["anomaly"]
    assert recon["records"] == 500
    assert recon["ground_truth_exceptions"] == 45
    assert recon["true_positives"] == 45
    assert recon["false_positives"] == 0
    assert recon["exception_recall_pct"] == 100.0
    assert anomaly["ground_truth_anomalies"] == 5
    assert anomaly["precision_pct"] == 100.0
    assert anomaly["recall_pct"] == 100.0
    assert anomaly["false_positive_rate_pct"] == 0.0
    assert report["unresolved_exceptions"] == 0
    assert report["records_processed"] == 500
    assert report["synthetic"] is True
    assert report["throughput_records_per_min"] > 0


def test_harness_is_strictly_read_only_on_engine_tables(seeded) -> None:
    """The engine-scoring passes never write rows; the scripted agent runs
    do persist — through the production tool path (Phase 8 behaviour) —
    but idempotently: a second full report adds no rows anywhere."""
    def counts() -> tuple[int, int, int]:
        with Session(seeded.engine) as db:
            return (
                db.execute(
                    select(func.count()).select_from(ReconciliationException)
                ).scalar_one(),
                db.execute(
                    select(func.count()).select_from(AnomalyScore)
                ).scalar_one(),
                db.execute(
                    select(func.count()).select_from(AgentRun)
                ).scalar_one(),
            )

    start = counts()

    # 1. The engine benchmarks alone (persist=False) change nothing.
    with Session(seeded.engine) as db:
        evaluate_engines(db)
    assert counts() == start

    # 2. A full report: the scripted runs persist their own traces and the
    #    agent's production-path tool calls upsert engine rows — once.
    with Session(seeded.engine) as db:
        report = evaluation_report(db, include_agent=True)
    exc, anom, runs = counts()
    assert runs == start[2] + report["agent"]["runs"]
    assert report["agent"]["completed_runs"] == report["agent"]["runs"]
    assert report["agent"]["tool_calls_per_run"] >= 1.0
    assert report["agent"]["failed_tool_calls"] == 0
    assert report["agent"]["tool_failure_rate_pct"] == 0.0
    assert report["agent"]["average_latency_ms"] > 0.0
    assert all(v > 0.0 for v in report["agent"]["latency_by_run_ms"])

    # 3. A second full report: no duplicates anywhere (idempotent upsert)
    #    apart from the new scripted runs' own trace rows.
    with Session(seeded.engine) as db:
        second = evaluation_report(db, include_agent=True)
    assert counts() == (exc, anom, runs + second["agent"]["runs"])
    assert second["reconciliation"]["true_positives"] == (
        report["reconciliation"]["true_positives"]
    )


def test_report_is_deterministic_across_runs(seeded) -> None:
    """Two harness passes over the same database produce identical scores
    (wall-clock latency fields excluded)."""

    def snapshot(db: Session) -> dict:
        report = evaluation_report(db, include_agent=False)
        recon = dict(report["reconciliation"])
        anomaly = dict(report["anomaly"])
        recon.pop("latency_ms")
        anomaly.pop("latency_ms")
        return {
            "records_processed": report["records_processed"],
            "reconciliation": recon,
            "anomaly": anomaly,
            "unresolved_exceptions": report["unresolved_exceptions"],
        }

    with Session(seeded.engine) as db:
        first = snapshot(db)
        second = snapshot(db)

    assert first == second


def test_unlabelled_database_raises_evaluation_error(tmp_path) -> None:
    """A database without seeded labels cannot be scored: the service
    raises EvaluationError instead of inventing metrics."""
    from app.models.base import Base

    engine = build_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        with pytest.raises(EvaluationError):
            evaluate_engines(db)
    engine.dispose()
# ---------------------------------------------------------------------------
# Benchmark table
# ---------------------------------------------------------------------------


def test_benchmark_table_renders_the_todo_shape(seeded) -> None:
    """The rendered table carries exactly the todo Phase 12 rows with the
    report's real numbers (never blanks or estimates)."""
    with Session(seeded.engine) as db:
        report = evaluation_report(db, include_agent=True)

    table = benchmark_table(report)
    lines = table.splitlines()

    assert "Records processed:" in lines[0]
    assert str(report["records_processed"]) in lines[0]
    for label, block, key in (
        ("Reconciliation accuracy:", "reconciliation", "match_accuracy_pct"),
        ("Exception precision:", "reconciliation", "exception_precision_pct"),
        ("Exception recall:", "reconciliation", "exception_recall_pct"),
        ("Anomaly precision:", "anomaly", "precision_pct"),
        ("Anomaly recall:", "anomaly", "recall_pct"),
        ("False-positive rate:", "anomaly", "false_positive_rate_pct"),
    ):
        line = next(l for l in lines if l.startswith(label))
        assert str(report[block][key]) in line
        assert line.rstrip().endswith("%")
    avg = next(l for l in lines if l.startswith("Average latency:"))
    assert str(report["agent"]["average_latency_ms"]) in avg
    throughput = next(l for l in lines if l.startswith("Throughput:"))
    assert str(report["throughput_records_per_min"]) in throughput
    unresolved = next(l for l in lines if l.startswith("Unresolved exceptions:"))
    assert str(report["unresolved_exceptions"]) in unresolved


# ---------------------------------------------------------------------------
# CLI script
# ---------------------------------------------------------------------------


def test_cli_script_reports_and_writes_machine_readable_report(
    tmp_path, monkeypatch, capsys
) -> None:
    """``run_evaluation.py`` seeds the fixed benchmark, prints the table,
    and writes a JSON report whose numbers match the service."""
    import runpy
    import sys

    db_url = f"sqlite:///{tmp_path / 'cli_eval.db'}"
    report_path = tmp_path / "evaluation_report.json"

    monkeypatch.setattr(
        sys,
        "argv",
        ["run_evaluation.py", "--database-url", db_url, "--report", str(report_path)],
    )
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
    assert exc_info.value.code in (0, None)

    captured = capsys.readouterr()
    assert "Records processed:" in captured.out
    assert "500" in captured.out

    assert report_path.exists()
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["status"] == "ok"
    assert saved["records_processed"] == 500
    assert saved["reconciliation"]["exception_recall_pct"] == 100.0
    assert saved["anomaly"]["precision_pct"] == 100.0
    assert "agent" in saved
    assert saved["agent"]["completed_runs"] == saved["agent"]["runs"]


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def http(seeded) -> Iterator[TestClient]:
    """TestClient with the module DB behind ``get_db`` (Phase 9 pattern)."""
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


def test_evaluation_endpoint_serves_engine_scores_and_history(seeded, http) -> None:
    """``GET /api/evaluation`` returns the service report (engines, no live
    agent benchmark) plus aggregates over the stored run history."""
    with Session(seeded.engine) as db:
        for _ in range(2):
            run_agent(ScriptedEvalProvider(), db, "Reconcile this week.")

    response = http.get("/api/evaluation")

    assert response.status_code == 200
    body = EvaluationResponse(**response.json())
    assert body.status == "ok"
    assert body.records_processed == 100
    assert body.reconciliation["exception_recall_pct"] == 100.0
    assert body.anomaly["precision_pct"] == 100.0
    assert body.unresolved_exceptions == 0
    # Engine-only (a GET must never start an agent run).
    assert "agent" not in response.json()
    assert body.agent_history is not None
    assert body.agent_history.runs >= 2
    assert body.agent_history.completed_runs >= 2
    assert body.agent_history.total_tool_calls >= 2
    assert body.agent_history.source == "stored_run_history"


def test_evaluation_endpoint_read_only(seeded, http) -> None:
    """The GET never writes engine rows."""
    with Session(seeded.engine) as db:
        before = db.execute(
            select(func.count()).select_from(ReconciliationException)
        ).scalar_one()

    assert http.get("/api/evaluation").status_code == 200

    with Session(seeded.engine) as db:
        after = db.execute(
            select(func.count()).select_from(ReconciliationException)
        ).scalar_one()
    assert after == before


def test_evaluation_endpoint_guard_envelope_on_unlabelled_db(tmp_path) -> None:
    """An unlabelled database returns the ``no_labels`` guard envelope with
    HTTP 200 — a valid state, not an error."""
    from app.models.base import Base

    engine = build_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    Base.metadata.create_all(engine)

    def override_db() -> Iterator[Session]:
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.get("/api/evaluation")
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert response.status_code == 200
    assert response.json()["status"] == "no_labels"
    assert response.json()["records_processed"] == 0