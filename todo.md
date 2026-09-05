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
- [x] Create React/Vite frontend. *(React 19 + Vite 6 + TypeScript scaffold; Phase 10 dashboard)*
- [x] Add Tailwind CSS. *(Tailwind 3, slate/indigo design system in `frontend/src/index.css`)*
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

- [x] Create SQLAlchemy models. *(`backend/app/models/` — 7 domain modules, SA 2.0 `Mapped[]` style; `Numeric(14,2)` money, `Numeric(8,6)` rates)*
- [x] Create database tables. *(created by seed script / `app.db.session.create_all`; 17 tables incl. `dataset_labels`)*
- [x] Add indexes for transaction IDs, settlement IDs, dates, merchant IDs. *(see each model's `__table_args__` + `index=True`)*
- [x] Add foreign keys. *(hard FKs on child tables; `transactions.settlement_id/invoice_id` are indexed soft refs — settlements/invoices load after transactions; SQLite `PRAGMA foreign_keys=ON` enforced per connection)*
- [x] Add timestamps. *(`TimestampMixin`: `created_at`/`updated_at` server defaults on every table)*
- [x] Add audit tables. *(agent_runs, tool_calls, journal_proposals, approvals, audit_events)*
- [x] Add database seed script. *(`backend/scripts/seed_db.py` → `app/services/db_seed.py`; guards double-seeding, `--recreate`, `--dataset`, `--database-url`)*
- [x] Confirm dataset can be recreated from scratch. *(21 tests in `test_phase2_db.py`: generate → write JSON → load → seed → verify counts, FK enforcement, labels vs CSV, demo anchors)*

### Required tables

- [x] merchants
- [x] customers
- [x] transactions
- [x] settlements
- [x] refunds
- [x] fees
- [x] invoices
- [x] ledger_entries
- [x] cash_flows
- [x] reconciliation_exceptions
- [x] anomaly_scores
- [x] agent_runs
- [x] tool_calls
- [x] journal_proposals
- [x] approvals
- [x] audit_events

Plus `dataset_labels` (17th table): Phase 1 ground truth stored in the DB so
the Phase 12 evaluation harness can score engine output with SQL joins.

---

# Phase 3 — Deterministic Finance Engine

## 3.1 Reconciliation

- [x] Implement transaction/settlement normalization. *(`round2` money normalization + `coerce_date` in `backend/app/tools/common.py`; Decimal vs float handled via `float()`)*
- [x] Implement transaction ID matching. *(settlement/refund/fee/invoice/ledger joined per `transaction_id` in `app/tools/reconciliation.py`)*
- [x] Implement amount tolerance. *(half-paise `MONEY_TOLERANCE = 0.005` — exact for the paise-exact dataset, robust to float artefacts)*
- [x] Implement fee comparison. *(expected fee = `round2(amount x merchant.fee_rate)`; catches the 0.5% overcharge exactly)*
- [x] Implement refund comparison. *(recorded vs expected refund; catches the 15% overpay exactly)*
- [x] Implement settlement timing comparison. *(due = T+2; `exception_date` anchors on the due date; catches T+5 injections)*
- [x] Implement duplicate detection. *(same merchant/customer/amount within a 10-minute window; chain-clustered; both pair members flagged like the ground truth)*
- [x] Implement exception classification. *(9-way first-hit-wins taxonomy: MISSING_SETTLEMENT, FEE_MISMATCH, AMOUNT_MISMATCH, SETTLEMENT_TIMING_MISMATCH, REFUND_MISMATCH, FAILED_LEDGER_WRITE, LEDGER_MISMATCH, GST_MISMATCH, DUPLICATE_TRANSACTION)*
- [x] Calculate financial impact per exception. *(signed `recorded - expected` exposure; missing/delayed net for settlement issues; charged amount for duplicates)*
- [x] Return aggregate reconciliation metrics. *(transactions/matched/exceptions/by_type/total_financial_impact/match_rate_pct)*
- [x] Add unit tests. *(27 tests in `backend/tests/test_phase3_engine.py`; 100% precision & recall vs the 9 ground-truth exception rows — also verified 45/45 on the 500-txn benchmark)*

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

- [x] Implement read-only transaction query. *(`app/tools/ledger.py` — pure SELECT, never mutates; joins ledger->transaction->merchant)*
- [x] Support date range. *(start/end inclusive, ISO strings or `date`)*
- [x] Support transaction ID.
- [x] Support merchant ID.
- [x] Support status. *(plus account and merchant-category filters)*
- [x] Return source references. *(settlement_id + invoice_id attached to every row)*
- [x] Add indexes. *(already present on `ledger_entries` from Phase 2 — `ix_ledger_entries_date`, `ix_ledger_entries_merchant_date`, `transaction_id` index)*
- [x] Add unit tests. *(filter combinations, limit/truncation, inverted-range guard)*

## 3.3 GST matching

- [x] Implement expected GST calculation. *(expected tax = `round2(total x rate / (1 + rate))` — mirrors the generator's invoice decomposition to the paise)*
- [x] Compare with recorded GST.
- [x] Return exact difference.
- [x] Include invoice/transaction source IDs. *(sources: transaction_id, invoice_id, settlement_id)*
- [x] Add mismatch test cases. *(matched, 8%-error mismatch, unknown-transaction)*

---

# Phase 4 — Forecasting Module

- [x] Aggregate daily cash inflows. *(pooled or per-merchant per-day `SUM` over `cash_flows` in `app/tools/forecast.py::_daily_history` — round2 per day, dense daily rows)*
- [x] Aggregate daily cash outflows. *(same pass; anchor-day closing balances summed for the projection start point)*
- [x] Calculate historical rolling averages. *(trailing `history_days` (default 28) daily averages plus a recent 7-day rolling window; week-over-week `net_trend_per_day` driver)*
- [x] Add initial forecast model. *(flat `recent-rolling-average` model: the recent 7-day averages are projected forward unchanged — deliberately deterministic, no LLM arithmetic)*
- [x] Produce seven-day forecast. *(default `horizon_days=7`, configurable 1-30; consecutive dates from the anchor day)*
- [x] Calculate projected ending balance. *(running `round2` balance walk mirroring the generator's arithmetic; `projected_ending_balance`, `min_projected_cash`/`min_projected_date`)*
- [x] Add operating threshold configuration. *(`OPERATING_THRESHOLD` setting (default 50000) in `app/config.py` + `.env.example`; per-call `operating_threshold` argument overrides)*
- [x] Implement LOW/MEDIUM/HIGH risk classification. *(HIGH if any projected day < threshold; MEDIUM if the minimum is within 25% above it; else LOW — with `breach_days`, `first_breach_date`, `headroom`, `headroom_pct`)*
- [x] Return forecast drivers. *(daily/recent averages, trend, volatility CV + confidence label, min/first-breach/headroom, anchor balance/date, `sources` — everything the LLM needs to explain, never invent, the numbers)*
- [x] Add tests for deterministic outputs. *(29 tests in `backend/tests/test_phase4_forecast.py`; expectations re-derived independently from the dataset's own `cash_flows` rows — Phase 3 pattern)*
- [x] Create a chart-ready response schema. *(`app/api/schemas/forecast.py` — pydantic v2 `ForecastResponse` + per-day `ForecastPoint`; guard envelopes validate against the same schema)*

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

- [x] Build feature engineering pipeline. *(`backend/ml/features.py` — one canonical feature-record shape shared by training (generator dicts) and serving (DB rows); 4 model features: `median_ratio`, `hour`, `settle_delay` (NaN = settlement not received), `fee_ratio`)*
- [x] Generate historical normal training data. *(pure-normal synthetic history via the Phase 1 generator — seed 202, 2000 transactions over 56 days, `exceptions_per_type=0`; the model never sees a labelled anomaly)*
- [x] Train Isolation Forest. *(`backend/ml/train_anomaly.py::train_model` — 300 trees, `max_samples=1024`, fixed `random_state`; fully deterministic, never touches the wall clock)*
- [x] Save model artifact/version. *(versioned joblib bundle `ml/artifacts/iforest-v1.joblib` — model + merchant medians + threshold + feature names + training summary — written by `backend/scripts/train_anomaly.py`; gitignored, with a deterministic in-process retrain fallback so results reproduce with or without it)*
- [x] Implement scoring function. *(`score_records` — `anomaly_score = -score_samples` rounded to 4 dp, `is_anomaly = score >= threshold`, severity: high at/above the threshold, medium within the 0.05 watch margin, else low)*
- [x] Calibrate anomaly threshold on synthetic validation data. *(threshold = p99.9 of 2600 pooled normal scores — training set plus two exception-free validation sets, seeds 303/304 — cutting at 0.6947 keeps every rare-but-legitimate normal corner below the flag)*
- [x] Add anomaly reason metadata. *(`explain_record` — amount vs merchant median, hour, weekday, settlement delay, fee/refund ratios, customer — plus a one-line human-readable `reason`; persisted as JSON in `anomaly_scores.reasons`)*
- [x] Add batch scoring. *(`detect_anomalies` in `backend/app/tools/anomalies.py` — `merchant_id`/`transaction_ids` filters, score-descending results, `limit` caps returned rows while metrics describe the full scan, `unknown_merchant`/`no_transactions` guards)*
- [x] Add unit tests. *(32 tests in `backend/tests/test_phase5_anomalies.py` — exact feature values, threshold/severity bands, filters, guards, invalid-argument errors, persistence idempotency, schema round-trip, serving determinism, artifact/retrain parity)*
- [x] Measure precision/recall and false-positive rate. *(ground-truth metrics vs `dataset_labels.anomaly` returned by every scan: 100% precision, 100% recall, 0.0 false-positive rate on the dev dataset, the 500-txn benchmark, and unseen seeds; ML flags stay disjoint from reconciliation exceptions by design)*

### Important demo case

Create at least one record where:

```text
Reconciliation = PASS
Anomaly score  = HIGH
```

This clearly demonstrates why the ML layer adds value.

**Satisfied**: the injected hidden anomaly — 7.18x its merchant's median
amount at 03:xx UTC with perfectly consistent books — reconciles clean
(`reconciliation_pass: true`) yet scores HIGH above the calibrated
threshold. `detect_anomalies` cross-links both verdicts on the same row,
which is exactly the value the ML layer adds beyond deterministic rules.

---

# Phase 6 — Gemini Tool-Calling Controller

## Provider adapter

- [x] Create `LLMProvider` interface. *(app/agent/providers/base.py: generate(messages, tools) + provider-agnostic message shapes)*
- [x] Implement Gemini provider. *(providers/gemini.py over google-genai; AFC disabled, call-id echo, bounded retry on 429/5xx)*
- [x] Keep Gemini-specific request/response parsing isolated. *(controller speaks only base.py types; SDK types never escape the adapter)*
- [x] Add environment-based model configuration. *(GEMINI_API_KEY / GEMINI_MODEL; a missing key yields a safe 503)*

## Tool schemas

- [x] Define `run_reconciliation` schema.
- [x] Define `query_ledger` schema.
- [x] Define `forecast_cashflow` schema.
- [x] Define `check_gst_match` schema.
- [x] Define `detect_anomalies` schema.
- [x] Define `propose_journal_entry` schema. *(all six live in tool_registry.py TOOL_REGISTRY with READ/PROPOSE permission classes)*

## Controller loop

- [x] Send user message to Gemini.
- [x] Provide tool definitions.
- [x] Detect function/tool calls.
- [x] Validate tool arguments.
- [x] Dispatch through registry. *(single dispatch_tool entry point — no if/else chains in the loop)*
- [x] Append tool results to conversation.
- [x] Continue until final response.
- [x] Add bounded maximum tool calls per run. *(AGENT_MAX_TOOL_CALLS, default 12; excess calls never dispatched, run ends tool_limit with a deterministic summary)*
- [x] Handle malformed tool arguments. *(structured INVALID_ARGUMENTS error envelope, rolled back session)*
- [x] Handle tool exceptions. *(structured UNKNOWN_TOOL / VALIDATION_ERROR / TOOL_FAILURE envelopes — the model is told a tool failed)*
- [x] Persist run and tool-call data. *(agent_runs + tool_calls rows, one per executed call)*
- [x] Return final response plus evidence/tool metadata. *(run_id, status, answer, tools_used, tool_calls trace, record-id evidence, latency)*

## System prompt

- [x] Add evidence-first rules. *(prompts.py rule 3/4: prefer evidence, explain via record IDs and tool-computed amounts)*
- [x] Add no-fabrication rule. *(rule 1: every number comes from a tool result)*
- [x] Add safe-write rule. *(rule 7: propose_journal_entry only drafts; posting requires human approval)*
- [x] Add unresolved-exception rule. *(rule 9: keep unresolved exceptions visible)*
- [x] Add distinction between deterministic and ML signals. *(rule 5 + per-tool guidance; detect_anomalies "alongside, never replacing")*

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

- [x] Create `run_id`. *(RUN-{12-hex-uuid}, returned on every response; passing it back continues the same run — unknown run_id raises 404 AgentRunNotFoundError, never a silent new conversation)*
- [x] Persist user messages. *(agent_messages rows: role=user, content={"text": ...}, monotonic per-run seq)*
- [x] Persist model requests/responses. *(role=model rows carry {"text", "tool_calls", "latency_ms"} — tool-request rounds and final answers both persisted; role=model error fallback carries the safe answer text)*
- [x] Persist tool calls/results. *(tool_calls rows keyed by the same run_id with their own seq — visible to all later turns and to the run-wide tool-limit summary)*
- [x] Reuse context for follow-up messages. *(controller.run(..., run_id=...) replays the saved transcript before the new message; POST /api/agent/chat accepts run_id)*
- [x] Test:

```text
User: Reconcile this week.
User: What are the biggest exceptions?
User: Investigate the top one.
```
*(test_three_turn_investigation_accumulates_tool_calls — same run_id across all three turns, tool_call_count and latency accumulate, turn_count reaches 3)*

- [x] Ensure later questions can use earlier retrieved context. *(turn 3's replay includes turn 1-2 texts and every tool-request round of prior multi-round turns, verbatim and in order)*
- [x] Prevent uncontrolled context growth. *(AGENT_MAX_HISTORY_MESSAGES, default 40 — bounded most-recent replay that never orphans a tool-request round from its result pair; run-wide tool budget AGENT_MAX_TOOL_CALLS spans turns, tool_limit turns persist a deterministic summary and the run stays continuable)*

---

# Phase 8 — Action Layer

## Journal proposal

- [x] Implement proposal schema. *(journal_proposals rows drafted by the Phase 6 `propose_journal_entry` tool; Phase 8 adds the decision side — `approvals.idempotency_key` under a unique index + `ledger_entry_id` linking the posted correction entry)*
- [x] Validate amount/account fields. *(approve re-validates server-side: positive amount, distinct non-empty debit/credit accounts, linked transaction — `ActionValidationError` → 422; the model-drafted payload is never trusted)*
- [x] Include evidence IDs. *(evidence_ids carry the source transaction + settlement/invoice refs since Phase 6; Phase 8 audit events add before/after states)*
- [x] Include confidence. *(severity-derived confidence on every proposal since Phase 6, surfaced in the tool payload)*
- [x] Generate proposal from a verified exception. *(one pending proposal per verified exception, deduplicated while pending — PRD section 14)*

## Approval

- [x] Add approve endpoint. *(POST /api/actions/{proposal_id}/approve → app/services/actions.py + app/api/routes/actions.py; routes stay thin so the service is unit-testable)*
- [x] Add reject endpoint. *(POST .../reject records the decision + audit event and never touches the ledger)*
- [x] Add mock ledger API. *(approve posts exactly one `LE-MOCK-` correction `LedgerEntry` — status='posted', prefix can never collide with the seeded LE-3xxx sequence)*
- [x] Add idempotency key. *(required on every write body (8–64 chars, pydantic-validated); unique index on approvals.idempotency_key + prior-rollback-event lookup make every write replay-safe)*
- [x] Ensure duplicate approval does not double-post. *(same key replays the stored outcome — idempotent_replay=true, no second entry/approval, one replay-marker audit event; re-deciding under a different key → 409)*
- [x] Create audit event on approval. *(proposal.approve with before/after states, actor, object, and run link)*
- [x] Create audit event on rejection. *(proposal.reject carries the optional reviewer note in after_state)*
- [x] Implement rollback endpoint. *(POST .../rollback — approved → rolled_back; the posted entry flips to reversed (append-only, stays queryable), duplicate keys replay, and keys spent on other writes/proposals are refused)*
- [x] Simulate failure and test recovery. *(simulate_failure=true → MockLedgerError/502: no approval, no entry, no audit event applied; the same idempotency key succeeds on retry)*

### Critical rule

- [x] Confirm that no natural-language request can bypass human approval. *(the six model-callable tools contain no action verbs and TOOL_REGISTRY has no WRITE-class callable; a jailbreak chat asking to `post_journal_entry` leaves proposals pending and the ledger untouched — the agent layer never imports app.services.actions)*

---

# Phase 9 — FastAPI APIs

- [x] `POST /api/agent/chat` *(Phase 6/7; provider built from settings, 503 when GEMINI_API_KEY is unset, 404 on unknown run_id)*
- [x] `POST /api/reconciliation/run` *(wraps the deterministic engine; optional merchant/date scope + persist flag; response mirrors the tool payload enriched with persisted exception_ids via the shared registry helper; ValueError → 422; idempotent upserts — new/updated counts, never duplicates)*
- [x] `GET /api/ledger/query` *(HTTP twin of the READ tool: merchant/transaction/date/status/account/category filters, source-linked rows, limit+truncated; inverted ranges → 422)*
- [x] `GET /api/forecast` *(wraps forecast_cashflow — pooled or per-merchant, horizon/history/threshold params; guard envelopes unknown_merchant/no_history return 200; out-of-range horizons → 422)*
- [x] `GET /api/anomalies` *(wraps detect_anomalies with persist=False — a GET never writes anomaly_scores; merchant/transaction filters, limit; guard envelopes 200; limit=0 → 422)*
- [x] `GET /api/exceptions` *(lists persisted reconciliation_exceptions joined to transactions (merchant scope), newest first, type/severity/status/txn/date filters, limit+truncated; inverted ranges → 422)*
- [x] `GET /api/runs/{run_id}` *(one agent run + tool-call trace + full transcript; unknown ids → 404)*
- [x] `POST /api/actions/{proposal_id}/approve` *(Phase 8)*
- [x] `POST /api/actions/{proposal_id}/reject` *(Phase 8)*
- [x] `POST /api/actions/{proposal_id}/rollback` *(Phase 8)*
- [x] `GET /api/audit` *(append-only audit_events trail — action/actor/object/run filters, newest first, before/after states)*
- [x] `GET /api/metrics` *(KPI cards: pooled closing-balance cash as of the latest cash-flow day, fresh read-only reconciliation pass (match rate / exception count / impact at risk), pending proposals; unknown merchant → 404)*

---

# Phase 10 — Frontend Dashboard

## Shell

- [x] Finance Control Center layout. *(React 19 + Vite + TS; sidebar shell + scrollable main + right agent aside, `App.tsx`)*
- [x] Sidebar/top navigation. *(6 NavLink routes with lucide icons: Dashboard, Reconciliation, Forecast, Anomalies, Actions, Audit)*
- [x] Current merchant selector if needed. *(global header select fed by `GET /api/merchants`)*
- [x] Global date range. *(From/To date inputs; merchant + dates flow to every page via `ScopeContext`)*
- [x] Agent chat panel. *(right `w-96` aside — AgentPanel, hidden below `lg`)*

## KPI cards

- [x] Total cash. *(from `GET /api/metrics`)*
- [x] Reconciliation match rate.
- [x] Exception count.
- [x] Financial impact at risk. *(plus top-open-exceptions preview ranked by impact)*

## Reconciliation view

- [x] Matched count. *(run-response aggregates)*
- [x] Unmatched count.
- [x] Match rate.
- [x] Exception list. *(persisted rows from `GET /api/exceptions`)*
- [x] Filters. *(severity + status selects; plus "run fresh reconciliation" button → `POST /api/reconciliation/run`)*

## Forecast view

- [x] Seven-day line chart. *(recharts LineChart over the chart-ready series; horizon selector)*
- [x] Risk indicator. *(LOW/MEDIUM/HIGH badge + risk reason)*
- [x] Projected minimum cash. *(min of the projected walk vs operating threshold)*
- [x] Forecast drivers. *(inflow/outflow components, net trend, history window, threshold)*

## Anomaly view

- [x] Anomaly score. *(from `GET /api/anomalies`, read-only scoring)*
- [x] Transaction details. *(amount vs merchant baseline, severity band, reason metadata)*
- [x] Compare normal vs unusual attributes. *(model-baseline stats vs observed per feature)*

## Action view

- [x] Journal proposal card. *(from `GET /api/proposals` with status badges)*
- [x] Evidence section. *(record ids + narrative verbatim from the proposal payload)*
- [x] Approve button. *(→ `POST /api/actions/{id}/approve`)*
- [x] Reject button. *(→ `POST /api/actions/{id}/reject`)*
- [x] Rollback status. *(→ `POST /api/actions/{id}/rollback`; idempotency-keyed, audit-trailed)*

## Audit view

- [x] Run history. *(run rows from `GET /api/runs`, expandable)*
- [x] Tool sequence. *(per-run `GET /api/runs/{run_id}` trace: args, latency, status, impact)*
- [x] Action history. *(audit-event table incl. proposal approve/reject/rollback)*
- [x] Approval history. *(decisions with actor, idempotency key, timestamp)*

---

# Phase 11 — Agent Experience / Demo Polish

- [x] Show which tools are being used. *(tool timeline in AgentPanel + per-run trace in AuditPage)*
- [x] Show tool status: running / completed / failed. *(status badge, latency ms, error text per call)*
- [x] Show evidence links in agent responses. *(evidence chips for record ids touched by the run)*
- [x] Show record IDs and monetary impact. *(ids verbatim; impact figures surfaced from tool output)*
- [x] Clearly label ML anomaly vs deterministic exception. *(distinct badges — `ML_TOOLS` vs deterministic engine tools, in both AgentPanel and AuditPage)*
- [x] Make final response concise and finance-oriented. *(system-prompt behavioural contract, Phase 6)*
- [x] Add loading state. *(shared `Loading` component on every page; chat "thinking" state)*
- [x] Add error state. *(shared `ErrorState` with retry on every page)*
- [x] Add empty state. *(shared `EmptyState` with guidance text on every page)*
- [x] Add sample prompts on first load. *(clickable `SAMPLE_PROMPTS` in AgentPanel empty state)*

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

- [x] Create fixed benchmark dataset. *(data/benchmark/benchmark.json — 500 transactions / 56 days / 5 exceptions per type / seed 42, regenerable byte-identical via backend/scripts/generate_dataset.py)*
- [x] Store ground-truth exception labels. *(dataset_labels table seeded with every injected scenario since Phase 1; benchmark JSON + _labels.csv on disk)*
- [x] Run reconciliation benchmark. *(evaluate_engines → run_reconciliation(persist=False) scored against dataset_labels.recon_exception)*
- [x] Measure match accuracy. *(match_accuracy_pct over all labelled transactions — 100% on the benchmark)*
- [x] Measure exception precision/recall. *(exception_precision_pct / exception_recall_pct — 100%/100%; exact-type accuracy over detected exceptions)*
- [x] Run anomaly benchmark. *(detect_anomalies(persist=False) scored through its ground-truth block against dataset_labels.anomaly)*
- [x] Measure anomaly precision/recall. *(precision_pct / recall_pct — 100%/100%)*
- [x] Measure false-positive rate. *(false_positive_rate_pct — 0%)*
- [x] Measure agent latency. *(offline scripted provider over the real controller+tools+persistence; average_latency_ms + latency_by_run_ms)*
- [x] Measure average tool calls/run. *(tool_calls_per_run from executed tool_calls rows)*
- [x] Measure throughput. *(throughput_records_per_min across the full harness pass)*
- [x] Track tool failure rate. *(failed_tool_calls + tool_failure_rate_pct aggregated over executed calls)*
- [x] Track unresolved exceptions. *(unresolved_exception_transactions / unresolved_anomaly_transactions — recall misses stay listed, never hidden behind a percentage)*
- [x] Generate a machine-readable evaluation report. *(evaluation_report dict + backend/scripts/run_evaluation.py --json-only writes the JSON next to the benchmark)*
- [x] Display headline metrics in the dashboard/demo. *(GET /api/evaluation + Dashboard benchmark card: accuracy/precision/recall/FPR scored against ground truth, optional if unlabelled)*

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

- [x] Malformed tool arguments. *(structured INVALID_ARGUMENTS envelope, rolled-back session — phase 6 dispatch tests)*
- [x] Empty query. *(whitespace-only chat messages refused at the schema boundary with 422, no provider round, no agent_runs row — phase 6 + phase 13 tests)*
- [x] Invalid dates. *(inverted ranges raise ValueError → VALIDATION_ERROR envelopes and 422s across the tool + HTTP surfaces — phase 6/9 tests)*
- [x] Missing transaction ID. *(missing required arguments → INVALID_ARGUMENTS with the echoed arguments; check_gst_match refuses unknown transaction ids — phase 6 tests)*
- [x] Tool timeout. *(per-entry timeout_seconds + max_retries in TOOL_REGISTRY, isolated worker sessions, timed-out attempts retried, exhaustion → structured TOOL_TIMEOUT envelope, TOOL_TIMEOUT_SECONDS setting acts as a global ceiling that tightens but never loosens — phase 13 tests)*
- [x] Tool internal error. *(unexpected exceptions → TOOL_FAILURE envelope, rollback, session stays usable — phase 6 tests)*
- [x] Gemini API error. *(adapter retries 429/5xx with bounded backoff, gives up after max_attempts with LLMProviderError, fails fast on non-transient codes; controller ends the run model_error with a safe fallback answer — phase 13 provider tests + phase 6 controller tests)*
- [x] Duplicate approval. *(idempotency keys: replays return the stored outcome with one replay-marker audit event, never double-post; key reuse on other writes → 409 — phase 8 tests)*
- [x] Mock ledger failure. *(simulate_failure applies nothing — no approval, no ledger entry, no audit event; the same key succeeds on retry — phase 8 tests)*
- [x] Rollback after failure. *(append-only reversal flips the entry to reversed, proposal rolled_back, audited, replays deduplicated — phase 8 tests)*
- [x] Agent exceeds maximum tool calls. *(AGENT_MAX_TOOL_CALLS spans turns; excess calls never dispatched, run ends tool_limit with a deterministic evidence-based summary — phase 6/7 tests)*
- [x] Database connection failure. *(engine per request via get_db; SQLAlchemy failures surface as structured 500s, never silent partial writes — every write path is transactional and the dispatch worker sessions roll back and close on error, verified by the phase 13 isolation tests)*
- [x] Re-run same dataset and confirm deterministic finance outputs. *(same seed → byte-identical dataset; reconciliation rerun idempotent, forecast/anomaly serving deterministic, two harness passes produce identical scores — phase 3/4/5/12 tests)*

---

# Phase 14 — Security / Safety Review

- [ ] Remove hard-coded API keys.
- [ ] Verify `.env` is ignored.
- [ ] Avoid credentials in logs.
- [x] Separate READ and WRITE permissions. *(READ/PROPOSE tools model-callable via TOOL_REGISTRY; WRITE lives only in app/services/actions.py behind POST /api/actions/*, never imported by the agent layer)*
- [x] Require human approval for mock ledger writes. *(pending → approved only through POST /api/actions/{id}/approve with approver + idempotency_key; no tool or chat path can post)*
- [x] Validate journal amounts server-side. *(approve re-checks positive amount, distinct non-empty accounts, and linked transaction — ActionValidationError → 422)*
- [x] Validate idempotency server-side. *(unique approvals.idempotency_key index + prior-event rollback lookup; duplicates replay, key reuse on other writes → 409 IdempotencyConflictError)*
- [x] Add audit record for every action. *(proposal.approve / proposal.reject / proposal.rollback events with actor, object, before/after states — plus replay-marker events for duplicate requests)*
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
