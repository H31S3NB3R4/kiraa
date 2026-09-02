# AI Finance Controller — Product Requirements Document

**Project:** AI Finance Controller
**Track:** Razorpay Buildathon — Track 4
**Primary AI:** Gemini API with function/tool calling
**Status:** Buildathon MVP specification
**Audience:** Product, engineering, ML, AI-agent, and demo teams

---

## 1. Product Overview

AI Finance Controller is an agentic finance-operations system that lets a finance analyst interact with one controller instead of operating separate reconciliation, ledger, tax, forecasting, and anomaly-detection scripts.

The controller uses Gemini as the reasoning/orchestration layer and deterministic backend services as tools. Gemini decides which tools are needed, in what order, and when enough evidence has been collected to answer the analyst.

The product is designed for synthetic finance data and a buildathon demo, but its architecture is intentionally shaped like a production finance-operations system: evidence-first decisions, structured tool outputs, auditability, idempotency, human approval for ledger-changing actions, and measurable performance.

### Core proposition

> **An AI finance controller that investigates, reconciles, forecasts, explains, and safely proposes corrective actions across the finance stack.**

---

## 2. Problem Statement

Finance operations typically involve multiple data sources and repetitive verification work:

- Payment and settlement records need to be reconciled against expected amounts.
- Ledger entries must be queried and traced back to source transactions.
- Refunds, fees, duplicates, settlement timing differences, and other exceptions require investigation.
- Tax/GST values must match invoices and accounting records.
- Cash position needs to be forecast from historical inflows/outflows.
- Unusual transactions can exist even when they technically pass deterministic checks.
- Proposed accounting corrections need human review and an audit trail.

A fixed processing pipeline can calculate these things, but it cannot naturally handle open-ended questions such as:

> "Why is Tuesday's cash short?"

> "Reconcile this week's settlements and tell me which exceptions are material."

> "Forecast next week's cash and flag risks."

The system needs an agent that can reason over the request, select the correct deterministic tools, combine their outputs, and return an evidence-backed answer.

---

## 3. Goals

### 3.1 Primary goals

1. Build a true tool-calling finance controller rather than a fixed five-stage script.
2. Reconcile 50+ synthetic finance records accurately and measurably.
3. Support natural-language investigation of financial exceptions.
4. Provide deterministic cash-flow forecasting through a dedicated backend tool.
5. Add ML-based anomaly detection as a second signal alongside rule-based checks.
6. Support GST/tax matching where relevant to an investigation.
7. Generate human-reviewable journal-entry proposals without allowing uncontrolled ledger mutation.
8. Maintain an audit trail for agent runs, tool calls, decisions, approvals, and actions.
9. Expose measurable metrics: accuracy, throughput, latency, tool success rate, and unresolved exceptions.
10. Deliver a polished finance-control dashboard suitable for a five-minute buildathon demo.

### 3.2 Secondary goals

- Keep the agent provider-agnostic so Gemini can later be replaced by Claude or another function-calling model.
- Use structured tool contracts so backend calculations remain deterministic and testable.
- Make the synthetic-data generator capable of injecting known exceptions for evaluation.
- Design the system so event-driven processing can be added later without rebuilding the core services.

---

## 4. Non-Goals for MVP

The MVP will **not** attempt to:

- Move real customer money.
- Connect to real production bank accounts or production accounting ledgers.
- Automatically post irreversible financial transactions without human approval.
- Provide legal or tax advice.
- Guarantee production accounting compliance.
- Build a fully generalized ERP.
- Solve every global tax jurisdiction.
- Implement multi-currency before the core controller is stable.

---

## 5. Target User

### Primary persona: Finance Analyst / Finance Operations Analyst

The analyst needs to:

- reconcile settlements,
- investigate discrepancies,
- understand why cash moved,
- identify risky or anomalous transactions,
- check tax mismatches,
- forecast upcoming cash requirements,
- and prepare corrections for approval.

The analyst should not need to know which backend tool is required.

---

## 6. User Experience

### 6.1 Natural-language controller

