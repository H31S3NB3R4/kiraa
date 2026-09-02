# AI Finance Controller — System Architecture

## 1. Architecture Goals

The architecture must make the project feel like a genuine finance-operations controller rather than an LLM wrapper.

Key principles:

1. **Agentic orchestration:** Gemini chooses tools and their order.
2. **Deterministic financial computation:** reconciliation, forecasting, GST calculations, and ledger reads live in backend services.
3. **Evidence-first answers:** financial claims come from tool outputs.
4. **Safe writes:** state-changing actions require explicit human approval.
5. **Provider independence:** the agent interface should not be tightly coupled to Gemini-specific business logic.
6. **Observable runs:** every agent/tool/action event is traceable.
7. **Testable tools:** tools work independently of the LLM for unit and evaluation tests.

---

## 2. High-Level Architecture

```text
┌──────────────────────────────────────────────────────────────┐
│                        React Dashboard                       │
│                                                              │
│  KPIs │ Exceptions │ Forecast │ Anomalies │ Actions │ Chat  │
└──────────────────────────────┬───────────────────────────────┘
                               │ HTTPS
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                         FastAPI API                          │
│                                                              │
│ /agent/chat  /reconciliation  /ledger  /forecast  /audit    │
└───────────────┬──────────────────┬───────────────────────────┘
                │                  │
                │                  ▼
                │        ┌─────────────────────┐
                │        │  Controller Service  │
                │        │ Gemini Tool Calling  │
                │        └──────────┬──────────┘
                │                   │
                │        tool calls │ tool results
                │                   ▼
                │   ┌──────────────────────────────────────┐
                │   │              Tool Layer               │
                │   │                                      │
                │   │ reconciliation                        │
                │   │ ledger query                          │
                │   │ cash forecast                         │
                │   │ GST match                             │
                │   │ anomaly detection                     │
                │   │ journal proposal                      │
                │   └───────┬────────┬────────┬─────────────┘
                │           │        │        │
                │           ▼        ▼        ▼
                │      Finance DB  ML Model  Forecast Engine
                │           │        │        │
                └───────────┴────────┴────────┴────────────────┐
                                                              │
                                                        PostgreSQL
                                                              │
                                                              ▼
                                                   Audit / Run Records

                              Safe write path

User ──approve──> Proposal ──> Policy/Validation ──> Mock Ledger API
                                             │
                                             ▼
                                         Audit Event
```

---

## 3. Component Responsibilities

### 3.1 Frontend

**Stack:** React + Vite + Tailwind CSS + Recharts.

Responsibilities:

- display financial KPIs,
- show reconciliation results,
- render forecasts,
- display exception evidence,
- display anomaly scores,
- provide controller chat,
- present proposed journal entries,
- collect approval/rejection,
- display audit activity.

The frontend should never contain financial business rules.

---

### 3.2 API Layer

**Stack:** FastAPI.

Responsibilities:

- authentication/session boundary for the demo,
- request validation,
- invoke controller service,
- expose dashboard APIs,
- validate action approvals,
- return normalized response schemas.

Suggested modules:

```text
backend/api/routes/agent.py
backend/api/routes/reconciliation.py
backend/api/routes/ledger.py
backend/api/routes/forecast.py
backend/api/routes/anomalies.py
backend/api/routes/actions.py
backend/api/routes/audit.py
```

---

### 3.3 Controller Service

This is the central agentic layer.

Responsibilities:

- maintain conversation/run state,
- send user input + tool definitions to Gemini,
- receive function/tool calls,
- execute local tool functions,
- send structured tool results back to Gemini,
- repeat until a final answer is produced,
- persist the complete run trace.

Pseudo-flow:

```text
while not final_response:
    response = gemini.generate_content(messages, tools=tool_definitions)

    if response contains tool_calls:
        for call in response.tool_calls:
            result = dispatch_tool(call.name, call.arguments)
            persist_tool_call(call, result)
            messages.append(tool_result(result))
    else:
        final_response = response.text
```

The actual Gemini SDK syntax may change; keep this implementation behind an adapter such as:

```text
backend/agent/providers/gemini_provider.py
```

with an interface like:

```python
class LLMProvider:
    def generate(self, messages, tools): ...
```

This keeps provider-specific code isolated.

---

## 4. Tool Registry

Create an explicit registry rather than scattering if/else statements across the agent loop.

```python
TOOL_REGISTRY = {
    "run_reconciliation": run_reconciliation,
    "query_ledger": query_ledger,
    "forecast_cashflow": forecast_cashflow,
    "check_gst_match": check_gst_match,
    "detect_anomalies": detect_anomalies,
    "propose_journal_entry": propose_journal_entry,
}
```

Each tool should have:

- name,
- description,
- JSON schema,
- callable function,
- permission class (`READ`, `PROPOSE`, `WRITE`),
- timeout,
- retry policy,
- version.

---

## 5. Tool Permission Model

