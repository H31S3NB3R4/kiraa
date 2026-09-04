"""Agentic controller loop (Phase 6, PRD FR-1, architecture section 3.3).

The controller is the *only* component that talks to the LLM. It owns the
bounded tool-calling loop:

    send user message + tool definitions to the provider
        -> receive tool calls -> dispatch through the registry
        -> append function responses -> repeat until final text
        or the safety limit is hit

and persists the full run trace (``agent_runs`` + one ``tool_calls`` row
per invocation) so every investigation is auditable (FR-9 groundwork).

Safety properties:

- the loop is bounded by ``settings.agent_max_tool_calls``; hitting the
  limit ends the run with a safe, deterministic answer (never a silent
  truncation),
- tool failures already arrive as structured error envelopes from
  ``dispatch_tool``, so the model is told a tool failed and can never
  invent a result (PRD section 14),
- provider failures end the run with status ``model_error`` and the run
  trace stays in the database for debugging (architecture section 16),
- tool results are JSON-serializable dicts; dates/Decimals are normalized
  so they survive both ``JSON`` columns and provider round-trips.

The controller never computes financial facts itself — every number it
returns is copied from tool output (golden rule).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.agent.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    TextMessage,
    ToolCallsMessage,
    ToolResult,
    ToolResultsMessage,
)
from app.agent.prompts import build_system_prompt
from app.agent.tool_registry import TOOL_DECLARATIONS, dispatch_tool
from app.config import get_settings
from app.models import AgentRun, ToolCall

logger = logging.getLogger(__name__)

__all__ = ["AgentController", "run_agent"]

# Statuses written to ``agent_runs.status``.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_TOOL_LIMIT = "tool_limit"
STATUS_MODEL_ERROR = "model_error"

_TOOL_LIMIT_ANSWER = (
    "I stopped after reaching the configured tool-call limit for this run. "
    "Here is what I found so far: {summary}"
)


def _jsonify(value: Any) -> Any:
    """Recursively normalize a tool payload for JSON columns / the provider."""
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep="T")
    if isinstance(value, date):
        return value.isoformat()
    return value


_ID_RE_PATTERN = (
    r"\b(?:TXN|SET|INV|LE|RFD|FEE|PROP)-[0-9A-Za-z-]+\b"
)
_ID_RE = re.compile(_ID_RE_PATTERN)


def _summarize_tools(tool_calls: list[dict[str, Any]]) -> str:
    """One-line deterministic summary of the tool sequence for safe answers."""
    if not tool_calls:
        return "no tools were executed."
    parts = [
        f"{call['tool_name']}({', '.join(f'{k}={v!r}' for k, v in call['arguments'].items())})"
        for call in tool_calls
    ]
    return "; ".join(parts) + "."


class AgentController:
    """Bounded tool-calling loop over a registered tool set (FR-1)."""

    def __init__(
        self,
        provider: LLMProvider,
        db: Session,
        *,
        max_tool_calls: int | None = None,
        merchant_id: str | None = None,
        temperature: float = 0.2,
    ) -> None:
        self.provider = provider
        self.db = db
        settings = get_settings()
        self.max_tool_calls = (
            settings.agent_max_tool_calls if max_tool_calls is None else max_tool_calls
        )
        self.merchant_id = merchant_id
        self.temperature = temperature
        self._tool_trace: list[dict[str, Any]] = []

    # -- persistence helpers ------------------------------------------

    def _create_run(self, user_query: str) -> AgentRun:
        run = AgentRun(
            run_id=f"RUN-{uuid4().hex[:12].upper()}",
            user_query=user_query,
            status=STATUS_RUNNING,
            started_at=datetime.now(),
        )
        self.db.add(run)
        self.db.commit()
        return run

    def _persist_tool_call(
        self, run: AgentRun, seq: int, call: dict[str, Any]
    ) -> None:
        self.db.add(
            ToolCall(
                run_id=run.run_id,
                seq=seq,
                tool_name=call["tool_name"],
                arguments=_jsonify(call["arguments"]),
                result=_jsonify(call["result"]),
                status=call["status"],
                error=call.get("error"),
                latency_ms=call["latency_ms"],
            )
        )
        self.db.commit()

    def _finalize_run(
        self,
        run: AgentRun,
        *,
        status: str,
        answer: str | None,
        error: str | None,
        total_llm_latency_ms: float,
    ) -> None:
        run.status = status
        run.final_response = answer
        run.error = error
        run.tool_call_count = len(self._tool_trace)
        run.total_llm_latency_ms = total_llm_latency_ms
        run.finished_at = datetime.now()
        self.db.commit()

    # -- evidence extraction ------------------------------------------

    @staticmethod
    def _extract_evidence(tool_trace: list[dict[str, Any]]) -> list[str]:
        """Collect record ids named by tool results (TXN-*/SET-*/INV-*/LE-*)."""
        seen: dict[str, None] = {}
        for call in tool_trace:
            blob = json.dumps(_jsonify(call["result"]), default=str)
            for token in _ID_RE.findall(blob):
                seen.setdefault(token, None)
        return list(seen.keys())

    # -- main entry ------------------------------------------------------

    def run(self, user_query: str) -> dict[str, Any]:
        """Run the bounded loop for one user message; always returns a dict."""
        self._tool_trace = []
        self._llm_latency = 0.0
        run = self._create_run(user_query)
        messages: list[Any] = [TextMessage(text=user_query)]
        system_instruction = build_system_prompt(merchant_id=self.merchant_id)
        try:
            self._loop(run, messages, system_instruction)
        except LLMProviderError as exc:
            logger.warning("agent run %s failed: %s", run.run_id, exc)
            self._finalize_run(
                run,
                status=STATUS_MODEL_ERROR,
                answer=None,
                error=str(exc),
                total_llm_latency_ms=self._llm_latency,
            )
            answer = (
                "The AI model is temporarily unavailable, so I could not "
                "complete this investigation. The run trace is preserved "
                f"for debugging (run_id={run.run_id})."
            )
        else:
            answer = run.final_response or ""

        return {
            "run_id": run.run_id,
            "status": run.status,
            "answer": answer,
            "tools_used": list(
                dict.fromkeys(c["tool_name"] for c in self._tool_trace)
            ),
            "tool_calls": self._tool_trace,
            "evidence": self._extract_evidence(self._tool_trace),
            "total_llm_latency_ms": run.total_llm_latency_ms,
        }

    # -- the loop ------------------------------------------------------

    def _loop(self, run: AgentRun, messages: list[Any], system_instruction: str) -> None:
        """Iterate provider rounds until final text, the tool limit, or failure."""
        while True:
            response = self.provider.generate(
                messages,
                TOOL_DECLARATIONS,
                system_instruction=system_instruction,
                temperature=self.temperature,
            )
            self._llm_latency += response.latency_ms

            if not response.tool_calls:
                # Final answer round.
                self._finalize_run(
                    run,
                    status=STATUS_COMPLETED,
                    answer=response.text or "",
                    error=None,
                    total_llm_latency_ms=self._llm_latency,
                )
                return

            if len(self._tool_trace) + len(response.tool_calls) > self.max_tool_calls:
                # Safety limit: never dispatch the excess calls; end with a
                # deterministic answer describing what was done so far.
                self._finalize_run(
                    run,
                    status=STATUS_TOOL_LIMIT,
                    answer=_TOOL_LIMIT_ANSWER.format(
                        summary=_summarize_tools(self._tool_trace)
                    ),
                    error=None,
                    total_llm_latency_ms=self._llm_latency,
                )
                return

            messages.append(ToolCallsMessage(calls=list(response.tool_calls)))
            results: list[ToolResult] = []
            for call in response.tool_calls:
                result = dispatch_tool(
                    self.db, call.name, call.args, run_id=run.run_id
                )
                error = (
                    result.get("error_type")
                    if result.get("status") == "error"
                    else None
                )
                trace_entry = {
                    "tool_name": call.name,
                    "arguments": _jsonify(call.args),
                    "result": _jsonify(result),
                    "status": "error" if error else "ok",
                    "error": error,
                    "latency_ms": result.pop("latency_ms", 0.0),
                }
                self._tool_trace.append(trace_entry)
                self._persist_tool_call(run, len(self._tool_trace), trace_entry)
                results.append(
                    ToolResult(id=call.id, name=call.name, result=result)
                )
            messages.append(ToolResultsMessage(results=results))


def run_agent(
    provider: LLMProvider,
    db: Session,
    user_query: str,
    *,
    merchant_id: str | None = None,
    max_tool_calls: int | None = None,
) -> dict[str, Any]:
    """One-call convenience wrapper around :class:`AgentController`."""
    controller = AgentController(
        provider, db, max_tool_calls=max_tool_calls, merchant_id=merchant_id
    )
    return controller.run(user_query)