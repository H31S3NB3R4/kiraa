"""Phase 2 tests: SQLAlchemy models, schema, and the seed service.

Runs against a disposable SQLite database in a temp directory (not the real
``data/finance.db``), generated and seeded through the same service the
CLI uses.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import Base
from app.models import (
    AgentRun,
    AnomalyScore,
    Approval,
    AuditEvent,
    CashFlow,
    DatasetLabel,
    Fee,
    Invoice,
    JournalProposal,
    LedgerEntry,
    Merchant,
    ReconciliationException,
    Refund,
    Settlement,
    ToolCall,
    Transaction,
)
from app.services.dataset_generator import (
    SCN_DUPLICATE,
    SCN_MISSING,
    SCN_NORMAL,
    generate_dataset,
    write_dataset,
)
from app.services.db_seed import (
    SeedError,
    build_engine,
    load_dataset_file,
    seed_database,
)

# 16 planned tables + dataset_labels (ground truth for Phase 12 evaluation).
REQUIRED_TABLES = {
    "merchants", "customers", "transactions", "settlements", "refunds",
    "fees", "invoices", "ledger_entries", "cash_flows",
    "reconciliation_exceptions", "anomaly_scores",
    "agent_runs", "tool_calls", "journal_proposals", "approvals",
    "audit_events", "dataset_labels",
}

# Small fixed params keep the suite fast; scenario coverage stays complete.
KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1}
END = date(2026, 9, 3)  # a Thursday: demo_tuesday == 2026-09-01

_TDATE_FIELDS = {  # section -> fields that must be `date` after seeding
    "settlements": {"settlement_date"},
    "refunds": {"initiated_date", "processed_date"},
    "fees": {"fee_date"},
    "invoices": {"issue_date"},
    "ledger_entries": {"entry_date"},
    "cash_flows": {"date"},
}


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate a small dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase2")
    dataset = generate_dataset(**KWS, end_date=END)
    json_path, labels_path = write_dataset(dataset, out_dir, "dataset")

    bundle = type("SeededDb", (), {})()
    bundle.path = json_path
    bundle.labels_path = labels_path
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'finance.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    return bundle


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


def test_all_required_tables_exist(seeded) -> None:
    names = set(inspect(seeded.engine).get_table_names())
    assert REQUIRED_TABLES <= names, names - REQUIRED_TABLES


def test_timestamps_and_audit_columns_present(seeded) -> None:
    insp = inspect(seeded.engine)
    ledger_cols = {c["name"] for c in insp.get_columns("ledger_entries")}
    assert {"created_at", "updated_at"} <= ledger_cols
    audit_cols = {c["name"] for c in insp.get_columns("audit_events")}
    assert {"event_id", "actor", "action", "object_type", "object_id"} <= audit_cols


def test_indexes_and_unique_constraints_created(seeded) -> None:
    insp = inspect(seeded.engine)
    txn_indexes = {ix["name"] for ix in insp.get_indexes("transactions")}
    assert {"ix_transactions_timestamp", "ix_transactions_merchant_ts"} <= txn_indexes
    settle_indexes = {ix["name"] for ix in insp.get_indexes("settlements")}
    assert {"ix_settlements_date_id", "ix_settlements_merchant_date"} <= settle_indexes
    cash_uniques = [
        ix["unique"] for ix in insp.get_indexes("cash_flows")
        if ix["name"] == "uq_cash_flows_merchant_date"
    ]
    assert cash_uniques == [True]


def test_foreign_keys_declared(seeded) -> None:
    insp = inspect(seeded.engine)

    def targets(table):
        return {
            (fk["referred_table"], fk["constrained_columns"][0])
            for fk in insp.get_foreign_keys(table)
        }

    assert ("transactions", "transaction_id") in targets("settlements")
    assert ("agent_runs", "run_id") in targets("tool_calls")
    assert ("agent_runs", "agent_run_id") in targets("journal_proposals")
    assert ("journal_proposals", "proposal_id") in targets("approvals")


# ---------------------------------------------------------------------------
# Seeded content
# ---------------------------------------------------------------------------


def test_row_counts_match_dataset(seeded) -> None:
    ds = seeded.dataset
    assert seeded.counts == {
        "merchants": len(ds["merchants"]),
        "customers": len(ds["customers"]),
        "transactions": len(ds["transactions"]),
        "settlements": len(ds["settlements"]),
        "refunds": len(ds["refunds"]),
        "fees": len(ds["fees"]),
        "invoices": len(ds["invoices"]),
        "ledger_entries": len(ds["ledger_entries"]),
        "cash_flows": len(ds["cash_flows"]),
        "labels": len(ds["labels"]),
    }
    with Session(seeded.engine) as session:
        assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 100
        assert session.execute(select(func.count()).select_from(Settlement)).scalar_one() == len(ds["settlements"])


def test_double_seed_guard(seeded) -> None:
    with pytest.raises(SeedError, match="already contains data"):
        seed_database(seeded.engine, seeded.dataset)


def test_recreate_replaces_data(seeded) -> None:
    small = generate_dataset(transactions=40, window_days=28, exceptions_per_type=0, end_date=END)
    counts = seed_database(seeded.engine, small, recreate=True)
    assert counts["transactions"] == 40
    # restore the original seeded state for later tests
    seed_database(seeded.engine, seeded.dataset, recreate=True)


def test_load_dataset_file_rejects_bad_input(tmp_path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(SeedError, match="not found"):
        load_dataset_file(missing)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SeedError, match="valid JSON"):
        load_dataset_file(bad)
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"metadata": {}}), encoding="utf-8")
    with pytest.raises(SeedError, match="missing sections"):
        load_dataset_file(incomplete)


def test_decimal_money_round_trip(seeded) -> None:
    with Session(seeded.engine) as session:
        txn = session.execute(select(Transaction).limit(1)).scalar_one()
        assert isinstance(txn.amount, Decimal)
        assert float(txn.amount) == pytest.approx(float(Decimal(str(txn.amount))))


def test_dates_coerced_to_python_types(seeded) -> None:
    model_map = {
        "settlements": Settlement,
        "refunds": Refund,
        "fees": Fee,
        "invoices": Invoice,
        "ledger_entries": LedgerEntry,
        "cash_flows": CashFlow,
    }
    with Session(seeded.engine) as session:
        for section, model in model_map.items():
            row = session.execute(select(model).limit(1)).scalar_one()
            for field in _TDATE_FIELDS[section]:
                assert isinstance(getattr(row, field), date), (section, field)


def test_labels_table_matches_labels_csv(seeded) -> None:
    with seeded.labels_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        csv_rows = {r["transaction_id"]: r for r in reader}
    with Session(seeded.engine) as session:
        db_rows = session.execute(select(DatasetLabel)).scalars().all()
        assert len(db_rows) == len(csv_rows) == 100
        for label in db_rows:
            csv_row = csv_rows[label.transaction_id]
            assert label.scenario == csv_row["scenario"]
            assert label.recon_exception is (csv_row["recon_exception"] == "true")
            assert label.anomaly is (csv_row["anomaly"] == "true")


def test_normal_records_are_reconcilable(seeded) -> None:
    """Every NORMAL transaction must satisfy all deterministic checks."""
    with Session(seeded.engine) as session:
        labels = session.execute(
            select(DatasetLabel).where(DatasetLabel.scenario == SCN_NORMAL)
        ).scalars().all()
        assert labels, "test dataset must contain NORMAL rows"
        for label in labels:
            txn = session.get(Transaction, label.transaction_id)
            assert txn.settlement_id is not None
            settlement = session.get(Settlement, txn.settlement_id)
            fee = session.execute(
                select(Fee).where(Fee.transaction_id == txn.transaction_id)
            ).scalar_one()
            assert float(settlement.fee_amount) == float(fee.recorded_amount)
            assert float(settlement.net_amount) == pytest.approx(
                float(settlement.gross_amount) - float(settlement.fee_amount), abs=0.01
            )


def test_missing_settlement_has_no_settlement_row(seeded) -> None:
    with Session(seeded.engine) as session:
        label = session.execute(
            select(DatasetLabel).where(DatasetLabel.scenario == SCN_MISSING)
        ).scalar_one()
        txn = session.get(Transaction, label.transaction_id)
        assert txn.settlement_id is None
        count = session.execute(
            select(func.count()).select_from(Settlement).where(
                Settlement.transaction_id == txn.transaction_id
            )
        ).scalar_one()
        assert count == 0


def test_duplicate_pair_both_labelled(seeded) -> None:
    with Session(seeded.engine) as session:
        dupes = session.execute(
            select(DatasetLabel).where(DatasetLabel.scenario == SCN_DUPLICATE)
        ).scalars().all()
        assert len(dupes) == 2  # original + duplicate (2 rows per round)
        roles = {d.details.get("role") for d in dupes}
        assert roles == {"original", "duplicate"}
        txns = [session.get(Transaction, d.transaction_id) for d in dupes]
        assert len({t.customer_id for t in txns}) == 1
        assert len({t.merchant_id for t in txns}) == 1
        assert txns[0].amount == txns[1].amount


def test_fk_enforcement_rejects_orphan_child(seeded) -> None:
    """SQLite PRAGMA foreign_keys=ON must reject orphan child rows."""
    with pytest.raises(IntegrityError):
        with Session(seeded.engine) as session:
            session.add(Settlement(
                settlement_id="SET-9999",
                transaction_id="TXN-DOES-NOT-EXIST",
                merchant_id="M001",
                settlement_date=date(2026, 9, 1),
                gross_amount=Decimal("100.00"),
                fee_amount=Decimal("2.00"),
                refund_amount=Decimal("0.00"),
                net_amount=Decimal("98.00"),
                status="settled",
            ))
            session.commit()


def test_transaction_timestamp_is_naive_utc(seeded) -> None:
    with Session(seeded.engine) as session:
        txn = session.execute(select(Transaction).limit(1)).scalar_one()
        assert isinstance(txn.timestamp, datetime)
        assert txn.timestamp.tzinfo is None
        assert date(2026, 8, 6) <= txn.timestamp.date() <= END


def test_create_all_idempotent(seeded) -> None:
    # A second create_all must not fail and must not duplicate definitions.
    Base.metadata.create_all(seeded.engine)


def test_engine_factory_enables_sqlite_fk_pragma(seeded) -> None:
    with seeded.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


# ---------------------------------------------------------------------------
# Smoke: future-phase tables accept writes through the ORM
# ---------------------------------------------------------------------------


def test_agent_and_audit_tables_accept_writes(seeded) -> None:
    with Session(seeded.engine) as session:
        run = AgentRun(
            run_id="run-1", user_query="test", status="completed",
            tool_call_count=1, started_at=datetime(2026, 9, 3, 12, 0, 0),
        )
        session.add(run)
        session.flush()
        session.add(ToolCall(
            run_id="run-1", seq=1, tool_name="query_ledger",
            arguments={"q": 1}, result={"rows": []}, status="ok",
        ))
        session.add(AuditEvent(
            event_id="evt-1", actor="agent", action="tool.call",
            object_type="tool_call", object_id="1", agent_run_id="run-1",
        ))
        proposal = JournalProposal(
            proposal_id="prop-1", agent_run_id="run-1",
            debit_account="Suspense", credit_account="Bank",
            amount=Decimal("10.00"), narrative="test", evidence_ids=["TXN-1001"],
        )
        session.add(proposal)
        session.flush()
        session.add(Approval(
            proposal_id="prop-1", decision="approved", approver="demo-user",
            decided_at=datetime(2026, 9, 3, 12, 5, 0),
        ))
        session.add(ReconciliationException(
            transaction_id=seeded.dataset["transactions"][0]["transaction_id"],
            exception_date=date(2026, 9, 1), exception_type="FEE_MISMATCH",
            expected_amount=Decimal("1.00"), recorded_amount=Decimal("2.00"),
            financial_impact=Decimal("1.00"), description="test",
        ))
        session.add(AnomalyScore(
            transaction_id=seeded.dataset["transactions"][0]["transaction_id"],
            scored_at=datetime(2026, 9, 3, 12, 0, 0), anomaly_score=-0.2,
            is_anomaly=False, reasons={},
        ))
        session.commit()

        evt = session.execute(select(AuditEvent)).scalar_one()
        assert evt.after_state == {}


def test_cash_flows_one_row_per_merchant_day(seeded) -> None:
    with Session(seeded.engine) as session:
        pairs = session.execute(
            select(CashFlow.merchant_id, CashFlow.date, func.count())
            .group_by(CashFlow.merchant_id, CashFlow.date)
        ).all()
        assert all(count == 1 for _, _, count in pairs)
        assert len(pairs) == 5 * 28  # 5 merchants x 28 days


def test_cash_flow_running_balance_matches_json(seeded) -> None:
    ds = seeded.dataset
    with Session(seeded.engine) as session:
        for row in ds["cash_flows"][:5]:
            db_row = session.execute(
                select(CashFlow).where(
                    CashFlow.merchant_id == row["merchant_id"],
                    CashFlow.date == date.fromisoformat(row["date"]),
                )
            ).scalar_one()
            assert float(db_row.closing_balance) == pytest.approx(row["closing_balance"])
            assert float(db_row.net_amount) == pytest.approx(row["net_amount"])

