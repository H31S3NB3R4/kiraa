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
**Phase 3 — deterministic finance engine**, **Phase 4 — forecasting module**,
**Phase 5 — ML anomaly detection**, **Phase 6 — Gemini tool-calling agent**,
**Phase 7 — multi-turn agent state**, **Phase 8 — action layer**,
**Phase 9 — FastAPI APIs**, **Phase 10 — frontend dashboard**,
**Phase 11 — agent experience**, **Phase 12 — evaluation harness**,
**Phase 13 — reliability hardening**, and **Phase 14 — security/safety
review** are complete:

- FastAPI backend skeleton with an application factory and CORS
- `GET /health` liveness endpoint
- Environment-driven configuration (`.env`, validated with pydantic)
- SQLAlchemy engine/session wiring — SQLite by default, PostgreSQL-ready
  via `DATABASE_URL`
- Deterministic seeded dataset generator (see below) with all 10 buildathon
  scenarios injected and labelled with ground truth
- 18-table SQLAlchemy schema (17 planned tables + `dataset_labels`) with
  foreign keys, indexes, and timestamps; populated by a seed script
- Deterministic finance tools (`app/tools/`): 9-way reconciliation engine
  with financial impact and idempotent persistence, read-only ledger
  queries, and GST matching — validated at **100% precision and recall**
  against the ground truth (dev dataset and 500-txn benchmark)
- Deterministic cash-flow forecasting (`forecast_cashflow`): pooled or
  per-merchant horizon forecasts from rolling averages with LOW/MEDIUM/HIGH
  risk classification, drivers, and a chart-ready response schema
- ML anomaly detection (`detect_anomalies`): Isolation Forest trained on
  pure-normal synthetic history with a p99.9-calibrated threshold,
  high/medium/low severity bands, reason metadata against the merchant
  baseline, and ground-truth metrics — validated at **100% precision and
  recall with zero false positives** (dev dataset, 500-txn benchmark, and
  unseen seeds)
- Gemini tool-calling agent (`app/agent/`): provider-agnostic `LLMProvider`
  with a Gemini adapter, six READ/PROPOSE tools returning structured
  envelopes, a bounded controller loop, `POST /api/agent/chat`, and multi-turn
  runs (`run_id` continuation with a persisted transcript and bounded replay)
- Human-gated action layer (`app/services/actions.py` +
  `POST /api/actions/{proposal_id}/approve|reject|rollback`): idempotency-keyed
  decisions, mock-ledger posting, a full audit trail, and an append-only
  rollback path — the only code that writes ledger rows, never model-callable
- Full REST read/report surface (`app/api/routes/`): the complete PRD
  section-16 API — `POST /api/reconciliation/run` (engine + persisted
  exception ids), `GET /api/ledger/query`, `GET /api/forecast`,
  `GET /api/anomalies` (read-only scoring), `GET /api/exceptions` (persisted
  rows), `GET /api/runs/{run_id}` (trace + transcript), `GET /api/audit`
  (decision trail), and `GET /api/metrics` (dashboard KPI cards) — every
  endpoint a thin wrapper over the same deterministic tools/services the
  agent uses, with guard envelopes, 422 validation, and 404 semantics
- Phase 10 endpoints: `GET /api/merchants` (merchant picker) and
  `GET /api/proposals` (action-queue cards) — same thin-wrapper pattern
- Evaluation harness (Phase 12): `app/services/evaluation.py` scores the
  deterministic engines against the seeded ground truth (reconciliation
  match accuracy / exception precision-recall / exact-type accuracy, anomaly
  precision / recall / false-positive rate), benchmarks the agent layer
  offline with a scripted provider (latency, tool calls, failure rate),
  renders the todo Phase 12 benchmark table, and ships a CLI
  (`backend/scripts/run_evaluation.py`); `GET /api/evaluation` exposes the
  engine scores plus stored agent-run history (strictly read-only — a GET
  never starts an agent run)
- Agent reliability hardening (Phase 13): every registry tool entry pins
  its own wall-clock `timeout_seconds` and bounded `max_retries`
  (`TOOL_TIMEOUTS` / `TOOL_RETRY_POLICY`); `dispatch_tool` runs each
  attempt on an isolated worker session, retries timed-out attempts,
  surfaces exhaustion as a structured `TOOL_TIMEOUT` envelope (never a
  hang), and the `TOOL_TIMEOUT_SECONDS` setting acts as a global ceiling
  that can tighten — never loosen — the per-tool budgets; the Gemini
  adapter retries transient HTTP failures (429/5xx) with bounded backoff;
  blank chat messages are refused at the schema boundary with 422
