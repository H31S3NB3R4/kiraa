"""GST matching tool (Phase 3).

``check_gst_match`` compares the GST recorded on an invoice with the tax
implied by the invoice's own tax-inclusive total and GST rate:

    expected_tax = round2(total * rate / (1 + rate))

The formula mirrors the dataset generator's invoice decomposition exactly
(same operand order), so a correctly recorded invoice matches to the paise
and a tampered one is flagged with the exact difference. The tool is
read-only and satisfies PRD FR-5 (taxable value, expected tax, recorded
tax, difference, matching status, source references).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Invoice, Transaction
from app.tools.common import MONEY_TOLERANCE, round2


def evaluate_invoice(
    invoice: Invoice, *, tolerance: float = MONEY_TOLERANCE
) -> dict[str, Any]:
    """Compare one invoice's recorded GST against the expected component.

    Pure function over the ORM row — reused by the reconciliation engine so
    both tools always apply the identical GST rule.
    """
    rate = float(invoice.gst_rate)
    total = round2(invoice.total_amount)
    expected_tax = round2(total * rate / (1.0 + rate))
    recorded_tax = round2(invoice.gst_amount)
    difference = round2(recorded_tax - expected_tax)
    return {
        "invoice_id": invoice.invoice_id,
        "issue_date": invoice.issue_date.isoformat(),
        "gst_rate": rate,
        "total_amount": total,
        "taxable_value": round2(invoice.taxable_value),
        "expected_taxable_value": round2(total - expected_tax),
        "expected_tax": expected_tax,
        "recorded_tax": recorded_tax,
        "difference": difference,
        "tolerance": tolerance,
        "status": "matched" if abs(difference) <= tolerance else "mismatch",
    }


def check_gst_match(
    db: Session, transaction_id: str, *, tolerance: float = MONEY_TOLERANCE
) -> dict[str, Any]:
    """Compare expected vs recorded GST for one transaction (PRD FR-5)."""
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        return {
            "tool": "check_gst_match",
            "transaction_id": transaction_id,
            "status": "not_found",
            "sources": {"transaction_id": transaction_id},
        }

    invoice = db.execute(
        select(Invoice).where(Invoice.transaction_id == transaction_id)
    ).scalar_one_or_none()
    if invoice is None:
        return {
            "tool": "check_gst_match",
            "transaction_id": transaction_id,
            "merchant_id": txn.merchant_id,
            "status": "no_invoice",
            "sources": {
                "transaction_id": transaction_id,
                "settlement_id": txn.settlement_id,
            },
        }

    return {
        "tool": "check_gst_match",
        "transaction_id": transaction_id,
        "merchant_id": txn.merchant_id,
        **evaluate_invoice(invoice, tolerance=tolerance),
        "sources": {
            "transaction_id": transaction_id,
            "invoice_id": invoice.invoice_id,
            "settlement_id": txn.settlement_id,
        },
    }