Example queries:

- "Reconcile this week's settlements."
- "Why is Tuesday's settlement short?"
- "Show me the top five exceptions by financial impact."
- "Check the GST for transaction TXN-1042."
- "Forecast cash for the next seven days and flag risk."
- "Investigate the fee mismatches from Tuesday."
- "Prepare a correction for the highest-confidence mismatch."

### 6.2 Dashboard

The application should present:

- Cash position.
- Reconciliation match rate.
- Exception count.
- High-risk financial impact.
- Seven-day forecast chart.
- Exception table.
- Anomaly indicators.
- GST/tax mismatch indicators.
- Recent controller runs.
- Proposed actions awaiting approval.
- Audit trail.
- Controller chat/input panel.

The chat must feel like a control interface, not a generic chatbot.

---

## 7. Core Functional Requirements

### FR-1 — Controller Agent

The backend must expose an agent loop that:

1. receives analyst input,
2. provides Gemini with system instructions and tool definitions,
3. allows Gemini to request one or more tools,
4. executes requested tools,
5. returns tool results to Gemini,
6. allows additional tool calls when required,
7. stops when the model returns a final response,
8. records the full run and tool-call sequence.

### FR-2 — Reconciliation Tool

`run_reconciliation()` must:

- accept merchant/date-range/filter parameters,
- compare transaction and settlement data,
- calculate match status,
- detect amount/timing/fee/refund/duplicate issues,
- return structured results,
- expose aggregate metrics,
- return exception-level evidence.

### FR-3 — Ledger Query Tool

`query_ledger()` must:

- support transaction ID, date range, merchant, category, and status filters,
- return source-linked ledger records,
- support investigative queries without allowing mutation.

### FR-4 — Cash Forecast Tool

`forecast_cashflow()` must:

- aggregate historical cash inflows/outflows,
- generate a deterministic seven-day forecast,
- expose forecast values and confidence/risk metadata,
- identify drivers of risk where possible,
- never ask the LLM to invent numerical forecasts.

### FR-5 — GST Match Tool

`check_gst_match()` must compare expected and recorded tax values and return:

- taxable value,
- expected tax,
- recorded tax,
- difference,
- matching status,
- source references.

### FR-6 — ML Anomaly Tool

`detect_anomalies()` must:

- compute model-based anomaly scores,
- use transaction features such as amount, timing, frequency, settlement delay, fee ratio, and refund behavior,
- return anomaly score and reason metadata,
- operate alongside deterministic rules rather than replacing them.

### FR-7 — Journal Proposal Tool

`propose_journal_entry()` must:

- generate a structured correction proposal,
- include amount, debit account, credit account, reason, source evidence, and confidence,
- never commit the change automatically.

### FR-8 — Human Approval

The UI must require explicit approval before a proposed ledger-changing action is sent to the mock ledger API.

### FR-9 — Audit Trail

Every agent run must capture:

- request ID,
- timestamp,
- analyst/user identifier for the demo,
- model/provider,
- prompt/run metadata,
- tool sequence,
- tool inputs and normalized outputs,
- final response,
- proposed action,
- approval decision,
- resulting state change.

Sensitive production credentials must never be stored in the audit log.

---

## 8. Agent Behavior Requirements

The model must be instructed to:

- use tools for financial facts,
- never fabricate figures,
- prefer evidence before explanation,
- use the minimum necessary tools when possible,
- ask for missing information only when genuinely required,
- distinguish deterministic results from model interpretation,
- surface unresolved exceptions honestly,
- avoid state-changing tools unless the user has explicitly requested a proposal or approval flow,
- never claim an action succeeded when the backend failed.

### Example controller reasoning pattern

User: `Why is Tuesday's cash short?`

Possible tool sequence:

```text
query_ledger()
        -> identify relevant settlement records
run_reconciliation()
        -> quantify discrepancy
check_gst_match()
        -> test tax-related cause when relevant
forecast_cashflow()
        -> assess whether the issue changes near-term liquidity risk
```

