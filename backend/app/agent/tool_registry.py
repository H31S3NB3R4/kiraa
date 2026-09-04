"""Agent tool registry (Phase 6, architecture section 4).

One explicit registry — no if/else chains in the controller loop. Each
entry carries the PRD section-10 tool contract (name, description,
JSON-schema parameters), the permission class (architecture section 5),
and the deterministic callable. ``dispatch_tool`` is the single entry
point the controller uses:

- unknown tools / malformed arguments / tool-raised ``ValueError`` /
  unexpected exceptions each return a *structured* error envelope
  (architecture section 16, PRD section 14) so the model is told the tool
  failed and never invents a result,
- successful ``run_reconciliation`` results are enriched with the persisted
  ``exception_id``s (the reconciliation payload only carries
  ``transaction_id``/``exception_type``) so follow-up calls can chain into
  ``propose_journal_entry(exception_id=...)``,
- proposal payloads always state ``posted=False`` / ``requires_approval=True``
  (PROPOSE class; never posts).

Parameter JSON schemas follow the PRD contracts; the schema builder uses
plain dicts so the declarations stay provider-agnostic (the Gemini adapter
converts them to ``types.Schema``).
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.agent.providers.base import ToolDeclaration
from app.models import ReconciliationException
from app.tools import (
    check_gst_match,
    detect_anomalies,
    forecast_cashflow,
    query_ledger,
    run_reconciliation,
)
from app.tools.journal import propose_journal_entry

__all__ = [
    "TOOL_REGISTRY",
    "TOOL_DECLARATIONS",
    "TOOL_PERMISSIONS",
    "dispatch_tool",
    "enrich_reconciliation_result",
]

# Permission classes (architecture section 5).
READ = "READ"
PROPOSE = "PROPOSE"


def _str(
    description: str | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if description:
        schema["description"] = description
    return schema


def _opt_str(description: str | None = None) -> dict[str, Any]:
    return _str(description) | {"nullable": True}


def _int(description: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer"}
    if description:
        schema["description"] = description
    return schema


def _str_array(description: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "array",
        "items": {"type": "string"},
    }
    if description:
        schema["description"] = description
    return schema


def _object(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema



TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "run_reconciliation": {
        "permission": READ,
        "description": (
            "Reconcile transactions against settlement and ledger records "
            "for a merchant and date range. Returns aggregate metrics and "
            "record-level exceptions with exception_id, severity, financial "
            "impact, and evidence."
        ),
        "parameters": _object(
            {
                "merchant_id": _opt_str("Optional merchant scope, e.g. 'M001'"),
                "start_date": _opt_str("Optional ISO date, e.g. '2026-08-30'"),
                "end_date": _opt_str("Optional ISO date, e.g. '2026-09-02'"),
            }
        ),
        "callable": run_reconciliation,
    },
    "query_ledger": {
        "permission": READ,
        "description": (
            "Read-only query over ledger entries and related financial "
            "records with source links back to transactions and settlements."
        ),
        "parameters": _object(
            {
                "merchant_id": _opt_str("Optional merchant scope"),
                "transaction_id": _opt_str("Optional transaction id, e.g. 'TXN-1042'"),
                "start_date": _opt_str("Optional ISO date"),
                "end_date": _opt_str("Optional ISO date"),
                "status": _opt_str("Optional ledger status, e.g. 'posted'/'failed'"),
                "account": _opt_str("Optional debit/credit account name"),
                "category": _opt_str("Optional merchant category"),
                "limit": _int("Optional row cap (default 500; pass null for no cap)"),
            }
        ),
        "callable": query_ledger,
    },
    "forecast_cashflow": {
        "permission": READ,
        "description": (
            "Produce a deterministic cash-flow forecast from historical "
            "data with LOW/MEDIUM/HIGH risk classification and the "
            "drivers behind it. Pools all merchants when merchant_id is null."
        ),
        "parameters": _object(
            {
                "merchant_id": _opt_str("Optional merchant scope; null pools all"),
                "horizon_days": _int("Forecast horizon in days (1-30, default 7)"),
                "history_days": _int("History window in days (default 28)"),
                "operating_threshold": {
                    "type": "number",
                    "description": "Optional minimum operating cash (INR)",
                    "nullable": True,
                },
            }
        ),
        "callable": forecast_cashflow,
    },
    "check_gst_match": {
        "permission": READ,
        "description": (
            "Compare expected GST on an invoice with recorded tax data "
            "for one transaction."
        ),
        "parameters": _object(
            {"transaction_id": _str("Transaction id, e.g. 'TXN-1042'")},
            required=["transaction_id"],
        ),
        "callable": check_gst_match,
    },
    "detect_anomalies": {
        "permission": READ,
        "description": (
            "Score transactions for statistical unusualness using the "
            "trained anomaly model. Runs alongside deterministic "
            "reconciliation — every row cross-links the reconciliation verdict."
        ),
        "parameters": _object(
            {
                "merchant_id": _opt_str("Optional merchant scope"),
                "transaction_ids": _str_array("Optional explicit transaction ids") | {"nullable": True},
                "limit": _int("Optional cap on returned scores (default 500)"),
            }
        ),
        "callable": detect_anomalies,
    },
    "propose_journal_entry": {
        "permission": PROPOSE,
        "description": (
            "Create a reviewable journal-entry proposal based on verified "
            "financial evidence. Does not post the entry; posting requires "
            "explicit human approval."
        ),
        "parameters": _object(
            {
                "exception_id": _int("Numeric id of a persisted reconciliation exception"),
                "reason": _str("Short analyst-facing justification for the correction"),
            },
            required=["exception_id", "reason"],
        ),
        "callable": propose_journal_entry,
    },
}

TOOL_DECLARATIONS: list[ToolDeclaration] = [
    {
        "name": name,
        "description": str(spec["description"]),
        "parameters": dict(spec["parameters"]),  # type: ignore[arg-type]
    }
    for name, spec in TOOL_REGISTRY.items()
]

TOOL_PERMISSIONS: dict[str, str] = {
    name: str(spec["permission"]) for name, spec in TOOL_REGISTRY.items()
}


_ERROR_ENVELOPE_KEYS = ("tool", "status", "error_type", "message", "details")


def _error_envelope(
    tool_name: str,
    error_type: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structured failure payload (architecture section 16)."""
    envelope: dict[str, Any] = {
        "tool": tool_name,
        "status": "error",
        "error_type": error_type,
        "message": message,
    }
    if details:
        envelope["details"] = details
    return envelope


