"""Deterministic cash-flow forecasting tool (Phase 4).

``forecast_cashflow`` projects a merchant's (or the pooled portfolio's)
cash position forward from the per-day aggregates in ``cash_flows``:

1. load the last ``history_days`` calendar days of daily inflows and
   outflows (pooled across merchants when ``merchant_id`` is ``None``),
2. compute trailing averages over the window and over the recent
   ``RECENT_WINDOW_DAYS``-day rolling window,
3. project the recent rolling averages flat across ``horizon_days``
   (the initial forecast model — deliberately deterministic),
4. roll the last actual closing balance forward with the projected
   daily net, using the same running ``round2`` arithmetic as the
   dataset generator,
5. compare every projected day against the operating threshold
   (per-call argument, else the ``OPERATING_THRESHOLD`` setting) and
   classify risk,
6. return the drivers behind the classification so the LLM explains —
   never invents — the numbers (PRD FR-4 and section 12).

Risk classification:

    HIGH    any projected day falls below the operating threshold
    MEDIUM  the minimum projection stays above the threshold but
            within 25% of it (thin buffer)
    LOW     at least 25% headroom above the threshold at the minimum

Confidence is a volatility label, not a probability: the coefficient of
variation of historical daily net flows, capped at ``low`` when the
history is shorter than the rolling window or the variation is extreme.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import fmean, pstdev
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CashFlow, Merchant
from app.tools.common import round2

DEFAULT_HORIZON_DAYS = 7
DEFAULT_HISTORY_DAYS = 28
MAX_HORIZON_DAYS = 30
MAX_HISTORY_DAYS = 366
# Rolling window that drives the forecast (recent weekly behaviour).
RECENT_WINDOW_DAYS = 7
# The minimum projection must exceed 1.25x the threshold to be LOW risk.
MEDIUM_BUFFER_FACTOR = 1.25
# Coefficient-of-variation cutoffs for the confidence label.
CONFIDENCE_MEDIUM_CV = 0.5
CONFIDENCE_LOW_CV = 1.0

MODEL_NAME = "recent-rolling-average"


def _daily_history(
    db: Session, merchant_id: str | None, history_days: int
) -> tuple[list[dict[str, Any]], date, float] | None:
    """Load the trailing daily aggregates for a scope (PRD section 12, 1-2).

    Returns ``(observed_days, anchor_date, anchor_balance)`` where
    ``observed_days`` carries one dict per calendar day that has rows
    (pooled across merchants when ``merchant_id`` is ``None``) and
    ``anchor_balance`` is the closing balance recorded on the anchor date
    (summed across the scope). ``None`` when the scope has no cash-flow
    rows at all.
    """
    filters: list[Any] = []
    if merchant_id is not None:
        filters.append(CashFlow.merchant_id == merchant_id)

    anchor_date = db.execute(
        select(func.max(CashFlow.date)).where(*filters)
    ).scalar()
    if anchor_date is None:
        return None

    window_start = anchor_date - timedelta(days=history_days - 1)
    stmt: Select = (
        select(
            CashFlow.date,
            CashFlow.inflow,
            CashFlow.outflow,
            CashFlow.net_amount,
            CashFlow.closing_balance,
        )
        .where(*filters)
        .where(CashFlow.date >= window_start, CashFlow.date <= anchor_date)
        .order_by(CashFlow.date)
    )

    daily: dict[date, dict[str, float]] = {}
    anchor_balance = 0.0
    for row in db.execute(stmt):
        slot = daily.setdefault(row.date, {"inflow": 0.0, "outflow": 0.0, "net": 0.0})
        slot["inflow"] += float(row.inflow)
        slot["outflow"] += float(row.outflow)
        slot["net"] += float(row.net_amount)
        if row.date == anchor_date:
            anchor_balance += float(row.closing_balance)

    observed = [
        {
            "date": day,
            "inflow": round2(slot["inflow"]),
            "outflow": round2(slot["outflow"]),
            "net": round2(slot["net"]),
        }
        for day, slot in sorted(daily.items())
    ]
    return observed, anchor_date, round2(anchor_balance)


def forecast_cashflow(
    db: Session,
    merchant_id: str | None = None,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    history_days: int = DEFAULT_HISTORY_DAYS,
    operating_threshold: float | None = None,
) -> dict[str, Any]:
    """Produce a deterministic cash-flow forecast (PRD FR-4, section 12).

    ``merchant_id=None`` pools every merchant's daily flows into one
    portfolio forecast. The projection starts from the closing balance
    recorded on the latest day with cash-flow rows (the anchor) and rolls
    the recent rolling-average net forward across ``horizon_days`` days
    using the generator's running ``round2`` arithmetic — the LLM never
    invents any of these numbers.

    Bad arguments raise ``ValueError`` (ledger-tool convention); unknown
    merchants and empty history return status envelopes instead (GST-tool
    convention). Read-only: never mutates the database.
    """
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int):
        raise ValueError("horizon_days must be an integer")
    if not 1 <= horizon_days <= MAX_HORIZON_DAYS:
        raise ValueError(f"horizon_days must be between 1 and {MAX_HORIZON_DAYS}")
    if isinstance(history_days, bool) or not isinstance(history_days, int):
        raise ValueError("history_days must be an integer")
    if not 1 <= history_days <= MAX_HISTORY_DAYS:
        raise ValueError(f"history_days must be between 1 and {MAX_HISTORY_DAYS}")

    if operating_threshold is None:
        threshold = round2(get_settings().operating_threshold)
    else:
        try:
            threshold = round2(float(operating_threshold))
        except (TypeError, ValueError) as exc:
            raise ValueError("operating_threshold must be a number") from exc
        if not math.isfinite(threshold) or threshold < 0:
            raise ValueError(
                "operating_threshold must be a finite non-negative amount"
            )

    merchant: Merchant | None = None
    if merchant_id is not None:
        merchant = db.get(Merchant, merchant_id)
        if merchant is None:
            return {
                "tool": "forecast_cashflow",
                "status": "unknown_merchant",
                "merchant_id": merchant_id,
                "horizon_days": horizon_days,
                "sources": {"merchant_id": merchant_id, "table": "cash_flows"},
            }

    history = _daily_history(db, merchant_id, history_days)
    if history is None:
        return {
            "tool": "forecast_cashflow",
            "status": "no_history",
            "merchant_id": merchant_id,
            "scope": "merchant" if merchant_id is not None else "all_merchants",
            "horizon_days": horizon_days,
            "history_days": history_days,
            "sources": {"merchant_id": merchant_id, "table": "cash_flows"},
        }
    observed, anchor_date, anchor_balance = history

    # --- Rolling averages and trend (PRD section 12, step 2) --------------
    daily_avg_inflow = round2(fmean([day["inflow"] for day in observed]))
    daily_avg_outflow = round2(fmean([day["outflow"] for day in observed]))
    daily_avg_net = round2(daily_avg_inflow - daily_avg_outflow)

    recent = observed[-RECENT_WINDOW_DAYS:]
    recent_avg_inflow = round2(fmean([day["inflow"] for day in recent]))
    recent_avg_outflow = round2(fmean([day["outflow"] for day in recent]))
    recent_avg_net = round2(recent_avg_inflow - recent_avg_outflow)

    # Trend: recent week minus the week (or partial window) before it.
    prior = (
        observed[-2 * RECENT_WINDOW_DAYS : -RECENT_WINDOW_DAYS]
        if len(observed) >= 2 * RECENT_WINDOW_DAYS
        else observed[: -RECENT_WINDOW_DAYS]
    )
    net_trend_per_day: float | None = None
    if prior:
        prior_avg_net = round2(
            fmean([day["inflow"] for day in prior])
            - fmean([day["outflow"] for day in prior])
        )
        net_trend_per_day = round2(recent_avg_net - prior_avg_net)

    # --- Confidence: volatility of daily net flows (not a probability) ----
    nets = [day["net"] for day in observed]
    mean_net = fmean(nets)
    sd_net = pstdev(nets)
    if sd_net == 0.0:
        volatility_cv: float | None = 0.0
    elif mean_net == 0.0:
        volatility_cv = None  # unscaleable: no average direction to relate to
    else:
        volatility_cv = round2(sd_net / abs(mean_net))
    if volatility_cv is None or volatility_cv > CONFIDENCE_LOW_CV:
        confidence = "low"
    elif len(observed) < RECENT_WINDOW_DAYS:
        confidence = "low"  # too little history to trust the averages
    elif volatility_cv <= CONFIDENCE_MEDIUM_CV:
        confidence = "high"
    else:
        confidence = "medium"

    # --- Flat projection rolled forward from the anchor (steps 3-4) --------
    balance = anchor_balance
    forecast_rows: list[dict[str, Any]] = []
    for offset in range(1, horizon_days + 1):
        balance = round2(balance + recent_avg_net)
        forecast_rows.append(
            {
                "day_offset": offset,
                "date": (anchor_date + timedelta(days=offset)).isoformat(),
                "projected_inflow": recent_avg_inflow,
                "projected_outflow": recent_avg_outflow,
                "projected_net": recent_avg_net,
                "projected_cash": balance,
            }
        )

    # --- Threshold comparison and risk classification (steps 5-6) ---------
    min_point = min(forecast_rows, key=lambda row: row["projected_cash"])
    min_projected_cash = min_point["projected_cash"]
    min_projected_date = min_point["date"]

    breach_rows = [row for row in forecast_rows if row["projected_cash"] < threshold]
    breach_days = len(breach_rows)
    first_breach_date = breach_rows[0]["date"] if breach_rows else None

    buffer_limit = round2(MEDIUM_BUFFER_FACTOR * threshold)
    if min_projected_cash < threshold:
        risk = "HIGH"
    elif min_projected_cash < buffer_limit:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    headroom = round2(min_projected_cash - threshold)
    headroom_pct = round2(headroom / threshold * 100.0) if threshold > 0 else None

    if risk == "HIGH":
        risk_reason = (
            f"projected cash falls below the {threshold} operating threshold on "
            f"{first_breach_date} ({breach_days} of {horizon_days} days below)"
        )
    elif risk == "MEDIUM":
        risk_reason = (
            f"minimum projected cash {min_projected_cash} stays above the "
            f"{threshold} operating threshold but within 25% of it"
        )
    else:
        risk_reason = (
            f"minimum projected cash {min_projected_cash} keeps at least 25% "
            f"headroom above the {threshold} operating threshold"
        )

    return {
        "tool": "forecast_cashflow",
        "status": "ok",
        "merchant_id": merchant_id,
        "merchant_name": merchant.name if merchant is not None else None,
        "scope": "merchant" if merchant_id is not None else "all_merchants",
        "model": MODEL_NAME,
        "horizon_days": horizon_days,
        "history_days": history_days,
        "history_observed_days": len(observed),
        "history_start": observed[0]["date"].isoformat(),
        "history_end": anchor_date.isoformat(),
        "anchor_date": anchor_date.isoformat(),
        "anchor_balance": anchor_balance,
        "daily_avg_inflow": daily_avg_inflow,
        "daily_avg_outflow": daily_avg_outflow,
        "daily_avg_net": daily_avg_net,
        "recent_window_days": len(recent),
        "recent_avg_inflow": recent_avg_inflow,
        "recent_avg_outflow": recent_avg_outflow,
        "recent_avg_net": recent_avg_net,
        "net_trend_per_day": net_trend_per_day,
        "projected_inflow_per_day": recent_avg_inflow,
        "projected_outflow_per_day": recent_avg_outflow,
        "projected_net_per_day": recent_avg_net,
        "forecast": forecast_rows,
        "projected_ending_balance": forecast_rows[-1]["projected_cash"],
        "min_projected_cash": min_projected_cash,
        "min_projected_date": min_projected_date,
        "first_breach_date": first_breach_date,
        "breach_days": breach_days,
        "operating_threshold": threshold,
        "headroom": headroom,
        "headroom_pct": headroom_pct,
        "risk": risk,
        "risk_reason": risk_reason,
        "confidence": confidence,
        "volatility_cv": volatility_cv,
        "sources": {
            "merchant_id": merchant_id,
            "table": "cash_flows",
            "anchor_date": anchor_date.isoformat(),
        },
    }

