"""Agentic controller loop (Phase 6, PRD FR-1, architecture section 3.3).

Multi-turn state (Phase 7, architecture section 11): every analyst turn is
appended to the same ``agent_messages`` transcript under one ``run_id``.
A follow-up request that passes the previous ``run_id`` replays the saved
conversation (bounded by ``settings.agent_max_history_messages``) so later
questions can use earlier retrieved context:

    turn 1: "Reconcile this week."
    turn 2: "What are the biggest exceptions?"   <- sees turn 1's tool evidence
    turn 3: "Investigate the top one."           <- sees turns 1-2

The controller is the *only* component that talks to the LLM. It owns the
bounded tool-calling loop:

    send user message + tool definitions to the provider
        -> receive tool calls -> dispatch through the registry
        -> append function responses -> repeat until final text
        or the safety limit is hit

and persists the full run trace (``agent_runs`` + one ``tool_calls`` row
per invocation + one ``agent_messages`` row per conversation event) so every
investigation is auditable (FR-9 groundwork).

Safety properties:

- the loop is bounded by ``settings.agent_max_tool_calls``; hitting the
  limit ends the run with a safe, deterministic answer (never a silent
  truncation). The budget applies to the whole multi-turn run, not per
  turn, so follow-ups cannot bypass it,
- replayed context is bounded by ``settings.agent_max_history_messages``
  so conversations cannot grow unboundedly (architecture section 11),
- tool failures already arrive as structured error envelopes from
  ``dispatch_tool``, so the model is told a tool failed and can never
  invent a result (PRD section 14),
- provider failures end the turn with status ``model_error`` and the run
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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    TextMessage,
    ToolCallsMessage,
    ToolResult,
    ToolResultsMessage,
)
from app.agent.prompts import build_system_prompt
from app.agent.tool_registry import TOOL_DECLARATIONS, dispatch_tool
from app.config import get_settings, redact_secrets
from app.models import AgentMessage, AgentRun, ToolCall

logger = logging.getLogger(__name__)

__all__ = [
    "AgentController",
    "AgentRunNotFoundError",
    "run_agent",
]

# Statuses written to ``agent_runs.status``.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_TOOL_LIMIT = "tool_limit"
STATUS_MODEL_ERROR = "model_error"

# Transcript roles written to ``agent_messages.role``.
ROLE_USER = "user"
ROLE_MODEL = "model"
ROLE_TOOL = "tool"


class AgentRunNotFoundError(LookupError):
    """A follow-up turn referenced a ``run_id`` that does not exist."""

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
        max_history_messages: int | None = None,
        merchant_id: str | None = None,
        temperature: float = 0.2,
    ) -> None:
        self.provider = provider
        self.db = db
        settings = get_settings()
        self.max_tool_calls = (
            settings.agent_max_tool_calls if max_tool_calls is None else max_tool_calls
        )
        self.max_history_messages = (
            settings.agent_max_history_messages
            if max_history_messages is None
            else max_history_messages
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
            turn_count=1,
            started_at=datetime.now(),
        )
        self.db.add(run)
        self.db.commit()
        return run

    def _continue_run(self, run_id: str) -> AgentRun:
        """Load an existing run for a follow-up turn or raise 404-style."""
        run = self.db.get(AgentRun, run_id)
        if run is None:
            raise AgentRunNotFoundError(
                f"run_id {run_id!r} does not exist; start a new conversation "
                "by omitting run_id"
            )
        return run

    def _next_transcript_seq(self, run: AgentRun) -> int:
        """One past the highest transcript seq recorded for the run."""
        last = self.db.execute(
            select(AgentMessage.seq)
            .where(AgentMessage.run_id == run.run_id)
            .order_by(AgentMessage.seq.desc())
            .limit(1)
        ).scalar_one_or_none()
        return (last or 0) + 1

    def _append_transcript(
        self, run: AgentRun, role: str, content: dict[str, Any]
    ) -> None:
        """Persist one conversation event at the next transcript seq."""
        self.db.add(
            AgentMessage(
                run_id=run.run_id,
                seq=self._transcript_seq_next,
                role=role,
                content=_jsonify(content),
            )
        )
        self.db.commit()
        self._transcript_seq_next += 1

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
        """Close out the current turn; run counters stay cumulative."""
        run.status = status
        run.final_response = answer
        run.error = error
        # Shared per-run seq space: next seq - 1 == total calls for the run.
        run.tool_call_count = self._tool_seq_next - 1
        run.total_llm_latency_ms = (
            run.total_llm_latency_ms or 0.0
        ) + total_llm_latency_ms
        run.finished_at = datetime.now()
        self.db.commit()

    def _next_tool_seq(self, run: AgentRun) -> int:
        """The seq for the next persisted tool call (dense per-run 1..N)."""
        last = self.db.execute(
            select(ToolCall.seq)
            .where(ToolCall.run_id == run.run_id)
            .order_by(ToolCall.seq.desc())
            .limit(1)
        ).scalar_one_or_none()
        return (last or 0) + 1

    def _run_tool_summary(self, run: AgentRun) -> str:
        """Summarize every tool call executed in the run so far (all turns).

        The tool-limit answer describes the whole conversation's work, so
        it reads the persisted ``tool_calls`` rows rather than the current
        turn's in-memory trace: a follow-up that trips the budget must
        still see the earlier turns' calls in the summary.
        """
        rows = self.db.execute(
            select(ToolCall)
            .where(ToolCall.run_id == run.run_id)
            .order_by(ToolCall.seq)
        ).scalars().all()
        return _summarize_tools(
            [
                {"tool_name": row.tool_name, "arguments": row.arguments or {}}
                for row in rows
            ]
        )

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

    # -- history replay ---------------------------------------------------

    def _replay_history(self, run: AgentRun) -> list[ChatMessage]:
        """Rebuild the saved conversation for a follow-up turn (Phase 7).

        Returns the most recent ``max_history_messages`` transcript events
        as provider-agnostic messages so later questions can use earlier
        retrieved context. Leading tool rows whose requesting model round
        fell outside the window are dropped — a tool batch is never
        separated from the round that asked for it.
        """
        rows = list(
            self.db.execute(
                select(AgentMessage)
                .where(AgentMessage.run_id == run.run_id)
                .order_by(AgentMessage.seq)
            ).scalars()
        )
        limit = self.max_history_messages
        window = rows[-limit:] if limit and limit > 0 else rows
        while window and window[0].role == ROLE_TOOL:
            window = window[1:]

        messages: list[ChatMessage] = []
        for row in window:
            content = row.content or {}
            if row.role == ROLE_USER:
                messages.append(TextMessage(text=content.get("text", ""), role="user"))
            elif row.role == ROLE_MODEL:
                if content.get("tool_limit_hit"):
                    # The requested calls were never dispatched; replay the
                    # deterministic limit answer as the model's text turn
                    # instead of an unanswered tool round.
                    messages.append(
                        TextMessage(
                            text=content.get("text") or "", role="model"
                        )
                    )
                    continue
                calls = content.get("tool_calls") or []
                if calls:
                    messages.append(
                        ToolCallsMessage(
                            calls=[
                                LLMToolCall(
                                    id=call.get("id"),
                                    name=call.get("name", ""),
                                    args=dict(call.get("arguments") or {}),
                                )
                                for call in calls
                            ]
                        )
                    )
                else:
                    messages.append(
                        TextMessage(text=content.get("text") or "", role="model")
                    )
            else:  # ROLE_TOOL
                messages.append(
                    ToolResultsMessage(
                        results=[
                            ToolResult(
                                id=item.get("id"),
                                name=item.get("name", ""),
                                result=dict(item.get("result") or {}),
                            )
                            for item in content.get("results") or []
                        ]
                    )
                )
        return messages

    # -- main entry ------------------------------------------------------

    def run(
        self, user_query: str, *, run_id: str | None = None
    ) -> dict[str, Any]:
        """Run one analyst turn (new conversation or ``run_id`` continuation).

        Always returns a result dict — provider failures end the turn with
        status ``model_error`` and a safe fallback answer, never an
        exception (the run trace stays auditable).
        """
        self._tool_trace = []
        self._llm_latency = 0.0

        if run_id is None:
            run = self._create_run(user_query)
            history: list[ChatMessage] = []
        else:
            run = self._continue_run(run_id)
            run.user_query = user_query  # latest analyst message
            run.turn_count = (run.turn_count or 1) + 1
            run.status = STATUS_RUNNING
            run.error = None
            self.db.commit()
            history = self._replay_history(run)

        # Per-run sequence counters continue across turns: transcript rows
        # and tool calls each share one dense 1..N space per run.
        self._transcript_seq_next = self._next_transcript_seq(run)
        self._tool_seq_next = self._next_tool_seq(run)
        self._append_transcript(run, ROLE_USER, {"text": user_query})

        messages = history + [TextMessage(text=user_query)]
        system_instruction = build_system_prompt(merchant_id=self.merchant_id)
        try:
            self._loop(run, messages, system_instruction)
        except LLMProviderError as exc:
            # Phase 14: provider/SDK exception text can echo request details;
            # filter configured secrets before the log line and the stored
            # run trace (GET /api/runs/{run_id} serves this field).
            safe_error = redact_secrets(str(exc))
            logger.warning("agent run %s failed: %s", run.run_id, safe_error)
            answer = (
                "The AI model is temporarily unavailable, so I could not "
                "complete this investigation. The run trace is preserved "
                f"for debugging (run_id={run.run_id})."
            )
            # Record the fallback as this turn's assistant answer so the
            # transcript stays coherent for follow-up turns.
            self._append_transcript(
                run,
                ROLE_MODEL,
                {"text": answer, "tool_calls": [], "latency_ms": 0.0},
            )
            self._finalize_run(
                run,
                status=STATUS_MODEL_ERROR,
                answer=None,
                error=safe_error,
                total_llm_latency_ms=self._llm_latency,
            )
        else:
            answer = run.final_response or ""

        return {
            "run_id": run.run_id,
            "status": run.status,
            "turn_count": run.turn_count,
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
        """Iterate provider rounds until final text, the tool limit, or failure.

        Every round is mirrored into the ``agent_messages`` transcript; the
        per-run tool-call budget spans all turns of the conversation.
        """
        while True:
            response = self.provider.generate(
                messages,
                TOOL_DECLARATIONS,
                system_instruction=system_instruction,
                temperature=self.temperature,
            )
            self._llm_latency += response.latency_ms

            if not response.tool_calls:
                # Final answer round (persisted for follow-up turns).
                self._append_transcript(
                    run,
                    ROLE_MODEL,
                    {
                        "text": response.text or "",
                        "tool_calls": [],
                        "latency_ms": response.latency_ms,
                    },
                )
                self._finalize_run(
                    run,
                    status=STATUS_COMPLETED,
                    answer=response.text or "",
                    error=None,
                    total_llm_latency_ms=self._llm_latency,
                )
                return

            run_total = self._tool_seq_next - 1  # calls from every turn
            if run_total + len(response.tool_calls) > self.max_tool_calls:
                # Safety limit: never dispatch the excess calls; end with a
                # deterministic answer describing what was done so far.
                # The requested calls stay in the transcript for audit, but
                # replay must not feed them back as a pending tool round
                # (they were never answered) — ``tool_limit_hit`` marks that.
                answer = _TOOL_LIMIT_ANSWER.format(
                    summary=self._run_tool_summary(run)
                )
                self._append_transcript(
                    run,
                    ROLE_MODEL,
                    {
                        "text": answer,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "name": call.name,
                                "arguments": _jsonify(call.args),
                            }
                            for call in response.tool_calls
                        ],
                        "latency_ms": response.latency_ms,
                        "tool_limit_hit": True,
                    },
                )
                self._finalize_run(
                    run,
                    status=STATUS_TOOL_LIMIT,
                    answer=answer,
                    error=None,
                    total_llm_latency_ms=self._llm_latency,
                )
                return

            messages.append(ToolCallsMessage(calls=list(response.tool_calls)))
            self._append_transcript(
                run,
                ROLE_MODEL,
                {
                    "text": response.text,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "name": call.name,
                            "arguments": _jsonify(call.args),
                        }
                        for call in response.tool_calls
                    ],
                    "latency_ms": response.latency_ms,
                },
            )
            results: list[ToolResult] = []
            outcomes: list[dict[str, Any]] = []  # per-call transcript mirror
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
                self._persist_tool_call(run, self._tool_seq_next, trace_entry)
                self._tool_seq_next += 1
                results.append(
                    ToolResult(id=call.id, name=call.name, result=result)
                )
                outcomes.append(
                    {
                        "id": call.id,
                        "name": call.name,
                        "status": trace_entry["status"],
                        "error": trace_entry["error"],
                        # Mirror exactly what the provider received for this
                        # call (the live round pops latency_ms before
                        # building the function response).
                        "result": {
                            key: value
                            for key, value in trace_entry["result"].items()
                            if key != "latency_ms"
                        },
                    }
                )
            messages.append(ToolResultsMessage(results=results))
            self._append_transcript(
                run, ROLE_TOOL, {"results": outcomes}
            )


def run_agent(
    provider: LLMProvider,
    db: Session,
    user_query: str,
    *,
    run_id: str | None = None,
    merchant_id: str | None = None,
    max_tool_calls: int | None = None,
) -> dict[str, Any]:
    """One-call convenience wrapper around :class:`AgentController`."""
    controller = AgentController(
        provider, db, max_tool_calls=max_tool_calls, merchant_id=merchant_id
    )
    return controller.run(user_query, run_id=run_id)