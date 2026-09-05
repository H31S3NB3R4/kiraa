"""Evaluation route (Phase 12, todo "Evaluation Harness").

``GET /api/evaluation`` serves the engine benchmark against the seeded
ground truth (match accuracy, exception precision/recall, anomaly
precision/recall/FPR — the same numbers the CLI harness reports) plus an
aggregate over the *stored* agent run history (latency, tool calls,
failure rate). It is strictly read-only: engine passes run with
``persist=False`` and no agent run is ever executed from a GET (the CLI
harness owns the live scripted benchmark). Databases without seeded
labels return a ``no_labels`` guard envelope with HTTP 200.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas.evaluation import (
    EvaluationAgentHistory,
    EvaluationResponse,
)
from app.db.session import get_db
from app.models import AgentRun, ToolCall
from app.services.evaluation import EvaluationError, evaluation_report

router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])


def _agent_history(db: Session) -> EvaluationAgentHistory:
    """Aggregate latency / tool-call / failure stats over stored runs."""
    runs = db.execute(select(AgentRun)).scalars().all()
    calls = db.execute(select(ToolCall)).scalars().all()
    if not runs:
        return EvaluationAgentHistory()

    completed = sum(1 for r in runs if r.status == "completed")
    model_errors = sum(1 for r in runs if r.status == "model_error")
    tool_limits = sum(1 for r in runs if r.status == "tool_limit")
    failed = sum(1 for c in calls if c.status == "error")
    latencies = [
        float(r.total_llm_latency_ms or 0.0)
        for r in runs
    ]
    return EvaluationAgentHistory(
        runs=len(runs),
        completed_runs=completed,
        model_error_runs=model_errors,
        tool_limit_runs=tool_limits,
        total_tool_calls=len(calls),
        failed_tool_calls=failed,
        average_tool_calls_per_run=round(len(calls) / len(runs), 2),
        tool_failure_rate_pct=round(100.0 * failed / len(calls), 2)
        if calls
        else 0.0,
        average_run_latency_ms=round(sum(latencies) / len(latencies), 2),
    )


@router.get("", response_model=EvaluationResponse)
def evaluation(
    db: Annotated[Session, Depends(get_db)],
) -> EvaluationResponse:
    """Report the Phase 12 engine benchmark + stored agent history (read-only)."""
    try:
        report = evaluation_report(db, include_agent=False)
    except EvaluationError:
        # Guard envelope: an unlabelled database is a valid state, not a
        # failure (e.g. a production DB the Phase 1 seeder never touched).
        return EvaluationResponse(
            tool="evaluation_report",
            status="no_labels",
            synthetic=True,
            records_processed=0,
            reconciliation={},
            anomaly={},
            unresolved_exceptions=0,
            agent_history=_agent_history(db),
        )
    return EvaluationResponse(
        **report,
        agent_history=_agent_history(db),
    )