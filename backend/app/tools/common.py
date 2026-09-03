"""Shared helpers for the deterministic finance tools (Phase 3).

Everything here is deliberately tiny and dependency-free so the tools stay
testable without the LLM or any service layer.
"""

from __future__ import annotations

from datetime import date, datetime

# Half a paise: two round-2 money values that differ by less than this are
# considered equal (float representation artefacts never reach this scale).
MONEY_TOLERANCE = 0.005


def round2(value: object) -> float:
    """Round a money value to 2 decimals (paise).

    Identical to the dataset generator's ``_round2`` so engine arithmetic
    and ground-truth arithmetic never drift apart.
    """
    return round(float(value), 2)


def coerce_date(value: date | str | datetime | None) -> date | None:
    """Coerce ``value`` (date, ISO string, datetime, or None) to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
