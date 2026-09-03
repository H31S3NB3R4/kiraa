"""Phase 4 tests: deterministic cash-flow forecasting.

Mirrors the Phase 3 fixture pattern: generate the same small dataset
(default seed, 100 transactions, 28-day window), seed a temp SQLite DB,
and verify ``forecast_cashflow`` against expectations re-derived
independently from the dataset's own ``cash_flows`` rows — the flat
recent-rolling-average model, the running ``round2`` balance walk, the
rolling averages/trend drivers, the volatility-based confidence label,
and the LOW/MEDIUM/HIGH threshold classification must all reproduce
exactly. The tool must stay read-only and deterministic.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import fmean, pstdev

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas.forecast import ForecastResponse
from app.config import get_settings
from app.models import Base, CashFlow, Merchant
from app.services.dataset_generator import generate_dataset, write_dataset
from app.services.db_seed import build_engine, load_dataset_file, seed_database
from app.tools import forecast_cashflow
from app.tools.common import round2

KWS = {"transactions": 100, "window_days": 28, "exceptions_per_type": 1}
END = date(2026, 9, 3)  # same anchor day as the Phase 3 suite
MERCHANT_IDS = ("M001", "M002", "M003", "M004", "M005")


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Generate a small dataset, write it to JSON, then seed a temp DB."""
    out_dir = tmp_path_factory.mktemp("phase4")
    dataset = generate_dataset(**KWS, end_date=END)
    json_path, _labels_path = write_dataset(dataset, out_dir, "dataset")

    bundle = type("SeededDb", (), {})()
    bundle.dataset = load_dataset_file(json_path)
    bundle.engine = build_engine(f"sqlite:///{out_dir / 'finance.db'}")
    bundle.counts = seed_database(bundle.engine, bundle.dataset)
    return bundle


# ---------------------------------------------------------------------------
# Independent re-derivation helpers (dataset -> expected forecast numbers)
# ---------------------------------------------------------------------------


def _history(dataset: dict, merchant_id: str | None = None, history_days: int = 28):
    """Re-derive the daily history the tool must consume from the dataset.

    Pools per-day inflow/outflow/net exactly like the tool does (round2 per
    day) and sums closing balances on the anchor date.
    """
    rows = [
        row
        for row in dataset["cash_flows"]
        if merchant_id is None or row["merchant_id"] == merchant_id
    ]
    anchor = max(date.fromisoformat(row["date"]) for row in rows)
    window_start = anchor - timedelta(days=history_days - 1)
    window = [
        row for row in rows if window_start <= date.fromisoformat(row["date"]) <= anchor
    ]

    pooled: dict[date, dict[str, float]] = {}
    anchor_balance = 0.0
    for row in window:
        day = date.fromisoformat(row["date"])
        slot = pooled.setdefault(day, {"inflow": 0.0, "outflow": 0.0, "net": 0.0})
        slot["inflow"] += row["inflow"]
        slot["outflow"] += row["outflow"]
        slot["net"] += row["net_amount"]
        if day == anchor:
            anchor_balance += row["closing_balance"]

    observed = [
        {"date": day, **{key: round2(value) for key, value in slot.items()}}
        for day, slot in sorted(pooled.items())
    ]
    return observed, anchor, round2(anchor_balance)


def _flat_projection(
    observed: list[dict], anchor_balance: float, horizon_days: int = 7
) -> tuple[float, float, float, list[dict]]:
    """Apply the tool's model independently: recent-7 averages rolled flat."""
    recent = observed[-7:]
    projected_inflow = round2(fmean([day["inflow"] for day in recent]))
    projected_outflow = round2(fmean([day["outflow"] for day in recent]))
    projected_net = round2(projected_inflow - projected_outflow)
    walk: list[dict] = []
    balance = anchor_balance
    for offset in range(1, horizon_days + 1):
        balance = round2(balance + projected_net)
        walk.append({"day_offset": offset, "projected_cash": balance})
    return projected_inflow, projected_outflow, projected_net, walk


def _run(seeded, **kwargs) -> dict:
    """Call the tool against the seeded DB (one short-lived session)."""
    with Session(seeded.engine) as session:
        return forecast_cashflow(session, **kwargs)


