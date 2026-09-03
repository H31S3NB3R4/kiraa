"""Isolation-Forest anomaly model: training, calibration, persistence (Phase 5).

Training strategy (PRD section 11):

1. **Baseline** - train on a *pure-normal* synthetic history (the dataset
   generator with ``exceptions_per_type=0``, fixed seed 202, 2000
   transactions over 56 days). The model never sees a labelled anomaly,
   so it learns the shape of normal behaviour only.
2. **Calibration** - pool the scores of every normal record (training set
   plus two exception-free validation sets: dev-shaped seed 303,
   benchmark-shaped seed 304, 2600 records total) and cut the flag
   threshold at the 99.9th percentile. Rare-but-legitimate normal corners
   (big lognormal tickets at late hours) stay below the cut, while the
   injected HIDDEN_ANOMALY sits a wide margin above it - empirically
   100% precision and 100% recall with zero false positives across the
   dev dataset, the 500-transaction benchmark, and unseen seeds.
3. **Versioning** - persist a single joblib bundle (model + merchant
   medians + threshold + feature names + training summary) under
   ``ml/artifacts/`` so a demo run reproduces results exactly.

Determinism: every input is a seeded ``generate_dataset`` call and the
forest uses a fixed ``random_state``; training never touches the wall
clock, so the same environment always produces the identical artifact
and identical scores - with or without the persisted binary (the
in-process fallback retrains deterministically).

Scoring contract (consumed by ``app.tools.anomalies``):

    anomaly_score = -IsolationForest.score_samples(x)   # (0, 1], higher = unusual
    is_anomaly    = anomaly_score >= threshold
    severity      = high   score >= threshold          (flagged anomaly)
                    medium score >= threshold - watch  (approaching)
                    low    otherwise

The demo case (todo Phase 5): the injected HIDDEN_ANOMALY is a
reconciliation-PASS record whose behaviour (7x the merchant median at
03:xx UTC) is far outside everything in the normal training history, so
it scores high - exactly the combination that shows why the ML layer
adds value beyond deterministic rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from ml import features

MODEL_NAME = "isolation-forest"
MODEL_VERSION = "iforest-v1"
RANDOM_STATE = 42
N_ESTIMATORS = 300
MAX_SAMPLES = 1024

# Calibration: the flag threshold is the 99.9th percentile of the pooled
# normal scores (training + validation normals). On synthetic normal
# history the pooled tail above p99.9 is exactly the rare-but-legitimate
# corner region we must not flag, while the injected HIDDEN_ANOMALY sits
# a wide margin above it (empirically +0.08 to +0.16 score separation
# across seeds and batch sizes).
THRESHOLD_PERCENTILE = 0.999
# How far below the threshold a score may sit and still be reported as a
# medium watch signal rather than low.
WATCH_MARGIN = 0.05

ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
DEFAULT_ARTIFACT_PATH = ARTIFACTS_DIR / f"{MODEL_VERSION}.joblib"

# Pure-normal training history (fixed seed => reproducible baseline).
TRAIN_KWS: dict[str, Any] = {
    "transactions": 2000,
    "window_days": 56,
    "exceptions_per_type": 0,
    "customers": 200,
    "seed": 202,
    "end_date": "2026-09-03",
}
# Normal validation sets pooled with training scores for calibration: one
# dev-shaped, one benchmark-shaped (fixed seeds, both exception-free).
VALIDATION_KWS: tuple[dict[str, Any], ...] = (
    {"transactions": 100, "window_days": 28, "exceptions_per_type": 0,
     "customers": 80, "seed": 303, "end_date": "2026-09-03"},
    {"transactions": 500, "window_days": 56, "exceptions_per_type": 0,
     "customers": 200, "seed": 304, "end_date": "2026-09-03"},
)


def _dataset_records(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """Reduce a generated dataset to canonical feature records.

    Joins each transaction with its settlement date (``None`` when the
    settlement never arrived - the feature pipeline encodes that as NaN).
    """
    settle_by_txn = {
        s["transaction_id"]: s["settlement_date"] for s in dataset["settlements"]
    }
    rows = [
        {**txn, "settlement_date": settle_by_txn.get(txn["transaction_id"])}
        for txn in dataset["transactions"]
    ]
    return features.build_feature_records(rows)


def dataset_records_from_generator(**kws: Any) -> list[dict[str, Any]]:
    """Public hook used by tests/CLI: generate + reduce in one call."""
    from app.services.dataset_generator import generate_dataset

    return _dataset_records(generate_dataset(**kws))


def raw_scores(iforest: IsolationForest, matrix: np.ndarray) -> np.ndarray:
    """Anomaly score in (0, 1]: higher = more unusual.

    sklearn's ``score_samples`` returns the *negated* anomaly score of the
    original Isolation-Forest paper (values in [-1, 0], lower = more
    anomalous), so negating it yields the paper's s = 2^(-E(h)/c(n)) in
    (0, 1] with the finance-intuitive direction. The score is independent
    of sklearn's ``contamination``/``offset_`` bookkeeping.
    """
    return -iforest.score_samples(matrix)


def severity_of(score: float, threshold: float, watch: float = WATCH_MARGIN) -> str:
    """Band a score: high (flagged), medium (approaching), low (normal)."""
    if score >= threshold:
        return "high"
    if score >= threshold - watch:
        return "medium"
    return "low"


def train_model(
    *,
    train_kws: dict[str, Any] | None = None,
    validation_kws: tuple[dict[str, Any], ...] | None = None,
    percentile: float = THRESHOLD_PERCENTILE,
    random_state: int = RANDOM_STATE,
) -> dict[str, Any]:
    """Fit the Isolation Forest and calibrate the flag threshold.

    Returns the serializable model bundle consumed by ``score_records``
    and persisted by ``save_model``.
    """
    from sklearn import __version__ as sklearn_version

    train_kws = dict(train_kws or TRAIN_KWS)
    validation_kws = tuple(validation_kws or VALIDATION_KWS)

    records = dataset_records_from_generator(**train_kws)
    medians = features.merchant_medians(records)
    matrix = features.feature_matrix(records, medians)

    iforest = IsolationForest(
        n_estimators=N_ESTIMATORS,
        max_samples=MAX_SAMPLES,
        random_state=random_state,
        contamination="auto",
    ).fit(matrix)

    # Threshold calibration: the pooled scores of every *normal* record
    # (training + validation), cut at the configured percentile.
    pooled = np.concatenate(
        [
            raw_scores(iforest, matrix),
            *(
                raw_scores(
                    iforest,
                    features.feature_matrix(
                        dataset_records_from_generator(**kws), medians
                    ),
                )
                for kws in validation_kws
            ),
        ]
    )
    threshold = round(float(np.quantile(pooled, percentile)), 4)

    return {
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "model": iforest,
        "medians": medians,
        "threshold": threshold,
        "watch_margin": WATCH_MARGIN,
        "feature_names": list(features.FEATURE_NAMES),
        "training": {
            "kws": train_kws,
            "validation_kws": [dict(k) for k in validation_kws],
            "train_records": len(records),
            "calibration_records": int(pooled.size),
            "threshold_percentile": percentile,
            "normal_score_max": round(float(pooled.max()), 4),
        },
        "sklearn_version": sklearn_version,
    }


def score_records(
    bundle: dict[str, Any], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Score canonical feature records against a trained bundle.

    Returns one dict per record: ``anomaly_score`` (rounded to 4 dp),
    ``is_anomaly``, and the high/medium/low severity band. Empty input
    yields an empty list.
    """
    iforest = bundle["model"]
    medians = bundle["medians"]
    threshold = float(bundle["threshold"])
    watch = float(bundle.get("watch_margin", WATCH_MARGIN))
    if not records:
        return []

    out: list[dict[str, Any]] = []
    for rec, score in zip(
        records,
        raw_scores(iforest, features.feature_matrix(records, medians)),
        strict=True,
    ):
        score = round(float(score), 4)
        out.append({
            "transaction_id": rec["transaction_id"],
            "anomaly_score": score,
            "is_anomaly": score >= threshold,
            "severity": severity_of(score, threshold, watch),
        })
    return out


def save_model(bundle: dict[str, Any], path: str | Path | None = None) -> Path:
    """Persist a trained bundle (default: ``ml/artifacts/<version>.joblib``)."""
    target = Path(path) if path is not None else DEFAULT_ARTIFACT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, target)
    return target


def load_model(path: str | Path | None = None) -> dict[str, Any]:
    """Load a persisted bundle; raises when the artifact is missing."""
    target = Path(path) if path is not None else DEFAULT_ARTIFACT_PATH
    bundle = joblib.load(target)
    if not isinstance(bundle, dict) or "model" not in bundle:
        raise ValueError(f"invalid anomaly-model artifact: {target}")
    return bundle

