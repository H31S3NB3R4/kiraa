"""Agentic controller layer (Phase 6).

- ``providers/``: the provider-agnostic ``LLMProvider`` interface and the
  Gemini adapter (AFC disabled, function-response ids, bounded retries).
- ``tool_registry``: PRD tool-contract declarations + permission classes
  + the single ``dispatch_tool`` entry point with structured error
  envelopes.
- ``prompts``: the evidence-first system prompt (architecture section 15).
- ``controller``: the bounded tool-calling loop with ``agent_runs`` /
  ``tool_calls`` persistence.

The controller decides *what to investigate*; it never computes
financial facts itself.
"""

from __future__ import annotations

from app.agent.controller import AgentController, run_agent
from app.agent.prompts import SYSTEM_PROMPT, build_system_prompt
from app.agent.tool_registry import (
    TOOL_DECLARATIONS,
    TOOL_PERMISSIONS,
    TOOL_REGISTRY,
    dispatch_tool,
)

__all__ = [
    "AgentController",
    "SYSTEM_PROMPT",
    "TOOL_DECLARATIONS",
    "TOOL_PERMISSIONS",
    "TOOL_REGISTRY",
    "build_system_prompt",
    "dispatch_tool",
    "run_agent",
]
