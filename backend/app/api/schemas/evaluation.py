"""Response schema for the evaluation endpoint (Phase 12).

``GET /api/evaluation`` returns the engine benchmark against the seeded
ground truth plus the *stored* agent run history — a read-only snapshot
that never executes a new agent run (that is what the CLI harness and
its scripted provider are for).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EvaluationAgentHistory(BaseModel):
    """Aggregate over the *stored* agent runs (no live benchmark)."""

    runs: int = 0
    completed_runs: int = 0
    model_error_runs: int = 0
    tool_limit_runs: int = 0
    total_tool_calls: int = 0
    failed_tool_calls: int = 0
    average_tool_calls_per_run: float = 0.0
    tool_failure_rate_pct: float = 0.0
    average_run_latency_ms: float = 0.0
    source: str = "stored_run_history"


class EvaluationResponse(BaseModel):
    """``GET /api/evaluation`` response body (machine-readable report)."""

    tool: str = "evaluation_report"
    status: str
    synthetic: bool = True
    records_processed: int
    reconciliation: dict[str, Any]
    anomaly: dict[str, Any]
    unresolved_exceptions: int
    throughput_records_per_min: float | None = None
    harness_latency_ms: float | None = None
    agent_history: EvaluationAgentHistory | None = None