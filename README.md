# AI Finance Controller

Agentic finance-operations controller built for the Razorpay Buildathon (Track 4).

Gemini is the reasoning layer that decides **what to investigate**. Deterministic
backend tools decide the **financial facts**. A human decides whether
**money-related state changes** happen — and everything is audited.

> **Product principle:** LLM for reasoning. Deterministic services for financial
> truth. Human approval for financial mutation. Audit everything.

## Documents

- `PRD.md` — product requirements
- `architecture.md` — system architecture (source of truth for design)
- `todo.md` — implementation roadmap and task status

## Current status

**Phase 0 — project scaffold**, **Phase 1 — data foundation**, **Phase 2 — database**,
**Phase 3 — deterministic finance engine**, and **Phase 4 — forecasting module**
are complete:

- FastAPI backend skeleton with an application factory and CORS
- `GET /health` liveness endpoint
- Environment-driven configuration (`.env`, validated with pydantic)
- SQLAlchemy engine/session wiring — SQLite by default, PostgreSQL-ready
  via `DATABASE_URL`
- Deterministic seeded dataset generator (see below) with all 10 buildathon
  scenarios injected and labelled with ground truth
- 17-table SQLAlchemy schema (16 planned tables + `dataset_labels`) with
  foreign keys, indexes, and timestamps; populated by a seed script
- Deterministic finance tools (`app/tools/`): 9-way reconciliation engine
  with financial impact and idempotent persistence, read-only ledger
  queries, and GST matching — validated at **100% precision and recall**
  against the ground truth (dev dataset and 500-txn benchmark)
- Deterministic cash-flow forecasting (`forecast_cashflow`): pooled or
  per-merchant horizon forecasts from rolling averages with LOW/MEDIUM/HIGH
  risk classification, drivers, and a chart-ready response schema
- Test suite runnable with pytest (35 dataset + 2 health + 21 database
  + 27 engine + 29 forecast tests)

All financial data in this system is **synthetic** and produced by a seeded
dataset generator. Nothing here moves real money.

## Synthetic dataset generator (Phase 1)

```powershell
# Dev dataset: 100 transactions over 28 days -> data/generated/
.\.venv\Scripts\python.exe backend\scripts\generate_dataset.py

# Benchmark dataset: 500 transactions / 56 days / 5 exceptions of each type
.\.venv\Scripts\python.exe backend\scripts\generate_dataset.py --transactions 500 `
    --exceptions-per-type 5 --window-days 56 --customers 200 `
    --out data/benchmark --name benchmark
```

Options: `--transactions`, `--seed`, `--end-date`, `--window-days`,
`--exceptions-per-type`, `--customers`, `--out`, `--name`.

Each run writes `<name>.json` (the full dataset) and `<name>_labels.csv`
(ground truth for the Phase 12 evaluation harness). The same seed and
parameters always produce a byte-identical dataset, and every injected
scenario is labelled: `scenario`, `recon_exception` (must be caught by
deterministic reconciliation), `anomaly` (ML ground truth; passes all
deterministic checks).

## Database (Phase 2)

Seventeen tables mirror the dataset one-to-one: ten are loaded by the
seeder (merchants, customers, transactions, settlements, refunds, fees,
invoices, ledger_entries, cash_flows, and `dataset_labels` — the ground
truth for the Phase 12 evaluation harness). The remaining operational
tables are written by later phases (reconciliation_exceptions,
anomaly_scores, agent_runs, tool_calls, journal_proposals, approvals,
audit_events). Money columns are `Numeric(14, 2)` (SQLAlchemy returns
`Decimal`), and SQLite enforces foreign keys via `PRAGMA foreign_keys=ON`
on every connection.

```powershell
# From backend/ — seed data/finance.db with the committed dev dataset
..\.venv\Scripts\python.exe scripts\seed_db.py
```

Options:

```text
--dataset <path>      dataset JSON (default: data/generated/dataset.json)
--database-url <url>  SQLAlchemy URL (default: DATABASE_URL env var)
--recreate            drop & rebuild all tables before seeding
```

Examples:

```powershell
..\.venv\Scripts\python.exe scripts\seed_db.py --recreate
..\.venv\Scripts\python.exe scripts\seed_db.py --dataset ..\data\benchmark\benchmark.json
```

The seeder refuses to run twice without `--recreate` (protects the demo
database), validates the dataset JSON structure, and inserts in FK-safe
order inside one transaction. Exit codes: `0` success, `1` already seeded,
`2` dataset load failure.

## Deterministic finance engine (Phase 3)

Three tools in `backend/app/tools/` decide the financial facts; they never
need the LLM and are unit-testable in isolation (the Phase 6 agent layer
will expose them to Gemini through the PRD tool contracts).

- `run_reconciliation(db, merchant_id?, start_date?, end_date?, persist=?)` —
  9-way exception classification (missing settlement, fee, amount, timing,
  refund, failed ledger write, ledger amount, GST, duplicates) with signed
  financial impact, exception-level evidence with source references,
  aggregate metrics, and **idempotent** persistence into
  `reconciliation_exceptions` (upsert keyed by
  `(transaction_id, exception_type)`).