@pytest.fixture(scope="module")
def empty_engine(tmp_path_factory):
    """Schema-only DB: every table exists but no rows (guard-case testing)."""
    out_dir = tmp_path_factory.mktemp("phase4_empty")
    engine = build_engine(f"sqlite:///{out_dir / 'empty.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture(scope="module")
def merchant_names(seeded) -> dict[str, str]:
    return {m["merchant_id"]: m["name"] for m in seeded.dataset["merchants"]}


# ---------------------------------------------------------------------------
# End-to-end equivalence with the independently re-derived model
# ---------------------------------------------------------------------------


def test_pooled_forecast_matches_independent_recomputation(seeded) -> None:
    observed, anchor, anchor_balance = _history(seeded.dataset)
    exp_in, exp_out, exp_net, walk = _flat_projection(observed, anchor_balance)
    prior = observed[-14:-7]
    exp_trend = round2(
        exp_net
        - round2(
            fmean([d["inflow"] for d in prior]) - fmean([d["outflow"] for d in prior])
        )
    )

    result = _run(seeded, horizon_days=7, history_days=28, operating_threshold=50_000.0)

    assert result["tool"] == "forecast_cashflow"
    assert result["status"] == "ok"
    assert result["scope"] == "all_merchants"
    assert result["merchant_id"] is None
    assert result["merchant_name"] is None
    assert result["model"] == "recent-rolling-average"
    assert result["horizon_days"] == 7
    assert result["history_days"] == 28
    assert result["history_observed_days"] == len(observed)
    assert result["history_start"] == observed[0]["date"].isoformat()
    assert result["history_end"] == anchor.isoformat()
    assert result["anchor_date"] == anchor.isoformat()
    assert result["anchor_balance"] == anchor_balance
    assert result["daily_avg_inflow"] == round2(fmean([d["inflow"] for d in observed]))
    assert result["daily_avg_outflow"] == round2(
        fmean([d["outflow"] for d in observed])
    )
    assert result["recent_window_days"] == 7
    assert result["recent_avg_inflow"] == exp_in
    assert result["recent_avg_outflow"] == exp_out
    assert result["recent_avg_net"] == exp_net
    assert result["net_trend_per_day"] == exp_trend
    assert result["projected_inflow_per_day"] == exp_in
    assert result["projected_outflow_per_day"] == exp_out
    assert result["projected_net_per_day"] == exp_net
    assert [p["projected_cash"] for p in result["forecast"]] == [
        w["projected_cash"] for w in walk
    ]
    assert result["projected_ending_balance"] == walk[-1]["projected_cash"]


@pytest.mark.parametrize("merchant_id", MERCHANT_IDS)
def test_per_merchant_forecast_matches_recomputation(
    seeded, merchant_names, merchant_id
) -> None:
    observed, anchor, anchor_balance = _history(seeded.dataset, merchant_id)
    exp_in, exp_out, exp_net, walk = _flat_projection(observed, anchor_balance)

    result = _run(seeded, merchant_id=merchant_id, operating_threshold=50_000.0)

    assert result["status"] == "ok"
    assert result["scope"] == "merchant"
    assert result["merchant_name"] == merchant_names[merchant_id]
    assert result["anchor_date"] == anchor.isoformat()
    assert result["anchor_balance"] == anchor_balance
    assert result["history_observed_days"] == len(observed)
    assert result["projected_net_per_day"] == exp_net
    assert [p["projected_cash"] for p in result["forecast"]] == [
        w["projected_cash"] for w in walk
    ]


def test_pooled_anchor_is_sum_of_merchant_anchor_balances(seeded) -> None:
    balances = {}
    for merchant_id in MERCHANT_IDS:
        _observed, _anchor, anchor_balance = _history(seeded.dataset, merchant_id)
        balances[merchant_id] = anchor_balance
    pooled = _run(seeded, operating_threshold=0)
    assert pooled["anchor_balance"] == round2(sum(balances.values()))
    for merchant_id, balance in balances.items():
        single = _run(seeded, merchant_id=merchant_id, operating_threshold=0)
        assert single["anchor_balance"] == balance
        assert single["anchor_balance"] != pooled["anchor_balance"]


# ---------------------------------------------------------------------------
# Series shape, windows, and custom horizons
# ---------------------------------------------------------------------------


def test_seven_day_series_is_consecutive_dates(seeded) -> None:
    result = _run(seeded)
    assert len(result["forecast"]) == 7
    assert [p["day_offset"] for p in result["forecast"]] == list(range(1, 8))
    anchor = date.fromisoformat(result["anchor_date"])
    dates = [date.fromisoformat(p["date"]) for p in result["forecast"]]
    assert dates == [anchor + timedelta(days=offset) for offset in range(1, 8)]
    for point in result["forecast"]:
        assert point["projected_net"] == (
            round2(point["projected_inflow"] - point["projected_outflow"])
        )


def test_short_history_window(seeded) -> None:
    # With only the recent week loaded, the recent window *is* the history:
    # no prior week exists, so the trend driver must be null.
    result = _run(seeded, history_days=7, operating_threshold=50_000.0)
    assert result["history_days"] == 7
    assert result["history_observed_days"] == 7
    assert result["recent_window_days"] == 7
    assert result["net_trend_per_day"] is None
    observed, _anchor, anchor_balance = _history(seeded.dataset, history_days=7)
    _in, _out, exp_net, _walk = _flat_projection(observed, anchor_balance)
    assert result["projected_net_per_day"] == exp_net


def test_custom_horizons(seeded) -> None:
    one = _run(seeded, horizon_days=1, operating_threshold=0)
    assert len(one["forecast"]) == 1
    assert one["projected_ending_balance"] == one["min_projected_cash"]

    thirty = _run(seeded, horizon_days=30, operating_threshold=0)
    observed, _anchor, anchor_balance = _history(seeded.dataset)
    _in, _out, _net, walk = _flat_projection(observed, anchor_balance, horizon_days=30)
    assert len(thirty["forecast"]) == 30
    assert [p["projected_cash"] for p in thirty["forecast"]] == [
        w["projected_cash"] for w in walk
    ]


# ---------------------------------------------------------------------------
# Risk classification, drivers, and confidence
# ---------------------------------------------------------------------------


def test_threshold_boundaries_and_breach_metadata(seeded) -> None:
    base = _run(seeded, operating_threshold=50_000.0)
    min_cash = base["min_projected_cash"]
    assert base["risk"] == "LOW"
    assert base["breach_days"] == 0
    assert base["first_breach_date"] is None
    assert base["headroom"] == round2(min_cash - 50_000.0)
    assert base["headroom_pct"] == round2((min_cash - 50_000.0) / 50_000.0 * 100.0)

    # MEDIUM band: a threshold strictly between min/1.25 and min keeps every
    # projection above water but with less than 25% headroom at the minimum.
    medium_threshold = round2((min_cash / 1.25 + min_cash) / 2)
    medium = _run(seeded, operating_threshold=medium_threshold)
    assert medium["risk"] == "MEDIUM"
    assert medium["breach_days"] == 0
    assert medium["first_breach_date"] is None
    assert medium["headroom"] == round2(min_cash - medium_threshold)

    # A threshold just above the minimum tips it to HIGH; breach metadata
    # must match the projected series itself.
    high = _run(seeded, operating_threshold=round2(min_cash + 100.0))
    assert high["risk"] == "HIGH"
    breach_dates = [
        p["date"] for p in high["forecast"] if p["projected_cash"] < high["operating_threshold"]
    ]
    assert high["breach_days"] == len(breach_dates)
    assert high["first_breach_date"] == breach_dates[0]
    assert high["headroom"] == -100.0

    # At the exact 25% buffer edge the classification follows the rule
    # ``min < round2(1.25 x threshold)``, re-derived here.
    edge = _run(seeded, operating_threshold=round2(min_cash / 1.25))
    buffer_limit = round2(1.25 * edge["operating_threshold"])
    assert edge["risk"] == ("MEDIUM" if min_cash < buffer_limit else "LOW")


def test_risk_reasons_mention_threshold_and_numbers(seeded) -> None:
    low = _run(seeded, operating_threshold=50_000.0)
    assert "headroom" in low["risk_reason"]
    assert "50000.0" in low["risk_reason"]

    high = _run(seeded, operating_threshold=10_000_000.0)
    assert "operating threshold" in high["risk_reason"]
    assert high["risk"] == "HIGH"

    min_cash = low["min_projected_cash"]
    medium = _run(
        seeded, operating_threshold=round2((min_cash / 1.25 + min_cash) / 2)
    )
    assert "25%" in medium["risk_reason"]
    assert medium["risk"] == "MEDIUM"


def test_confidence_labels_and_volatility(seeded) -> None:
    observed, _anchor, _balance = _history(seeded.dataset)
    nets = [day["net"] for day in observed]
    mean_net = fmean(nets)
    sd_net = pstdev(nets)
    if sd_net == 0.0:
        expected_cv: float | None = 0.0
    elif mean_net == 0.0:
        expected_cv = None
    else:
        expected_cv = round2(sd_net / abs(mean_net))
    if expected_cv is None or expected_cv > 1.0:
        expected_label = "low"
    elif expected_cv <= 0.5:
        expected_label = "high"
    else:
        expected_label = "medium"

    result = _run(seeded, operating_threshold=50_000.0)
    assert result["volatility_cv"] == expected_cv
    assert result["confidence"] == expected_label
    assert result["confidence"] in {"low", "medium", "high"}


def test_zero_threshold_headroom_pct(seeded) -> None:
    result = _run(seeded, operating_threshold=0)
    assert result["risk"] == "LOW"
    assert result["headroom"] == result["min_projected_cash"]
    assert result["headroom_pct"] is None
    assert result["breach_days"] == 0
    assert result["first_breach_date"] is None


# ---------------------------------------------------------------------------
# Guards: unknown merchant, no history, argument validation
# ---------------------------------------------------------------------------


def test_unknown_merchant_envelope(seeded) -> None:
    result = _run(seeded, merchant_id="M999")
    assert result == {
        "tool": "forecast_cashflow",
        "status": "unknown_merchant",
        "merchant_id": "M999",
        "horizon_days": 7,
        "sources": {"merchant_id": "M999", "table": "cash_flows"},
    }


def test_no_history_on_empty_db(empty_engine) -> None:
    with Session(empty_engine) as session:
        result = forecast_cashflow(session)
    assert result["status"] == "no_history"
    assert result["scope"] == "all_merchants"
    assert result["history_days"] == 28
    # Guard envelopes carry no forecast series at all.
    assert "forecast" not in result


def test_no_history_for_merchant_without_flows(seeded) -> None:
    # Merchants exist in the seeded DB, so only a scope with zero cash-flow
    # rows triggers the guard. Delete-free check: use a fresh merchant row.
    with Session(seeded.engine) as session:
        session.add(
            Merchant(
                merchant_id="M900",
                name="No Flows Ltd",
                category="test",
                fee_rate=0.02,
                currency="INR",
                opening_balance=1_000.0,
            )
        )
        session.commit()
        result = forecast_cashflow(session, "M900")
    assert result["status"] == "no_history"
    assert result["merchant_id"] == "M900"
    assert result["scope"] == "merchant"
    assert "forecast" not in result


@pytest.mark.parametrize(
    "kwargs",
    [
        {"horizon_days": 0},
        {"horizon_days": 31},
        {"horizon_days": "7"},
        {"history_days": 0},
        {"history_days": 400},
        {"history_days": "28"},
        {"operating_threshold": -1.0},
        {"operating_threshold": "abc"},
    ],
)
def test_invalid_arguments_raise(seeded, kwargs) -> None:
    with pytest.raises(ValueError):
        _run(seeded, **kwargs)


# ---------------------------------------------------------------------------
# Defaults, schema mapping, determinism, read-only behaviour
# ---------------------------------------------------------------------------


def test_default_threshold_comes_from_settings(seeded) -> None:
    # No explicit threshold: the configured default must apply (50k unless
    # the environment overrides it, e.g. a local .env).
    result = _run(seeded)
    assert result["operating_threshold"] == round2(get_settings().operating_threshold)


def test_result_validates_against_chart_ready_schema(seeded) -> None:
    ok = _run(seeded, merchant_id="M001", operating_threshold=50_000.0)
    parsed = ForecastResponse.model_validate(ok)
    assert parsed.status == "ok"
    assert parsed.risk in {"LOW", "MEDIUM", "HIGH"}
    assert len(parsed.forecast) == 7
    assert parsed.forecast[0].day_offset == 1
    assert parsed.projected_ending_balance == ok["projected_ending_balance"]

    # Guard envelopes validate against the same schema (analytics default
    # to None and the per-day series to an empty list).
    unknown = ForecastResponse.model_validate(_run(seeded, merchant_id="M999"))
    assert unknown.status == "unknown_merchant"
    assert unknown.forecast == []


def test_deterministic_across_calls(seeded) -> None:
    first = _run(seeded, merchant_id="M002", operating_threshold=50_000.0)
    second = _run(seeded, merchant_id="M002", operating_threshold=50_000.0)
    assert first == second


def test_tool_is_read_only(seeded) -> None:
    with Session(seeded.engine) as session:
        before = session.execute(
            select(func.count()).select_from(CashFlow)
        ).scalar_one()
        merchants_before = session.execute(
            select(func.count()).select_from(Merchant)
        ).scalar_one()
        forecast_cashflow(session, "M001")
        forecast_cashflow(session)
        session.rollback()  # any accidental writes must vanish anyway
        after = session.execute(select(func.count()).select_from(CashFlow)).scalar_one()
        merchants_after = session.execute(
            select(func.count()).select_from(Merchant)
        ).scalar_one()
    assert before == after
    assert merchants_before == merchants_after



