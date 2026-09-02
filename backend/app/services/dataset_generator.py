"""Deterministic synthetic finance dataset generator (Phase 1).

Produces a fully reproducible, seeded synthetic finance dataset for the AI
Finance Controller buildathon demo. All data is fake: it exists only to
exercise reconciliation, ledger queries, forecasting, GST matching, anomaly
detection, and journal proposals. No real financial records are used.

Design invariants
-----------------
- Deterministic: the same seed and parameters always produce identical data
  (no wall-clock values are embedded in the dataset).
- Every injected scenario is labelled with ground truth so the evaluation
  harness (Phase 12) can measure exception/anomaly precision and recall.
- "Normal" records satisfy every deterministic reconciliation check, so the
  reconciliation engine (Phase 3) can be validated against these labels.
- HIDDEN_ANOMALY is fully consistent (reconciliation PASS) but statistically
  unusual, so the ML anomaly layer (Phase 5) can find it.
- Demo anchoring: the missing settlement, the fee overcharge, and the
  oversized refund are placed so the most recent Tuesday's settled cash is
  visibly short - powering the "Why is Tuesday's cash short?" demo.

The Phase 2 database seed script loads these JSON/CSV files; the file layout
mirrors the planned SQLAlchemy schema one-to-one.
"""

from __future__ import annotations

import csv
import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Fixed generation parameters
# ---------------------------------------------------------------------------

GENERATOR_VERSION = 1
CURRENCY = "INR"
GST_RATE = 0.18                      # flat GST rate used across the dataset
SETTLEMENT_LAG_DAYS = 2              # expected settlement is T+2
TIMING_MISMATCH_EXTRA_DAYS = 3       # timing mismatch settles T+5 instead of T+2
DUPLICATE_WINDOW_MINUTES = 10       # transactions closer than this are duplicates
FEE_OVERCHARGE_RATE = 0.005          # FEE_MISMATCH charges an extra 0.5% of amount
REFUND_OVERPAY_RATE = 0.15           # REFUND_MISMATCH refunds 115% of expected
GST_ERROR_RATE = 0.08                # GST_MISMATCH records 108% of expected GST
ANOMALY_AMOUNT_FACTOR = 7            # HIDDEN_ANOMALY amount = 7x merchant median
ANOMALY_HOUR = 3                     # HIDDEN_ANOMALY occurs at 03:xx UTC
NORMAL_REFUND_PROBABILITY = 0.07     # share of normal transactions that get refunded

DEFAULT_END_DATE = date(2026, 9, 3)  # fixed anchor keeps default runs reproducible
DEFAULT_SEED = 42
DEFAULT_TRANSACTIONS = 100
DEFAULT_WINDOW_DAYS = 28
DEFAULT_CUSTOMERS = 80

# Scenario taxonomy (single source of truth for labels and evaluation).
SCN_NORMAL = "NORMAL"
SCN_FEE_MISMATCH = "FEE_MISMATCH"
SCN_REFUND_MISMATCH = "REFUND_MISMATCH"
SCN_DUPLICATE = "DUPLICATE_TRANSACTION"
SCN_TIMING = "SETTLEMENT_TIMING_MISMATCH"
SCN_MISSING = "MISSING_SETTLEMENT"
SCN_LEDGER = "LEDGER_MISMATCH"
SCN_GST = "GST_MISMATCH"
SCN_ANOMALY = "HIDDEN_ANOMALY"
SCN_FAILED_WRITE = "FAILED_LEDGER_WRITE"

INJECTED_SCENARIOS: tuple[str, ...] = (
    SCN_FEE_MISMATCH, SCN_REFUND_MISMATCH, SCN_DUPLICATE, SCN_TIMING,
    SCN_MISSING, SCN_LEDGER, SCN_GST, SCN_ANOMALY, SCN_FAILED_WRITE,
)
# Scenarios a correct reconciliation engine must flag as exceptions.
RECON_EXCEPTION_SCENARIOS = frozenset({
    SCN_FEE_MISMATCH, SCN_REFUND_MISMATCH, SCN_DUPLICATE, SCN_TIMING,
    SCN_MISSING, SCN_LEDGER, SCN_GST, SCN_FAILED_WRITE,
})
# Scenarios that are ML-anomaly ground truths (reconciliation passes them).
ANOMALY_SCENARIOS = frozenset({SCN_ANOMALY})

