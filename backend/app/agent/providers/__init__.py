"""LLM provider adapters.

`base.py` will define the provider-agnostic `LLMProvider` interface and
`gemini.py` the Gemini implementation (Phase 6). Keeping Gemini-specific
request/response parsing isolated here allows a later provider swap
(e.g. Claude) without touching the controller loop.
"""
