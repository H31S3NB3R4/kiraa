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

from app.agent.controller import AgentController
from app.agent.providers.base import LLMProvider, LLMProviderError
from app.agent.providers.gemini import GeminiProvider
from app.api.schemas.agent import AgentChatRequest, AgentChatResponse
from app.config import get_settings
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
            detail=f"LLM provider unavailable: {exc}",
        ) from exc


@router.post("/chat", response_model=AgentChatResponse)
def chat(
    request: AgentChatRequest,
    provider: Annotated[LLMProvider, Depends(get_provider)],
    db: Annotated[Session, Depends(get_db)],
) -> AgentChatResponse:
    """Run the bounded tool-calling loop for one analyst message (FR-1)."""
    controller = AgentController(provider, db, merchant_id=request.merchant_id)
    result = controller.run(request.message)
    return AgentChatResponse(**result)