```text
READ
 ├── query_ledger
 ├── run_reconciliation
 ├── forecast_cashflow
 ├── check_gst_match
 └── detect_anomalies

PROPOSE
 └── propose_journal_entry

WRITE
 └── post_journal_entry (mock only)
```

The agent may call READ tools freely.

The agent can create a PROPOSE action only when the user's request supports it and evidence is sufficient.

WRITE actions must not be exposed as ordinary model-callable tools for the MVP. Use a separate API endpoint requiring human approval.

---

## 6. Reconciliation Architecture

```text
Transactions ─────┐
                  ├── normalization ── matching engine ──> status
Settlements ──────┤                         │
                  │                         ├── amount check
Ledger ───────────┘                         ├── fee check
                                            ├── refund check
                                            ├── duplicate check
                                            └── timing check
                                                      │
                                                      ▼
                                              Exception records
```

### Matching stages

1. normalize identifiers and timestamps,
2. create candidate matches,
3. compare amounts,
4. compare fees,
5. compare refunds,
6. compare settlement timing,
7. detect duplicates,
8. assign match/exception status,
9. calculate financial impact.

The engine should return both aggregates and record-level evidence.

---

## 7. Forecasting Architecture

```text
Historical cash records
        │
        ▼
Daily aggregation
        │
        ▼
Feature/trend preparation
        │
        ▼
Deterministic forecasting model
        │
        ├── projected inflow
        ├── projected outflow
        ├── projected ending balance
        └── uncertainty / confidence metadata
        │
        ▼
Risk policy
        │
        ├── LOW
        ├── MEDIUM
        └── HIGH
```

Suggested first implementation:

- rolling mean,
- trend adjustment,
- configurable operating threshold.

Add a more sophisticated model only after the baseline is reliable.

---

## 8. ML Anomaly Architecture

```text
Transaction history
       │
       ▼
Feature engineering
       │
       ├── amount
       ├── amount deviation
       ├── time-of-day
       ├── settlement delay
       ├── fee ratio
       ├── refund behavior
       └── frequency features
       │
       ▼
Isolation Forest
       │
       ▼
Anomaly score
       │
       ▼
Threshold + explanation metadata
```

Important: anomaly detection and reconciliation answer different questions.

- **Reconciliation:** Does the record match expected financial state?
- **Anomaly detection:** Is the record statistically unusual compared with normal historical behavior?

A record can therefore be:

```text
Reconciliation = PASS
Anomaly        = HIGH
```

That combination is one of the strongest demo scenarios.

---

## 9. GST Matching Architecture

```text
Transaction
    │
    ├── invoice
    ├── taxable amount
    ├── applicable tax rate
    └── recorded GST
             │
             ▼
      expected GST calculation
             │
             ▼
          comparison
             │
        ┌────┴────┐
       MATCH   MISMATCH
```

The calculation should be deterministic and auditable.

---

## 10. Safe Action Architecture

```text
Evidence
   │
   ▼
Agent proposes correction
   │
   ▼
Journal Proposal
   │
   ├── amount
   ├── debit account
   ├── credit account
   ├── reason
   ├── source evidence
   └── confidence
   │
   ▼
Human approval
   │
   ├── Reject ──> audit event
   │
   └── Approve
          │
          ▼
   Validation / idempotency
          │
          ▼
     Mock Ledger API
          │
     ┌────┴────┐
 success    failure
     │          │
   audit      retry/rollback
```

Never let a generic chat message directly mutate the ledger.

---

## 11. Event and State Model

Every request gets a `run_id`.

Example:

```text
run_id
  │
  ├── user_message
  ├── model_request
  ├── tool_call_1
  ├── tool_result_1
  ├── tool_call_2
  ├── tool_result_2
  ├── model_request_2
  ├── final_response
  └── optional_action
```

This allows multi-turn context, debugging, and demo observability.

---

## 12. Database Schema

Suggested tables:

```text
merchants
customers
transactions
settlements
refunds
fees
invoices
ledger_entries
cash_flows
reconciliation_exceptions
anomaly_scores
agent_runs
tool_calls
journal_proposals
approvals
audit_events
```

### Key relationships

```text
merchant
  ├── transactions
  ├── settlements
  ├── ledger_entries
  └── cash_flows

transaction
  ├── settlement
  ├── refund
  ├── invoice
  ├── ledger_entry
  └── anomaly_score

agent_run
  ├── tool_calls
  └── audit_events

journal_proposal
  └── approval
```

---

## 13. API Architecture

### Agent

```http
POST /api/agent/chat
```

Request:

```json
{
  "run_id": null,
  "message": "Why is Tuesday's cash short?",
  "merchant_id": "M001"
}
```

Response:

```json
{
  "run_id": "RUN-1001",
  "answer": "Tuesday's settlement is short by ...",
  "tools_used": [
    "query_ledger",
    "run_reconciliation"
  ],
  "evidence": [
    "SET-5501",
    "TXN-1042"
  ]
}
```

