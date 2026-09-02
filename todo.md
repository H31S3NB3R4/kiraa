# AI Finance Controller — TODO / Implementation Plan

## How to use this file

Build in the order below. Do not start with UI polish, event streaming, or multi-currency. Get the agent/tool loop and measurable finance engine working first.

Legend:

- `[ ]` not started
- `[~]` in progress
- `[x]` completed
- `[!]` blocker / needs attention

---

# Phase 0 — Project Setup

- [x] Create repository structure.
- [x] Initialize Python virtual environment. *(pre-existing `.venv`, Python 3.13.14)*
- [x] Create FastAPI backend.
- [ ] Create React/Vite frontend. *(placeholder dir only; scaffolded in Phase 10)*
- [ ] Add Tailwind CSS. *(with frontend scaffold)*
- [x] Add PostgreSQL or SQLite for the first local prototype. *(SQLite default via `DATABASE_URL`; `psycopg` driver installed for later PostgreSQL switch)*
- [x] Create `.env.example`.
- [x] Add Gemini API key configuration. *(env var `GEMINI_API_KEY`; key supplied by user in Phase 6)*
- [x] Add `.gitignore` for secrets, virtualenvs, model artifacts if needed.
- [x] Create `README.md` with setup instructions.
- [x] Add basic health endpoint: `GET /health`.

### Suggested backend dependencies

```text
fastapi
uvicorn
pydantic
sqlalchemy
psycopg
pandas
numpy
scikit-learn
statsmodels
python-dotenv
google-genai  # use the current official Gemini SDK/API package available in your environment
```

### Suggested frontend dependencies

```text
react
react-router-dom
axios
recharts
lucide-react
```

---

# Phase 1 — Data Foundation

## Synthetic dataset

All entities are produced by one deterministic generator:
`backend/app/services/dataset_generator.py` (CLI: `backend/scripts/generate_dataset.py`).

- [x] Create merchants. *(5 fixed merchants with fee schedules and ticket-size profiles)*
- [x] Create customers. *(80 default, weighted repeat-purchase behaviour)*
- [x] Create transactions.
- [x] Create settlements. *(T+2 lag, net of fees)*
- [x] Create refunds. *(post-settlement outflows; ~7% of normal txns refunded)*
- [x] Create fees.
- [x] Create invoices. *(paise-exact GST decomposition: taxable + GST = total)*
- [x] Create ledger entries. *(double-entry style rows; posted / failed)*
- [x] Create daily cash-flow records. *(per-merchant daily grid incl. closing balances)*

## Scenario injection

- [x] Correct transaction. *(all NORMAL records pass every deterministic check)*
- [x] Fee mismatch. *(+0.5% overcharge vs merchant fee schedule)*
- [x] Refund mismatch. *(refund recorded at 115% of expected)*
- [x] Duplicate transaction. *(same merchant/customer/amount within 10 minutes)*
- [x] Settlement timing mismatch. *(settles T+5 instead of T+2)*
- [x] Missing settlement. *(receivable booked, no settlement record)*
- [x] Ledger mismatch. *(gross amount posted instead of net)*
- [x] GST mismatch. *(GST recorded 8% above expected)*
- [x] Correctly reconciled but statistically unusual transaction. *(HIDDEN_ANOMALY: 7x merchant median at 03:xx, recon-clean)*
- [x] Failed downstream write simulation. *(ledger entry with status `failed`)*

Demo anchoring: the missing settlement, the fee overcharge, and the oversized
refund are all placed so the most recent Tuesday's settled cash is visibly
short — powering the "Why is Tuesday's cash short?" investigation.

## Dataset scale

- [x] Start with 100 rows for local testing. *(`data/generated/dataset.json`)*
- [x] Increase to 500+ rows for benchmark. *(`data/benchmark/benchmark.json`, 500 txns / 56 days)*
- [x] Add a fixed random seed for reproducibility. *(seed=42; same seed ⇒ byte-identical dataset)*
- [x] Export benchmark ground-truth labels. *(`<name>_labels.csv` with scenario, recon_exception, anomaly flags)*

Verified by 35 tests in `backend/tests/test_dataset_generator.py` (37 suite-wide).

---

# Phase 2 — Database

- [ ] Create SQLAlchemy models.
- [ ] Create database tables.
- [ ] Add indexes for transaction IDs, settlement IDs, dates, merchant IDs.
- [ ] Add foreign keys.
- [ ] Add timestamps.
- [ ] Add audit tables.
- [ ] Add database seed script.
- [ ] Confirm dataset can be recreated from scratch.

### Required tables

