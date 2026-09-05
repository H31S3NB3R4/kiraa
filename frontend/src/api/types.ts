/**
 * Typed API client — one interface per backend pydantic schema
 * (backend/app/api/schemas/*). Shapes mirror the Phase 8-10 contracts
 * exactly; optional-safe fields stay optional so guard envelopes
 * (`unknown_merchant`, `no_history`, …) type-check too.
 */

import axios from 'axios'

export const api = axios.create({
  // Vite dev server proxies /api -> 127.0.0.1:8000 (vite.config.ts);
  // in production the dashboard is served next to the API.
  baseURL: '/api',
  timeout: 90_000,
})

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------

export interface ReconciliationMetrics {
  transactions: number
  matched: number
  exception_transactions: number
  exceptions: number
  by_type: Record<string, number>
  total_financial_impact: number
  match_rate_pct: number
}

export interface MetricsResponse {
  merchant_id: string | null
  total_cash: number | null
  cash_as_of_date: string | null
  reconciliation: ReconciliationMetrics
  exception_count: number
  exception_transactions: number
  financial_impact_at_risk: number
  match_rate_pct: number
  pending_proposals: number
}

export interface ExceptionRow {
  exception_id: number
  transaction_id: string
  merchant_id: string
  exception_date: string
  exception_type: string
  severity: string
  expected_amount: number
  recorded_amount: number
  financial_impact: number
  description: string
  status: string
}

export interface ListEnvelope<T> {
  count: number
  limit: number | null
  truncated: boolean
  filters: Record<string, unknown>
  rows: T[]
}

export interface ForecastPoint {
  day_offset: number
  date: string
  projected_inflow: number
  projected_outflow: number
  projected_net: number
  projected_cash: number
}

export interface ForecastResponse {
  tool: string
  status: string
  merchant_id: string | null
  merchant_name: string | null
  scope: string | null
  model: string | null
  horizon_days: number | null
  history_days: number | null
  history_observed_days: number | null
  history_start: string | null
  history_end: string | null
  anchor_date: string | null
  anchor_balance: number | null
  daily_avg_inflow: number | null
  daily_avg_outflow: number | null
  daily_avg_net: number | null
  recent_window_days: number | null
  recent_avg_inflow: number | null
  recent_avg_outflow: number | null
  recent_avg_net: number | null
  net_trend_per_day: number | null
  projected_inflow_per_day: number | null
  projected_outflow_per_day: number | null
  projected_net_per_day: number | null
  forecast: ForecastPoint[]
  projected_ending_balance: number | null
  min_projected_cash: number | null
  min_projected_date: string | null
  first_breach_date: string | null
  breach_days: number | null
  operating_threshold: number | null
  headroom: number | null
  headroom_pct: number | null
  risk: string | null
  risk_reason: string | null
  confidence: string | null
  volatility_cv: number | null
  sources: Record<string, unknown> | null
}

export interface AnomalyScoreRow {
  transaction_id: string
  merchant_id: string | null
  anomaly_score: number
  severity: string | null
  is_anomaly: boolean | null
  reconciliation_pass: boolean | null
  reason: string | null
  features: Record<string, unknown> | null
}

export interface AnomalyResponse {
  tool: string
  status: string
  merchant_id: string | null
  filters: Record<string, unknown> | null
  model: Record<string, unknown> | null
  scores: AnomalyScoreRow[]
  truncated: boolean | null
  metrics: Record<string, unknown> | null
  ground_truth: Record<string, unknown> | null
  persisted: Record<string, number> | null
  sources: Record<string, unknown> | null
}

export interface ProposalRow {
  proposal_id: string
  agent_run_id: string | null
  transaction_id: string | null
  merchant_id: string | null
  merchant_name: string | null
  entry_date: string | null
  debit_account: string
  credit_account: string
  amount: number
  narrative: string
  evidence_ids: string[]
  confidence: number
  status: string
  created_at: string
}

export interface ActionRequest {
  idempotency_key: string
  approver: string
  note?: string | null
  simulate_failure?: boolean
}

export interface ActionResponse {
  proposal_id: string
  status: string
  decision: string | null
  ledger_entry_id: string | null
  idempotent_replay: boolean
  audit_event_id: string
  message: string
}

export interface AuditEventRow {
  event_id: string
  actor: string
  action: string
  object_type: string
  object_id: string
  agent_run_id: string | null
  before_state: Record<string, unknown>
  after_state: Record<string, unknown>
  created_at: string
}

export interface RunSummaryRow {
  run_id: string
  user_query: string
  status: string
  turn_count: number
  tool_call_count: number
}

export interface RunToolCall {
  seq: number
  tool_name: string
  arguments: Record<string, unknown>
  result: Record<string, unknown>
  status: string
  error: string | null
  latency_ms: number
}

export interface RunDetailResponse {
  run_id: string
  user_query: string
  status: string
  turn_count: number
  tool_call_count: number
  total_llm_latency_ms: number
  error: string | null
  final_response: string | null
  started_at: string
  finished_at: string | null
  tool_calls: RunToolCall[]
  messages: { seq: number; role: string; content: Record<string, unknown> }[]
}

export interface MerchantRow {
  merchant_id: string
  name: string
  category: string
  currency: string
}

export interface MerchantsListResponse {
  count: number
  rows: MerchantRow[]
}

export interface AgentChatRequest {
  run_id?: string | null
  message: string
  merchant_id?: string | null
}

/** One ledger entry with its source references (FR-3, Phase 9). */
export interface LedgerQueryRow {
  entry_id: string
  transaction_id: string
  merchant_id: string
  merchant_name: string
  merchant_category: string
  entry_date: string
  debit_account: string
  credit_account: string
  amount: number
  status: string
  description: string
  settlement_id: string | null
  invoice_id: string | null
}

/** `GET /api/ledger/query` — read-only, source-linked ledger rows. */
export interface LedgerQueryResponse {
  tool: string
  filters: Record<string, unknown>
  count: number
  limit: number | null
  truncated: boolean
  rows: LedgerQueryRow[]
}

export interface ToolCallInfo {
  tool_name: string
  arguments: Record<string, unknown>
  status: string
  error: string | null
  latency_ms: number
}

export interface AgentChatResponse {
  run_id: string
  status: string
  turn_count: number
  answer: string
  tools_used: string[]
  evidence: string[]
  tool_calls: ToolCallInfo[]
  total_llm_latency_ms: number
}
