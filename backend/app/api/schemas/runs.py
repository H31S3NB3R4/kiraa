"""Response schemas for the agent-run detail endpoint (Phase 9).

``GET /api/runs/{run_id}`` returns one conversation run with its tool-call
trace and full transcript — the data behind the Phase 10 audit view's run
history and tool sequence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RunToolCall(BaseModel):
    """One persisted tool invocation inside the run."""

    seq: int
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    status: str = "ok"
    error: str | None = None
    latency_ms: float = 0.0


class RunMessage(BaseModel):
    """One ordered transcript event inside the run (user/model/tool)."""

    seq: int
    role: str
    content: dict[str, Any] = Field(default_factory=dict)


class RunDetailResponse(BaseModel):
    """``GET /api/runs/{run_id}`` response body."""

    run_id: str
    user_query: str
    status: str
    turn_count: int = 1
    tool_call_count: int = 0
    total_llm_latency_ms: float = 0.0
    error: str | None = None
    final_response: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    tool_calls: list[RunToolCall] = Field(default_factory=list)
    messages: list[RunMessage] = Field(default_factory=list)


class RunSummaryRow(BaseModel):
    """One agent-run row in the history listing (no transcript payload)."""

    run_id: str
    user_query: str
    status: str
    turn_count: int = 1
    tool_call_count: int = 0


class RunsListResponse(BaseModel):
    """``GET /api/runs`` response body (list envelope convention)."""

    count: int = 0
    limit: int | None = None
    truncated: bool = False
    filters: dict[str, Any] = Field(default_factory=dict)
    rows: list[RunSummaryRow] = Field(default_factory=list)
