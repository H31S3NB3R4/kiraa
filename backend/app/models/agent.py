"""Agent models: runs, transcript messages, and tool calls (Phase 6-7)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AgentRun(TimestampMixin, Base):
    """One agent conversation run (Phase 6; multi-turn continuation in Phase 7).

    ``run_id`` identifies the whole conversation: every follow-up turn that
    passes it back extends the same run (cumulative turn/tool/latency
    counters) instead of starting a new one.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_created", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    turn_count: Mapped[int] = mapped_column(default=1, nullable=False)
    tool_call_count: Mapped[int] = mapped_column(default=0, nullable=False)
    total_llm_latency_ms: Mapped[int] = mapped_column(Float, nullable=False, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    final_response: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ToolCall(TimestampMixin, Base):
    """A single tool invocation inside an agent run (Phase 6)."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_run_seq", "run_id", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.run_id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    def __repr__(self) -> str:
        return f"<ToolCall {self.run_id}#{self.seq} {self.tool_name} {self.status}>"


class AgentMessage(TimestampMixin, Base):
    """One ordered transcript event inside an agent run (Phase 7).

    ``role`` is one of ``user`` (analyst message), ``model`` (one provider
    round: final text and/or requested tool calls), or ``tool`` (the batch
    of outcomes for the preceding model round). ``content`` shapes:

    - user:  ``{"text": str}``
    - model: ``{"text": str | None, "tool_calls": [{"id", "name",
      "arguments"}], "latency_ms": float}`` — plus ``tool_limit_hit: true``
      when the round's calls were refused by the safety limit (replayed
      as text, never as a pending tool round),
    - tool:  ``{"results": [{"id", "name", "status", "error", "result"}]}``

    The transcript is the replay source for follow-up turns: it carries
    everything the provider needs (calls and full results) in order.
    """

    __tablename__ = "agent_messages"
    __table_args__ = (
        Index("ix_agent_messages_run_seq", "run_id", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.run_id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    def __repr__(self) -> str:
        return f"<AgentMessage {self.run_id}#{self.seq} {self.role}>"
