"""Phase 2 database seeding service.

Loads a Phase 1 dataset JSON (``data/generated/dataset.json`` by default)
into the database behind the given engine. Row layouts map one-to-one onto
the Phase 2 ORM models, so seeding is a straight per-table insert in
FK-safe order:

    merchants -> customers -> transactions -> settlements -> refunds ->
    fees -> invoices -> ledger_entries -> cash_flows -> dataset_labels

The service is importable (and unit-tested) separately from the
``backend/scripts/seed_db.py`` CLI wrapper.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.session import create_engine_for
from app.models import (
    CashFlow,
    Customer,
    DatasetLabel,
    Fee,
    Invoice,
    LedgerEntry,
    Merchant,
    Refund,
    Settlement,
    Transaction,
)
from app.models.base import Base

# dataset section -> ORM model (insertion order respects foreign keys).
_TABLES: tuple[tuple[str, type], ...] = (
    ("merchants", Merchant),
    ("customers", Customer),
    ("transactions", Transaction),
    ("settlements", Settlement),
    ("refunds", Refund),
    ("fees", Fee),
    ("invoices", Invoice),
    ("ledger_entries", LedgerEntry),
    ("cash_flows", CashFlow),
    ("labels", DatasetLabel),
)

_REQUIRED_SECTIONS = tuple(name for name, _ in _TABLES)

# Date/timestamp fields that arrive as ISO strings in the dataset and must
# be coerced to date/datetime objects for the ORM.
_DATE_FIELDS: dict[str, set[str]] = {
    "customers": {"signup_date"},
    "settlements": {"settlement_date"},
    "refunds": {"initiated_date", "processed_date"},
    "fees": {"fee_date"},
    "invoices": {"issue_date"},
    "ledger_entries": {"entry_date"},
    "cash_flows": {"date"},
}
_TIMESTAMP_FIELDS: dict[str, set[str]] = {
    "transactions": {"timestamp"},
}


class SeedError(RuntimeError):
    """Raised when seeding cannot proceed (bad dataset or non-empty DB)."""


def build_engine(url: str) -> Engine:
    """Create an engine for ``url`` (delegates to the shared DB session)."""
    return create_engine_for(url)


def load_dataset_file(path: str | Path) -> dict[str, Any]:
    """Load and structurally validate a dataset JSON produced by Phase 1."""
    file_path = Path(path)
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            dataset = json.load(handle)
    except FileNotFoundError:
        raise SeedError(f"dataset file not found: {file_path}") from None
    except json.JSONDecodeError as exc:
        raise SeedError(f"dataset file is not valid JSON: {file_path} ({exc})") from exc

    if not isinstance(dataset, dict):
        raise SeedError(f"dataset root must be a JSON object: {file_path}")
    missing = [name for name in _REQUIRED_SECTIONS if name not in dataset]
    if missing:
        raise SeedError(f"dataset missing sections {missing}: {file_path}")
    if not dataset["transactions"]:
        raise SeedError(f"dataset contains no transactions: {file_path}")
    return dataset


def _coerce_row(section: str, row: dict[str, Any]) -> dict[str, Any]:
    """Convert ISO date strings / timestamps into ORM-ready values."""
    values = dict(row)
    for field in _DATE_FIELDS.get(section, ()):
        values[field] = _to_date(values[field])
    for field in _TIMESTAMP_FIELDS.get(section, ()):
        values[field] = _to_datetime(values[field])
    return values


def _to_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _to_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    # Phase 1 writes naive UTC stamps like "2026-08-12T14:03:11Z".
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=None)


def seed_database(
    engine: Engine, dataset: dict[str, Any], *, recreate: bool = False
) -> dict[str, int]:
    """Create the schema and load ``dataset`` into ``engine``.

    Returns a mapping of table section -> inserted row count. Raises
    ``SeedError`` when the database already holds data and ``recreate`` is
    not set (prevents accidental double-seeding).
    """
    if recreate:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        with session.begin():
            existing = session.execute(
                select(func.count()).select_from(Merchant)
            ).scalar_one()
            if existing and not recreate:
                raise SeedError(
                    "database already contains data; pass --recreate to reset it"
                )
            counts: dict[str, int] = {}
            for section, model in _TABLES:
                rows = dataset[section]
                session.add_all(
                    model(**_coerce_row(section, row)) for row in rows
                )
                # Flush per section: the unit of work orders inserts by
                # *relationship* dependencies only, so plain-FK tables (e.g.
                # cash_flows -> merchants) could otherwise flush first.
                # Intermediate flushes stay inside this one transaction.
                session.flush()
                counts[section] = len(rows)
    return counts


def summarize_counts(counts: dict[str, int]) -> str:
    """Human-readable one-line-per-table summary for CLI output."""
    width = max(len(name) for name in counts)
    return "\n".join(
        f"  {name:<{width}}  {count:>6} rows" for name, count in counts.items()
    )
