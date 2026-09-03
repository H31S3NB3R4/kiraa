"""Response schemas for the ML anomaly-detection tool (Phase 5).

``AnomalyResponse`` mirrors the dict returned by
``app.tools.anomalies.detect_anomalies`` so a future API route can wrap
the tool directly. Every analytic field defaults to ``None`` (and the
per-transaction series to an empty list), so the guard envelopes
(``unknown_merchant`` / ``no_transactions``) validate against the same
schema.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AnomalyScoreRow(BaseModel):
    """One scored transaction: score, band, reason, and cross-links."""

    transaction_id: str
    merchant_id: str | None = None
    anomaly_score: float
    severity: str | None = None
    is_anomaly: bool | None = None
    reconciliation_pass: bool | None = None
    reason: str | None = None
    features: dict[str, Any] | None = None


class AnomalyResponse(BaseModel):
    """Serialization contract for ``detect_anomalies`` results (FR-6)."""

    tool: str = "detect_anomalies"
    status: str
    merchant_id: str | None = None
    filters: dict[str, Any] | None = None
    model: dict[str, Any] | None = None
    scores: list[AnomalyScoreRow] = Field(default_factory=list)
    truncated: bool | None = None
    metrics: dict[str, Any] | None = None
    ground_truth: dict[str, Any] | None = None
    persisted: dict[str, int] | None = None
    sources: dict[str, Any] | None = None
