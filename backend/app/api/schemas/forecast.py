"""Chart-ready response schemas for the cash-flow forecast tool (Phase 4).

``ForecastResponse`` mirrors the dict returned by
``app.tools.forecast.forecast_cashflow`` so a future API route can wrap the
tool directly. Every analytic field defaults to ``None`` (and the per-day
series to an empty list), so the guard envelopes
(``unknown_merchant`` / ``no_history``) validate against the same schema.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class ForecastPoint(BaseModel):
    """One projected day: date, components, and the balance walk for it."""

    day_offset: int = Field(ge=1)
    date: date
    projected_inflow: float
    projected_outflow: float
    projected_net: float
    projected_cash: float


class ForecastResponse(BaseModel):
    """Serialization contract for ``forecast_cashflow`` results (FR-4)."""

    tool: str = "forecast_cashflow"
    status: str
    merchant_id: str | None = None
    merchant_name: str | None = None
    scope: str | None = None
    model: str | None = None
    horizon_days: int | None = None
    history_days: int | None = None
    history_observed_days: int | None = None
    history_start: date | None = None
    history_end: date | None = None
    anchor_date: date | None = None
    anchor_balance: float | None = None
    daily_avg_inflow: float | None = None
    daily_avg_outflow: float | None = None
    daily_avg_net: float | None = None
    recent_window_days: int | None = None
    recent_avg_inflow: float | None = None
    recent_avg_outflow: float | None = None
    recent_avg_net: float | None = None
    net_trend_per_day: float | None = None
    projected_inflow_per_day: float | None = None
    projected_outflow_per_day: float | None = None
    projected_net_per_day: float | None = None
    forecast: list[ForecastPoint] = Field(default_factory=list)
    projected_ending_balance: float | None = None
    min_projected_cash: float | None = None
    min_projected_date: date | None = None
    first_breach_date: date | None = None
    breach_days: int | None = None
    operating_threshold: float | None = None
    headroom: float | None = None
    headroom_pct: float | None = None
    risk: str | None = None
    risk_reason: str | None = None
    confidence: str | None = None
    volatility_cv: float | None = None
    sources: dict[str, Any] | None = None
