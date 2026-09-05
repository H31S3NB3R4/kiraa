"""Merchants route (Phase 10, PRD section 16, architecture section 13).

``GET /api/merchants`` lists the merchant master rows so the dashboard's
merchant selector mirrors exactly what the read/report endpoints can
scope by. Strictly read-only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas.merchants import MerchantsListResponse
from app.db.session import get_db
from app.services.queries import list_merchants

router = APIRouter(prefix="/api/merchants", tags=["merchants"])


@router.get("", response_model=MerchantsListResponse)
def merchants(db: Annotated[Session, Depends(get_db)]) -> MerchantsListResponse:
    """List merchants (the dashboard's selector), ordered by id."""
    return MerchantsListResponse(**list_merchants(db))