- Security/safety review (Phase 14): `redact_credentials` /
  `redact_secrets` in `app/config.py` keep the database password and the
  Gemini key out of CLI output, logs, stored run traces, and HTTP
  errors; `GET /health` reports `data: "synthetic"` (and the OpenAPI
  description plus a dashboard badge say so); the propose tool's schema
  accepts only `exception_id`/`reason`, injected financial arguments are
  rejected with `INVALID_ARGUMENTS`, and every proposal amount/account/
  confidence is derived from the verified exception row — model-generated
  numbers can never become financial values
- React dashboard (`frontend/`): Vite + TypeScript + Tailwind, six pages
  (Dashboard KPIs, Reconciliation, Forecast, Anomalies, Actions, Audit)
  behind a global merchant/date scope, an axios client over the 16-endpoint
  REST surface, and the agent chat panel (AgentPanel) with tool timeline,
  evidence chips, ML-vs-deterministic labels, loading/error/empty states,
  and clickable sample prompts — `tsc -b` clean, `vite build` green
- Test suite runnable with pytest (35 dataset + 2 health + 21 database
  + 27 engine + 29 forecast + 32 anomaly + 31 agent + 12 multi-turn
  + 14 action + 29 API + 9 Phase-10 endpoint + 10 evaluation + 13
  reliability + 10 security tests — 274 total, all offline)

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

## ML anomaly detection (Phase 5)

`detect_anomalies(db, merchant_id=None, transaction_ids=None, limit=500, *,
persist=True, model=None)` in `backend/app/tools/anomalies.py` scores
transactions with a trained Isolation Forest. It runs *alongside*
deterministic reconciliation — never replacing it — and every result
cross-links the deterministic verdict via `reconciliation_pass` (PRD FR-6):

- the model (`ml/train_anomaly.py`) trains on a **pure-normal** synthetic
  history (seed 202, 2000 transactions, zero injected exceptions) so it
  learns the shape of normal behaviour only; at serving time the bundle
  resolves as explicit model > persisted artifact > deterministic
  in-process retrain
- four features (`ml/features.py`): amount vs the merchant's median, UTC
  hour, settlement delay (missing = NaN, "settlement not received"), and
  fee ratio — one canonical pipeline shared by training and serving
- the flag threshold (0.6947) is calibrated at the **99.9th percentile**
  of 2600 pooled normal scores (training + two exception-free validation
  sets), keeping every rare-but-legitimate normal corner below the flag
- every result carries a high/medium/low **severity band** (medium is a
  watch signal within 0.05 of the threshold), a human-readable reason,
  and full feature metadata comparing the record against the merchant
  baseline
- `merchant_id`/`transaction_ids` scope the scan, results come back
  score-descending, and `limit` caps returned rows while metrics describe
  the full scan; guards return `unknown_merchant`/`no_transactions`
  envelopes that still schema-validate
- ground-truth metrics vs `dataset_labels.anomaly` ship with every scan —
  **100% precision, 100% recall, 0.0 false-positive rate** on the dev
  dataset, the 500-transaction benchmark, and unseen seeds; scores upsert
  idempotently into `anomaly_scores` per `(transaction_id, model_version)`
- `app/api/schemas/anomalies.py` mirrors the result as a pydantic
  `AnomalyResponse` + per-row `AnomalyScoreRow`

The flagship demo case (todo Phase 5): the injected hidden anomaly — 7.18x
its merchant's median at 03:xx UTC with perfectly consistent books — has
`reconciliation = PASS` and `anomaly = HIGH`, exactly the value the ML layer
adds beyond deterministic rules.

```powershell
# Retrain / inspect the anomaly model (deterministic, seeded)
.\.venv\Scripts\python.exe backend\scripts\train_anomaly.py
```

The versioned artifact lives at `backend/ml/artifacts/iforest-v1.joblib`
(gitignored); the in-process fallback retrains to the identical bundle, so
results reproduce exactly with or without the binary.

## Action layer: approve / reject / rollback (Phase 8)

`app/services/actions.py` is the **only** code that writes ledger rows (the
seeder aside). Its three operations — exposed at
`POST /api/actions/{proposal_id}/approve|reject|rollback` and deliberately
absent from the agent tool registry — take a proposal from `pending` through
the human decision gate:

