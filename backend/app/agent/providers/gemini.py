"""Gemini LLM provider adapter (Phase 6).

Isolates every google-genai-specific construction behind the provider-
agnostic ``LLMProvider`` interface so the controller loop never touches
SDK types:

- tool declarations -> ``types.FunctionDeclaration``/``types.Tool``,
- messages -> ``types.Content`` parts (text, function_call, function_response),
- responses -> parsed ``LLMResponse`` (final text and/or tool calls),
- automatic function calling is disabled explicitly so the controller
  (not the SDK) executes tools,
- function responses echo the call ``id`` when present (multi-call turns),
- transient failures (429 rate limit / 5xx server) retry with a short
  bounded backoff; anything else raises ``LLMProviderError`` so the
  controller can record a failed run instead of guessing.

Verified against google-genai 2.22.0 offline probes (see Phase 6 notes).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from app.agent.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    TextMessage,
    ToolDeclaration,
    ToolCallsMessage,
    ToolResult,
    ToolResultsMessage,
)

try:  # pragma: no cover - exercised implicitly by import in CI too
    from google import genai as google_genai
    from google.genai import types as genai_types

    GOOGLE_GENAI_AVAILABLE = True
    _IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover
    GOOGLE_GENAI_AVAILABLE = False
    _IMPORT_ERROR = str(exc)


# Transient HTTP codes worth a bounded retry (rate limit + server errors).
_RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.5


def _require_sdk() -> None:
    if not GOOGLE_GENAI_AVAILABLE:
        raise LLMProviderError(
            f"google-genai SDK is not installed: {_IMPORT_ERROR}"
        )


def _to_schema(parameters: dict[str, Any]) -> Any:
    """Convert one plain-JSON parameter schema into a ``genai_types.Schema``."""
    if not parameters:
        return genai_types.Schema(type="OBJECT", properties={})
    node = dict(parameters)
    properties = node.pop("properties", None)
    items = node.pop("items", None)
    converted = {k: v for k, v in node.items()}
    if properties:
        converted["properties"] = {
            name: _to_schema(prop) for name, prop in properties.items()
        }
    if items is not None:
        converted["items"] = _to_schema(items)
    return genai_types.Schema(**converted)


def _to_declarations(tools: Sequence[ToolDeclaration]) -> Any:
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name=tool["name"],
                description=tool["description"],
                parameters=_to_schema(tool["parameters"]),
            )
            for tool in tools
        ]
    )


def _message_to_content(message: ChatMessage) -> Any:
    """Translate one provider-agnostic message into a ``types.Content``."""
    if isinstance(message, TextMessage):
        return genai_types.Content(
            role=message.role,
            parts=[genai_types.Part(text=message.text)],
        )
    if isinstance(message, ToolCallsMessage):
        parts = [
            genai_types.Part(
                function_call=genai_types.FunctionCall(
                    id=call.id, name=call.name, args=dict(call.args)
                )
            )
            for call in message.calls
        ]
        return genai_types.Content(role="model", parts=parts)
    if isinstance(message, ToolResultsMessage):
        parts = []
        for result in message.results:
            response = dict(result.result)
            if result.id is not None:
                parts.append(
                    genai_types.Part(
                        function_response=genai_types.FunctionResponse(
                            id=result.id,
                            name=result.name,
                            response=response,
                        )
                    )
                )
            else:
                parts.append(
                    genai_types.Part.from_function_response(
                        name=result.name,
                        response=response,
                    )
                )
        return genai_types.Content(role="user", parts=parts)
    raise LLMProviderError(f"unsupported chat message type: {type(message).__name__}")


class GeminiProvider(LLMProvider):
    """``LLMProvider`` implementation over the official google-genai SDK."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        *,
        max_attempts: int = _MAX_ATTEMPTS,
        backoff_seconds: float = _BACKOFF_SECONDS,
        client: Any = None,
    ) -> None:
        _require_sdk()
        if not api_key:
            raise LLMProviderError("Gemini API key is required")
        self.model = model
        self.name = "gemini"
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = backoff_seconds
        self._client = client or google_genai.Client(api_key=api_key)

    def _config(
        self,
        tools: Sequence[ToolDeclaration],
        system_instruction: str | None,
        temperature: float,
    ) -> genai_types.GenerateContentConfig:
        return genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[_to_declarations(tools)] if tools else None,
            temperature=temperature,
            # The controller — not the SDK — executes tools and feeds results
            # back, so automatic function calling must stay disabled.
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

    def _call_model(self, contents: list, config: Any) -> Any:
        """One SDK round with a bounded retry on transient HTTP codes."""
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                code = getattr(exc, "code", None)
                if isinstance(code, int) and code in _RETRYABLE_CODES:
                    last_error = exc
                    if attempt < self._max_attempts:
                        time.sleep(self._backoff_seconds * attempt)
                        continue
                raise LLMProviderError(f"Gemini request failed: {exc}") from exc
        raise LLMProviderError(f"Gemini request failed: {last_error}") from last_error

    def generate(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDeclaration],
        *,
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        contents = [_message_to_content(message) for message in messages]
        config = self._config(tools, system_instruction, temperature)

        started = time.perf_counter()
        response = self._call_model(contents, config)
        latency_ms = (time.perf_counter() - started) * 1000.0

        text = response.text
        calls = [
            LLMToolCall(
                id=call.id,
                name=call.name or "",
                args=dict(call.args) if call.args else {},
            )
            for call in (response.function_calls or [])
        ]
        return LLMResponse(
            text=text.strip() if isinstance(text, str) else text,
            tool_calls=calls,
            latency_ms=latency_ms,
        )