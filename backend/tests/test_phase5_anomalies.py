"""Phase 5 tests: ML anomaly detection (features, training, serving tool).

Mirrors the earlier fixture patterns: generate the dev dataset (seed 42,
100 transactions, 1 exception per type) and a benchmark-shaped dataset
(500 transactions, 5 exceptions per type), seed temp SQLite DBs, then
validate the whole ML layer against the dataset's ground truth:

- the feature pipeline must reproduce the dataset's own values (amounts,
  fees, settlement dates) exactly, with missing settlements becoming NaN,
- training must be deterministic and its threshold calibrated on pooled
  normal scores,
- ``detect_anomalies`` must flag exactly the HIDDEN_ANOMALY records with
  **100% precision and 100% recall and zero false positives** on both
  datasets - while every deterministic-recon exception stays unflagged,
  proving the ML layer runs *alongside* reconciliation (FR-6),
- the flagship demo case (reconciliation PASS + anomaly HIGH) must hold,
- persistence must be idempotent, filters/guards/limits must behave, and
  results must validate against the API schema.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas.anomalies import AnomalyResponse
from app.models import AnomalyScore
from app.services.dataset_generator import (
    SCN_ANOMALY,
    SCN_FEE_MISMATCH,
    SCN_MISSING,
    generate_dataset,
    write_dataset,
)
from app.services.db_seed import build_engine, load_dataset_file, seed_database
from app.tools import detect_anomalies, run_reconciliation
from ml import features
from ml.train_anomaly import (
    DEFAULT_ARTIFACT_PATH,
    MODEL_VERSION,
    TRAIN_KWS,
    dataset_records_from_generator,
    load_model,
    save_model,
    score_records,
    severity_of,
    train_model,
)

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1,
       "customers": 80, "seed": 42}
BENCH_KWS = {"transactions": 500, "window_days": 56, "exceptions_per_type": 5,
             "customers": 200, "seed": 42}
END = date(2026, 9, 3)  # same anchor day as the Phase 3/4 suites

# Columns of the (n, 4) feature matrix, by name.
_COL = {name: i for i, name in enumerate(features.FEATURE_NAMES)}


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate the dev dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase5")
    dataset = generate_dataset(**KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "dataset")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'finance.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    bundle.txns = {t["transaction_id"]: t for t in dataset["transactions"]}
    bundle.labels = {l["transaction_id"]: l for l in dataset["labels"]}
    return bundle


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    """Benchmark-shaped dataset (500 txns, 5 exceptions per type) -> temp DB."""
    out_dir = tmp_path_factory.mktemp("phase5_bench")
    dataset = generate_dataset(**BENCH_KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "benchmark")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'benchmark.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    bundle.labels = {l["transaction_id"]: l for l in dataset["labels"]}
    return bundle


@pytest.fixture(scope="module")
def model_bundle():
    """Train the anomaly model once (deterministic; ~seconds)."""
    return train_model()


@pytest.fixture(scope="module")
def anomalies(seeded, model_bundle, recon):
    """Run detect_anomalies once over the dev DB (read-only).

    Depends on ``recon`` so the persisted reconciliation exceptions exist
    before the cross-link snapshot is taken (the flagship PASS + HIGH case
    and the cross-link tests rely on this ordering).
    """
    with Session(seeded.engine) as session:
        return detect_anomalies(session, model=model_bundle, persist=False)


@pytest.fixture(scope="module")
def bench_anomalies(bench, model_bundle):
    """Run detect_anomalies once over the benchmark DB (read-only)."""
    with Session(bench.engine) as session:
        return detect_anomalies(session, model=model_bundle, persist=False)


@pytest.fixture(scope="module")
def recon(seeded):
    """Run deterministic reconciliation once over the dev DB (persisted)."""
    with Session(seeded.engine) as session:
        return run_reconciliation(session)


def _hidden_anomaly(labels: dict) -> str:
    """The single ground-truth anomaly transaction id (dev dataset)."""
    hits = [tid for tid, label in labels.items() if label["anomaly"]]
    assert len(hits) == 1
    return hits[0]


def _by_scenario(labels: dict, scenario: str) -> list[str]:
    return [tid for tid, label in labels.items() if label["scenario"] == scenario]



# ---------------------------------------------------------------------------
# Feature pipeline (train/serve parity, exact values, NaN semantics)
# ---------------------------------------------------------------------------


def test_feature_records_reproduce_dataset_values(seeded) -> None:
    from ml.train_anomaly import _dataset_records

    records = _dataset_records(seeded.dataset)
    assert len(records) == KWS["transactions"]
    by_id = {r["transaction_id"]: r for r in records}

    for txn in seeded.dataset["transactions"]:
        rec = by_id[txn["transaction_id"]]
        assert rec["amount"] == round(float(txn["amount"]), 2)
        assert rec["fee"] == round(float(txn["fee"]), 2)
        assert rec["merchant_id"] == txn["merchant_id"]
        assert rec["customer_id"] == txn["customer_id"]
        assert rec["timestamp"].isoformat() + "Z" == txn["timestamp"]


def test_missing_settlement_becomes_nan(seeded) -> None:
    from ml.train_anomaly import _dataset_records

    records = _dataset_records(seeded.dataset)
    by_id = {r["transaction_id"]: r for r in records}
    missing = [t["transaction_id"] for t in seeded.dataset["transactions"]
               if t["settlement_id"] is None]
    assert missing, "dataset must contain a MISSING_SETTLEMENT row"
    for txn_id in missing:
        assert by_id[txn_id]["settlement_date"] is None
        matrix = features.feature_matrix(
            [by_id[txn_id]], features.merchant_medians(records)
        )
        assert np.isnan(matrix[0, _COL["settle_delay"]])


def test_feature_matrix_values_are_exact(seeded) -> None:
    from ml.train_anomaly import _dataset_records

    records = _dataset_records(seeded.dataset)
    medians = features.merchant_medians(records)
    matrix = features.feature_matrix(records, medians)
    assert matrix.shape == (len(records), features.N_FEATURES)

    settle_by_txn = {
        s["transaction_id"]: s for s in seeded.dataset["settlements"]
    }
    for rec, row in zip(records, matrix, strict=True):
        assert row[_COL["median_ratio"]] == pytest.approx(
            rec["amount"] / medians[rec["merchant_id"]]
        )
        assert row[_COL["hour"]] == rec["timestamp"].hour
        assert row[_COL["fee_ratio"]] == pytest.approx(rec["fee"] / rec["amount"])
        settle = settle_by_txn.get(rec["transaction_id"])
        if settle is None:
            assert np.isnan(row[_COL["settle_delay"]])
        else:
            expected = (
                date.fromisoformat(settle["settlement_date"]) - rec["timestamp"].date()
            ).days
            assert row[_COL["settle_delay"]] == expected


def test_medians_computed_from_training_history() -> None:
    records = dataset_records_from_generator(**TRAIN_KWS)
    medians = features.merchant_medians(records)
    assert set(medians) == {"M001", "M002", "M003", "M004", "M005"}
    amounts: dict[str, list[float]] = {}
    for rec in records:
        amounts.setdefault(rec["merchant_id"], []).append(rec["amount"])
    for merchant_id, values in amounts.items():
        assert medians[merchant_id] == pytest.approx(float(np.median(values)))


def test_train_and_serving_matrices_match(seeded) -> None:
    """The serving query must reduce to the same records as training rows."""
    from app.tools.anomalies import _serving_records
    from ml.train_anomaly import _dataset_records

    with Session(seeded.engine) as session:
        serving = _serving_records(session, None, None)
    training = _dataset_records(seeded.dataset)
    assert [r["transaction_id"] for r in serving] == [
        r["transaction_id"] for r in training
    ]
    for s, t in zip(serving, training, strict=True):
        assert s == t


# ---------------------------------------------------------------------------
# Training and calibration
# ---------------------------------------------------------------------------


def test_training_is_deterministic() -> None:
    first = train_model()
    second = train_model()
    assert first["threshold"] == second["threshold"]
    assert first["medians"] == second["medians"]
    assert first["training"] == second["training"]


def test_training_summary(model_bundle) -> None:
    bundle = model_bundle
    assert bundle["model_name"] == "isolation-forest"
    assert bundle["model_version"] == MODEL_VERSION
    assert bundle["feature_names"] == list(features.FEATURE_NAMES)
    training = bundle["training"]
    assert training["train_records"] == TRAIN_KWS["transactions"]
    # pooled: train + two validation sets (100 + 500)
    assert training["calibration_records"] == TRAIN_KWS["transactions"] + 100 + 500
    assert training["threshold_percentile"] == 0.999
    # threshold is cut from the pooled normal scores, below their max.
    assert training["normal_score_max"] >= bundle["threshold"]


def test_scores_round_trip(model_bundle) -> None:
    records = dataset_records_from_generator(**TRAIN_KWS)
    scored = score_records(model_bundle, records)
    assert len(scored) == len(records)
    threshold = model_bundle["threshold"]
    for s in scored:
        assert 0.0 < s["anomaly_score"] <= 1.0
        assert s["is_anomaly"] is (s["anomaly_score"] >= threshold)
        assert s["severity"] in {"low", "medium", "high"}
        assert s["severity"] == severity_of(s["anomaly_score"], threshold)


# ---------------------------------------------------------------------------
# Ground-truth equivalence: 100% precision, recall, zero false positives
# ---------------------------------------------------------------------------


def test_metrics_shape(anomalies) -> None:
    result = anomalies
    assert result["tool"] == "detect_anomalies"
    assert result["status"] == "ok"
    assert result["metrics"]["transactions_scored"] == KWS["transactions"]
    assert result["truncated"] is False
    assert result["model"]["version"] == MODEL_VERSION
    assert result["model"]["origin"] == "provided"
    assert result["scores"], "scores must not be empty"


def test_perfect_precision_recall_dev(anomalies) -> None:
    gt = anomalies["ground_truth"]
    assert gt["labelled_transactions"] == KWS["transactions"]
    assert gt["ground_truth_anomalies"] == 1
    assert gt["true_positives"] == 1
    assert gt["false_positives"] == 0
    assert gt["false_negatives"] == 0
    assert gt["precision"] == 1.0
    assert gt["recall"] == 1.0
    assert gt["false_positive_rate"] == 0.0


def test_perfect_precision_recall_benchmark(bench_anomalies) -> None:
    gt = bench_anomalies["ground_truth"]
    assert gt["labelled_transactions"] == BENCH_KWS["transactions"]
    assert gt["ground_truth_anomalies"] == 5
    assert gt["true_positives"] == 5
    assert gt["false_positives"] == 0
    assert gt["false_negatives"] == 0
    assert gt["precision"] == 1.0
    assert gt["recall"] == 1.0
    assert gt["false_positive_rate"] == 0.0


def test_flagged_exactly_the_hidden_anomalies(seeded, anomalies) -> None:
    flagged = {s["transaction_id"] for s in anomalies["scores"] if s["is_anomaly"]}
    truth = {tid for tid, label in seeded.labels.items() if label["anomaly"]}
    assert flagged == truth
    assert flagged == set(_by_scenario(seeded.labels, SCN_ANOMALY))


def test_hidden_anomaly_details(seeded, anomalies) -> None:
    txn_id = _hidden_anomaly(seeded.labels)
    row = next(s for s in anomalies["scores"] if s["transaction_id"] == txn_id)
    assert row["severity"] == "high"
    assert row["is_anomaly"] is True
    # 7x merchant median, at the injected 03:xx UTC hour.
    assert row["features"]["amount_vs_median"] == pytest.approx(7.0, abs=0.5)
    assert row["features"]["hour"] == 3
    assert "7.18x" in row["reason"] or "x the merchant median" in row["reason"]
    # Scores are ordered descending; the anomaly leads the list.
    assert anomalies["scores"][0]["transaction_id"] == txn_id


def test_no_normal_or_exception_record_flagged(seeded, anomalies) -> None:
    flagged = {s["transaction_id"] for s in anomalies["scores"] if s["is_anomaly"]}
    normals = _by_scenario(seeded.labels, "NORMAL")
    assert not (flagged & set(normals))
    # Deterministic-recon scenarios (fee mismatch, missing settlement, ...)
    # stay ML-clean: bookkeeping errors are reconciliation's job.
    fee = _by_scenario(seeded.labels, SCN_FEE_MISMATCH)
    missing = _by_scenario(seeded.labels, SCN_MISSING)
    assert not (flagged & set(fee))
    assert not (flagged & set(missing))


# ---------------------------------------------------------------------------
# Flagship demo case: reconciliation PASS + anomaly HIGH
# ---------------------------------------------------------------------------


def test_reconciliation_pass_with_high_anomaly(seeded, anomalies, recon) -> None:
    txn_id = _hidden_anomaly(seeded.labels)
    row = next(s for s in anomalies["scores"] if s["transaction_id"] == txn_id)
    flagged_recon = {e["transaction_id"] for e in recon["exceptions"]}
    assert txn_id not in flagged_recon          # reconciliation passes it...
    assert row["reconciliation_pass"] is True    # ...and the tool reports that
    assert row["is_anomaly"] is True             # ...while ML flags it HIGH


def test_cross_link_reflects_recon_flags(seeded, anomalies, recon) -> None:
    flagged_recon = {e["transaction_id"] for e in recon["exceptions"]}
    for row in anomalies["scores"]:
        expected = row["transaction_id"] not in flagged_recon
        assert row["reconciliation_pass"] is expected


def test_reason_and_features_present(anomalies) -> None:
    for row in anomalies["scores"]:
        assert row["reason"].startswith(("LOW - ", "MEDIUM - ", "HIGH - "))
        feats = row["features"]
        assert feats["merchant_median"] > 0
        assert feats["settlement_date"] is None or feats["settlement_delay_days"] >= 0
        assert 0 <= feats["hour"] <= 23
        assert feats["amount_vs_median"] is not None


def test_score_records_empty_input(model_bundle) -> None:
    assert score_records(model_bundle, []) == []
    assert features.feature_matrix([]).shape == (0, features.N_FEATURES)


def test_severity_bands(model_bundle) -> None:
    threshold = model_bundle["threshold"]
    watch = model_bundle["watch_margin"]
    assert severity_of(threshold, threshold) == "high"
    assert severity_of(threshold + 0.01, threshold) == "high"
    assert severity_of(threshold - 0.01, threshold) == "medium"
    assert severity_of(threshold - watch, threshold) == "medium"
    assert severity_of(threshold - watch - 0.01, threshold) == "low"


# ---------------------------------------------------------------------------
# Tool contract: filters, guards, limits, persistence, schema, fallback
# ---------------------------------------------------------------------------


def test_merchant_filter_scopes_the_scan(seeded, model_bundle) -> None:
    """Merchant filter scopes the scan (the dev hidden anomaly is M003's)."""
    hidden = _hidden_anomaly(seeded.labels)
    merchant_id = seeded.txns[hidden]["merchant_id"]
    with Session(seeded.engine) as session:
        result = detect_anomalies(
            session, merchant_id=merchant_id, model=model_bundle, persist=False
        )
    scoped = [
        t for t in seeded.dataset["transactions"] if t["merchant_id"] == merchant_id
    ]
    assert result["status"] == "ok"
    assert result["merchant_id"] == merchant_id
    assert result["metrics"]["transactions_scored"] == len(scoped)
    assert all(s["merchant_id"] == merchant_id for s in result["scores"])
    # The hidden anomaly belongs to this merchant, so ground truth still holds.
    gt = result["ground_truth"]
    assert gt["ground_truth_anomalies"] == 1
    assert gt["true_positives"] == 1
    assert gt["precision"] == 1.0
    assert gt["recall"] == 1.0
    flagged = {s["transaction_id"] for s in result["scores"] if s["is_anomaly"]}
    assert flagged == {hidden}


def test_merchant_filter_without_anomaly_is_clean(seeded, model_bundle) -> None:
    """A merchant holding no hidden anomaly reports zero anomalies, zero FPs."""
    hidden_merchant = seeded.txns[_hidden_anomaly(seeded.labels)]["merchant_id"]
    other = next(
        t["merchant_id"] for t in seeded.dataset["transactions"]
        if t["merchant_id"] != hidden_merchant
    )
    with Session(seeded.engine) as session:
        result = detect_anomalies(
            session, merchant_id=other, model=model_bundle, persist=False
        )
    gt = result["ground_truth"]
    assert result["metrics"]["flagged_anomalies"] == 0
    assert gt["ground_truth_anomalies"] == 0
    assert gt["false_positives"] == 0
    assert gt["false_positive_rate"] == 0.0


def test_transaction_ids_filter(seeded, model_bundle) -> None:
    """An explicit id list scopes the scan; ordering stays score-descending."""
    hidden = _hidden_anomaly(seeded.labels)
    normal = next(
        tid for tid, label in seeded.labels.items()
        if label["scenario"] == "NORMAL"
    )
    with Session(seeded.engine) as session:
        result = detect_anomalies(
            session, transaction_ids=[normal, hidden],
            model=model_bundle, persist=False,
        )
    assert result["metrics"]["transactions_scored"] == 2
    assert result["filters"]["transaction_ids"] == [normal, hidden]
    ids = [s["transaction_id"] for s in result["scores"]]
    assert set(ids) == {normal, hidden}
    assert result["scores"][0]["transaction_id"] == hidden  # highest score first


def test_guard_unknown_merchant(seeded, model_bundle) -> None:
    """Unknown merchant: guard envelope (no scores) that still schema-validates."""
    with Session(seeded.engine) as session:
        result = detect_anomalies(
            session, merchant_id="M999", model=model_bundle, persist=False
        )
    assert result["status"] == "unknown_merchant"
    assert result["merchant_id"] == "M999"
    assert "scores" not in result
    response = AnomalyResponse(**result)
    assert response.scores == []
    assert response.model is None


def test_guard_no_transactions(seeded, model_bundle) -> None:
    """A filter that matches nothing returns the no_transactions envelope."""
    with Session(seeded.engine) as session:
        result = detect_anomalies(
            session, transaction_ids=["TXN-DOES-NOT-EXIST"],
            model=model_bundle, persist=False,
        )
    assert result["status"] == "no_transactions"
    assert result["scores"] == []
    assert result["truncated"] is False
    assert result["model"] is None
    assert result["metrics"] is None
    assert result["ground_truth"] is None
    assert result["persisted"] is None


def test_invalid_arguments_raise(seeded, model_bundle) -> None:
    """Bad limit / merchant_id / transaction_ids values raise immediately."""
    bad_kwargs = (
        {"limit": 0},
        {"limit": -1},
        {"limit": True},
        {"limit": "5"},
        {"merchant_id": 123},
        {"transaction_ids": "TXN-1001"},
    )
    with Session(seeded.engine) as session:
        for kwargs in bad_kwargs:
            with pytest.raises(ValueError):
                detect_anomalies(session, model=model_bundle, persist=False, **kwargs)


def test_limit_truncates_but_metrics_cover_full_scan(seeded, model_bundle) -> None:
    """limit caps the returned rows; metrics and ground truth span the scan."""
    with Session(seeded.engine) as session:
        result = detect_anomalies(session, limit=5, model=model_bundle, persist=False)
    assert len(result["scores"]) == 5
    assert result["truncated"] is True
    assert result["metrics"]["transactions_scored"] == KWS["transactions"]
    assert result["metrics"]["flagged_anomalies"] == 1
    # Descending score order survives truncation: the hidden anomaly leads.
    assert result["scores"][0]["transaction_id"] == _hidden_anomaly(seeded.labels)
    scores = [s["anomaly_score"] for s in result["scores"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Persistence, schema, determinism, and model resolution
# ---------------------------------------------------------------------------


def test_persistence_is_an_idempotent_upsert(seeded, model_bundle) -> None:
    """persist=True upserts into anomaly_scores; a rerun updates, not adds."""
    with Session(seeded.engine) as session:
        first = detect_anomalies(session, model=model_bundle, persist=True)
        rows = session.execute(
            select(func.count()).select_from(AnomalyScore)
        ).scalar_one()
    assert first["persisted"] == {"new": KWS["transactions"], "updated": 0}
    assert rows == KWS["transactions"]

    with Session(seeded.engine) as session:
        second = detect_anomalies(session, model=model_bundle, persist=True)
        rows = session.execute(
            select(func.count()).select_from(AnomalyScore)
        ).scalar_one()
    # Idempotent: the rerun updates every row in place and inserts none.
    assert second["persisted"] == {"new": 0, "updated": KWS["transactions"]}
    assert rows == KWS["transactions"]
    assert second["scores"] == first["scores"]

    # The persisted row mirrors the returned payload for the hidden anomaly.
    with Session(seeded.engine) as session:
        row = session.execute(
            select(AnomalyScore).where(
                AnomalyScore.transaction_id == _hidden_anomaly(seeded.labels),
                AnomalyScore.model_version == MODEL_VERSION,
            )
        ).scalar_one()
    assert row.is_anomaly is True
    assert row.anomaly_score == pytest.approx(first["scores"][0]["anomaly_score"])
    reasons = row.reasons
    assert reasons["severity"] == "high"
    assert reasons["reconciliation_pass"] is True
    assert "reason" in reasons and "features" in reasons


def test_result_validates_against_api_schema(anomalies) -> None:
    """The full payload serializes through the pydantic AnomalyResponse."""
    response = AnomalyResponse(**anomalies)
    assert response.tool == "detect_anomalies"
    assert response.status == "ok"
    assert len(response.scores) == KWS["transactions"]
    assert response.model is not None
    assert response.model["version"] == MODEL_VERSION
    top = response.scores[0]
    assert top.severity == "high"
    assert top.is_anomaly is True
    assert top.reconciliation_pass is True
    assert top.reason.startswith("HIGH - ")
    assert response.ground_truth["precision"] == 1.0


def test_serving_is_deterministic(seeded, model_bundle) -> None:
    """Two identical calls return identical payloads (same model, same data)."""
    with Session(seeded.engine) as session:
        one = detect_anomalies(session, model=model_bundle, persist=False)
        two = detect_anomalies(session, model=model_bundle, persist=False)
    assert one == two


def test_model_resolution_priority(model_bundle) -> None:
    """Explicit model wins; the fallback is the persisted artifact or a
    deterministic in-process retrain - both score identically."""
    from app.tools.anomalies import resolve_model

    bundle, origin = resolve_model(model_bundle)
    assert bundle is model_bundle
    assert origin == "provided"

    resolved, origin = resolve_model(None)
    assert origin in {"artifact", "in-process"}
    records = dataset_records_from_generator(**TRAIN_KWS)[:20]
    assert score_records(resolved, records) == score_records(model_bundle, records)


def test_fallback_retrain_matches_artifact(model_bundle) -> None:
    """The in-process fallback retrains deterministically and reproduces
    the persisted artifact exactly (version, threshold, medians, scores)."""
    from app.tools.anomalies import _fallback_bundle

    fallback = _fallback_bundle()
    assert fallback["model_version"] == MODEL_VERSION
    assert fallback["threshold"] == model_bundle["threshold"]
    assert fallback["medians"] == model_bundle["medians"]
    assert fallback["training"] == model_bundle["training"]

    records = dataset_records_from_generator(**TRAIN_KWS)[:20]
    assert score_records(fallback, records) == score_records(model_bundle, records)

    if DEFAULT_ARTIFACT_PATH.exists():
        artifact = load_model()
        assert artifact["threshold"] == fallback["threshold"]
        assert artifact["medians"] == fallback["medians"]
        assert score_records(artifact, records) == score_records(fallback, records)


def test_save_and_load_model_round_trip(model_bundle, tmp_path) -> None:
    """save_model -> load_model preserves the trained bundle."""
    path = save_model(model_bundle, tmp_path / "bundle.joblib")
    loaded = load_model(path)
    assert loaded["model_version"] == model_bundle["model_version"]
    assert loaded["threshold"] == model_bundle["threshold"]
    assert loaded["medians"] == model_bundle["medians"]
    assert loaded["training"] == model_bundle["training"]
    records = dataset_records_from_generator(**TRAIN_KWS)[:20]
    assert score_records(loaded, records) == score_records(model_bundle, records)
