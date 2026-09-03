"""Feature engineering for transaction anomaly detection (Phase 5).

One canonical pipeline shared by training and serving so the Isolation
Forest always sees the same feature semantics:

    dataset generator dicts (training)  -->  feature record  -->  matrix
    database rows via db session (serving)  -->  feature record  -->  matrix

Feature set (PRD section 11 candidates, empirically selected):

    median_ratio      amount / merchant median amount: deviation from the
                      merchant's behavioural baseline (carries the PRD's
                      amount signal, normalised so ticket *size* alone
                      cannot drive scores)
    hour              transaction hour (0-23, UTC)
    settle_delay      settlement_date - txn_date in days (missing = NaN)
    fee_ratio         fee / amount

Two deliberate scope choices keep the ML layer *alongside* (not instead
of) deterministic reconciliation, per FR-6:

- Features read only the *merchant's own system-of-record* (transaction
  row, fee, refund) plus the settlement date - never the processor's
  recorded fee/refund/GST values that reconciliation compares. Bookkeeping
  mismatches therefore stay invisible here (reconciliation's job), while
  behaviour unusual *relative to normal history* - the HIDDEN_ANOMALY (7x
  merchant median at 03:xx UTC) - stands out on several axes at once.
- The raw rupee amount, its log, weekday, refund share, and raw frequency
  counts were evaluated as candidates and excluded from the *model*: their
  rare-but-legitimate corners (lognormal amount tails, refunded normals,
  one-off customers) score above the injected anomaly and would produce
  false positives. They still surface in the reason metadata below, so
  flagged transactions can be compared attribute-by-attribute.

``explain_record`` therefore reports the full candidate picture (amount,
median, ratio, hour, weekday, settlement behaviour, fee/refund ratios,
customer) while the forest consumes the four features above.

NaN handling: IsolationForest accepts np.nan in fit/score. ``settle_delay``
is NaN only for transactions whose settlement has not arrived yet, which is
exactly the information we want the model to see.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Iterable

import numpy as np

FEATURE_NAMES: tuple[str, ...] = (
    "median_ratio",
    "hour",
    "settle_delay",
    "fee_ratio",
)
N_FEATURES = len(FEATURE_NAMES)

# One transaction reduced to exactly what the features need. Both training
# (dataset generator dicts) and serving (ORM rows) funnel through this
# shape, guaranteeing train/serve parity.
FEATURE_KEYS: tuple[str, ...] = (
    "transaction_id",
    "merchant_id",
    "customer_id",
    "timestamp",
    "amount",
    "fee",
    "refund_amount",
    "settlement_date",   # None for MISSING_SETTLEMENT-style rows
)


def round2(value: object) -> float:
    """Round a money value to 2 decimals (mirrors ``app.tools.common.round2``)."""
    return round(float(value), 2)


def round6(value: object) -> float:
    """Round a derived ratio for readability in reason payloads."""
    return round(float(value), 6)


def _as_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    # Phase 1 writes naive UTC stamps like "2026-08-12T14:03:11Z".
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def _as_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def build_feature_records(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalise raw transaction-ish rows into canonical feature records.

    ``rows`` are dicts carrying (at least) the keys in ``FEATURE_KEYS`` -
    exactly the layout the dataset generator emits for ``transactions``
    joined with their settlement dates, and the shape the serving query
    builds in ``app.tools.anomalies``.
    """
    records: list[dict[str, Any]] = []
    for row in rows:
        records.append({
            "transaction_id": row["transaction_id"],
            "merchant_id": row["merchant_id"],
            "customer_id": row["customer_id"],
            "timestamp": _as_datetime(row["timestamp"]),
            "amount": round2(row["amount"]),
            "fee": round2(row.get("fee") or 0.0),
            "refund_amount": round2(row.get("refund_amount") or 0.0),
            "settlement_date": _as_date(row.get("settlement_date")),
        })
    return records


def merchant_medians(records: list[dict[str, Any]]) -> dict[str, float]:
    """Per-merchant median amount, computed from normal-history records.

    Used as the behavioural baseline for ``median_ratio``. Serving passes
    the *training* medians so the baseline never shifts.
    """
    amounts: dict[str, list[float]] = {}
    for rec in records:
        amounts.setdefault(rec["merchant_id"], []).append(rec["amount"])
    return {
        merchant_id: float(np.median(values))
        for merchant_id, values in amounts.items()
    }


def feature_matrix(
    records: list[dict[str, Any]],
    medians: dict[str, float] | None = None,
) -> np.ndarray:
    """Build the (n, 4) float matrix the Isolation Forest consumes.

    ``medians`` defaults to the medians of ``records`` itself (training).
    Serving passes the *training* medians so the baseline never shifts.
    """
    if medians is None:
        medians = merchant_medians(records)
    if not records:
        return np.zeros((0, N_FEATURES), dtype=float)

    rows_out: list[list[float]] = []
    for rec in records:
        amount = rec["amount"]
        median = medians.get(rec["merchant_id"])
        median_ratio = amount / median if median else 1.0
        ts = rec["timestamp"]
        settle = rec["settlement_date"]
        settle_delay = (settle - ts.date()).days if settle is not None else math.nan
        rows_out.append([
            median_ratio,
            float(ts.hour),
            float(settle_delay),
            rec["fee"] / amount if amount > 0 else 0.0,
        ])
    return np.asarray(rows_out, dtype=float)


def explain_record(
    record: dict[str, Any],
    medians: dict[str, float],
) -> dict[str, Any]:
    """Human-readable reason metadata for one scored record (FR-6).

    Returns every feature used by the model plus the derived ratio against
    the merchant baseline, so a flagged transaction can be compared with
    normal behaviour attribute-by-attribute (PRD anomaly-view requirement:
    "compare normal vs unusual attributes").
    """
    amount = record["amount"]
    median = medians.get(record["merchant_id"])
    median_ratio = amount / median if median else None
    ts = record["timestamp"]
    settle = record["settlement_date"]
    return {
        "merchant_id": record["merchant_id"],
        "timestamp": ts.isoformat() + "Z",
        "amount": amount,
        "merchant_median": round2(median) if median is not None else None,
        "amount_vs_median": round6(median_ratio) if median_ratio is not None else None,
        "hour": ts.hour,
        "day_of_week": int(ts.weekday()),
        "settlement_date": settle.isoformat() if settle else None,
        "settlement_delay_days": (settle - ts.date()).days if settle else None,
        "fee_ratio": round6(record["fee"] / amount) if amount > 0 else None,
        "refund_ratio": round6(record["refund_amount"] / amount) if amount > 0 else None,
        "customer_id": record["customer_id"],
    }