- **approve** re-validates the proposal server-side (positive amount, distinct
  non-empty debit/credit accounts, linked transaction — a model-drafted
  payload is never trusted), posts exactly one `LE-MOCK-` correction
  `LedgerEntry` (`status='posted'`; the prefix can never collide with the
  seeded `LE-3xxx` sequence), records the `Approval` with the request's
  idempotency key and the posted entry id, and appends a `proposal.approve`
  audit event with before/after states.
- **idempotency** (PRD section 14): every write body carries an
  `idempotency_key` (8–64 chars). A replayed key returns the stored outcome —
  `idempotent_replay: true`, no second ledger entry, no second approval, one
  replay-marker audit event. Re-deciding a decided proposal under a *different*
  key is refused (409), so duplicate approvals can never double-post; a key
  spent on another write or proposal is refused too.
- **reject** records the decision (plus optional note) and a
  `proposal.reject` audit event — the ledger is never touched. Approve after
  reject and reject after approve are both refused (409 state machine).
- **rollback** (PRD section 15): `approved → rolled_back`. The posted entry
  flips to `status='reversed'` (append-only — the row stays queryable), the
  proposal is marked `rolled_back`, and the transition is audited. Duplicate
  rollbacks replay; only approved proposals can be rolled back.
- **failure branch** (architecture section 10): `simulate_failure=true` on
  approve fails the post atomically — no approval, no ledger entry, no audit
  event — and the same idempotency key succeeds on retry (the key was never
  spent).

Typed service errors map onto HTTP codes: 404 unknown proposal, 409 wrong
lifecycle state / key conflict, 422 unpostable proposal or bad fields, 502
failed ledger post. The routes stay thin — validation, posting, and audit
live in the service, which is unit-testable without FastAPI.

The safety gate is enforced structurally: the six model-callable tools
contain no action verbs, `TOOL_REGISTRY` carries no WRITE-class callable, the
agent layer never imports `app.services.actions`, and an "approve it" chat
message leaves proposals pending and the ledger untouched.

## Repository layout

```text
backend/
  app/
    main.py            FastAPI application factory
    config.py          environment-based settings (pydantic + dotenv)
    api/routes/        HTTP routers (health, agent chat, actions, and the
                       Phase 9 read/report surface: reconciliation, ledger,
                       forecast, anomalies, exceptions, runs, audit, metrics)
    api/schemas/       shared request/response schemas
    agent/             controller loop, tool registry, LLM provider adapters
    tools/             deterministic finance tools
    services/          cross-cutting services (dataset generator, DB seeding, action layer)
    models/            SQLAlchemy ORM models (18 tables, Base + TimestampMixin)
    db/                engine, session, create_all/drop_all
  ml/                  anomaly model training + artifacts
  scripts/             dataset generator and utilities
  tests/               pytest suite
frontend/              React + Vite + TypeScript + Tailwind dashboard
                       (six pages, global merchant/date scope, agent chat)
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

## Quick start (frontend)

Requirements: Node 18+ (Node 22 used in development). Run the backend first.

```powershell
# 1. Install dependencies (once)
cd frontend
npm install

# 2. Start the dev server (proxies /api and /health to :8000)
npm run dev
```

Verify: open http://localhost:5173 — the dashboard loads with merchant /
date scoping, six pages, and the agent chat panel.

Production build: `npm run build` (output in `frontend/dist/`).

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
| `AGENT_MAX_TOOL_CALLS` | `12`                          | Bounded tool-call limit per agent run (spans all turns) |
| `AGENT_MAX_HISTORY_MESSAGES` | `40`                     | Conversation events replayed on follow-up turns (bounded context) |
| `TOOL_TIMEOUT_SECONDS`  | `30`                          | Global ceiling (s) per agent tool attempt — tightens, never loosens, the per-tool registry budgets |
| `OPERATING_THRESHOLD`  | `50000`                       | Minimum operating cash (INR) for forecast risk  |

## Safety model (summary)

- **READ** tools (reconciliation, ledger, forecast, GST, anomalies) are freely
  callable by the agent.
- **PROPOSE** tools (journal entries) create reviewable proposals only.
- **WRITE** actions (mock ledger posts) are never model-callable and require
  explicit human approval through a dedicated endpoint, with idempotency keys,
  audit events, and rollback.

See `architecture.md` for details.