The exact sequence is decided by the agent based on the available tool contracts and returned evidence.

---

## 9. Data Model Requirements

### Core entities

- Merchant
- Customer
- PaymentTransaction
- Settlement
- Refund
- Fee
- Invoice
- LedgerEntry
- CashFlowRecord
- AnomalyScore
- ReconciliationException
- AgentRun
- ToolCall
- JournalProposal
- Approval
- AuditEvent

### Example transaction record

```json
{
  "transaction_id": "TXN-1042",
  "merchant_id": "M001",
  "timestamp": "2026-08-25T14:32:11Z",
  "currency": "INR",
  "amount": 11800.0,
  "fee": 236.0,
  "refund_amount": 0.0,
  "customer_id": "C091",
  "settlement_id": "SET-5501",
  "invoice_id": "INV-8821"
}
```

---

## 10. Tool Contracts

### `run_reconciliation`

```json
{
  "name": "run_reconciliation",
  "description": "Reconcile transactions against settlement and ledger records for a merchant and date range.",
  "parameters": {
    "merchant_id": "string",
    "start_date": "string",
    "end_date": "string"
  }
}
```

### `query_ledger`

```json
{
  "name": "query_ledger",
  "description": "Read-only query over ledger entries and related financial records.",
  "parameters": {
    "merchant_id": "string",
    "transaction_id": "string|null",
    "start_date": "string|null",
    "end_date": "string|null",
    "status": "string|null"
  }
}
```

### `forecast_cashflow`

```json
{
  "name": "forecast_cashflow",
  "description": "Produce a deterministic seven-day cash-flow forecast from historical financial data.",
  "parameters": {
    "merchant_id": "string",
    "horizon_days": "integer"
  }
}
```

### `check_gst_match`

```json
{
  "name": "check_gst_match",
  "description": "Compare expected GST on an invoice or transaction with recorded tax data.",
  "parameters": {
    "transaction_id": "string"
  }
}
```

### `detect_anomalies`

```json
{
  "name": "detect_anomalies",
  "description": "Score transactions for statistical unusualness using the trained anomaly model.",
  "parameters": {
    "merchant_id": "string",
    "transaction_ids": "array<string>|null",
    "limit": "integer"
  }
}
```

### `propose_journal_entry`

```json
{
  "name": "propose_journal_entry",
  "description": "Create a reviewable journal-entry proposal based on verified financial evidence. Does not post the entry.",
  "parameters": {
    "exception_id": "string",
    "reason": "string"
  }
}
```

---

## 11. ML Requirements

### Anomaly model

Initial model: **Isolation Forest**.

Candidate features:

- transaction amount,
- log(transaction amount),
- hour of day,
- day of week,
- settlement delay,
- fee ratio,
- refund ratio,
- customer transaction frequency,
- merchant transaction frequency,
- rolling average amount,
- deviation from merchant baseline.

### Training strategy

Use synthetic normal-history data as the baseline and inject controlled anomalous records for evaluation.

The model should be saved/versioned so a demo run can reproduce results.

---

## 12. Forecasting Requirements

The forecasting module must be independent from Gemini.

Recommended MVP approach:

1. aggregate daily inflows and outflows,
2. calculate rolling averages/trends,
3. use a deterministic forecasting model,
4. calculate projected ending cash,
5. compare against a configurable operating threshold,
6. classify risk as LOW / MEDIUM / HIGH.

The LLM's responsibility is interpretation and explanation, not arithmetic.

---

## 13. Synthetic Data and Evaluation

The project must include a repeatable dataset generator.

### Baseline

At least 50 records; preferably 500–1,000 for more meaningful metrics.

### Injected scenarios

- correct match,
- fee mismatch,
- refund discrepancy,
- duplicate transaction,
- settlement timing difference,
- missing settlement,
- GST mismatch,
- statistically unusual but correctly reconciled transaction,
- ledger-record mismatch,
- recoverable downstream write failure.

### Metrics

Report:

