"""Phase 3 tests: deterministic finance engine (reconciliation, ledger, GST).

Mirrors the Phase 2 fixture pattern: generate a small dataset (seed 42,
100 transactions, 1 exception per type), seed a temp SQLite DB, then run
the tools against it. The engine must reproduce the dataset's ground
truth with 100% precision and recall: exactly the 9 labelled exception
rows (the duplicate pair contributes two), while every NORMAL record and
the HIDDEN_ANOMALY pass clean. Impact values must match the injected
error rates (0.5% fee overcharge, 15% refund overpay, 8% GST error,
T+5 instead of T+2 settlement). Re-runs must be deterministic and
persistence idempotent.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DatasetLabel, ReconciliationException
from app.services.dataset_generator import (
    SCN_ANOMALY,
    SCN_DUPLICATE,
    SCN_FAILED_WRITE,
    SCN_FEE_MISMATCH,
    SCN_GST,
    SCN_LEDGER,
    SCN_MISSING,
    SCN_NORMAL,
    SCN_REFUND_MISMATCH,
    SCN_TIMING,
    generate_dataset,
    write_dataset,
)
from app.services.db_seed import build_engine, load_dataset_file, seed_database
from app.tools import check_gst_match, query_ledger, run_reconciliation

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1}
END = date(2026, 9, 3)  # a Thursday: demo_tuesday == 2026-09-01


def _by_txn(dataset: dict, section: str) -> dict:
    """Index a dataset section (whose rows all carry transaction_id) by txn."""
    return {row["transaction_id"]: row for row in dataset[section]}


def _txn_day(txn: dict) -> date:
    return date.fromisoformat(txn["timestamp"][:10])


def _exc(result: dict, txn_id: str) -> dict:
    """Return the single exception the engine produced for ``txn_id``."""
    matches = [e for e in result["exceptions"] if e["transaction_id"] == txn_id]
    assert len(matches) == 1, f"expected exactly one exception for {txn_id}: {matches}"
    return matches[0]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate a small dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase3")
    dataset = generate_dataset(**KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "dataset")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'finance.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    bundle.demo_tuesday = date.fromisoformat(dataset["metadata"]["demo_tuesday"])
    bundle.merchants = {m["merchant_id"]: m for m in dataset["merchants"]}
    bundle.txns = _by_txn(dataset, "transactions")
    bundle.settlements = _by_txn(dataset, "settlements")
    bundle.refunds = _by_txn(dataset, "refunds")
    bundle.invoices = _by_txn(dataset, "invoices")
    bundle.ledger = _by_txn(dataset, "ledger_entries")
    return bundle


@pytest.fixture(scope="module")
def label_rows(seeded) -> dict[str, DatasetLabel]:
    """Ground truth as seeded into ``dataset_labels`` (per transaction)."""
    with Session(seeded.engine) as session:
        rows = session.execute(select(DatasetLabel)).scalars().all()
    assert len(rows) == KWS["transactions"]
    return {row.transaction_id: row for row in rows}


@pytest.fixture(scope="module")
def by_scenario(label_rows) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for txn_id, row in sorted(label_rows.items()):
        grouped.setdefault(row.scenario, []).append(txn_id)
    return grouped


@pytest.fixture(scope="module")
def recon(seeded):
    """Run the reconciliation engine once over the whole seeded DB."""
    with Session(seeded.engine) as session:
        return run_reconciliation(session)


# ---------------------------------------------------------------------------
# Ground-truth equivalence: 100% precision and recall
# ---------------------------------------------------------------------------


def test_labels_cover_all_ten_scenarios(by_scenario) -> None:
    assert set(by_scenario) == {
        SCN_NORMAL, SCN_FEE_MISMATCH, SCN_REFUND_MISMATCH, SCN_DUPLICATE,
        SCN_TIMING, SCN_MISSING, SCN_LEDGER, SCN_GST, SCN_ANOMALY, SCN_FAILED_WRITE,
    }


def test_exact_exception_count(recon) -> None:
    # 8 exception scenarios x 1 + duplicate pair (2 rows, both labelled).
    assert recon["metrics"]["exceptions"] == 9
    assert len(recon["exceptions"]) == 9


def test_perfect_precision_and_recall(seeded, label_rows, recon) -> None:
    found = {e["transaction_id"] for e in recon["exceptions"]}
    truth = {
        txn_id for txn_id, label in label_rows.items()
        if label.recon_exception
    }
    assert found == truth, (
        f"missed={sorted(truth - found)} false-positives={sorted(found - truth)}"
    )


def test_exception_types_match_ground_truth(by_scenario, recon) -> None:
    type_by_txn = {e["transaction_id"]: e["exception_type"] for e in recon["exceptions"]}
    expected_type_by_txn = {
        **{txn_id: "FEE_MISMATCH" for txn_id in by_scenario[SCN_FEE_MISMATCH]},
        **{txn_id: "REFUND_MISMATCH" for txn_id in by_scenario[SCN_REFUND_MISMATCH]},
        **{txn_id: "DUPLICATE_TRANSACTION" for txn_id in by_scenario[SCN_DUPLICATE]},
        **{txn_id: "SETTLEMENT_TIMING_MISMATCH" for txn_id in by_scenario[SCN_TIMING]},
        **{txn_id: "MISSING_SETTLEMENT" for txn_id in by_scenario[SCN_MISSING]},
        **{txn_id: "LEDGER_MISMATCH" for txn_id in by_scenario[SCN_LEDGER]},
        **{txn_id: "GST_MISMATCH" for txn_id in by_scenario[SCN_GST]},
        **{txn_id: "FAILED_LEDGER_WRITE" for txn_id in by_scenario[SCN_FAILED_WRITE]},
    }
    assert type_by_txn == expected_type_by_txn


def test_normal_and_hidden_anomaly_pass_clean(by_scenario, recon) -> None:
    flagged = {e["transaction_id"] for e in recon["exceptions"]}
    for scenario in (SCN_NORMAL, SCN_ANOMALY):
        for txn_id in by_scenario[scenario]:
            assert txn_id not in flagged, f"{scenario} {txn_id} wrongly flagged"


def test_metrics_shape(seeded, recon) -> None:
    metrics = recon["metrics"]
    assert metrics["transactions"] == KWS["transactions"]
    # 9 exception rows across 9 distinct txns (the duplicate pair spans 2).
    assert metrics["exception_transactions"] == 9
    assert metrics["matched"] == KWS["transactions"] - 9
    assert metrics["by_type"]["DUPLICATE_TRANSACTION"] == 2
    for exception_type, count in metrics["by_type"].items():
        if exception_type != "DUPLICATE_TRANSACTION":
            assert count == 1, exception_type
    assert metrics["exceptions"] == sum(metrics["by_type"].values())
    expected_rate = round(100.0 * (KWS["transactions"] - 9) / KWS["transactions"], 2)
    assert metrics["match_rate_pct"] == expected_rate


# ---------------------------------------------------------------------------
# Per-scenario evidence: impacts and dates match the injected error rates
# ---------------------------------------------------------------------------


def test_fee_mismatch_impact_matches_overcharge(seeded, by_scenario, recon) -> None:
    txn_id = by_scenario[SCN_FEE_MISMATCH][0]
    exc = _exc(recon, txn_id)
    txn = seeded.txns[txn_id]
    expected_fee = round(float(txn["amount"]) * float(seeded.merchants[txn["merchant_id"]]["fee_rate"]), 2)
    overcharge = round(float(txn["amount"]) * 0.005, 2)
    assert exc["exception_type"] == "FEE_MISMATCH"
    assert exc["expected_amount"] == pytest.approx(expected_fee)
    assert exc["recorded_amount"] == pytest.approx(expected_fee + overcharge)
    assert exc["financial_impact"] == pytest.approx(overcharge)
    assert exc["exception_date"] == date.fromisoformat(
        seeded.settlements[txn_id]["settlement_date"]
    )


def test_refund_mismatch_impact_matches_overpay(seeded, by_scenario, recon) -> None:
    txn_id = by_scenario[SCN_REFUND_MISMATCH][0]
    exc = _exc(recon, txn_id)
    amount = float(seeded.txns[txn_id]["amount"])
    overpay = round(amount * 0.15, 2)
    assert exc["exception_type"] == "REFUND_MISMATCH"
    assert exc["expected_amount"] == pytest.approx(amount)
    assert exc["recorded_amount"] == pytest.approx(amount + overpay)
    assert exc["financial_impact"] == pytest.approx(overpay)
    assert exc["exception_date"] == date.fromisoformat(
        seeded.refunds[txn_id]["processed_date"]
    )


def test_gst_mismatch_impact_matches_error_rate(seeded, by_scenario, recon) -> None:
    txn_id = by_scenario[SCN_GST][0]
    exc = _exc(recon, txn_id)
    gst_expected = round(float(seeded.invoices[txn_id]["total_amount"]) * 0.18 / 1.18, 2)
    gst_error = round(gst_expected * 0.08, 2)
    assert exc["exception_type"] == "GST_MISMATCH"
    assert exc["expected_amount"] == pytest.approx(gst_expected)
    assert exc["recorded_amount"] == pytest.approx(gst_expected + gst_error)
    assert exc["financial_impact"] == pytest.approx(gst_error)
    assert exc["exception_date"] == date.fromisoformat(
        seeded.invoices[txn_id]["issue_date"]
    )


def test_timing_mismatch_dates_and_impact(seeded, by_scenario, recon) -> None:
    txn_id = by_scenario[SCN_TIMING][0]
    exc = _exc(recon, txn_id)
    settlement = seeded.settlements[txn_id]
    due = _txn_day(seeded.txns[txn_id]) + timedelta(days=2)
    net = round(float(settlement["net_amount"]), 2)
    assert exc["exception_type"] == "SETTLEMENT_TIMING_MISMATCH"
    assert exc["exception_date"] == due
    assert exc["expected_amount"] == pytest.approx(net)
    assert exc["financial_impact"] == pytest.approx(net)
    assert date.fromisoformat(settlement["settlement_date"]) == due + timedelta(days=3)


def test_missing_settlement_dates_and_impact(seeded, by_scenario, recon) -> None:
    txn_id = by_scenario[SCN_MISSING][0]
    exc = _exc(recon, txn_id)
    txn = seeded.txns[txn_id]
    expected_fee = round(float(txn["amount"]) * float(seeded.merchants[txn["merchant_id"]]["fee_rate"]), 2)
    net = round(float(txn["amount"]) - expected_fee, 2)
    assert exc["exception_type"] == "MISSING_SETTLEMENT"
    assert exc["exception_date"] == _txn_day(txn) + timedelta(days=2)
    assert exc["expected_amount"] == pytest.approx(net)
    assert exc["recorded_amount"] == 0.0
    assert exc["financial_impact"] == pytest.approx(net)
    assert txn["settlement_id"] is None  # engine saw the truth, not a hidden row


def test_ledger_mismatch_impact_matches_gross_posting(seeded, by_scenario, recon) -> None:
    txn_id = by_scenario[SCN_LEDGER][0]
    exc = _exc(recon, txn_id)
    settlement = seeded.settlements[txn_id]
    expected_net = round(float(settlement["net_amount"]), 2)
    gross = round(float(settlement["gross_amount"]), 2)
    assert exc["exception_type"] == "LEDGER_MISMATCH"
    assert exc["expected_amount"] == pytest.approx(expected_net)
    assert exc["recorded_amount"] == pytest.approx(gross)
    assert exc["financial_impact"] == pytest.approx(gross - expected_net)
    assert exc["exception_date"] == date.fromisoformat(
        seeded.ledger[txn_id]["entry_date"]
    )


def test_failed_ledger_write_impact(seeded, by_scenario, recon) -> None:
    txn_id = by_scenario[SCN_FAILED_WRITE][0]
    exc = _exc(recon, txn_id)
    expected_net = round(float(seeded.settlements[txn_id]["net_amount"]), 2)
    assert exc["exception_type"] == "FAILED_LEDGER_WRITE"
    assert exc["expected_amount"] == pytest.approx(expected_net)
    assert exc["recorded_amount"] == 0.0
    assert exc["financial_impact"] == pytest.approx(expected_net)
    assert seeded.ledger[txn_id]["status"] == "failed"


def test_duplicate_pair_both_flagged_with_partner_refs(seeded, by_scenario, recon) -> None:
    pair = by_scenario[SCN_DUPLICATE]
    assert len(pair) == 2
    excs = {txn_id: _exc(recon, txn_id) for txn_id in pair}
    assert set(excs) == set(pair)
    for txn_id in pair:
        exc = excs[txn_id]
        partner = next(t for t in pair if t != txn_id)
        assert exc["exception_type"] == "DUPLICATE_TRANSACTION"
        assert exc["sources"]["duplicate_of"] == [partner]
        assert exc["financial_impact"] == pytest.approx(float(seeded.txns[txn_id]["amount"]))
    first, second = sorted(pair, key=lambda t: seeded.txns[t]["timestamp"])
    gap = (
        _tx_datetime(seeded.txns[second]["timestamp"])
        - _tx_datetime(seeded.txns[first]["timestamp"])
    )
    assert timedelta(minutes=0) < gap <= timedelta(minutes=10)


# ---------------------------------------------------------------------------
# Determinism, idempotent persistence, filters
# ---------------------------------------------------------------------------


def _tx_datetime(iso: str):
    from datetime import datetime

    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=None)


def test_rerun_deterministic_and_idempotent(seeded, recon) -> None:
    with Session(seeded.engine) as session:
        second = run_reconciliation(session)
    assert second["exceptions"] == recon["exceptions"]  # deterministic
    assert second["persisted"] == {"new": 0, "updated": 9}  # idempotent upsert

    with Session(seeded.engine) as session:
        rows = session.execute(select(ReconciliationException)).scalars().all()
    assert len(rows) == 9  # no duplicates after re-run
    assert {(r.transaction_id, r.exception_type) for r in rows} == {
        (e["transaction_id"], e["exception_type"]) for e in recon["exceptions"]
    }


def test_persisted_rows_match_payload(seeded, recon) -> None:
    with Session(seeded.engine) as session:
        rows = {
            (r.transaction_id, r.exception_type): r
            for r in session.execute(select(ReconciliationException)).scalars()
        }
    assert recon["persisted"] == {"new": 9, "updated": 0}
    for exception in recon["exceptions"]:
        row = rows[(exception["transaction_id"], exception["exception_type"])]
        assert row.severity == exception["severity"]
        assert float(row.expected_amount) == pytest.approx(exception["expected_amount"])
        assert float(row.recorded_amount) == pytest.approx(exception["recorded_amount"])
        assert float(row.financial_impact) == pytest.approx(exception["financial_impact"])
        assert row.description == exception["description"]
        assert row.status == "open"


def test_recon_skips_persistence_when_disabled(seeded) -> None:
    with Session(seeded.engine) as session:
        before = session.execute(select(ReconciliationException)).scalars().all()
        result = run_reconciliation(session, persist=False)
    assert result["persisted"] is None
    assert result["metrics"]["exceptions"] == 9
    with Session(seeded.engine) as session:
        after = session.execute(select(ReconciliationException)).scalars().all()
    assert len(before) == len(after) == 9


def test_merchant_filter_scopes_transactions(seeded) -> None:
    merchant_id = next(iter(seeded.merchants))
    with Session(seeded.engine) as session:
        result = run_reconciliation(session, merchant_id=merchant_id)
    assert result["filters"]["merchant_id"] == merchant_id
    assert result["metrics"]["transactions"] > 0
    in_scope_txn_ids = {
        t["transaction_id"] for t in seeded.dataset["transactions"]
        if t["merchant_id"] == merchant_id
    }
    assert {e["transaction_id"] for e in result["exceptions"]} <= in_scope_txn_ids


def _count_txns_between(seeded, start: str, end: str) -> int:
    return sum(
        1 for t in seeded.dataset["transactions"]
        if start <= t["timestamp"][:10] <= end
    )


def test_date_filters_and_bad_range(seeded) -> None:
    with Session(seeded.engine) as session:
        full = run_reconciliation(session)
        scoped = run_reconciliation(
            session, start_date="2026-08-06", end_date="2026-08-07"
        )
    expected = _count_txns_between(seeded, "2026-08-06", "2026-08-07")
    assert scoped["metrics"]["transactions"] == expected
    assert scoped["metrics"]["transactions"] <= full["metrics"]["transactions"]

    with pytest.raises(ValueError):
        with Session(seeded.engine) as session:
            run_reconciliation(session, start_date="2026-09-01", end_date="2026-08-01")


# ---------------------------------------------------------------------------
# GST tool (FR-5)
# ---------------------------------------------------------------------------


def test_gst_match_matched_invoice(seeded, by_scenario) -> None:
    txn_id = by_scenario[SCN_NORMAL][0]
    with Session(seeded.engine) as session:
        result = check_gst_match(session, txn_id)
    assert result["status"] == "matched"
    invoice = seeded.invoices[txn_id]
    assert result["invoice_id"] == invoice["invoice_id"]
    assert result["sources"]["invoice_id"] == invoice["invoice_id"]
    assert result["difference"] == pytest.approx(0.0, abs=1e-9)
    assert result["expected_tax"] == pytest.approx(invoice["gst_amount"])


def test_gst_match_mismatched_invoice(seeded, by_scenario) -> None:
    txn_id = by_scenario[SCN_GST][0]
    with Session(seeded.engine) as session:
        result = check_gst_match(session, txn_id)
    assert result["status"] == "mismatch"
    assert result["difference"] == pytest.approx(
        round(result["expected_tax"] * 0.08, 2)
    )
    assert result["sources"]["invoice_id"] == seeded.invoices[txn_id]["invoice_id"]


def test_gst_match_unknown_transaction(seeded) -> None:
    with Session(seeded.engine) as session:
        result = check_gst_match(session, "TXN-9999")
    assert result["status"] == "not_found"
    assert result["sources"] == {"transaction_id": "TXN-9999"}


# ---------------------------------------------------------------------------
# Ledger tool (FR-3)
# ---------------------------------------------------------------------------


def test_ledger_by_transaction_id(seeded) -> None:
    txn_id = next(iter(seeded.txns))
    with Session(seeded.engine) as session:
        result = query_ledger(session, transaction_id=txn_id)
    assert result["count"] == 1
    row = result["rows"][0]
    assert row["transaction_id"] == txn_id
    assert row["entry_id"] == seeded.ledger[txn_id]["entry_id"]
    assert row["settlement_id"] == seeded.txns[txn_id]["settlement_id"]
    assert row["invoice_id"] == seeded.txns[txn_id]["invoice_id"]


def test_ledger_merchant_date_status_filters(seeded) -> None:
    with Session(seeded.engine) as session:
        failed = query_ledger(session, status="failed")
    assert failed["count"] == 1  # exactly one FAILED_LEDGER_WRITE in the dataset
    merchant_id = failed["rows"][0]["merchant_id"]

    with Session(seeded.engine) as session:
        result = query_ledger(session, merchant_id=merchant_id, status="failed")
    assert result["count"] == 1
    assert all(r["status"] == "failed" for r in result["rows"])
    assert all(r["merchant_id"] == merchant_id for r in result["rows"])

    with Session(seeded.engine) as session:
        ranged = query_ledger(session, start_date="2026-08-06", end_date="2026-08-07")
    dates = [r["entry_date"] for r in ranged["rows"]]
    assert all("2026-08-06" <= d <= "2026-08-07" for d in dates)
    assert result["truncated"] is False


def test_ledger_account_and_category_filters(seeded) -> None:
    with Session(seeded.engine) as session:
        failed_writes = query_ledger(session, status="failed")
        account = failed_writes["rows"][0]["debit_account"]
        by_account = query_ledger(session, account=account)
    assert by_account["count"] >= failed_writes["count"]
    assert all(
        account in (r["debit_account"], r["credit_account"])
        for r in by_account["rows"]
    )

    with Session(seeded.engine) as session:
        category = query_ledger(session, category="grocery-ecommerce")
    assert category["count"] > 0
    assert all(r["merchant_category"] == "grocery-ecommerce" for r in category["rows"])


def test_ledger_limit_and_truncation(seeded) -> None:
    with Session(seeded.engine) as session:
        limited = query_ledger(session, limit=5)
        unlimited = query_ledger(session, limit=None)
    assert limited["count"] == 5
    assert limited["truncated"] is True
    assert unlimited["count"] == KWS["transactions"]
    assert unlimited["truncated"] is False


def test_ledger_rejects_inverted_range(seeded) -> None:
    with pytest.raises(ValueError):
        with Session(seeded.engine) as session:
            query_ledger(session, start_date="2026-09-01", end_date="2026-08-01")