- [ ] merchants
- [ ] customers
- [ ] transactions
- [ ] settlements
- [ ] refunds
- [ ] fees
- [ ] invoices
- [ ] ledger_entries
- [ ] cash_flows
- [ ] reconciliation_exceptions
- [ ] anomaly_scores
- [ ] agent_runs
- [ ] tool_calls
- [ ] journal_proposals
- [ ] approvals
- [ ] audit_events

---

# Phase 3 — Deterministic Finance Engine

## 3.1 Reconciliation

- [ ] Implement transaction/settlement normalization.
- [ ] Implement transaction ID matching.
- [ ] Implement amount tolerance.
- [ ] Implement fee comparison.
- [ ] Implement refund comparison.
- [ ] Implement settlement timing comparison.
- [ ] Implement duplicate detection.
- [ ] Implement exception classification.
- [ ] Calculate financial impact per exception.
- [ ] Return aggregate reconciliation metrics.
- [ ] Add unit tests.

Target tool result:

```json
{
  "total_transactions": 500,
  "matched": 470,
  "exceptions": 30,
  "match_rate": 94.0
}
```

## 3.2 Ledger query

- [ ] Implement read-only transaction query.
- [ ] Support date range.
- [ ] Support transaction ID.
- [ ] Support merchant ID.
- [ ] Support status.
- [ ] Return source references.
- [ ] Add indexes.
- [ ] Add unit tests.

## 3.3 GST matching

- [ ] Implement expected GST calculation.
- [ ] Compare with recorded GST.
- [ ] Return exact difference.
- [ ] Include invoice/transaction source IDs.
- [ ] Add mismatch test cases.

---

# Phase 4 — Forecasting Module

- [ ] Aggregate daily cash inflows.
- [ ] Aggregate daily cash outflows.
- [ ] Calculate historical rolling averages.
- [ ] Add initial forecast model.
- [ ] Produce seven-day forecast.
- [ ] Calculate projected ending balance.
- [ ] Add operating threshold configuration.
- [ ] Implement LOW/MEDIUM/HIGH risk classification.
- [ ] Return forecast drivers.
- [ ] Add tests for deterministic outputs.
- [ ] Create a chart-ready response schema.

### MVP output

```json
{
  "horizon_days": 7,
  "forecast": [
    {"date": "2026-09-04", "projected_cash": 320000},
    {"date": "2026-09-05", "projected_cash": 305000}
  ],
  "risk": "HIGH"
}
```

---

# Phase 5 — ML Anomaly Detection

- [ ] Build feature engineering pipeline.
- [ ] Generate historical normal training data.
- [ ] Train Isolation Forest.
- [ ] Save model artifact/version.
- [ ] Implement scoring function.
- [ ] Calibrate anomaly threshold on synthetic validation data.
- [ ] Add anomaly reason metadata.
- [ ] Add batch scoring.
- [ ] Add unit tests.
- [ ] Measure precision/recall and false-positive rate.

### Important demo case

Create at least one record where:

```text
Reconciliation = PASS
Anomaly score  = HIGH
```

This clearly demonstrates why the ML layer adds value.

---

# Phase 6 — Gemini Tool-Calling Controller

## Provider adapter

- [ ] Create `LLMProvider` interface.
- [ ] Implement Gemini provider.
- [ ] Keep Gemini-specific request/response parsing isolated.
- [ ] Add environment-based model configuration.

## Tool schemas

- [ ] Define `run_reconciliation` schema.
- [ ] Define `query_ledger` schema.
- [ ] Define `forecast_cashflow` schema.
- [ ] Define `check_gst_match` schema.
- [ ] Define `detect_anomalies` schema.
- [ ] Define `propose_journal_entry` schema.

## Controller loop

- [ ] Send user message to Gemini.
- [ ] Provide tool definitions.
- [ ] Detect function/tool calls.
- [ ] Validate tool arguments.
- [ ] Dispatch through registry.
- [ ] Append tool results to conversation.
- [ ] Continue until final response.
- [ ] Add bounded maximum tool calls per run.
- [ ] Handle malformed tool arguments.
- [ ] Handle tool exceptions.
- [ ] Persist run and tool-call data.
- [ ] Return final response plus evidence/tool metadata.

## System prompt

- [ ] Add evidence-first rules.
- [ ] Add no-fabrication rule.
- [ ] Add safe-write rule.
- [ ] Add unresolved-exception rule.
- [ ] Add distinction between deterministic and ML signals.

### First milestone

The following must work from a single API request:

```text
POST /api/agent/chat
"Reconcile this week's settlements"
       ↓
Gemini
       ↓
run_reconciliation()
       ↓
Gemini
       ↓
final answer
```

---

# Phase 7 — Multi-Turn Agent State