def enrich_reconciliation_result(result: dict[str, Any], db: Session) -> dict[str, Any]:
    """Attach persisted ``exception_id``s to a fresh reconciliation result.

    The engine payload identifies exceptions by
    ``(transaction_id, exception_type)``; ``propose_journal_entry`` needs
    the persisted row id. The mapping is exact (both tables share the
    ``exception_type`` taxonomy) so each exception gains ``exception_id``.

    Shared by the agent dispatch path and the Phase 9 reconciliation API
    route so both surfaces return the same enriched payload.
    """
    pairs: list[tuple[str, str]] = [
        (exc["transaction_id"], exc["exception_type"])
        for exc in result.get("exceptions", [])
    ]
    if not pairs:
        return result
    rows = db.execute(
        select(ReconciliationException).where(
            tuple_(ReconciliationException.transaction_id, ReconciliationException.exception_type).in_(pairs)
        )
    ).scalars().all()
    id_by_pair = {(row.transaction_id, row.exception_type): row.id for row in rows}
    for exception in result["exceptions"]:
        exception["exception_id"] = id_by_pair.get(
            (exception["transaction_id"], exception["exception_type"])
        )
    return result


def dispatch_tool(
    db: Session,
    name: str,
    arguments: dict[str, Any] | None,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute one tool call and always return a JSON-serializable payload."""
    started = time.perf_counter()

    def _fail(envelope: dict[str, Any]) -> dict[str, Any]:
        """Stamp wall-clock latency on an error envelope before returning."""
        envelope.setdefault(
            "latency_ms", round((time.perf_counter() - started) * 1000.0, 2)
        )
        return envelope

    if name not in TOOL_REGISTRY:
        return _fail(
            _error_envelope(name, "UNKNOWN_TOOL", f"no tool named {name!r} is registered")
        )
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _fail(
            _error_envelope(name, "INVALID_ARGUMENTS", "tool arguments must be a JSON object")
        )

    spec = TOOL_REGISTRY[name]
    tool = spec["callable"]
    known = set(spec["parameters"].get("properties", {}))
    unknown = [key for key in arguments if key not in known]
    if unknown:
        return _fail(
            _error_envelope(
                name,
                "INVALID_ARGUMENTS",
                f"unknown argument(s) {unknown} for tool {name}",
            )
        )
    missing = [
        key
        for key in spec["parameters"].get("required", [])
        if arguments.get(key) is None
    ]
    if missing:
        return _fail(
            _error_envelope(
                name,
                "INVALID_ARGUMENTS",
                f"missing required argument(s) {missing} for tool {name}",
                {"arguments": arguments},
            )
        )
    # Provide the run id so PROPOSE tools can link their rows to the run.
    kwargs = dict(arguments)
    if spec["permission"] == PROPOSE:
        kwargs.setdefault("run_id", run_id)

    try:
        result = tool(db, **kwargs)
    except ValueError as exc:
        # A failed tool must not leave half-applied state behind: undo any
        # pending flush so the shared session stays usable for the trace.
        db.rollback()
        return _fail(
            _error_envelope(name, "VALIDATION_ERROR", str(exc), {"arguments": kwargs})
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, never raised
        db.rollback()
        return _fail(
            _error_envelope(
                name, "TOOL_FAILURE", f"{type(exc).__name__}: {exc}", {"arguments": kwargs}
            )
        )

    if name == "run_reconciliation":
        result = enrich_reconciliation_result(result, db)
    result.setdefault("latency_ms", round((time.perf_counter() - started) * 1000.0, 2))
    return result