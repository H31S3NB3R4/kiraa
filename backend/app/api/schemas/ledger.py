"""Response schemas for the read-only ledger query endpoint (Phase 9).

``LedgerQueryResponse`` mirrors the dict returned by
``app.tools.ledger.query_ledger`` (FR-3): source-linked rows joined back
to their transaction and merchant, with the filter echo and the
``truncated`` cap indicator.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class LedgerQueryRow(BaseModel):
    """One ledger entry with its source references (FR-3)."""

    entry_id: str
    transaction_id: str
    merchant_id: str
    merchant_name: str
    merchant_category: str
    entry_date: date
    debit_account: str
    credit_account: str
    amount: float
    status: str
    description: str
    settlement_id: str | None = None
    invoice_id: str | None = None


class LedgerQueryResponse(BaseModel):
    """Serialization contract for ``query_ledger`` results (FR-3)."""

    tool: str = "query_ledger"
    filters: dict[str, Any] = Field(default_factory=dict)
    count: int = 0
    limit: int | None = None
    truncated: bool = False
    rows: list[LedgerQueryRow] = Field(default_factory=list)