- [ ] Create `run_id`.
- [ ] Persist user messages.
- [ ] Persist model requests/responses.
- [ ] Persist tool calls/results.
- [ ] Reuse context for follow-up messages.
- [ ] Test:

```text
User: Reconcile this week.
User: What are the biggest exceptions?
User: Investigate the top one.
```

- [ ] Ensure later questions can use earlier retrieved context.
- [ ] Prevent uncontrolled context growth.

---

# Phase 8 — Action Layer

## Journal proposal

- [ ] Implement proposal schema.
- [ ] Validate amount/account fields.
- [ ] Include evidence IDs.
- [ ] Include confidence.
- [ ] Generate proposal from a verified exception.

## Approval

- [ ] Add approve endpoint.
- [ ] Add reject endpoint.
- [ ] Add mock ledger API.
- [ ] Add idempotency key.
- [ ] Ensure duplicate approval does not double-post.
- [ ] Create audit event on approval.
- [ ] Create audit event on rejection.
- [ ] Implement rollback endpoint.
- [ ] Simulate failure and test recovery.

### Critical rule

- [ ] Confirm that no natural-language request can bypass human approval.

---

# Phase 9 — FastAPI APIs

- [ ] `POST /api/agent/chat`
- [ ] `POST /api/reconciliation/run`
- [ ] `GET /api/ledger/query`
- [ ] `GET /api/forecast`
- [ ] `GET /api/anomalies`
- [ ] `GET /api/exceptions`
- [ ] `GET /api/runs/{run_id}`
- [ ] `POST /api/actions/{proposal_id}/approve`
- [ ] `POST /api/actions/{proposal_id}/reject`
- [ ] `POST /api/actions/{proposal_id}/rollback`
- [ ] `GET /api/audit`
- [ ] `GET /api/metrics`

---

# Phase 10 — Frontend Dashboard

## Shell

- [ ] Finance Control Center layout.
- [ ] Sidebar/top navigation.
- [ ] Current merchant selector if needed.
- [ ] Global date range.
- [ ] Agent chat panel.

## KPI cards

- [ ] Total cash.
- [ ] Reconciliation match rate.
- [ ] Exception count.
- [ ] Financial impact at risk.

## Reconciliation view

- [ ] Matched count.
- [ ] Unmatched count.
- [ ] Match rate.
- [ ] Exception list.
- [ ] Filters.

## Forecast view

- [ ] Seven-day line chart.
- [ ] Risk indicator.
- [ ] Projected minimum cash.
- [ ] Forecast drivers.

## Anomaly view

- [ ] Anomaly score.
- [ ] Transaction details.
- [ ] Compare normal vs unusual attributes.

## Action view

- [ ] Journal proposal card.
- [ ] Evidence section.
- [ ] Approve button.
- [ ] Reject button.
- [ ] Rollback status.

## Audit view

- [ ] Run history.
- [ ] Tool sequence.
- [ ] Action history.
- [ ] Approval history.

---

# Phase 11 — Agent Experience / Demo Polish

- [ ] Show which tools are being used.
- [ ] Show tool status: running / completed / failed.
- [ ] Show evidence links in agent responses.
- [ ] Show record IDs and monetary impact.
- [ ] Clearly label ML anomaly vs deterministic exception.
- [ ] Make final response concise and finance-oriented.
- [ ] Add loading state.
- [ ] Add error state.
- [ ] Add empty state.
- [ ] Add sample prompts on first load.

Suggested sample prompts:

```text
Reconcile this week's settlements.
Why is Tuesday's cash short?
Forecast next week's cash and flag risk.
Show me the highest-impact exceptions.
Investigate transaction TXN-1042.
```

---

# Phase 12 — Evaluation Harness

- [ ] Create fixed benchmark dataset.
- [ ] Store ground-truth exception labels.
- [ ] Run reconciliation benchmark.
- [ ] Measure match accuracy.
- [ ] Measure exception precision/recall.
- [ ] Run anomaly benchmark.
- [ ] Measure anomaly precision/recall.
- [ ] Measure false-positive rate.
- [ ] Measure agent latency.
- [ ] Measure average tool calls/run.
- [ ] Measure throughput.
- [ ] Track tool failure rate.
- [ ] Track unresolved exceptions.
- [ ] Generate a machine-readable evaluation report.
- [ ] Display headline metrics in the dashboard/demo.

### Benchmark table

```text
Records processed:       ____
Reconciliation accuracy: ____%
Exception precision:     ____%
Exception recall:        ____%
Anomaly precision:       ____%
Anomaly recall:          ____%
False-positive rate:     ____%
Average latency:         ____ ms
Throughput:              ____ records/min
Unresolved exceptions:   ____
```