- `query_ledger(db, merchant_id?, transaction_id?, start_date?, end_date?,
  status?, account?, category?, limit?)` — read-only, source-linked ledger
  queries (every row carries its settlement/invoice references).
- `check_gst_match(db, transaction_id)` — expected tax
  (`total x rate / (1 + rate)`) vs recorded tax, exact difference, and
  source references.

Rules mirror the dataset generator exactly (T+2 settlement, 10-minute
duplicate window, net = amount − fee), so engine arithmetic and
ground-truth arithmetic can never drift. Validated at **100% precision and
100% recall**: the 9 ground-truth exception rows on the dev dataset (both
duplicate rows flagged; every NORMAL record and the HIDDEN_ANOMALY pass
clean) and 45/45 on the 500-transaction benchmark. Impact values match the
injected error rates to the paise (0.5% fee overcharge, 15% refund
overpay, 8% GST error, T+5 settlement delay).

## Cash-flow forecasting (Phase 4)

`forecast_cashflow(db, merchant_id=None, *, horizon_days=7, history_days=28,
operating_threshold=None)` in `backend/app/tools/forecast.py` projects the
cash position forward from the per-day aggregates in `cash_flows` —
deliberately deterministic, independent of Gemini (PRD FR-4, section 12):

- aggregates daily inflows/outflows over the trailing `history_days`
  (default 28), pooled across all merchants when `merchant_id=None`
- computes trailing daily averages plus a recent 7-day rolling window and a
  week-over-week net trend
- projects the recent rolling averages flat across `horizon_days` (1-30,
  default 7) from the anchor day's closing balance, using the same running
  `round2` arithmetic as the dataset generator
- classifies risk against the operating threshold (per-call argument, else
  `OPERATING_THRESHOLD`, default 50000): HIGH if any projected day falls
  below it, MEDIUM if the minimum stays within 25% above it, else LOW
- returns the drivers behind the classification — averages, trend,
  volatility-based confidence label, minimum/first-breach/headroom, anchor
  balance, `sources` — so the LLM explains (never invents) the numbers
- `app/api/schemas/forecast.py` mirrors the tool result as a chart-ready
  pydantic `ForecastResponse` + per-day `ForecastPoint`

## Repository layout

```text
backend/
  app/
    main.py            FastAPI application factory
    config.py          environment-based settings (pydantic + dotenv)
    api/routes/        HTTP routers (health now; agent/recon/ledger/... later)
    api/schemas/       shared request/response schemas
    agent/             controller loop, tool registry, LLM provider adapters
    tools/             deterministic finance tools
    services/          cross-cutting services (dataset generator, DB seeding; audit/approvals later)
    models/            SQLAlchemy ORM models (17 tables, Base + TimestampMixin)
    db/                engine, session, create_all/drop_all
  ml/                  anomaly model training + artifacts
  scripts/             dataset generator and utilities
  tests/               pytest suite
frontend/              React + Vite + Tailwind dashboard (scaffolded next)
data/
  raw/                 generator inputs
  generated/           seeded synthetic datasets
  benchmark/           fixed evaluation datasets and labels
```

## Quick start (backend)

Requirements: Python 3.12+ on Windows/macOS/Linux.

```powershell
# 1. Virtual environment (skip if `.venv` already exists)
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate

# 2. Install dependencies
pip install -r backend/requirements.txt

# 3. Configure environment
Copy-Item .env.example .env         # macOS/Linux: cp .env.example .env
# A Gemini API key is only needed from the agent phase onward.

# 4. Start the API
uvicorn app.main:app --reload --port 8000 --app-dir backend
```

Verify:

- Health: http://127.0.0.1:8000/health → `{"status": "ok", ...}`
- Interactive API docs: http://127.0.0.1:8000/docs

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -v
```

## Environment variables

All configuration is environment-driven; see `.env.example` for the full list.

| Variable               | Default                       | Purpose                                        |
|------------------------|-------------------------------|------------------------------------------------|
| `GEMINI_API_KEY`       | *(empty)*                    | Gemini API key (required from the agent phase)  |
| `GEMINI_MODEL`         | `gemini-2.5-flash`           | Model used by the controller                   |
| `DATABASE_URL`         | `sqlite:///./data/finance.db` | SQLAlchemy URL; switch to PostgreSQL when ready |
| `CORS_ORIGINS`         | Vite dev origins              | Comma-separated allowed browser origins        |
| `AGENT_MAX_TOOL_CALLS` | `12`                          | Bounded tool-call limit per agent run (safety) |
| `OPERATING_THRESHOLD`  | `50000`                       | Minimum operating cash (INR) for forecast risk  |

## Safety model (summary)

- **READ** tools (reconciliation, ledger, forecast, GST, anomalies) are freely
  callable by the agent.
- **PROPOSE** tools (journal entries) create reviewable proposals only.
- **WRITE** actions (mock ledger posts) are never model-callable and require
  explicit human approval through a dedicated endpoint, with idempotency keys,
  audit events, and rollback.

See `architecture.md` for details.
