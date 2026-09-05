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
- every registry entry carries a timeout and a bounded retry policy
  (architecture section 4): the callable runs on an isolated worker
  session under a wall-clock bound, a timed-out attempt is retried at
  most ``max_retries`` times (architecture section 16, transient tool
  failure), and exhaustion returns a ``TOOL_TIMEOUT`` envelope instead of
  hanging the agent loop. The controller's own session is never handed to
  the tool, so a slow or stuck call can never leave it mid-statement,
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

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from app.agent.providers.base import ToolDeclaration
from app.config import get_settings
from app.models import ReconciliationException
from app.tools import (
    check_gst_match,
    detect_anomalies,
    forecast_cashflow,
    query_ledger,
    run_reconciliation,
)
from app.tools.journal import propose_journal_entry

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_REGISTRY",
    "TOOL_DECLARATIONS",
    "TOOL_PERMISSIONS",
    "TOOL_TIMEOUTS",
    "TOOL_RETRY_POLICY",
    "dispatch_tool",
    "enrich_reconciliation_result",
]

# Permission classes (architecture section 5).
READ = "READ"
PROPOSE = "PROPOSE"

# Default wall-clock bound for one tool attempt and the bounded number of
# retries a timed-out attempt gets (architecture sections 4 and 16). The
# per-entry ``timeout_seconds``/``max_retries`` keys in ``TOOL_REGISTRY``
# carry each tool's own policy; the constants below are the fallback for
# entries that do not pin one.
DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
DEFAULT_TOOL_RETRIES = 1

# One shared bounded pool for tool attempts: the controller loop is
# single-threaded, so at most one dispatch runs at a time, but a *timed
# out* attempt keeps occupying its worker until the stuck callable
# finishes — the extra workers keep later dispatches from queueing behind
# a stuck attempt (their own timeouts still apply from submit time).
_DISPATCH_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-dispatch")


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
        "timeout_seconds": 10.0,
        "max_retries": 1,
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
        "timeout_seconds": 10.0,
        "max_retries": 1,
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
        "timeout_seconds": 10.0,
        "max_retries": 1,
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
        "timeout_seconds": 5.0,
        "max_retries": 1,
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
        "timeout_seconds": 20.0,
        "max_retries": 1,
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
        "timeout_seconds": 5.0,
        "max_retries": 0,
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

# Architecture section 4: every tool entry carries a timeout and a retry
# policy. These projections keep the dispatch path (and the Phase 12/13
# reliability surface) free of dict spelunking.
TOOL_TIMEOUTS: dict[str, float] = {
    name: float(spec["timeout_seconds"]) for name, spec in TOOL_REGISTRY.items()
}

TOOL_RETRY_POLICY: dict[str, int] = {
    name: int(spec["max_retries"]) for name, spec in TOOL_REGISTRY.items()
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


def _worker_session(db: Session) -> Session:
    """Build an isolated session on the caller's engine for one tool attempt.

    The controller's own session stays untouched by tool code: a slow or
    stuck call can never leave it mid-statement, and a timed-out attempt's
    connection is simply closed with the worker session (SQLite commits
    from another connection are fine — the shared engine pools them).
    """
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)
    return factory()


def _run_one_attempt(
    tool: Any,
    db: Session,
    kwargs: dict[str, Any],
) -> tuple[dict[str, Any] | None, Exception | None]:
    """Run the tool once on an isolated worker session; never raise.

    Returns ``(result, None)`` on success, ``(None, exception)`` on failure
    (the session is rolled back and closed either way).
    """
    worker = _worker_session(db)
    try:
        return tool(worker, **kwargs), None
    except Exception as exc:  # noqa: BLE001 - classified by the caller
        worker.rollback()
        return None, exc
    finally:
        worker.close()


def dispatch_tool(
    db: Session,
    name: str,
    arguments: dict[str, Any] | None,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute one tool call and always return a JSON-serializable payload.

    Phase 13 reliability contract (architecture sections 4/16): the tool
    callable runs on an isolated worker session bounded by the entry's
    ``timeout_seconds``; a timed-out attempt is retried at most
    ``max_retries`` times and then surfaces a ``TOOL_TIMEOUT`` envelope
    instead of hanging the agent loop. Every other failure path returns a
    structured error envelope so the model is told a tool failed and
    never invents a result.
    """
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

    timeout_seconds = float(spec.get("timeout_seconds", DEFAULT_TOOL_TIMEOUT_SECONDS))
    # The settings value is a global *ceiling*: it can tighten every
    # dispatch (one env var) but never loosen a registry entry's own
    # budget (architecture section 4 keeps per-tool policy explicit).
    timeout_seconds = min(timeout_seconds, get_settings().tool_timeout_seconds)
    max_retries = int(spec.get("max_retries", DEFAULT_TOOL_RETRIES))
    attempts = max(1, 1 + max_retries)

    result: dict[str, Any] | None = None
    error: Exception | None = None
    for attempt in range(1, attempts + 1):
        future = _DISPATCH_POOL.submit(_run_one_attempt, tool, db, kwargs)
        try:
            result, error = future.result(timeout=timeout_seconds)
        except FutureTimeout:
            # Architecture section 16, transient tool failure: a timed-out
            # attempt gets a bounded retry; the final timeout surfaces a
            # structured envelope (never a hang, never a silent truncation).
            future.cancel()
            if attempt < attempts:
                logger.warning(
                    "tool %s attempt %d/%d timed out after %.1fs; retrying",
                    name, attempt, attempts, timeout_seconds,
                )
                continue
            logger.warning(
                "tool %s timed out after %d attempt(s) of %.1fs: returning "
                "TOOL_TIMEOUT envelope",
                name, attempts, timeout_seconds,
            )
            return _fail(
                _error_envelope(
                    name,
                    "TOOL_TIMEOUT",
                    (
                        f"tool {name} exceeded its {timeout_seconds:g}s time "
                        f"budget after {attempts} attempt(s); it was stopped and "
                        "no result was produced"
                    ),
                    {"timeout_seconds": timeout_seconds, "attempts": attempts},
                )
            )
        # A non-timeout failure is not retried: the registry tools are
        # deterministic, so a second attempt would fail the same way.
        break

    if error is not None:
        # A failed tool must not leave half-applied state behind: undo any
        # pending flush so the shared session stays usable for the trace.
        db.rollback()
        if isinstance(error, ValueError):
            return _fail(
                _error_envelope(name, "VALIDATION_ERROR", str(error), {"arguments": kwargs})
            )
        return _fail(
            _error_envelope(
                name, "TOOL_FAILURE", f"{type(error).__name__}: {error}", {"arguments": kwargs}
            )
        )

    assert result is not None
    if name == "run_reconciliation":
        result = enrich_reconciliation_result(result, db)
    result.setdefault("latency_ms", round((time.perf_counter() - started) * 1000.0, 2))
    return result