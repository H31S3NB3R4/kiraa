"""Provider-agnostic LLM interface (Phase 6, architecture section 3.3).

The controller loop speaks only in the shapes defined here; provider
adapters (``gemini.py``) translate to and from their SDK types. This keeps
the loop testable with a scripted fake provider (no network, no API key)
and lets a later provider swap (e.g. Claude) happen without touching the
controller.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypedDict, Union


class LLMProviderError(RuntimeError):
    """Raised when the provider cannot complete a model request."""


@dataclass
class LLMToolCall:
    """One function call requested by the model."""

    id: str | None
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """The controller's answer to one ``LLMToolCall``."""

    id: str | None
    name: str
    result: dict[str, Any]


@dataclass
class LLMResponse:
    """One provider round: final text and/or requested tool calls."""

    text: str | None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    latency_ms: float = 0.0


@dataclass
class TextMessage:
    """A plain user/model text turn."""

    text: str
    role: str = "user"


@dataclass
class ToolCallsMessage:
    """Model turn requesting one or more tool calls (echoed to the provider)."""

    calls: list[LLMToolCall]


@dataclass
class ToolResultsMessage:
    """User turn carrying the results for the previous ``ToolCallsMessage``."""

    results: list[ToolResult]


ChatMessage = Union[TextMessage, ToolCallsMessage, ToolResultsMessage]


class ToolDeclaration(TypedDict):
    """Provider-agnostic tool contract: name, description, JSON-schema parameters."""

    name: str
    description: str
    parameters: dict[str, Any]


class LLMProvider(abc.ABC):
    """Minimal provider interface (architecture: ``generate(messages, tools)``)."""

    name: str = "unknown"
    model: str = "unknown"

    @abc.abstractmethod
    def generate(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDeclaration],
        *,
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Run one model round over ``messages`` with ``tools`` available."""