### Dashboard metrics

```http
GET /api/metrics?merchant_id=M001
```

### Approval

```http
POST /api/actions/{proposal_id}/approve
```

### Rejection

```http
POST /api/actions/{proposal_id}/reject
```

### Rollback

```http
POST /api/actions/{proposal_id}/rollback
```

---

## 14. Suggested Repository Structure

```text
ai-finance-controller/
│
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── api/
│   │   │   ├── routes/
│   │   │   │   ├── agent.py
│   │   │   │   ├── reconciliation.py
│   │   │   │   ├── ledger.py
│   │   │   │   ├── forecast.py
│   │   │   │   ├── anomalies.py
│   │   │   │   ├── actions.py
│   │   │   │   └── audit.py
│   │   │   └── schemas/
│   │   ├── agent/
│   │   │   ├── controller.py
│   │   │   ├── tool_registry.py
│   │   │   ├── prompts.py
│   │   │   └── providers/
│   │   │       ├── base.py
│   │   │       └── gemini.py
│   │   ├── tools/
│   │   │   ├── reconciliation.py
│   │   │   ├── ledger.py
│   │   │   ├── forecast.py
│   │   │   ├── gst.py
│   │   │   ├── anomalies.py
│   │   │   └── journal.py
│   │   ├── services/
│   │   ├── models/
│   │   └── db/
│   │
│   ├── ml/
│   │   ├── train_anomaly.py
│   │   ├── features.py
│   │   └── artifacts/
│   │
│   ├── scripts/
│   │   └── generate_dataset.py
│   └── tests/
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   ├── hooks/
│   │   ├── services/
│   │   └── types/
│   └── package.json
│
├── data/
│   ├── raw/
│   ├── generated/
│   └── benchmark/
│
├── docs/
│   ├── PRD.md
│   ├── architecture.md
│   └── todo.md
│
├── .env.example
├── docker-compose.yml
└── README.md
```

---

## 15. Agent System Prompt Design

Use a strong system prompt with explicit behavioral boundaries.

```text
You are AI Finance Controller.

Your role is to help a finance analyst investigate and operate on financial data.

Rules:
1. Never invent financial figures.
2. Use tools whenever factual financial data is required.
3. Prefer evidence before conclusions.
4. Explain discrepancies using record IDs and calculated amounts.
5. Distinguish reconciliation results from ML anomaly signals.
6. Do not claim a write succeeded unless the backend confirms success.
7. Never directly mutate financial records without explicit human approval.
8. When data is insufficient, say what is missing.
9. Keep unresolved exceptions visible.
10. Use concise financial reasoning and show the important evidence.
```

Do not put complex business calculations in this prompt; keep them in tools.

---

## 16. Failure Handling

### Transient tool failure

```text
Tool timeout
   ↓
retry with bounded attempts
   ↓
success → continue
failure → return structured error to agent
```

### Non-transient validation failure

Return immediately with:

```json
{
  "ok": false,
  "error_type": "VALIDATION_ERROR",
  "message": "start_date must be before end_date"
}
```

### Model failure

The API should return a safe message and preserve the run trace so the failure can be debugged.

---

## 17. Observability

Log:

- request latency,
- model latency,
- tool latency,
- tool success/error,
- token usage where available,
- number of tool calls per run,
- records processed,
- reconciliation results,
- forecast execution,
- action approvals.

Dashboard metrics should be based on stored backend facts, not model-generated text.

---

## 18. Deployment Architecture for MVP

```text
Browser
   │
   ▼
Frontend hosting
   │
   ▼
FastAPI backend
   │
   ├── Gemini API
   ├── PostgreSQL
   └── ML/forecast services
```

For a buildathon, keep infrastructure simple. Containerize the backend/database when useful, but do not let DevOps work delay the core agent demo.

---

## 19. Architecture Decisions

### Why Gemini?

- available for development on the free tier subject to current quotas,
- supports function/tool calling,
- appropriate for the controller's orchestration role.

### Why deterministic tools?

Financial arithmetic and matching should be reproducible, testable, and auditable.

### Why Isolation Forest?

It gives a relatively lightweight unsupervised anomaly detector that is easy to train and explain at buildathon scale.

### Why FastAPI?

Python makes it convenient to combine finance logic, ML, forecasting, and the API layer.

### Why PostgreSQL?

Structured financial relationships and audit records benefit from a relational database.

---

## 20. Future Architecture Extensions

### Event-driven ingestion

```text
Settlement Event
      ↓
Queue
      ↓
Consumer
      ↓
Reconciliation
      ↓
Exception event
      ↓
Dashboard update
```

### Multi-currency

Add:

```text
source currency
FX rate
converted amount
currency precision
FX tolerance
```

before matching settlement values.

### More advanced agent memory

Add explicit financial context retrieval rather than relying on raw chat history.

---

## 21. Golden Rule

**The controller decides what to investigate. The tools decide the financial facts. The human decides whether money-related state changes happen.**