# ID sequences: TXN-1xxx, LE-3xxx, SET-5xxx, RFD-7xxx, INV-88xx, FEE-9xxx
_TXN_START, _LE_START = 1000, 3000
_SET_START, _RFD_START = 5000, 7000
_INV_START, _FEE_START = 8800, 9000

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MerchantSpec:
    merchant_id: str
    name: str
    category: str
    fee_rate: float         # processor fee schedule (fraction of amount)
    median_amount: float    # typical ticket size (lognormal median)
    amount_sigma: float     # lognormal spread of amounts
    opening_balance: float  # opening cash position for the flow window


MERCHANT_SPECS: tuple[MerchantSpec, ...] = (
    MerchantSpec("M001", "Kirana Direct", "grocery-ecommerce", 0.020, 1500.0, 0.35, 250_000.00),
    MerchantSpec("M002", "StreamFlix India", "digital-subscriptions", 0.018, 800.0, 0.25, 500_000.00),
    MerchantSpec("M003", "TiffinExpress", "food-delivery", 0.022, 450.0, 0.30, 120_000.00),
    MerchantSpec("M004", "TravelNest", "travel-booking", 0.019, 5000.0, 0.45, 750_000.00),
    MerchantSpec("M005", "GadgetHub", "consumer-electronics", 0.025, 3500.0, 0.40, 400_000.00),
)

FIRST_NAMES = (
    "Aarav", "Ananya", "Arjun", "Bhavana", "Chetan", "Deepa", "Devika",
    "Esha", "Farhan", "Gaurav", "Hema", "Ishaan", "Jaya", "Kabir",
    "Kiran", "Lata", "Manav", "Meera", "Nikhil", "Nisha", "Omkar",
    "Priya", "Ravi", "Sneha",
)
LAST_NAMES = (
    "Agarwal", "Bansal", "Chatterjee", "Desai", "Dutta", "Gokhale",
    "Gupta", "Iyer", "Jain", "Joshi", "Kapoor", "Khanna", "Kulkarni",
    "Mehta", "Mishra", "Nair", "Nathan", "Patel", "Rao", "Reddy",
    "Shah", "Sharma", "Singh", "Verma",
)
CITIES = (
    "Mumbai", "Pune", "Bengaluru", "Chennai", "Hyderabad", "New Delhi",
    "Gurugram", "Noida", "Kolkata", "Ahmedabad", "Jaipur", "Kochi",
    "Indore", "Lucknow",
)
CHANNELS = ("upi", "card", "netbanking", "wallet")
CHANNEL_WEIGHTS = (55, 30, 10, 5)

# ---------------------------------------------------------------------------
# Small helpers (all randomness flows through a single seeded rng)
# ---------------------------------------------------------------------------


def _round2(value: float) -> float:
    """Round a money value to 2 decimals (paise)."""
    return round(float(value), 2)