Do not invent these numbers for the pitch. Run the benchmark and report the actual results.

---

# Phase 13 — Reliability Testing

- [ ] Malformed tool arguments.
- [ ] Empty query.
- [ ] Invalid dates.
- [ ] Missing transaction ID.
- [ ] Tool timeout.
- [ ] Tool internal error.
- [ ] Gemini API error.
- [ ] Duplicate approval.
- [ ] Mock ledger failure.
- [ ] Rollback after failure.
- [ ] Agent exceeds maximum tool calls.
- [ ] Database connection failure.
- [ ] Re-run same dataset and confirm deterministic finance outputs.

---

# Phase 14 — Security / Safety Review

- [ ] Remove hard-coded API keys.
- [ ] Verify `.env` is ignored.
- [ ] Avoid credentials in logs.
- [ ] Separate READ and WRITE permissions.
- [ ] Require human approval for mock ledger writes.
- [ ] Validate journal amounts server-side.
- [ ] Validate idempotency server-side.
- [ ] Add audit record for every action.
- [ ] Make synthetic/demo nature clear.
- [ ] Prevent model-generated numbers from being treated as authoritative financial values.

---

# Phase 15 — Buildathon Demo Preparation

## Demo flow

- [ ] Open dashboard.
- [ ] Show baseline KPIs.
- [ ] Ask: `Reconcile this week's settlements.`
- [ ] Show tool call and updated metrics.
- [ ] Ask: `Why is Tuesday's cash short?`
- [ ] Show investigation and evidence.
- [ ] Ask: `Forecast next week's cash and flag risk.`
- [ ] Show seven-day graph.
- [ ] Reveal a transaction where reconciliation passes but ML anomaly is high.
- [ ] Ask agent to investigate it.
- [ ] Generate highest-confidence journal proposal.
- [ ] Approve it.
- [ ] Show mock ledger update.
- [ ] Show audit trail.
- [ ] Finish with benchmark metrics.

## Demo safety

- [ ] Pre-seed deterministic synthetic data.
- [ ] Keep API calls fast.
- [ ] Keep a fallback demo dataset in case the model/API is temporarily unavailable.
- [ ] Avoid depending on network-heavy optional features during the final demo.

---

# Phase 16 — Optional After MVP

Only start these after the core controller is reliable.

## Event-driven architecture

- [ ] Add async event queue.
- [ ] Emit settlement-created event.
- [ ] Consume settlement event.
- [ ] Trigger reconciliation.
- [ ] Add idempotency key per event.
- [ ] Add retry queue.
- [ ] Push live dashboard updates.

## Multi-currency

- [ ] Add currency code to every financial record.
- [ ] Add FX source abstraction.
- [ ] Add converted settlement amount.
- [ ] Add currency-specific precision.
- [ ] Add FX-aware tolerance bands.
- [ ] Add explicit FX discrepancy classification.

---

# Recommended Build Order — Critical Path

```text
1. Dataset
   ↓
2. Database
   ↓
3. Reconciliation engine
   ↓
4. Ledger queries
   ↓
5. Forecasting
   ↓
6. GST matching
   ↓
7. Gemini tool calling
   ↓
8. Multi-turn state
   ↓
9. ML anomaly layer
   ↓
10. Journal proposals
   ↓
11. Human approval
   ↓
12. Dashboard
   ↓
13. Evaluation harness
   ↓
14. Demo polish
```

---

# Definition of Done

The project is ready for submission/demo when all of the following are true:

- [ ] Gemini is actually making function/tool calls.
- [ ] Tool selection is not hardcoded by user intent strings.
- [ ] At least 50 synthetic records are processed.
- [ ] Reconciliation outputs are deterministic.
- [ ] Forecasting is implemented as a real backend module.
- [ ] ML anomaly detection is implemented and evaluated.
- [ ] GST matching works on test scenarios.
- [ ] Multi-turn investigation works.
- [ ] Journal proposals are structured and evidence-backed.
- [ ] Human approval gates ledger mutation.
- [ ] Audit trail records the operation.
- [ ] Tool failures are visible and handled.
- [ ] Evaluation metrics are real and reproducible.
- [ ] Dashboard demonstrates the entire finance-operations loop.
- [ ] README contains local setup and demo instructions.

---

# Stretch Goal Checklist

- [ ] Event-driven settlement ingestion.
- [ ] Multi-currency reconciliation.
- [ ] Real-time dashboard updates.
- [ ] Advanced anomaly model comparison.
- [ ] Role-based permissions.
- [ ] Rich agent trace visualization.
- [ ] Provider switch: Gemini ↔ Claude.
- [ ] Production-like container deployment.
