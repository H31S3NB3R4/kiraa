"""Deterministic reconciliation engine (Phase 3).

``run_reconciliation`` compares every in-scope transaction against the
processor's settlement, refund, ledger, and invoice records and classifies
each mismatch into the exception taxonomy the synthetic dataset labels
(see ``app.services.dataset_generator``):

    MISSING_SETTLEMENT          no settlement row for the transaction
    FEE_MISMATCH               processor fee != amount x merchant fee rate
    AMOUNT_MISMATCH            settlement gross != transaction amount
    SETTLEMENT_TIMING_MISMATCH settlement date != transaction date + T+2
    REFUND_MISMATCH            recorded refund != expected refund
    FAILED_LEDGER_WRITE        ledger write has status "failed"
    LEDGER_MISMATCH            posted ledger amount != settlement net
    GST_MISMATCH               recorded GST != GST implied by invoice total
    DUPLICATE_TRANSACTION      same merchant/customer/amount within 10 min

Rules are evaluated in the order above and the first hit wins, so one
transaction yields at most one exception. Duplicate pairs are each
labelled â€” mirroring the dataset's ground truth (both rows carry
``recon_exception: true``).

``exception_date`` is the day the financial position diverged (settlement
due date, refund processing date, ledger entry date, ...). This keeps the
"Why is Tuesday's cash short?" demo answerable with a single date filter.

``financial_impact`` is the signed exposure to the merchant:
``recorded - expected`` for value mismatches, the missing/delayed net for
settlement issues, and the charged amount for duplicates.

Detected exceptions are persisted to ``reconciliation_exceptions``
idempotently: re-running upserts by ``(transaction_id, exception_type)``
and never duplicates rows.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Invoice,
    LedgerEntry,
    Merchant,
    ReconciliationException,
    Refund,
    Settlement,
    Transaction,
)
from app.tools.common import MONEY_TOLERANCE, coerce_date, round2
from app.tools.gst import evaluate_invoice

# Business policies mirrored from the dataset generator.
SETTLEMENT_LAG_DAYS = 2        # settlements are expected at T+2
DUPLICATE_WINDOW_MINUTES = 10  # same-key charges within this window are duplicates

SEVERITY_BY_TYPE: dict[str, str] = {
    "MISSING_SETTLEMENT": "high",
    "DUPLICATE_TRANSACTION": "high",
    "FAILED_LEDGER_WRITE": "high",
    "FEE_MISMATCH": "medium",
    "REFUND_MISMATCH": "medium",
    "LEDGER_MISMATCH": "medium",
    "GST_MISMATCH": "medium",
    "AMOUNT_MISMATCH": "medium",
    "SETTLEMENT_TIMING_MISMATCH": "low",
}

__all__ = ["run_reconciliation"]


def _exception(
    txn: Transaction,
    exception_type: str,
    exception_date: date,
    expected: float,
    recorded: float,
    impact: float,
    description: str,
    sources: dict[str, Any],
) -> dict[str, Any]:
    """Build one structured exception record (result payload + row shape)."""
    return {
        "transaction_id": txn.transaction_id,
        "merchant_id": txn.merchant_id,
        "exception_type": exception_type,
        "exception_date": exception_date,
        "severity": SEVERITY_BY_TYPE[exception_type],
        "expected_amount": round2(expected),
        "recorded_amount": round2(recorded),
        "financial_impact": round2(impact),
        "description": description,
        "status": "open",
        "sources": sources,
    }


def _evaluate_transaction(
    txn: Transaction,
    fee_rate: float,
    settlement: Settlement | None,
    refunds: list[Refund],
    ledger_rows: list[LedgerEntry],
    invoice: Invoice | None,
) -> dict[str, Any] | None:
    """Apply every per-transaction rule; first hit wins."""
    amount = round2(txn.amount)
    expected_fee = round2(amount * fee_rate)
    expected_net = round2(amount - expected_fee)
    due_date = txn.timestamp.date() + timedelta(days=SETTLEMENT_LAG_DAYS)
    sources: dict[str, Any] = {
        "transaction_id": txn.transaction_id,
        "settlement_id": txn.settlement_id,
        "invoice_id": txn.invoice_id,
    }

    if settlement is None:
        return _exception(
            txn, "MISSING_SETTLEMENT", due_date, expected_net, 0.0, expected_net,
            f"No settlement received: net {txn.currency} {expected_net:,.2f} was due "
            f"{due_date.isoformat()} (T+{SETTLEMENT_LAG_DAYS}).",
            sources,
        )

    recorded_fee = round2(settlement.fee_amount)
    if abs(recorded_fee - expected_fee) > MONEY_TOLERANCE:
        impact = round2(recorded_fee - expected_fee)
        return _exception(
            txn, "FEE_MISMATCH", settlement.settlement_date,
            expected_fee, recorded_fee, impact,
            f"Fee overcharged: expected {txn.currency} {expected_fee:,.2f} "
            f"({fee_rate * 100:.2f}% of {txn.currency} {amount:,.2f}), "
            f"processor recorded {txn.currency} {recorded_fee:,.2f}.",
            sources,
        )

    recorded_gross = round2(settlement.gross_amount)
    if abs(recorded_gross - amount) > MONEY_TOLERANCE:
        impact = round2(recorded_gross - amount)
        return _exception(
            txn, "AMOUNT_MISMATCH", settlement.settlement_date,
            amount, recorded_gross, impact,
            f"Settlement gross {txn.currency} {recorded_gross:,.2f} does not match "
            f"transaction amount {txn.currency} {amount:,.2f}.",
            sources,
        )

    if settlement.settlement_date != due_date:
        net = round2(settlement.net_amount)
        delay = (settlement.settlement_date - due_date).days
        return _exception(
            txn, "SETTLEMENT_TIMING_MISMATCH", due_date, net, 0.0, net,
            f"Settlement arrived {delay} day(s) late: due {due_date.isoformat()}, "
            f"received {settlement.settlement_date.isoformat()}; "
            f"net {txn.currency} {net:,.2f} delayed.",
            sources,
        )

    for refund in refunds:
        expected_refund = round2(refund.expected_amount)
        recorded_refund = round2(refund.recorded_amount)
        if abs(recorded_refund - expected_refund) > MONEY_TOLERANCE:
            impact = round2(recorded_refund - expected_refund)
            return _exception(
                txn, "REFUND_MISMATCH", refund.processed_date,
                expected_refund, recorded_refund, impact,
                f"Refund overpaid: expected {txn.currency} {expected_refund:,.2f}, "
                f"processed {txn.currency} {recorded_refund:,.2f} on "
                f"{refund.processed_date.isoformat()}.",
                {**sources, "refund_id": refund.refund_id},
            )

    expected_ledger = round2(settlement.net_amount)
    if not ledger_rows:
        return _exception(
            txn, "LEDGER_MISMATCH", settlement.settlement_date,
            expected_ledger, 0.0, expected_ledger,
            f"No ledger entry posted for settlement {settlement.settlement_id} "
            f"(net {txn.currency} {expected_ledger:,.2f}).",
            sources,
        )
    for entry in ledger_rows:
        recorded_ledger = round2(entry.amount)
        entry_sources = {**sources, "ledger_entry_id": entry.entry_id}
        if entry.status == "failed":
            return _exception(
                txn, "FAILED_LEDGER_WRITE", entry.entry_date,
                expected_ledger, 0.0, expected_ledger,
                f"Ledger write failed for {entry.entry_id}: net "
                f"{txn.currency} {expected_ledger:,.2f} not posted ({entry.description}).",
                entry_sources,
            )
        if abs(recorded_ledger - expected_ledger) > MONEY_TOLERANCE:
            impact = round2(recorded_ledger - expected_ledger)
            return _exception(
                txn, "LEDGER_MISMATCH", entry.entry_date,
                expected_ledger, recorded_ledger, impact,
                f"Ledger posted {txn.currency} {recorded_ledger:,.2f} to "
                f"{entry.debit_account} but settlement net is "
                f"{txn.currency} {expected_ledger:,.2f} (fee not deducted).",
                entry_sources,
            )

    if invoice is not None:
        gst = evaluate_invoice(invoice)
        if gst["status"] == "mismatch":
            rate_pct = float(invoice.gst_rate) * 100
            return _exception(
                txn, "GST_MISMATCH", invoice.issue_date,
                gst["expected_tax"], gst["recorded_tax"], gst["difference"],
                f"GST mismatch on invoice {invoice.invoice_id}: expected "
                f"{txn.currency} {gst['expected_tax']:,.2f} at {rate_pct:.0f}%, "
                f"recorded {txn.currency} {gst['recorded_tax']:,.2f}.",
                {**sources, "invoice_id": invoice.invoice_id},
            )

    return None


def _detect_duplicates(
    txns: list[Transaction], already_flagged: set[str]
) -> list[dict[str, Any]]:
    """Flag same merchant/customer/amount charges within the duplicate window.

    Clusters are chains of consecutive records whose gap is at most
    ``DUPLICATE_WINDOW_MINUTES``; every member of a multi-record chain is
    flagged (the ground truth labels both rows of an injected pair).
    """
    groups: dict[tuple[str, str, float], list[Transaction]] = defaultdict(list)
    for txn in txns:
        if txn.transaction_id in already_flagged:
            continue
        groups[(txn.merchant_id, txn.customer_id, round2(txn.amount))].append(txn)

    window = timedelta(minutes=DUPLICATE_WINDOW_MINUTES)
    found: list[dict[str, Any]] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda t: (t.timestamp, t.transaction_id))
        chains: list[list[Transaction]] = []
        chain = [ordered[0]]
        for txn in ordered[1:]:
            if txn.timestamp - chain[-1].timestamp <= window:
                chain.append(txn)
            else:
                chains.append(chain)
                chain = [txn]
        chains.append(chain)
        for members in chains:
            if len(members) < 2:
                continue
            for member in members:
                partners = [
                    m.transaction_id
                    for m in members
                    if m.transaction_id != member.transaction_id
                ]
                amount = round2(member.amount)
                found.append(_exception(
                    member, "DUPLICATE_TRANSACTION", member.timestamp.date(),
                    0.0, amount, amount,
                    f"Duplicate charge: {member.currency} {amount:,.2f} to customer "
                    f"{member.customer_id} within {DUPLICATE_WINDOW_MINUTES} minutes "
                    f"of {', '.join(partners)}.",
                    {
                        "transaction_id": member.transaction_id,
                        "settlement_id": member.settlement_id,
                        "invoice_id": member.invoice_id,
                        "duplicate_of": partners,
                    },
                ))
    return found


def _load_support_rows(
    db: Session, txn_ids: list[str]
) -> tuple[
    dict[str, float],
    dict[str, Settlement],
    dict[str, list[Refund]],
    dict[str, list[LedgerEntry]],
    dict[str, Invoice],
]:
    """Load settlements/refunds/ledger/invoices for ``txn_ids`` in bulk."""
    if not txn_ids:
        return {}, {}, {}, {}, {}

    fee_rates = {
        m.merchant_id: float(m.fee_rate)
        for m in db.execute(select(Merchant)).scalars()
    }
    settlements = {
        s.transaction_id: s
        for s in db.execute(
            select(Settlement).where(Settlement.transaction_id.in_(txn_ids))
        ).scalars()
    }
    refunds_map: dict[str, list[Refund]] = defaultdict(list)
    for refund in db.execute(
        select(Refund).where(Refund.transaction_id.in_(txn_ids))
    ).scalars():
        refunds_map[refund.transaction_id].append(refund)
    for rows in refunds_map.values():
        rows.sort(key=lambda r: r.refund_id)
    ledger_map: dict[str, list[LedgerEntry]] = defaultdict(list)
    for entry in db.execute(
        select(LedgerEntry).where(LedgerEntry.transaction_id.in_(txn_ids))
    ).scalars():
        ledger_map[entry.transaction_id].append(entry)
    for rows in ledger_map.values():
        rows.sort(key=lambda e: e.entry_id)
    invoices = {
        i.transaction_id: i
        for i in db.execute(
            select(Invoice).where(Invoice.transaction_id.in_(txn_ids))
        ).scalars()
    }
    return fee_rates, settlements, refunds_map, ledger_map, invoices


def run_reconciliation(
    db: Session,
    merchant_id: str | None = None,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Reconcile transactions against settlements/ledger (PRD FR-2).

    Scans transactions in scope (optional merchant and transaction-date
    range filters), classifies every mismatch, returns structured
    exception-level evidence plus aggregate metrics, and upserts the
    findings into ``reconciliation_exceptions`` (idempotent by
    ``(transaction_id, exception_type)``).
    """
    start = coerce_date(start_date)
    end = coerce_date(end_date)
    if start is not None and end is not None and start > end:
        raise ValueError("start_date must not be after end_date")

    stmt = select(Transaction).order_by(
        Transaction.timestamp, Transaction.transaction_id
    )
    if merchant_id is not None:
        stmt = stmt.where(Transaction.merchant_id == merchant_id)
    txns = [
        txn
        for txn in db.execute(stmt).scalars()
        if (start is None or txn.timestamp.date() >= start)
        and (end is None or txn.timestamp.date() <= end)
    ]
    ids = [txn.transaction_id for txn in txns]
    fee_rates, settlements, refunds_map, ledger_map, invoices = _load_support_rows(db, ids)

    exceptions: list[dict[str, Any]] = []
    for txn in txns:
        exception = _evaluate_transaction(
            txn,
            fee_rates.get(txn.merchant_id, 0.0),
            settlements.get(txn.transaction_id),
            refunds_map.get(txn.transaction_id, []),
            ledger_map.get(txn.transaction_id, []),
            invoices.get(txn.transaction_id),
        )
        if exception is not None:
            exceptions.append(exception)

    exceptions.extend(
        _detect_duplicates(txns, {e["transaction_id"] for e in exceptions})
    )
    exceptions.sort(
        key=lambda e: (e["exception_date"], e["exception_type"], e["transaction_id"])
    )

    exception_txn_ids = {e["transaction_id"] for e in exceptions}
    matched = len(txns) - len(exception_txn_ids)
    metrics = {
        "transactions": len(txns),
        "matched": matched,
        "exception_transactions": len(exception_txn_ids),
        "exceptions": len(exceptions),
        "by_type": dict(sorted(Counter(e["exception_type"] for e in exceptions).items())),
        "total_financial_impact": round2(sum(e["financial_impact"] for e in exceptions)),
        "match_rate_pct": round2(100.0 * matched / len(txns)) if txns else 100.0,
    }

    persisted: dict[str, int] | None = None
    if persist and ids:
        existing = db.execute(
            select(ReconciliationException).where(
                ReconciliationException.transaction_id.in_(ids)
            )
        ).scalars().all()
        rows_by_key = {(r.transaction_id, r.exception_type): r for r in existing}
        new_count = updated_count = 0
        for exception in exceptions:
            key = (exception["transaction_id"], exception["exception_type"])
            row = rows_by_key.get(key)
            if row is None:
                db.add(ReconciliationException(
                    transaction_id=exception["transaction_id"],
                    exception_date=exception["exception_date"],
                    exception_type=exception["exception_type"],
                    severity=exception["severity"],
                    expected_amount=exception["expected_amount"],
                    recorded_amount=exception["recorded_amount"],
                    financial_impact=exception["financial_impact"],
                    description=exception["description"],
                    status=exception["status"],
                ))
                new_count += 1
            else:
                row.exception_date = exception["exception_date"]
                row.severity = exception["severity"]
                row.expected_amount = exception["expected_amount"]
                row.recorded_amount = exception["recorded_amount"]
                row.financial_impact = exception["financial_impact"]
                row.description = exception["description"]
                updated_count += 1
        db.commit()
        persisted = {"new": new_count, "updated": updated_count}

    return {
        "tool": "run_reconciliation",
        "filters": {
            "merchant_id": merchant_id,
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
        },
        "metrics": metrics,
        "exceptions": exceptions,
        "persisted": persisted,
    }

