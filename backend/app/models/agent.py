"""Agent models: runs and tool calls (populated from Phase 6 onward)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AgentRun(TimestampMixin, Base):
    """One end-to-end agent conversation run (Phase 6)."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_created", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
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
