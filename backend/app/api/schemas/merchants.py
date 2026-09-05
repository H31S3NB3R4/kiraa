"""Response schemas for the merchant listing endpoint (Phase 10).

``GET /api/merchants`` feeds the dashboard's merchant selector — the
master rows the read/report endpoints scope by. Strictly read-only.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MerchantRow(BaseModel):
    """One merchant master row."""

    merchant_id: str
    name: str
    category: str
    currency: str


class MerchantsListResponse(BaseModel):
    """``GET /api/merchants`` response body."""

    count: int = 0
    rows: list[MerchantRow] = Field(default_factory=list)