- reconciliation match accuracy,
- exception precision/recall where labels are available,
- anomaly precision/recall,
- false-positive rate,
- tool success rate,
- average agent latency,
- throughput records/minute,
- unresolved exception count,
- proposed-action approval rate in demo data.

---

## 14. Reliability Requirements

### Tool failure handling

If a tool fails:

- return a structured error,
- log the failure,
- allow retry for transient errors,
- tell the agent the tool failed,
- prevent the agent from inventing a result.

### Idempotency

Every write request must include an idempotency key.

The same request processed twice must not create duplicate ledger entries.

### State

Agent runs should have a run ID so multi-turn interactions can retrieve relevant conversation state and tool results.

---

## 15. Security and Safety

- API keys stored in environment variables/secrets.
- No credentials in prompts or logs.
- Read-only tools separated from write tools.
- Human approval required for ledger mutation.
- Audit trail for every state-changing operation.
- Rollback path for mock ledger actions.
- Explicit distinction between synthetic demo data and real financial records.

---

## 16. API Surface

Suggested FastAPI routes:

```text
POST /api/agent/chat
POST /api/reconciliation/run
GET  /api/ledger/query
GET  /api/forecast
GET  /api/anomalies
GET  /api/exceptions
GET  /api/runs/{run_id}
POST /api/actions/{proposal_id}/approve
POST /api/actions/{proposal_id}/reject
POST /api/actions/{proposal_id}/rollback
GET  /api/audit
GET  /api/metrics
```

The frontend should normally call `/api/agent/chat` for conversational work and dedicated read APIs for dashboard data.

---

## 17. UX Acceptance Criteria

A reviewer should be able to:

1. Open the dashboard and immediately understand the current financial state.
2. Ask a finance question in natural language.
3. See the controller execute one or more tools.
4. See evidence supporting the answer.
5. Drill into an exception.
6. Run or view a seven-day cash forecast.
7. See an ML anomaly that deterministic checks did not catch.
8. Generate a correction proposal.
9. Approve/reject the proposal.
10. View an audit record of the full operation.

---

## 18. Buildathon Demo Scenario

### Scenario A — Weekly reconciliation

User: `Reconcile this week's settlements.`

Expected:

- agent calls reconciliation,
- dashboard updates match rate,
- exceptions appear,
- totals are sourced from the tool.

### Scenario B — Root-cause investigation

User: `Why is Tuesday's cash short?`

Expected:

- agent queries ledger,
- identifies relevant exception(s),
- optionally checks GST/refunds/fees depending on evidence,
- explains the root cause with amounts and record IDs.

### Scenario C — Forecast

User: `Forecast next week's cash and flag risk.`

Expected:

- forecast tool executes,
- seven-day graph appears,
- risk level and key drivers are shown.

### Scenario D — Hidden anomaly

Show a transaction that passes deterministic reconciliation.

Expected:

- anomaly tool flags statistical unusualness,
- agent explains the anomaly signal separately from the reconciliation result.

### Scenario E — Corrective action

User: `Prepare the correction for the highest-confidence exception.`

Expected:

- proposal generated,
- human approval required,
- mock ledger updated only after approval,
- audit event generated.

---

## 19. Success Criteria

The MVP is considered successful when:

- the controller performs real Gemini tool calls,
- at least four deterministic finance tools are integrated,
- at least 50 synthetic records are evaluated,
- metrics are displayed or exportable,
- the same tool outputs are reproducible outside the LLM,
- exceptions are not hidden,
- a human can approve/reject a mock correction,
- the dashboard demonstrates an end-to-end finance-operations loop.

---

## 20. Future Scope

- streaming/event-driven settlement ingestion,
- multi-currency FX-aware reconciliation,
- real payment/ERP/accounting integrations,
- policy-based automatic approval for very low-risk actions,
- richer anomaly ensembles,
- role-based access control,
- multi-merchant support,
- deeper tax support,
- agent evaluation and tracing platform.

---

## 21. Product Principle

**LLM for reasoning. Deterministic services for financial truth. Human approval for financial mutation. Audit everything.**