def _iso_ts(dt: datetime) -> str:
    """Format a naive datetime as a UTC ISO-8601 timestamp."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_date(value: date | str | None) -> date:
    if value is None:
        return DEFAULT_END_DATE
    if isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(value, datetime):
        return value.date()
    return value


def most_recent_tuesday(end: date) -> date:
    """Return the most recent Tuesday strictly before ``end``.

    The demo scenario "Why is Tuesday's cash short?" anchors on this date.
    """
    day = end
    while day.weekday() != 1:  # Monday == 0, Tuesday == 1
        day -= timedelta(days=1)
    if day == end:
        day -= timedelta(days=7)
    return day


def _pick(rng: random.Random, options: tuple) -> Any:
    return options[rng.randrange(len(options))]


def _pick_channel(rng: random.Random) -> str:
    return rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS)[0]


def _rand_day(rng: random.Random, start: date, latest: date) -> date:
    return start + timedelta(days=rng.randint(0, (latest - start).days))


def _rand_hour(rng: random.Random) -> int:
    if rng.random() < 0.88:  # business hours dominate
        return rng.randint(9, 21)
    return rng.choice((6, 7, 8, 22, 23))


def _rand_ts(rng: random.Random, day: date, hour: int | None = None) -> datetime:
    return datetime(
        day.year, day.month, day.day,
        _rand_hour(rng) if hour is None else hour,
        rng.randint(0, 59), rng.randint(0, 59),
    )


def _rand_amount(rng: random.Random, spec: MerchantSpec) -> float:
    return max(20.0, _round2(rng.lognormvariate(math.log(spec.median_amount), spec.amount_sigma)))


class _IdSeq:
    """Deterministic sequential id generator (TXN-1001, TXN-1002, ...)."""

    def __init__(self, prefix: str, start: int) -> None:
        self._prefix = prefix
        self._n = start

    def next(self) -> str:
        self._n += 1
        return f"{self._prefix}{self._n}"


@dataclass
class _TxnPlan:
    """Internal plan for one transaction before ids are assigned."""

    style: str
    ts: datetime
    merchant: MerchantSpec
    customer_id: str
    amount: float
    channel: str
    extra: dict[str, Any] = field(default_factory=dict)
    txn_id: str | None = None


def _build_customers(
    rng: random.Random, count: int, start: date
) -> tuple[list[dict[str, Any]], list[str], list[int]]:
    """Create customers plus sampling weights (some customers shop more)."""
    rows: list[dict[str, Any]] = []
    ids: list[str] = []
    weights: list[int] = []
    for i in range(1, count + 1):
        customer_id = f"C{i:03d}"
        signup = start - timedelta(days=rng.randint(30, 400))
        rows.append({
            "customer_id": customer_id,
            "name": f"{_pick(rng, FIRST_NAMES)} {_pick(rng, LAST_NAMES)}",
            "city": _pick(rng, CITIES),
            "signup_date": signup.isoformat(),
        })
        ids.append(customer_id)
        weights.append(rng.choices((1, 2, 3), weights=(70, 20, 10))[0])
    return rows, ids, weights


def _sorted_by(rows: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: tuple(row[key] for key in keys))

# ---------------------------------------------------------------------------
# Scenario planning
# ---------------------------------------------------------------------------


def _plan_transactions(
    rng: random.Random,
    total: int,
    start: date,
    end: date,
    demo_tuesday: date,
    customer_ids: list[str],
    customer_weights: list[int],
    exceptions_per_type: int,
) -> list[_TxnPlan]:
    """Plan every transaction (normal + injected) with its scenario style.

    rng calls happen in a fixed sequence, so a given seed always yields the
    same plans. Plans are sorted by timestamp afterwards, and ids are then
    assigned in chronological order.
    """
    # Normal records must be able to settle (T+2) within the window.
    latest_normal_day = end - timedelta(days=SETTLEMENT_LAG_DAYS)
    # Scenario records keep a margin so late settlements and refunds stay
    # inside the window (timing mismatch settles T+5, refunds process T+4).
    latest_scenario_day = end - timedelta(days=SETTLEMENT_LAG_DAYS + TIMING_MISMATCH_EXTRA_DAYS + 1)
    anchor_txn_day = demo_tuesday - timedelta(days=SETTLEMENT_LAG_DAYS)
    refund_anchor_day = demo_tuesday - timedelta(days=4)

    def merchant() -> MerchantSpec:
        return _pick(rng, MERCHANT_SPECS)

    def customer() -> str:
        return rng.choices(customer_ids, weights=customer_weights)[0]

    plans: list[_TxnPlan] = []
    for i in range(exceptions_per_type):
        # FEE_MISMATCH: the first instance settles on the demo Tuesday.
        day = anchor_txn_day if (i == 0 and anchor_txn_day >= start) else _rand_day(rng, start, latest_scenario_day)
        m = merchant()
        plans.append(_TxnPlan(SCN_FEE_MISMATCH, _rand_ts(rng, day), m, customer(), _rand_amount(rng, m), _pick_channel(rng)))

        # REFUND_MISMATCH: the first instance's outflow lands on the demo Tuesday.
        anchored = i == 0 and refund_anchor_day >= start
        day = refund_anchor_day if anchored else _rand_day(rng, start, latest_scenario_day)
        m = merchant()
        plans.append(_TxnPlan(SCN_REFUND_MISMATCH, _rand_ts(rng, day), m, customer(), _rand_amount(rng, m), _pick_channel(rng), extra={"anchored": anchored}))

        # DUPLICATE_TRANSACTION: same merchant/customer/amount minutes apart.
        day = _rand_day(rng, start, latest_scenario_day)
        m = merchant()
        cust = customer()
        amount = _rand_amount(rng, m)
        base_ts = _rand_ts(rng, day)
        gap = timedelta(minutes=rng.randint(2, DUPLICATE_WINDOW_MINUTES - 2))
        first = _TxnPlan(SCN_DUPLICATE, base_ts, m, cust, amount, _pick_channel(rng), extra={"role": "original"})
        second = _TxnPlan(SCN_DUPLICATE, base_ts + gap, m, cust, amount, _pick_channel(rng), extra={"role": "duplicate"})
        first.extra["partner"] = second
        second.extra["partner"] = first
        plans.extend((first, second))

        # SETTLEMENT_TIMING_MISMATCH.
        day = _rand_day(rng, start, latest_scenario_day)
        m = merchant()
        plans.append(_TxnPlan(SCN_TIMING, _rand_ts(rng, day), m, customer(), _rand_amount(rng, m), _pick_channel(rng)))

        # MISSING_SETTLEMENT: the first instance was due to settle on the demo Tuesday.
        day = anchor_txn_day if (i == 0 and anchor_txn_day >= start) else _rand_day(rng, start, latest_scenario_day)
        m = merchant()
        plans.append(_TxnPlan(SCN_MISSING, _rand_ts(rng, day), m, customer(), _rand_amount(rng, m), _pick_channel(rng)))

        # LEDGER_MISMATCH.
        day = _rand_day(rng, start, latest_scenario_day)
        m = merchant()
        plans.append(_TxnPlan(SCN_LEDGER, _rand_ts(rng, day), m, customer(), _rand_amount(rng, m), _pick_channel(rng)))

        # GST_MISMATCH.
        day = _rand_day(rng, start, latest_scenario_day)
        m = merchant()
        plans.append(_TxnPlan(SCN_GST, _rand_ts(rng, day), m, customer(), _rand_amount(rng, m), _pick_channel(rng)))

        # HIDDEN_ANOMALY: every deterministic check passes, but the behaviour is unusual.
        day = _rand_day(rng, start, latest_normal_day)
        m = merchant()
        plans.append(_TxnPlan(SCN_ANOMALY, _rand_ts(rng, day, hour=ANOMALY_HOUR), m, customer(), _round2(m.median_amount * ANOMALY_AMOUNT_FACTOR), _pick_channel(rng)))

        # FAILED_LEDGER_WRITE.
        day = _rand_day(rng, start, latest_scenario_day)
        m = merchant()
        plans.append(_TxnPlan(SCN_FAILED_WRITE, _rand_ts(rng, day), m, customer(), _rand_amount(rng, m), _pick_channel(rng)))

    # Fill the remainder with normal, fully consistent records.
    while len(plans) < total:
        day = _rand_day(rng, start, latest_normal_day)
        m = merchant()
        plans.append(_TxnPlan(SCN_NORMAL, _rand_ts(rng, day), m, customer(), _rand_amount(rng, m), _pick_channel(rng)))
    return plans

# ---------------------------------------------------------------------------
# Record emission
# ---------------------------------------------------------------------------


@dataclass
class _BuildContext:
    rng: random.Random
    end: date
    demo_tuesday: date
    ids: dict[str, _IdSeq]
    rows: dict[str, list[dict[str, Any]]]


def _add_refund(
    ctx: _BuildContext, txn_id: str, spec: MerchantSpec,
    initiated: date, processed: date, expected: float, recorded: float,
) -> dict[str, Any]:
    row = {
        "refund_id": ctx.ids["refund"].next(),
        "transaction_id": txn_id,
        "merchant_id": spec.merchant_id,
        "initiated_date": initiated.isoformat(),
        "processed_date": processed.isoformat(),
        "expected_amount": expected,
        "recorded_amount": recorded,
        "status": "processed",
    }
    ctx.rows["refunds"].append(row)
    return row


def _add_ledger(
    ctx: _BuildContext, txn_id: str, spec: MerchantSpec, entry_date: date,
    debit: str, credit: str, amount: float, status: str, description: str,
) -> dict[str, Any]:
    row = {
        "entry_id": ctx.ids["ledger"].next(),
        "transaction_id": txn_id,
        "merchant_id": spec.merchant_id,
        "entry_date": entry_date.isoformat(),
        "debit_account": debit,
        "credit_account": credit,
        "amount": amount,
        "status": status,
        "description": description,
    }
    ctx.rows["ledger_entries"].append(row)
    return row


def _emit(plan: _TxnPlan, ctx: _BuildContext) -> None:
    """Build every downstream record for one planned transaction."""
    spec = plan.merchant
    style = plan.style
    ts = plan.ts
    txn_id = plan.txn_id
    amount = plan.amount

    fee_expected = _round2(amount * spec.fee_rate)
    expected_settle_date = ts.date() + timedelta(days=SETTLEMENT_LAG_DAYS)

    # --- settlement side (the processor's version of the truth) -----------
    fee_recorded = fee_expected
    settle_date = expected_settle_date
    if style == SCN_FEE_MISMATCH:
        fee_recorded = _round2(fee_expected + amount * FEE_OVERCHARGE_RATE)
    elif style == SCN_TIMING:
        settle_date = ts.date() + timedelta(days=SETTLEMENT_LAG_DAYS + TIMING_MISMATCH_EXTRA_DAYS)

    net_amount = _round2(amount - fee_recorded)
    settlement_id = None
    if style != SCN_MISSING:
        settlement_id = ctx.ids["settlement"].next()
        ctx.rows["settlements"].append({
            "settlement_id": settlement_id,
            "transaction_id": txn_id,
            "merchant_id": spec.merchant_id,
            "settlement_date": settle_date.isoformat(),
            "gross_amount": amount,
            "fee_amount": fee_recorded,
            "refund_amount": 0.0,  # refunds are post-settlement outflows
            "net_amount": net_amount,
            "status": "settled",
        })

    # --- fee record --------------------------------------------------------
    ctx.rows["fees"].append({
        "fee_id": ctx.ids["fee"].next(),
        "transaction_id": txn_id,
        "merchant_id": spec.merchant_id,
        "fee_date": (settle_date if settlement_id else expected_settle_date).isoformat(),
        "expected_amount": fee_expected,
        "recorded_amount": fee_recorded,
    })

    # --- invoice / GST -----------------------------------------------------
    # Decompose so that taxable + gst == amount exactly for correct invoices
    # (rounding gst first keeps the internal consistency check paise-exact).
    gst_expected = _round2(amount * GST_RATE / (1.0 + GST_RATE))
    taxable_value = _round2(amount - gst_expected)
    gst_recorded = gst_expected
    if style == SCN_GST:
        gst_recorded = _round2(gst_expected * (1.0 + GST_ERROR_RATE))
    invoice_id = ctx.ids["invoice"].next()
    ctx.rows["invoices"].append({
        "invoice_id": invoice_id,
        "transaction_id": txn_id,
        "merchant_id": spec.merchant_id,
        "issue_date": ts.date().isoformat(),
        "taxable_value": taxable_value,
        "gst_rate": GST_RATE,
        "gst_amount": gst_recorded,
        "total_amount": amount,
    })

    # --- refunds ------------------------------------------------------------
    refund_attached = False
    if style == SCN_NORMAL:
        latest_initiation = ctx.end - timedelta(days=2)
        days_available = (latest_initiation - ts.date()).days
        if days_available >= 3 and ctx.rng.random() < NORMAL_REFUND_PROBABILITY:
            initiated = ts.date() + timedelta(days=min(ctx.rng.randint(3, 10), days_available))
            _add_refund(ctx, txn_id, spec, initiated, initiated + timedelta(days=1), amount, amount)
            refund_attached = True
    elif style == SCN_REFUND_MISMATCH:
        if plan.extra.get("anchored"):
            initiated = ctx.demo_tuesday - timedelta(days=1)
        else:
            initiated = ts.date() + timedelta(days=3)
        recorded = _round2(amount * (1.0 + REFUND_OVERPAY_RATE))
        _add_refund(ctx, txn_id, spec, initiated, initiated + timedelta(days=1), amount, recorded)
        refund_attached = True

    # --- ledger -------------------------------------------------------------
    if style == SCN_MISSING:
        _add_ledger(ctx, txn_id, spec, expected_settle_date,
                    "Payment Receivable", "Sales Revenue",
                    _round2(amount - fee_expected), "posted",
                    "receivable booked; settlement not received")
    elif style == SCN_FAILED_WRITE:
        _add_ledger(ctx, txn_id, spec, settle_date,
                    "Bank - Settlement Account", "Sales Revenue",
                    net_amount, "failed", "ledger write failed; retry pending")
    elif style == SCN_LEDGER:
        _add_ledger(ctx, txn_id, spec, settle_date,
                    "Bank - Settlement Account", "Sales Revenue",
                    amount, "posted", "posted gross; fee not deducted")
    else:
        _add_ledger(ctx, txn_id, spec, settle_date,
                    "Bank - Settlement Account", "Sales Revenue",
                    net_amount, "posted", "settlement posted")

    # --- transaction (the merchant's system of record) -----------------------
    ctx.rows["transactions"].append({
        "transaction_id": txn_id,
        "merchant_id": spec.merchant_id,
        "customer_id": plan.customer_id,
        "timestamp": _iso_ts(ts),
        "currency": CURRENCY,
        "amount": amount,
        "fee": fee_expected,
        "refund_amount": amount if refund_attached else 0.0,
        "channel": plan.channel,
        "status": "refunded" if refund_attached else "captured",
        "settlement_id": settlement_id,
        "invoice_id": invoice_id,
    })

    # --- ground-truth label --------------------------------------------------
    details: dict[str, Any] = {}
    if style == SCN_DUPLICATE:
        details = {"role": plan.extra.get("role"), "partner": plan.extra["partner"].txn_id}
    ctx.rows["labels"].append({
        "transaction_id": txn_id,
        "scenario": style,
        "recon_exception": style in RECON_EXCEPTION_SCENARIOS,
        "anomaly": style in ANOMALY_SCENARIOS,
        "details": details,
    })


# ---------------------------------------------------------------------------
# Cash-flow aggregation
# ---------------------------------------------------------------------------


def _build_cash_flows(
    merchant_rows: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
    start: date,
    window_days: int,
) -> list[dict[str, Any]]:
    """Aggregate daily cash flows per merchant from settlements and refunds."""
    inflow: dict[tuple[str, str], float] = defaultdict(float)
    outflow: dict[tuple[str, str], float] = defaultdict(float)
    for s in settlements:
        inflow[(s["merchant_id"], s["settlement_date"])] += s["net_amount"]
    for r in refunds:
        outflow[(r["merchant_id"], r["processed_date"])] += r["recorded_amount"]

    rows: list[dict[str, Any]] = []
    for merchant_row in merchant_rows:
        merchant_id = merchant_row["merchant_id"]
        running = merchant_row["opening_balance"]
        for offset in range(window_days):
            day = (start + timedelta(days=offset)).isoformat()
            day_in = _round2(inflow.get((merchant_id, day), 0.0))
            day_out = _round2(outflow.get((merchant_id, day), 0.0))
            net = _round2(day_in - day_out)
            running = _round2(running + net)
            rows.append({
                "merchant_id": merchant_id,
                "date": day,
                "inflow": day_in,
                "outflow": day_out,
                "net_amount": net,
                "closing_balance": running,
            })
        merchant_row["current_balance"] = running
    return rows

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_dataset(
    *,
    transactions: int = DEFAULT_TRANSACTIONS,
    seed: int = DEFAULT_SEED,
    end_date: date | str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    exceptions_per_type: int = 1,
    customers: int = DEFAULT_CUSTOMERS,
) -> dict[str, Any]:
    """Generate the complete synthetic dataset (deterministic per seed)."""
    minimum = exceptions_per_type * (len(INJECTED_SCENARIOS) + 1) + 10
    if exceptions_per_type < 0:
        raise ValueError("exceptions_per_type must be >= 0")
    if transactions < minimum:
        raise ValueError(
            f"transactions must be >= {minimum} for exceptions_per_type={exceptions_per_type}"
        )
    if window_days < 8:
        raise ValueError("window_days must be >= 8")
    if customers < 20:
        raise ValueError("customers must be >= 20")

    rng = random.Random(seed)
    end = _coerce_date(end_date)
    start = end - timedelta(days=window_days - 1)
    demo_tuesday = most_recent_tuesday(end)

    merchant_rows = [
        {
            "merchant_id": spec.merchant_id,
            "name": spec.name,
            "category": spec.category,
            "fee_rate": spec.fee_rate,
            "currency": CURRENCY,
            "opening_balance": spec.opening_balance,
        }
        for spec in MERCHANT_SPECS
    ]
    customer_rows, customer_ids, customer_weights = _build_customers(rng, customers, start)

    plans = _plan_transactions(
        rng, transactions, start, end, demo_tuesday,
        customer_ids, customer_weights, exceptions_per_type,
    )
    plans.sort(key=lambda p: p.ts)

    ctx = _BuildContext(
        rng=rng, end=end, demo_tuesday=demo_tuesday,
        ids={
            "txn": _IdSeq("TXN-", _TXN_START),
            "settlement": _IdSeq("SET-", _SET_START),
            "refund": _IdSeq("RFD-", _RFD_START),
            "fee": _IdSeq("FEE-", _FEE_START),
            "invoice": _IdSeq("INV-", _INV_START),
            "ledger": _IdSeq("LE-", _LE_START),
        },
        rows={
            "transactions": [], "settlements": [], "refunds": [], "fees": [],
            "invoices": [], "ledger_entries": [], "labels": [],
        },
    )

    for plan in plans:
        plan.txn_id = ctx.ids["txn"].next()
    for plan in plans:
        _emit(plan, ctx)

    cash_flows = _build_cash_flows(
        merchant_rows, ctx.rows["settlements"], ctx.rows["refunds"], start, window_days
    )

    return {
        "metadata": {
            "generator": "app.services.dataset_generator",
            "generator_version": GENERATOR_VERSION,
            "seed": seed,
            "transactions": transactions,
            "window_days": window_days,
            "exceptions_per_type": exceptions_per_type,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "demo_tuesday": demo_tuesday.isoformat(),
            "currency": CURRENCY,
            "synthetic": True,
            "note": "Synthetic buildathon data. Not real financial records.",
        },
        "merchants": merchant_rows,
        "customers": customer_rows,
        "transactions": ctx.rows["transactions"],
        "settlements": _sorted_by(ctx.rows["settlements"], "settlement_date", "settlement_id"),
        "refunds": _sorted_by(ctx.rows["refunds"], "processed_date", "refund_id"),
        "fees": _sorted_by(ctx.rows["fees"], "fee_date", "fee_id"),
        "invoices": _sorted_by(ctx.rows["invoices"], "issue_date", "invoice_id"),
        "ledger_entries": _sorted_by(ctx.rows["ledger_entries"], "entry_date", "entry_id"),
        "cash_flows": cash_flows,
        "labels": sorted(ctx.rows["labels"], key=lambda label: label["transaction_id"]),
    }


def summarize(dataset: dict[str, Any]) -> str:
    """Human-readable summary for CLI output and demo scripts."""
    meta = dataset["metadata"]
    labels = dataset["labels"]
    total = len(dataset["transactions"])
    exceptions = sum(1 for label in labels if label["recon_exception"])
    anomalies = sum(1 for label in labels if label["anomaly"])
    scenario_counts = Counter(label["scenario"] for label in labels)
    cash = _round2(sum(m["current_balance"] for m in dataset["merchants"]))
    match_rate = _round2(100.0 * (total - exceptions) / total) if total else 0.0
    lines = [
        "Synthetic dataset summary (all data is fake)",
        f"  seed={meta['seed']}  window={meta['start_date']}..{meta['end_date']}"
        f"  demo_tuesday={meta['demo_tuesday']}",
        f"  merchants={len(dataset['merchants'])}  customers={len(dataset['customers'])}"
        f"  transactions={total}",
        f"  settlements={len(dataset['settlements'])}  refunds={len(dataset['refunds'])}"
        f"  fees={len(dataset['fees'])}",
        f"  invoices={len(dataset['invoices'])}"
        f"  ledger_entries={len(dataset['ledger_entries'])}"
        f"  cash_flow_rows={len(dataset['cash_flows'])}",
        "  scenario counts:",
    ]
    for scenario in (SCN_NORMAL, *INJECTED_SCENARIOS):
        lines.append(f"    {scenario:<32} {scenario_counts.get(scenario, 0)}")
    lines.append(
        f"  expected recon pass rate: {match_rate}%"
        f"  (exceptions: {exceptions}, hidden anomalies: {anomalies})"
    )
    lines.append(f"  total cash position at end date: {CURRENCY} {cash:,.2f}")
    return "\n".join(lines)


def write_dataset(
    dataset: dict[str, Any], out_dir: str | Path, name: str
) -> tuple[Path, Path]:
    """Write the dataset JSON and the ground-truth labels CSV.

    Returns (json_path, labels_path).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{name}.json"
    labels_path = out / f"{name}_labels.csv"

    with json_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(dataset, fh, indent=2)

    with labels_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["transaction_id", "scenario", "recon_exception", "anomaly", "details"])
        for label in sorted(dataset["labels"], key=lambda row: row["transaction_id"]):
            writer.writerow([
                label["transaction_id"],
                label["scenario"],
                str(label["recon_exception"]).lower(),
                str(label["anomaly"]).lower(),
                json.dumps(label["details"], sort_keys=True),
            ])
    return json_path, labels_path



