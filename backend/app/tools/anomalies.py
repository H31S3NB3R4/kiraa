"""ML anomaly-detection tool (Phase 5, PRD FR-6).

``detect_anomalies`` scores transactions with the trained Isolation Forest
(``ml/train_anomaly``) and returns, per transaction, the anomaly score, a
high/medium/low severity band, and reason metadata that compares the
record against the merchant's normal behaviour. It runs *alongside*
deterministic reconciliation - never replacing it - so every result also
cross-links the deterministic verdict: the flagship demo case is a
transaction with

    reconciliation = PASS
    anomaly        = HIGH

(the injected HIDDEN_ANOMALY: 7x the merchant median at 03:xx UTC,
perfectly consistent books).

Scope and filters mirror the PRD tool contract: optional ``merchant_id``,
optional ``transaction_ids``, and a ``limit`` that caps the returned
list (results are ordered by score descending; metrics always describe
the full scan). Scores are computed with the *training* merchant medians
so the behavioural baseline never drifts, and the model is resolved as

    explicit ``model`` argument  >  persisted artifact (ml/artifacts/)
                                   >  deterministic in-process retraining

Ground-truth metrics (precision/recall/false-positive rate against
``dataset_labels.anomaly``) are included whenever labels exist, which
makes the Phase 12 evaluation harness a single tool call. Scores are
upserted into ``anomaly_scores`` idempotently per
``(transaction_id, model_version)``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AnomalyScore,
    DatasetLabel,
    Merchant,
    ReconciliationException,
    Settlement,
    Transaction,
)
from ml import features
from ml.train_anomaly import (
    DEFAULT_ARTIFACT_PATH,
    MODEL_VERSION,
    load_model,
    score_records,
    train_model,
)

__all__ = ["detect_anomalies"]

DEFAULT_LIMIT = 500


@lru_cache(maxsize=1)
def _fallback_bundle() -> dict[str, Any]:
    """Deterministic in-process retrain (used when no artifact exists)."""
    return train_model()


def resolve_model(model: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """Resolve the scoring model: explicit bundle, artifact, or retrain."""
    if model is not None:
        return model, "provided"
    if DEFAULT_ARTIFACT_PATH.exists():
        return load_model(), "artifact"
    return _fallback_bundle(), "in-process"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)



def _serving_records(
    db: Session,
    merchant_id: str | None,
    transaction_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """Load in-scope transactions joined with their settlement dates.

    Ordered by (timestamp, transaction_id) so results are stable; the
    feature pipeline turns a missing settlement into a NaN ``settle_delay``
    (the model sees "settlement not received", never a fabricated value).
    """
    stmt = (
        select(
            Transaction.transaction_id,
            Transaction.merchant_id,
            Transaction.customer_id,
            Transaction.timestamp,
            Transaction.amount,
            Transaction.fee,
            Transaction.refund_amount,
            Settlement.settlement_date,
        )
        .outerjoin(Settlement, Settlement.transaction_id == Transaction.transaction_id)
        .order_by(Transaction.timestamp, Transaction.transaction_id)
    )
    if merchant_id is not None:
        stmt = stmt.where(Transaction.merchant_id == merchant_id)
    if transaction_ids is not None:
        stmt = stmt.where(Transaction.transaction_id.in_(transaction_ids))

    rows = [
        {
            "transaction_id": txn_id,
            "merchant_id": mid,
            "customer_id": cust,
            "timestamp": ts,
            "amount": amount,
            "fee": fee,
            "refund_amount": refund,
            "settlement_date": settle,
        }
        for txn_id, mid, cust, ts, amount, fee, refund, settle in db.execute(stmt)
    ]
    return features.build_feature_records(rows)


def _ground_truth(db: Session, txn_ids: list[str]) -> dict[str, bool]:
    """Ground-truth anomaly labels for the scored transactions (if seeded)."""
    if not txn_ids:
        return {}
    rows = db.execute(
        select(DatasetLabel.transaction_id, DatasetLabel.anomaly).where(
            DatasetLabel.transaction_id.in_(txn_ids)
        )
    ).all()
    return {txn_id: anomaly for txn_id, anomaly in rows}


def _recon_flags(db: Session, txn_ids: list[str]) -> set[str]:
    """Transactions currently flagged by deterministic reconciliation."""
    if not txn_ids:
        return set()
    return set(
        db.execute(
            select(ReconciliationException.transaction_id).where(
                ReconciliationException.transaction_id.in_(txn_ids)
            )
        ).scalars()
    )


def _reason_text(explanation: dict[str, Any], severity: str) -> str:
    """One-sentence human reason for a scored transaction."""
    ratio = explanation.get("amount_vs_median")
    hour = explanation.get("hour")
    parts: list[str] = []
    if ratio is not None:
        parts.append(
            f"amount {explanation['amount']:,.2f} is {ratio:.2f}x the merchant "
            f"median {explanation['merchant_median']:,.2f}"
        )
    if hour is not None:
        parts.append(f"posted at {int(hour):02d}:xx UTC")
    delay = explanation.get("settlement_delay_days")
    if delay is not None:
        parts.append(f"settlement T+{int(delay)}")
    detail = "; ".join(parts) if parts else "no feature data"
    return f"{severity.upper()} - {detail}"


def _precision_recall(
    scored: list[dict[str, Any]], truth: dict[str, bool]
) -> dict[str, Any] | None:
    """Precision/recall/FPR of ``is_anomaly`` against ground truth."""
    labelled = [s for s in scored if s["transaction_id"] in truth]
    if not labelled:
        return None
    flagged = [s for s in labelled if s["is_anomaly"]]
    positives = [s for s in labelled if truth[s["transaction_id"]]]
    negatives = [s for s in labelled if not truth[s["transaction_id"]]]
    tp = sum(1 for s in flagged if truth[s["transaction_id"]])
    fp = len(flagged) - tp
    fn = len(positives) - tp
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    fpr = fp / len(negatives) if negatives else 0.0
    return {
        "labelled_transactions": len(labelled),
        "ground_truth_anomalies": len(positives),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "false_positive_rate": round(fpr, 4),
    }


def detect_anomalies(
    db: Session,
    merchant_id: str | None = None,
    transaction_ids: list[str] | None = None,
    limit: int | None = DEFAULT_LIMIT,
    *,
    persist: bool = True,
    model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score transactions for statistical unusualness (PRD FR-6).

    Scans the in-scope transactions (optional merchant / explicit id
    list), scores each with the trained Isolation Forest, cross-links the
    deterministic verdict, computes ground-truth metrics when
    ``dataset_labels`` cover the scan, and upserts the scores into
    ``anomaly_scores`` idempotently.
    """
    if transaction_ids is not None and not isinstance(transaction_ids, (list, tuple)):
        raise ValueError("transaction_ids must be a list of transaction ids")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be a positive integer or None")
    if merchant_id is not None and not isinstance(merchant_id, str):
        raise ValueError("merchant_id must be a string")

    filters: dict[str, Any] = {
        "merchant_id": merchant_id,
        "transaction_ids": list(transaction_ids) if transaction_ids is not None else None,
    }
    sources: dict[str, Any] = {"tables": ["transactions", "settlements"]}

    if merchant_id is not None:
        merchant = db.get(Merchant, merchant_id)
        if merchant is None:
            return {
                "tool": "detect_anomalies",
                "status": "unknown_merchant",
                "merchant_id": merchant_id,
                "sources": sources,
            }
        sources["merchant_id"] = merchant_id

    records = _serving_records(db, merchant_id, filters["transaction_ids"])
    if not records:
        return {
            "tool": "detect_anomalies",
            "status": "no_transactions",
            "merchant_id": merchant_id,
            "filters": filters,
            "model": None,
            "scores": [],
            "truncated": False,
            "metrics": None,
            "ground_truth": None,
            "persisted": None,
            "sources": sources,
        }

    bundle, origin = resolve_model(model)
    scored = score_records(bundle, records)

    truth = _ground_truth(db, [r["transaction_id"] for r in records])
    recon_flags = _recon_flags(db, [r["transaction_id"] for r in records])

    results: list[dict[str, Any]] = []
    for rec, s in zip(records, scored, strict=True):
        explanation = features.explain_record(rec, bundle["medians"])
        reason = _reason_text(explanation, s["severity"])
        recon_pass = rec["transaction_id"] not in recon_flags
        results.append({
            "transaction_id": rec["transaction_id"],
            "merchant_id": rec["merchant_id"],
            "anomaly_score": s["anomaly_score"],
            "severity": s["severity"],
            "is_anomaly": s["is_anomaly"],
            "reconciliation_pass": recon_pass,
            "reason": reason,
            "features": explanation,
        })

    results.sort(key=lambda r: (-r["anomaly_score"], r["transaction_id"]))

    flagged = [r for r in results if r["is_anomaly"]]
    metrics: dict[str, Any] = {
        "transactions_scored": len(results),
        "flagged_anomalies": len(flagged),
        "high": sum(1 for r in results if r["severity"] == "high"),
        "medium": sum(1 for r in results if r["severity"] == "medium"),
        "low": sum(1 for r in results if r["severity"] == "low"),
    }
    ground_truth_metrics = _precision_recall(results, truth)

    truncated = limit is not None and len(results) > limit
    returned = results[:limit] if limit is not None else results

    persisted: dict[str, int] | None = None
    if persist:
        now = _utcnow()
        txn_ids = [r["transaction_id"] for r in results]
        existing = {
            row.transaction_id: row
            for row in db.execute(
                select(AnomalyScore).where(
                    AnomalyScore.transaction_id.in_(txn_ids),
                    AnomalyScore.model_version == bundle["model_version"],
                )
            ).scalars()
        }
        new_count = updated_count = 0
        for r in results:
            row = existing.get(r["transaction_id"])
            reasons = {
                "severity": r["severity"],
                "reason": r["reason"],
                "features": r["features"],
                "reconciliation_pass": r["reconciliation_pass"],
            }
            if row is None:
                db.add(AnomalyScore(
                    transaction_id=r["transaction_id"],
                    scored_at=now,
                    model_version=bundle["model_version"],
                    anomaly_score=r["anomaly_score"],
                    is_anomaly=r["is_anomaly"],
                    reasons=reasons,
                ))
                new_count += 1
            else:
                row.scored_at = now
                row.anomaly_score = r["anomaly_score"]
                row.is_anomaly = r["is_anomaly"]
                row.reasons = reasons
                updated_count += 1
        db.commit()
        persisted = {"new": new_count, "updated": updated_count}

    return {
        "tool": "detect_anomalies",
        "status": "ok",
        "merchant_id": merchant_id,
        "filters": filters,
        "model": {
            "name": bundle["model_name"],
            "version": bundle["model_version"],
            "threshold": bundle["threshold"],
            "watch_margin": bundle.get("watch_margin"),
            "origin": origin,
        },
        "scores": returned,
        "truncated": truncated,
        "metrics": metrics,
        "ground_truth": ground_truth_metrics,
        "persisted": persisted,
        "sources": sources,
    }
