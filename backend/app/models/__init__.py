"""SQLAlchemy ORM models (Phase 2).

Importing this package registers every model on the shared `Base.metadata`,
so callers only need ``import app.models`` (or ``from app.models import X``)
before calling ``Base.metadata.create_all(engine)``.

Domain layout:
- master_data: merchants, customers
- payments:     transactions, settlements, refunds, fees, invoices
- accounting:   ledger_entries, cash_flows
- operations:   dataset_labels, reconciliation_exceptions, anomaly_scores
- agent:        agent_runs, tool_calls
- audit:        journal_proposals, approvals, audit_events
"""

from __future__ import annotations

from app.models.accounting import CashFlow, LedgerEntry
from app.models.agent import AgentRun, ToolCall
from app.models.audit import Approval, AuditEvent, JournalProposal
from app.models.base import Base, TimestampMixin
from app.models.master_data import Customer, Merchant
from app.models.operations import (
    AnomalyScore,
    DatasetLabel,
    ReconciliationException,
)
from app.models.payments import Fee, Invoice, Refund, Settlement, Transaction

__all__ = [
    "Base",
    "TimestampMixin",
    # master data
    "Merchant",
    "Customer",
    # payments
    "Transaction",
    "Settlement",
    "Refund",
    "Fee",
    "Invoice",
    # accounting
    "LedgerEntry",
    "CashFlow",
    # operations
    "DatasetLabel",
    "ReconciliationException",
    "AnomalyScore",
    # agent
    "AgentRun",
    "ToolCall",
    # audit
    "JournalProposal",
    "Approval",
    "AuditEvent",
]

