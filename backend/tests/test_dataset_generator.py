"""Phase 1 tests: synthetic dataset generator.

Validates the invariants later phases rely on: determinism, referential
integrity, per-scenario detectability, demo anchoring (short Tuesday), and
cash-flow math. All checks run against in-memory datasets (no files needed).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import pytest

from app.services.dataset_generator import (
    ANOMALY_SCENARIOS,
    DUPLICATE_WINDOW_MINUTES,
    FEE_OVERCHARGE_RATE,
    GST_ERROR_RATE,
    GST_RATE,
    INJECTED_SCENARIOS,
    MERCHANT_SPECS,
    RECON_EXCEPTION_SCENARIOS,
    REFUND_OVERPAY_RATE,
    SCN_ANOMALY,
    SCN_DUPLICATE,
    SCN_FEE_MISMATCH,
    SCN_GST,
    SCN_LEDGER,
    SCN_MISSING,
    SCN_NORMAL,
    SCN_REFUND_MISMATCH,
    SCN_TIMING,
    SCN_FAILED_WRITE,
    SETTLEMENT_LAG_DAYS,
    TIMING_MISMATCH_EXTRA_DAYS,
    generate_dataset,
    most_recent_tuesday,
    summarize,
    write_dataset,
)

# Small fixed params keep the suite fast; scenario coverage stays complete.
KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1}
END = date(2026, 9, 3)  # a Thursday: demo_tuesday == 2026-09-01


@pytest.fixture(scope="module")
def ds() -> dict:
    return generate_dataset(**KWS, end_date=END)


def by_id(rows: list[dict], key: str) -> dict[str, dict]:
    return {row[key]: row for row in rows}


def _spec(merchant_id: str):
    for spec in MERCHANT_SPECS:
        if spec.merchant_id == merchant_id:
            return spec
    raise AssertionError(merchant_id)


def _dt(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")


def _labelled(ds: dict, scenario: str) -> list[dict]:
    ids = {l["transaction_id"] for l in ds["labels"] if l["scenario"] == scenario}
    return [t for t in ds["transactions"] if t["transaction_id"] in ids]


# ---------------------------------------------------------------------------
# Determinism and metadata
# ---------------------------------------------------------------------------


def test_same_seed_same_output() -> None:
    a = generate_dataset(**KWS, end_date=END)
    b = generate_dataset(**KWS, end_date=END)
    assert a == b


def test_different_seed_changes_output() -> None:
    a = generate_dataset(**KWS, seed=42, end_date=END)
    b = generate_dataset(**KWS, seed=43, end_date=END)
    assert a["transactions"] != b["transactions"]


def test_metadata_basics(ds: dict) -> None:
    meta = ds["metadata"]
    assert meta["seed"] == 42
    assert meta["transactions"] == 100
    assert meta["start_date"] == "2026-08-07"
    assert meta["end_date"] == "2026-09-03"
    assert meta["demo_tuesday"] == "2026-09-01"
    assert meta["synthetic"] is True
    assert len(ds["merchants"]) == 5


def test_default_call_is_reproducible() -> None:
    # No arguments: still deterministic (fixed anchor date, not wall clock).
    assert generate_dataset() == generate_dataset()


def test_most_recent_tuesday() -> None:
    assert most_recent_tuesday(date(2026, 9, 3)) == date(2026, 9, 1)
    assert most_recent_tuesday(date(2026, 9, 1)) == date(2026, 8, 25)  # strictly before
    assert most_recent_tuesday(date(2026, 9, 8)) == date(2026, 9, 1)


def test_parameter_validation() -> None:
    with pytest.raises(ValueError):
        generate_dataset(transactions=5, end_date=END)
    with pytest.raises(ValueError):
        generate_dataset(window_days=4, end_date=END)
    with pytest.raises(ValueError):
        generate_dataset(customers=2, end_date=END)
    with pytest.raises(ValueError):
        generate_dataset(exceptions_per_type=-1, end_date=END)


# ---------------------------------------------------------------------------
# Counts and referential integrity
# ---------------------------------------------------------------------------


def test_expected_record_counts(ds: dict) -> None:
    # 9 scenario txns + 1 duplicate twin = 10 injected; rest normal.
    assert len(ds["transactions"]) == 100
    assert len(ds["labels"]) == 100
    assert len(ds["invoices"]) == 100
    assert len(ds["fees"]) == 100
    assert len(ds["settlements"]) == 99  # 100 minus one MISSING_SETTLEMENT
    scenarios = Counter(l["scenario"] for l in ds["labels"])
    assert scenarios[SCN_NORMAL] == 90
    for name in INJECTED_SCENARIOS:
        assert scenarios[name] >= 1
    assert scenarios[SCN_DUPLICATE] == 2  # original + duplicate twin
    assert scenarios[SCN_NORMAL] + sum(scenarios[s] for s in INJECTED_SCENARIOS) == 100


def test_ids_are_unique_and_prefixed(ds: dict) -> None:
    assert len({t["transaction_id"] for t in ds["transactions"]}) == 100
    assert all(t["transaction_id"].startswith("TXN-") for t in ds["transactions"])
    assert all(s["settlement_id"].startswith("SET-") for s in ds["settlements"])
    assert all(f["fee_id"].startswith("FEE-") for f in ds["fees"])
    assert all(i["invoice_id"].startswith("INV-") for i in ds["invoices"])
    assert all(r["refund_id"].startswith("RFD-") for r in ds["refunds"])
    assert all(e["entry_id"].startswith("LE-") for e in ds["ledger_entries"])
    for entity, key in (
        ("settlements", "settlement_id"), ("fees", "fee_id"),
        ("invoices", "invoice_id"), ("refunds", "refund_id"),
        ("ledger_entries", "entry_id"),
    ):
        ids = [row[key] for row in ds[entity]]
        assert len(ids) == len(set(ids)), entity


def test_foreign_keys_resolve(ds: dict) -> None:
    txns = by_id(ds["transactions"], "transaction_id")
    merchants = {m["merchant_id"] for m in ds["merchants"]}
    customers = {c["customer_id"] for c in ds["customers"]}
    settlement_ids = {s["settlement_id"] for s in ds["settlements"]}
    invoice_ids = {i["invoice_id"] for i in ds["invoices"]}
    for t in ds["transactions"]:
        assert t["merchant_id"] in merchants
        assert t["customer_id"] in customers
        assert t["settlement_id"] is None or t["settlement_id"] in settlement_ids
        assert t["invoice_id"] in invoice_ids
    for s in ds["settlements"]:
        assert s["transaction_id"] in txns
        assert s["merchant_id"] in merchants
    for f in ds["fees"]:
        assert f["transaction_id"] in txns
    for i in ds["invoices"]:
        assert i["transaction_id"] in txns
    for r in ds["refunds"]:
        assert r["transaction_id"] in txns
        assert r["merchant_id"] == txns[r["transaction_id"]]["merchant_id"]
    for e in ds["ledger_entries"]:
        assert e["transaction_id"] in txns


def test_one_fee_one_invoice_one_label_per_transaction(ds: dict) -> None:
    txn_ids = {t["transaction_id"] for t in ds["transactions"]}
    assert len(ds["fees"]) == 100
    assert len(ds["invoices"]) == 100
    assert len(ds["labels"]) == 100
    assert {f["transaction_id"] for f in ds["fees"]} == txn_ids
    assert {i["transaction_id"] for i in ds["invoices"]} == txn_ids
    assert {l["transaction_id"] for l in ds["labels"]} == txn_ids


def test_dates_inside_window(ds: dict) -> None:
    start = date.fromisoformat(ds["metadata"]["start_date"])
    end = date.fromisoformat(ds["metadata"]["end_date"])
    for t in ds["transactions"]:
        d = date.fromisoformat(t["timestamp"][:10])
        assert start <= d <= end
    for s in ds["settlements"]:
        assert start <= date.fromisoformat(s["settlement_date"]) <= end
    for r in ds["refunds"]:
        assert start <= date.fromisoformat(r["processed_date"]) <= end
    for row in ds["cash_flows"]:
        assert start <= date.fromisoformat(row["date"]) <= end


# ---------------------------------------------------------------------------
# Scenario: per-type detectability (Phase 3 recon + Phase 6 ML ground truth)
# ---------------------------------------------------------------------------


def test_normal_records_are_fully_consistent(ds: dict) -> None:
    """Every NORMAL transaction must pass all deterministic checks."""
    txns = by_id(ds["transactions"], "transaction_id")
    settles = by_id(ds["settlements"], "transaction_id")
    fees = by_id(ds["fees"], "transaction_id")
    invoices = by_id(ds["invoices"], "invoice_id")
    ledger = defaultdict(list)
    for e in ds["ledger_entries"]:
        ledger[e["transaction_id"]].append(e)

    for label in ds["labels"]:
        if label["scenario"] != SCN_NORMAL:
            continue
        txn = txns[label["transaction_id"]]
        s = settles[txn["transaction_id"]]
        assert txn["settlement_id"] is not None
        assert s["gross_amount"] == txn["amount"]
        assert s["fee_amount"] == txn["fee"]
        assert s["net_amount"] == round(txn["amount"] - txn["fee"], 2)
        # T+2 settlement
        txn_day = date.fromisoformat(txn["timestamp"][:10])
        assert date.fromisoformat(s["settlement_date"]) == txn_day + timedelta(days=SETTLEMENT_LAG_DAYS)
        # fee expected == recorded
        fee = fees[txn["transaction_id"]]
        assert fee["expected_amount"] == fee["recorded_amount"] == txn["fee"]
        # GST internally consistent (paise-exact decomposition)
        inv = invoices[txn["invoice_id"]]
        assert round(inv["taxable_value"] + inv["gst_amount"], 2) == txn["amount"]
        gst_true = round(txn["amount"] * GST_RATE / (1 + GST_RATE), 2)
        assert inv["gst_amount"] == gst_true
        assert abs(inv["gst_amount"] - round(inv["taxable_value"] * GST_RATE, 2)) <= 0.02
        # ledger: one posted net entry
        entries = ledger[txn["transaction_id"]]
        assert len(entries) == 1
        assert entries[0]["status"] == "posted"
        assert entries[0]["amount"] == s["net_amount"]
        # refund consistency
        refunds = [r for r in ds["refunds"] if r["transaction_id"] == txn["transaction_id"]]
        if txn["status"] == "refunded":
            assert len(refunds) == 1
            assert refunds[0]["expected_amount"] == refunds[0]["recorded_amount"] == txn["amount"]
        else:
            assert refunds == []
        # labels: not an exception or anomaly
        assert label["recon_exception"] is False
        assert label["anomaly"] is False


def test_fee_mismatch_overcharged(ds: dict) -> None:
    txn = _labelled(ds, SCN_FEE_MISMATCH)[0]
    settle = by_id(ds["settlements"], "transaction_id")[txn["transaction_id"]]
    expected_fee = round(txn["amount"] * _spec(txn["merchant_id"]).fee_rate, 2)
    assert settle["fee_amount"] == round(expected_fee + txn["amount"] * FEE_OVERCHARGE_RATE, 2)
    assert settle["net_amount"] == round(txn["amount"] - settle["fee_amount"], 2)
    fee = by_id(ds["fees"], "transaction_id")[txn["transaction_id"]]
    assert fee["expected_amount"] == expected_fee
    assert fee["recorded_amount"] == settle["fee_amount"] > expected_fee


def test_refund_mismatch_over_refunded(ds: dict) -> None:
    txn = _labelled(ds, SCN_REFUND_MISMATCH)[0]
    refunds = [r for r in ds["refunds"] if r["transaction_id"] == txn["transaction_id"]]
    assert len(refunds) == 1
    r = refunds[0]
    assert r["expected_amount"] == txn["amount"]
    assert r["recorded_amount"] == round(txn["amount"] * (1 + REFUND_OVERPAY_RATE), 2)
    assert r["recorded_amount"] > r["expected_amount"]
    assert txn["status"] == "refunded"

def test_duplicate_pair(ds: dict) -> None:
    pair = _labelled(ds, SCN_DUPLICATE)
    assert len(pair) == 2
    a, b = pair
    # same merchant/customer/amount
    assert a["merchant_id"] == b["merchant_id"]
    assert a["customer_id"] == b["customer_id"]
    assert a["amount"] == b["amount"]
    # within the duplicate window
    gap_minutes = abs((_dt(b["timestamp"]) - _dt(a["timestamp"])).total_seconds()) / 60.0
    assert gap_minutes <= DUPLICATE_WINDOW_MINUTES
    # both settled normally (the money moved twice)
    settles = by_id(ds["settlements"], "transaction_id")
    assert a["settlement_id"] and b["settlement_id"]
    assert settles[a["transaction_id"]]["net_amount"] == settles[b["transaction_id"]]["net_amount"]
    # label details cross-reference each other
    labels = {l["transaction_id"]: l for l in ds["labels"] if l["scenario"] == SCN_DUPLICATE}
    assert {l["details"]["role"] for l in labels.values()} == {"original", "duplicate"}
    for l in labels.values():
        assert l["details"]["partner"] in labels


def test_timing_mismatch_settles_late(ds: dict) -> None:
    txn = _labelled(ds, SCN_TIMING)[0]
    settle = by_id(ds["settlements"], "transaction_id")[txn["transaction_id"]]
    txn_day = date.fromisoformat(txn["timestamp"][:10])
    assert date.fromisoformat(settle["settlement_date"]) == \
        txn_day + timedelta(days=SETTLEMENT_LAG_DAYS + TIMING_MISMATCH_EXTRA_DAYS)


def test_missing_settlement(ds: dict) -> None:
    txn = _labelled(ds, SCN_MISSING)[0]
    assert txn["settlement_id"] is None
    assert txn["transaction_id"] not in {s["transaction_id"] for s in ds["settlements"]}
    # but the receivable was booked in the ledger
    entries = [e for e in ds["ledger_entries"] if e["transaction_id"] == txn["transaction_id"]]
    assert len(entries) == 1
    assert entries[0]["debit_account"] == "Payment Receivable"
    assert entries[0]["status"] == "posted"


def test_ledger_mismatch_gross_posted(ds: dict) -> None:
    txn = _labelled(ds, SCN_LEDGER)[0]
    settle = by_id(ds["settlements"], "transaction_id")[txn["transaction_id"]]
    entry = [e for e in ds["ledger_entries"] if e["transaction_id"] == txn["transaction_id"]][0]
    assert entry["amount"] == txn["amount"]           # gross, not net
    assert entry["amount"] > settle["net_amount"]    # overstates cash


def test_gst_mismatch(ds: dict) -> None:
    txn = _labelled(ds, SCN_GST)[0]
    inv = by_id(ds["invoices"], "invoice_id")[txn["invoice_id"]]
    expected = round(txn["amount"] * GST_RATE / (1 + GST_RATE), 2)
    assert inv["gst_amount"] == round(expected * (1 + GST_ERROR_RATE), 2)
    assert inv["gst_amount"] > expected
    # invoice no longer internally consistent
    assert round(inv["taxable_value"] + inv["gst_amount"], 2) != txn["amount"]

def test_hidden_anomaly_is_recon_consistent(ds: dict) -> None:
    """HIDDEN_ANOMALY passes every deterministic check (but is unusual)."""
    txn = _labelled(ds, SCN_ANOMALY)[0]
    label = [l for l in ds["labels"] if l["transaction_id"] == txn["transaction_id"]][0]
    assert label["recon_exception"] is False
    assert label["anomaly"] is True
    settle = by_id(ds["settlements"], "transaction_id")[txn["transaction_id"]]
    assert settle["fee_amount"] == txn["fee"]
    assert settle["net_amount"] == round(txn["amount"] - txn["fee"], 2)
    spec = _spec(txn["merchant_id"])
    assert txn["amount"] == round(spec.median_amount * 7, 2)  # 7x merchant median
    assert int(txn["timestamp"][11:13]) == 3  # 03:xx UTC: odd hour
    # no refund/ledger weirdness
    assert [r for r in ds["refunds"] if r["transaction_id"] == txn["transaction_id"]] == []
    entries = [e for e in ds["ledger_entries"] if e["transaction_id"] == txn["transaction_id"]]
    assert len(entries) == 1 and entries[0]["status"] == "posted"
    assert entries[0]["amount"] == settle["net_amount"]


def test_failed_ledger_write(ds: dict) -> None:
    txn = _labelled(ds, SCN_FAILED_WRITE)[0]
    entries = [e for e in ds["ledger_entries"] if e["transaction_id"] == txn["transaction_id"]]
    assert len(entries) == 1
    assert entries[0]["status"] == "failed"
    assert entries[0]["amount"] == round(txn["amount"] - txn["fee"], 2)


def test_label_sets_are_correct(ds: dict) -> None:
    for l in ds["labels"]:
        assert l["recon_exception"] == (l["scenario"] in RECON_EXCEPTION_SCENARIOS)
        assert l["anomaly"] == (l["scenario"] in ANOMALY_SCENARIOS)
        if l["scenario"] == SCN_NORMAL:
            assert not l["recon_exception"] and not l["anomaly"]


# ---------------------------------------------------------------------------
# Demo anchoring: "Why is Tuesday's cash short?"
# ---------------------------------------------------------------------------


def test_missing_settlement_anchors_on_demo_tuesday(ds: dict) -> None:
    txn = _labelled(ds, SCN_MISSING)[0]
    due = date.fromisoformat(txn["timestamp"][:10]) + timedelta(days=SETTLEMENT_LAG_DAYS)
    assert due == date.fromisoformat(ds["metadata"]["demo_tuesday"])
    # the missing money therefore never appears in Tuesday's aggregate inflow
    tuesday_rows = [r for r in ds["cash_flows"] if r["date"] == ds["metadata"]["demo_tuesday"]]
    assert len(tuesday_rows) == 5
    assert all(r["inflow"] >= 0 for r in tuesday_rows)


def test_fee_overcharge_settles_on_demo_tuesday(ds: dict) -> None:
    txn = _labelled(ds, SCN_FEE_MISMATCH)[0]
    settle = by_id(ds["settlements"], "transaction_id")[txn["transaction_id"]]
    assert settle["settlement_date"] == ds["metadata"]["demo_tuesday"]


def test_over_refund_lands_on_demo_tuesday(ds: dict) -> None:
    txn = _labelled(ds, SCN_REFUND_MISMATCH)[0]
    refunds = [r for r in ds["refunds"] if r["transaction_id"] == txn["transaction_id"]]
    assert refunds[0]["processed_date"] == ds["metadata"]["demo_tuesday"]


def test_demo_tuesday_is_short(ds: dict) -> None:
    """Tuesday shows both a missing inflow and an inflated outflow."""
    tuesday = ds["metadata"]["demo_tuesday"]
    missing = _labelled(ds, SCN_MISSING)[0]
    over_refund = [r for r in ds["refunds"] if r["recorded_amount"] > r["expected_amount"]][0]
    assert date.fromisoformat(tuesday) == \
        date.fromisoformat(missing["timestamp"][:10]) + timedelta(days=SETTLEMENT_LAG_DAYS)
    assert over_refund["processed_date"] == tuesday

# ---------------------------------------------------------------------------
# Cash-flow math
# ---------------------------------------------------------------------------


def test_cash_flows_match_settlements_and_refunds(ds: dict) -> None:
    inflow = defaultdict(float)
    outflow = defaultdict(float)
    for s in ds["settlements"]:
        inflow[(s["merchant_id"], s["settlement_date"])] += s["net_amount"]
    for r in ds["refunds"]:
        outflow[(r["merchant_id"], r["processed_date"])] += r["recorded_amount"]
    for row in ds["cash_flows"]:
        assert row["inflow"] == round(inflow.get((row["merchant_id"], row["date"]), 0.0), 2)
        assert row["outflow"] == round(outflow.get((row["merchant_id"], row["date"]), 0.0), 2)
        assert row["net_amount"] == round(row["inflow"] - row["outflow"], 2)


def test_cash_flow_balances_run_correctly(ds: dict) -> None:
    by_merchant = defaultdict(list)
    for row in ds["cash_flows"]:
        by_merchant[row["merchant_id"]].append(row)
    opening = {m["merchant_id"]: m["opening_balance"] for m in ds["merchants"]}
    for mid, rows in by_merchant.items():
        assert len(rows) == 28
        running = opening[mid]
        for row in rows:
            running = round(running + row["net_amount"], 2)
            assert row["closing_balance"] == running
    # merchants dict carries the end-of-window balance
    for m in ds["merchants"]:
        assert m["current_balance"] == by_merchant[m["merchant_id"]][-1]["closing_balance"]


def test_cash_flow_daily_grid_complete(ds: dict) -> None:
    start = date.fromisoformat(ds["metadata"]["start_date"])
    days = {(start + timedelta(days=i)).isoformat() for i in range(28)}
    for m in ds["merchants"]:
        got = {r["date"] for r in ds["cash_flows"] if r["merchant_id"] == m["merchant_id"]}
        assert got == days


# ---------------------------------------------------------------------------
# Scaling and I/O
# ---------------------------------------------------------------------------


def test_scaling_and_more_exceptions() -> None:
    ds = generate_dataset(transactions=300, window_days=28, exceptions_per_type=5, end_date=END)
    scenarios = Counter(l["scenario"] for l in ds["labels"])
    for name in INJECTED_SCENARIOS:
        # DUPLICATE labels both the original and the twin (2 rows per round).
        expected = 10 if name == SCN_DUPLICATE else 5
        assert scenarios[name] == expected, name
    assert len(ds["settlements"]) == 300 - 5  # one missing settlement per round
    assert len(ds["cash_flows"]) == 5 * 28
    # anomalies remain 7x merchant median at any scale
    txns = by_id(ds["transactions"], "transaction_id")
    for l in ds["labels"]:
        if l["scenario"] == SCN_ANOMALY:
            txn = txns[l["transaction_id"]]
            assert txn["amount"] == round(_spec(txn["merchant_id"]).median_amount * 7, 2)


def test_zero_exceptions_dataset_is_all_normal() -> None:
    ds = generate_dataset(transactions=100, exceptions_per_type=0, end_date=END)
    assert all(l["scenario"] == SCN_NORMAL for l in ds["labels"])
    assert all(not l["recon_exception"] for l in ds["labels"])
    assert len(ds["settlements"]) == 100
    assert 0 <= len(ds["refunds"]) < 12  # ~7% of normal txns get refunded


def test_write_dataset_roundtrip(ds: dict, tmp_path) -> None:
    import csv as csv_mod
    import json

    json_path, labels_path = write_dataset(ds, tmp_path, "unit")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data == ds
    with labels_path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv_mod.DictReader(fh))
    assert len(rows) == len(ds["labels"])
    assert {r["transaction_id"] for r in rows} == {l["transaction_id"] for l in ds["labels"]}
    dup_rows = [r for r in rows if r["scenario"] == SCN_DUPLICATE]
    assert len(dup_rows) == 2
    assert all(r["details"] for r in dup_rows)


def test_summarize_is_informative(ds: dict) -> None:
    text = summarize(ds)
    assert "transactions=100" in text
    assert SCN_MISSING in text
    assert "demo_tuesday=2026-09-01" in text
    assert "expected recon pass rate" in text


def test_ledger_entries_cover_all_transactions(ds: dict) -> None:
    tx_ids = {t["transaction_id"] for t in ds["transactions"]}
    le_ids = {e["transaction_id"] for e in ds["ledger_entries"]}
    assert le_ids == tx_ids


def test_cli_script_runs(tmp_path, capsys) -> None:
    """Smoke test the CLI wrapper end to end (writes to tmp_path)."""
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "backend" / "scripts" / "generate_dataset.py"
    runpy_ns: dict = {"__name__": "generate_dataset_cli", "__file__": str(script)}
    code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
    try:
        exec(code, runpy_ns)  # noqa: S102 - executing our own script in-process
        rc = runpy_ns["main"]([
            "--transactions", "100", "--out", str(tmp_path), "--name", "cli_smoke",
        ])
        assert rc == 0
    finally:
        sys.argv = ["generate_dataset.py"]
    out = capsys.readouterr().out
    assert "Synthetic dataset summary" in out
    assert (tmp_path / "cli_smoke.json").exists()
    assert (tmp_path / "cli_smoke_labels.csv").exists()






