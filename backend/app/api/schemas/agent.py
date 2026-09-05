"""Request/response schemas for the agent chat endpoint (Phase 6).

Mirrors the architecture section-13 contract: request carries the user
message plus optional run/merchant scope; the response returns the run id,
the final answer, the tools used, and record-id evidence. Every field is
optional-safe so failed runs (``model_error``) still serialize cleanly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class AgentChatRequest(BaseModel):
    """``POST /api/agent/chat`` request body (architecture section 13).

    The message must be a non-empty, non-whitespace string (Phase 13
    reliability: an empty query is refused at the schema boundary with a
    422, never forwarded to the provider or charged a run).
    """

    run_id: str | None = None
    message: str = Field(min_length=1)
    merchant_id: str | None = None

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must contain at least one non-whitespace character")
        return value


class ToolCallInfo(BaseModel):
    """One executed tool call in the run trace."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str = "ok"
    error: str | None = None
    latency_ms: float = 0.0


class AgentChatResponse(BaseModel):
    """``POST /api/agent/chat`` response body (architecture section 13)."""

    run_id: str
    status: str
    turn_count: int = 1
    answer: str
    tools_used: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallInfo] = Field(default_factory=list)
    total_llm_latency_ms: float = 0.0