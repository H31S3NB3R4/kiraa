"""LLM provider adapters (Phase 6).

- ``base``: the provider-agnostic ``LLMProvider`` interface and message /
  response shapes. The controller loop speaks only these types.
- ``gemini``: the google-genai implementation — tool declarations to
  ``FunctionDeclaration``/``Tool`` schemas, automatic function calling
  disabled (the controller executes tools), function responses echoing
  call ids, and bounded retries on transient HTTP codes.

Keeping Gemini-specific request/response parsing isolated here allows a
later provider swap (e.g. Claude) without touching the controller loop.
"""

from __future__ import annotations

from app.agent.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    TextMessage,
    ToolCallsMessage,
    ToolDeclaration,
    ToolResult,
    ToolResultsMessage,
)

__all__ = [
    "LLMProvider",
    "LLMProviderError",
    "LLMResponse",
    "LLMToolCall",
    "TextMessage",
    "ToolCallsMessage",
    "ToolDeclaration",
    "ToolResult",
    "ToolResultsMessage",
]
