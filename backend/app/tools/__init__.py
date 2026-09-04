"""Deterministic finance tools.

Phase 3 ships the reconciliation engine, the read-only ledger query, and
the GST match check. Phase 4 adds the deterministic cash-flow forecast.
Phase 5 adds ML anomaly detection. Tools decide the *financial facts* and
must remain testable independently of the LLM; the Phase 6 agent layer
wraps them with declarations derived from the PRD tool contracts.

- ``run_reconciliation``: 9-way exception classification with financial
  impact, aggregate metrics, and idempotent persistence
- ``query_ledger``:       read-only, source-linked ledger queries
- ``check_gst_match``:    expected vs recorded GST for one transaction
- ``forecast_cashflow``:  deterministic horizon forecast with rolling
  averages, LOW/MEDIUM/HIGH risk classification, and drivers
- ``detect_anomalies``:   Isolation-Forest scoring with reasons and
  ground-truth metrics, cross-linked with reconciliation verdicts
- ``propose_journal_entry``: reviewable correction proposals drafted from
  verified exceptions (never posts; human approval gates the ledger)

The agent layer (Phase 6) wraps these with declarations derived from the
PRD tool contracts; ``propose_journal_entry`` is the only tool that
creates state, and only pending proposal rows.
"""

from __future__ import annotations

from app.tools.anomalies import detect_anomalies
from app.tools.forecast import forecast_cashflow
from app.tools.gst import check_gst_match, evaluate_invoice
from app.tools.journal import propose_journal_entry
from app.tools.ledger import query_ledger
from app.tools.reconciliation import run_reconciliation

__all__ = [
    "check_gst_match",
    "detect_anomalies",
    "evaluate_invoice",
    "forecast_cashflow",
    "propose_journal_entry",
    "query_ledger",
    "run_reconciliation",
]

