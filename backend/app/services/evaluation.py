"""Evaluation harness service (Phase 12, todo "Evaluation Harness").

Scores the deterministic engines and the agent layer against the seeded
ground truth (``dataset_labels``, Phase 1) and returns one machine-
readable report:

- **reconciliation**: a read-only ``run_reconciliation`` pass is scored
  against ``dataset_labels.recon_exception`` — match accuracy over all
  transactions, exception precision/recall, and exact-type accuracy over
  the detected exceptions,
- **anomaly**: a read-only ``detect_anomalies`` pass is scored through its
  ground-truth block against ``dataset_labels.anomaly`` — precision,
  recall, and false-positive rate,
- **agent**: latency, tool calls, and throughput are measured over a
  scripted provider (deterministic, offline — the same ``LLMProvider``
  contract the Gemini adapter implements), so the numbers are
  reproducible; ``tool_failure_rate`` aggregates the executed calls,
- **throughput**: records processed per minute across the full harness
  pass (reconciliation + anomaly scan over the benchmark records),
- **unresolved exceptions**: ground-truth exception/anomaly transactions
  the engines left unflagged (recall misses) — they stay listed so no
  failure hides behind an aggregate percentage.

Every number is computed from a real run over a real database (golden
rule): nothing here invents or estimates metrics. ``benchmark_table``
renders the fixed table shape from todo Phase 12.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.controller import run_agent
from app.agent.providers.base import LLMProvider, LLMResponse, LLMToolCall
from app.models import DatasetLabel
from app.tools import detect_anomalies, run_reconciliation

__all__ = [
    "EvaluationError",
    "benchmark_table",
    "evaluate_engines",
    "evaluation_report",
]


class EvaluationError(RuntimeError):
    """The harness cannot score this database (e.g. no seeded labels)."""


# ---------------------------------------------------------------------------
# Ground truth loading
# ---------------------------------------------------------------------------


def _ground_truth(db: Session) -> dict[str, DatasetLabel]:
    """All seeded labels keyed by transaction id (raises when absent)."""
    labels = {
        row.transaction_id: row
        for row in db.execute(select(DatasetLabel)).scalars()
    }
    if not labels:
        raise EvaluationError(
            "no dataset_labels rows in this database; seed a dataset first "
            "(the harness scores engine output against the Phase 1 ground truth)"
        )
    return labels

# ---------------------------------------------------------------------------
# Reconciliation scoring
# ---------------------------------------------------------------------------


def _score_reconciliation(
    db: Session, labels: dict[str, DatasetLabel]
) -> dict[str, Any]:
    """Score one read-only reconciliation pass against the labels."""
    started = time.perf_counter()
    result = run_reconciliation(db, persist=False)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    flagged = {
        exc["transaction_id"]: exc["exception_type"]
        for exc in result["exceptions"]
    }
    labelled_txn_ids = list(labels)

    truth_positive = [t for t in labelled_txn_ids if labels[t].recon_exception]
    truth_negative = [t for t in labelled_txn_ids if not labels[t].recon_exception]

    true_positives = [t for t in truth_positive if t in flagged]
    false_positives = [t for t in truth_negative if t in flagged]
    false_negatives = [t for t in truth_positive if t not in flagged]

    # Type accuracy: the detected exception also carries the labelled
    # scenario as its exception_type. NORMAL/HIDDEN_ANOMALY rows are never
    # ground-truth exceptions, so they never enter the denominator.
    type_expected = [
        t for t in true_positives
        if labels[t].scenario not in {"NORMAL", "HIDDEN_ANOMALY"}
    ]
    type_hits = [
        t for t in type_expected if flagged[t] == labels[t].scenario
    ]

    def _pct(numerator: int, denominator: int) -> float:
        return round(100.0 * numerator / denominator, 2) if denominator else 100.0

    total = len(labelled_txn_ids)
    return {
        "tool": "run_reconciliation",
        "records": total,
        "matched": result["metrics"]["matched"],
        "exceptions_detected": len(flagged),
        "ground_truth_exceptions": len(truth_positive),
        "true_positives": len(true_positives),
        "false_positives": len(false_positives),
        "false_negatives": len(false_negatives),
        # Match accuracy: transactions whose engine verdict equals the
        # ground-truth verdict (both clean, or both flagged).
        "match_accuracy_pct": _pct(
            total - len(false_positives) - len(false_negatives), total
        ),
        "exception_precision_pct": _pct(
            len(true_positives), len(true_positives) + len(false_positives)
        ),
        "exception_recall_pct": _pct(
            len(true_positives), len(true_positives) + len(false_negatives)
        ),
        "exception_type_accuracy_pct": _pct(len(type_hits), len(type_expected)),
        "financial_impact_at_risk": result["metrics"]["total_financial_impact"],
        "latency_ms": round(elapsed_ms, 2),
        "unresolved_exception_transactions": false_negatives,
    }


# ---------------------------------------------------------------------------
# Anomaly scoring
# ---------------------------------------------------------------------------


def _score_anomaly(
    db: Session, labels: dict[str, DatasetLabel]
) -> dict[str, Any]:
    """Score one read-only anomaly pass against the labels."""
    started = time.perf_counter()
    result = detect_anomalies(db, persist=False)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    truth = result.get("ground_truth")
    if not truth:
        raise EvaluationError(
            "detect_anomalies returned no ground-truth metrics; the harness "
            "cannot score anomaly precision/recall without seeded labels"
        )

    flagged_ids = {row["transaction_id"] for row in result["scores"] if row["is_anomaly"]}
    truth_positive = [t for t in labels if labels[t].anomaly]
    false_negatives = [t for t in truth_positive if t not in flagged_ids]

    return {
        "tool": "detect_anomalies",
        "records": truth["labelled_transactions"],
        "flagged": len(flagged_ids),
        "ground_truth_anomalies": truth["ground_truth_anomalies"],
        "precision_pct": round(truth["precision"] * 100.0, 2),
        "recall_pct": round(truth["recall"] * 100.0, 2),
        "false_positive_rate_pct": round(truth["false_positive_rate"] * 100.0, 2),
        "model": result.get("model"),
        "latency_ms": round(elapsed_ms, 2),
        "unresolved_anomaly_transactions": false_negatives,
    }
# ---------------------------------------------------------------------------
# Agent benchmark (offline, deterministic)
# ---------------------------------------------------------------------------


class ScriptedEvalProvider(LLMProvider):
    """Deterministic offline provider: one reconciliation call, one answer.

    Implements the same ``LLMProvider`` contract as the Gemini adapter, so
    the benchmarked controller path is the production path — only the
    network is replaced. Latency therefore measures the controller +
    tools + persistence, not the provider network.
    """

    name = "scripted-eval"
    model = "offline-benchmark"

    def __init__(self) -> None:
        self._calls = 0

    def generate(
        self,
        messages,
        tools,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                text=None,
                tool_calls=[
                    LLMToolCall(id="eval-1", name="run_reconciliation", args={})
                ],
            )
        return LLMResponse(
            tool_calls=[],
            text=(
                "Reconciliation complete: the aggregate metrics and the "
                "exception-level evidence are in the tool result above."
            ),
        )


def _benchmark_agent(db: Session, *, runs: int = 3) -> dict[str, Any]:
    """Measure agent latency / tool calls / failures with a scripted provider."""
    latencies: list[float] = []
    tool_calls = 0
    failed_calls = 0
    statuses: list[str] = []
    for _ in range(runs):
        started = time.perf_counter()
        outcome = run_agent(ScriptedEvalProvider(), db, "Reconcile this week.")
        latencies.append((time.perf_counter() - started) * 1000.0)
        tool_calls += len(outcome["tool_calls"])
        failed_calls += sum(
            1 for call in outcome["tool_calls"] if call["status"] == "error"
        )
        statuses.append(outcome["status"])

    return {
        "runs": runs,
        "completed_runs": sum(1 for s in statuses if s == "completed"),
        "tool_calls_per_run": round(tool_calls / runs, 2),
        "failed_tool_calls": failed_calls,
        "tool_failure_rate_pct": round(100.0 * failed_calls / tool_calls, 2)
        if tool_calls
        else 0.0,
        "average_latency_ms": round(sum(latencies) / len(latencies), 2),
        "latency_by_run_ms": [round(v, 2) for v in latencies],
        "provider": "scripted-offline",
        "note": (
            "Latency measured with the deterministic offline provider over the "
            "real controller loop + tools + persistence (no network calls); "
            "provider network latency is excluded by design."
        ),
    }
# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def evaluate_engines(db: Session) -> dict[str, Any]:
    """Run the Phase 12 engine benchmarks over ``db`` (strictly read-only).

    Both engine passes run with ``persist=False``: the harness never
    writes ``reconciliation_exceptions`` or ``anomaly_scores`` rows, so
    repeated evaluations cannot drift the stored operational facts (the
    golden rule — evaluate the facts, never mutate them).
    """
    labels = _ground_truth(db)
    recon = _score_reconciliation(db, labels)
    anomaly = _score_anomaly(db, labels)
    return {"reconciliation": recon, "anomaly": anomaly}


def evaluation_report(db: Session, *, include_agent: bool = True) -> dict[str, Any]:
    """Assemble the full Phase 12 evaluation report for ``db``.

    ``include_agent=False`` skips the scripted agent benchmark — used by
    the read-only HTTP surface, which reports stored run history instead
    of executing new agent runs (a GET must never start one).
    """
    started = time.perf_counter()
    engines = evaluate_engines(db)
    records = engines["reconciliation"]["records"]

    # Throughput: every record the harness processed — one reconciliation
    # pass + one anomaly pass, plus one agent conversation per scripted
    # run when the agent benchmark is included.
    processed = records * 2
    if include_agent:
        agent = _benchmark_agent(db)
        processed += agent["runs"]

    report: dict[str, Any] = {
        "tool": "evaluation_report",
        "status": "ok",
        "synthetic": True,
        "records_processed": records,
        "reconciliation": engines["reconciliation"],
        "anomaly": engines["anomaly"],
        "unresolved_exceptions": len(
            engines["reconciliation"]["unresolved_exception_transactions"]
        )
        + len(engines["anomaly"]["unresolved_anomaly_transactions"]),
        "throughput_records_per_min": round(
            60.0 * processed / max(time.perf_counter() - started, 1e-6), 2
        ),
    }
    if include_agent:
        report["agent"] = agent
    report["harness_latency_ms"] = round(
        (time.perf_counter() - started) * 1000.0, 2
    )
    return report


def benchmark_table(report: dict[str, Any]) -> str:
    """Render the fixed todo Phase 12 benchmark table from a real report."""
    recon = report["reconciliation"]
    anomaly = report["anomaly"]

    def line(label: str, value: Any, suffix: str = "") -> str:
        rendered = f"{value:,}" if isinstance(value, int) else f"{value}"
        return f"{label:<24}{rendered}{suffix}"

    lines = [
        line("Records processed:", report["records_processed"]),
        line("Reconciliation accuracy:", recon["match_accuracy_pct"], "%"),
        line("Exception precision:", recon["exception_precision_pct"], "%"),
        line("Exception recall:", recon["exception_recall_pct"], "%"),
        line("Anomaly precision:", anomaly["precision_pct"], "%"),
        line("Anomaly recall:", anomaly["recall_pct"], "%"),
        line("False-positive rate:", anomaly["false_positive_rate_pct"], "%"),
    ]
    if "agent" in report:
        lines.append(
            line("Average latency:", report["agent"]["average_latency_ms"], " ms")
        )
    lines.extend(
        [
            line(
                "Throughput:",
                report["throughput_records_per_min"],
                " records/min",
            ),
            line("Unresolved exceptions:", report["unresolved_exceptions"]),
        ]
    )
    return "\n".join(lines)