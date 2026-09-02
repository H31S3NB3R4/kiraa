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

**Phase 0 — project scaffold** and **Phase 1 — data foundation** are complete:

- FastAPI backend skeleton with an application factory and CORS
- `GET /health` liveness endpoint
- Environment-driven configuration (`.env`, validated with pydantic)
- SQLAlchemy engine/session wiring — SQLite by default, PostgreSQL-ready
  via `DATABASE_URL`
- Deterministic seeded dataset generator (see below) with all 10 buildathon
  scenarios injected and labelled with ground truth
- Test suite runnable with pytest (35 dataset invariants + 2 health checks)

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
    services/          cross-cutting services (audit, approvals, metrics)
    models/            SQLAlchemy ORM models
    db/                engine, session, declarative base
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

## Safety model (summary)

- **READ** tools (reconciliation, ledger, forecast, GST, anomalies) are freely
  callable by the agent.
- **PROPOSE** tools (journal entries) create reviewable proposals only.
- **WRITE** actions (mock ledger posts) are never model-callable and require
  explicit human approval through a dedicated endpoint, with idempotency keys,
  audit events, and rollback.

See `architecture.md` for details.
