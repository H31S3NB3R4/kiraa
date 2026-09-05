"""Agent chat route (Phase 6, PRD FR-1, architecture section 13).

``POST /api/agent/chat`` runs the controller loop for one user message.
The Gemini provider is resolved per request: it is built from settings
when ``GEMINI_API_KEY`` is configured and returns a 503 with a safe
message otherwise (the deterministic tools and health endpoint never
require the key). A cached client holds no request state, so reuse is
safe; the DB session is request-scoped via ``get_db``.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.agent.controller import AgentController, AgentRunNotFoundError
from app.agent.providers.base import LLMProvider, LLMProviderError
from app.agent.providers.gemini import GeminiProvider
from app.api.schemas.agent import AgentChatRequest, AgentChatResponse
from app.config import get_settings, redact_secrets
from app.db.session import get_db
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])


def get_provider() -> LLMProvider:
    """Build the Gemini provider from settings; 503 when unconfigured."""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Gemini API key is not configured. Set GEMINI_API_KEY in "
                "the environment to enable the agent."
            ),
        )
    try:
        return GeminiProvider(
            api_key=settings.gemini_api_key, model=settings.gemini_model
        )
    except LLMProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            # Phase 14: never let SDK exception text leak a configured secret.
            detail=f"LLM provider unavailable: {redact_secrets(str(exc))}",
        ) from exc


@router.post("/chat", response_model=AgentChatResponse)
def chat(
    request: AgentChatRequest,
    provider: Annotated[LLMProvider, Depends(get_provider)],
    db: Annotated[Session, Depends(get_db)],
) -> AgentChatResponse:
    """Run one analyst turn: new conversation, or continue ``run_id``.

    Passing a previous ``run_id`` replays its saved transcript (bounded by
    ``AGENT_MAX_HISTORY_MESSAGES``) so follow-up questions can use earlier
    retrieved context (FR-1, architecture section 11). Unknown run ids
    return 404 rather than silently starting a new conversation.
    """
    controller = AgentController(provider, db, merchant_id=request.merchant_id)
    try:
        result = controller.run(request.message, run_id=request.run_id)
    except AgentRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return AgentChatResponse(**result)