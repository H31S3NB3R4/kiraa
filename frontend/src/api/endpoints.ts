/**
 * Endpoint wrappers — one typed function per backend route. All GETs pass
 * `params` straight through; writes (approve/reject/rollback/chat) carry
 * their bodies from the Action/Chat UIs.
 */

import { api } from './types'
import type {
  ActionRequest,
  ActionResponse,
  AgentChatRequest,
  AgentChatResponse,
  AnomalyResponse,
  AuditEventRow,
  EvaluationResponse,
  ExceptionRow,
  ForecastResponse,
  LedgerQueryResponse,
  ListEnvelope,
  MerchantsListResponse,
  MetricsResponse,
  ProposalRow,
  RunDetailResponse,
  RunSummaryRow,
} from './types'

export const getMetrics = (params?: { merchant_id?: string }) =>
  api.get<MetricsResponse>('/metrics', { params }).then((r) => r.data)

export const getExceptions = (params?: Record<string, unknown>) =>
  api.get<ListEnvelope<ExceptionRow>>('/exceptions', { params }).then((r) => r.data)

export const getForecast = (params?: { merchant_id?: string; horizon_days?: number }) =>
  api.get<ForecastResponse>('/forecast', { params }).then((r) => r.data)

export const getAnomalies = (params?: Record<string, unknown>) =>
  api.get<AnomalyResponse>('/anomalies', { params }).then((r) => r.data)

export const getProposals = (params?: Record<string, unknown>) =>
  api.get<ListEnvelope<ProposalRow>>('/proposals', { params }).then((r) => r.data)

export const approveProposal = (proposalId: string, body: ActionRequest) =>
  api.post<ActionResponse>(`/actions/${proposalId}/approve`, body).then((r) => r.data)

export const rejectProposal = (proposalId: string, body: ActionRequest) =>
  api.post<ActionResponse>(`/actions/${proposalId}/reject`, body).then((r) => r.data)

export const rollbackProposal = (proposalId: string, body: ActionRequest) =>
  api.post<ActionResponse>(`/actions/${proposalId}/rollback`, body).then((r) => r.data)

export const getAudit = (params?: Record<string, unknown>) =>
  api.get<ListEnvelope<AuditEventRow>>('/audit', { params }).then((r) => r.data)

export const getRuns = (params?: Record<string, unknown>) =>
  api.get<ListEnvelope<RunSummaryRow>>('/runs', { params }).then((r) => r.data)

export const getRunDetail = (runId: string) =>
  api.get<RunDetailResponse>(`/runs/${runId}`).then((r) => r.data)

export const getMerchants = () =>
  api.get<MerchantsListResponse>('/merchants').then((r) => r.data)

/** Phase 12 benchmark metrics scored against the seeded ground truth. */
export const getEvaluation = () =>
  api.get<EvaluationResponse>('/evaluation').then((r) => r.data)

export const chat = (body: AgentChatRequest) =>
  api.post<AgentChatResponse>('/agent/chat', body).then((r) => r.data)

export const runReconciliation = (body: {
  merchant_id?: string | null
  start_date?: string | null
  end_date?: string | null
  persist?: boolean
}) =>
  api
    .post<Record<string, unknown>>('/reconciliation/run', body)
    .then((r) => r.data)

export const getHealth = () =>
  // The health probe lives at the root (`/health`), outside the `/api`
  // prefix — the vite dev server proxies both paths.
  api.get<{ status: string }>('/health', { baseURL: '' }).then((r) => r.data)

/** Read-only, source-linked ledger query (FR-3). */
export const getLedger = (params?: Record<string, unknown>) =>
  api.get<LedgerQueryResponse>('/ledger/query', { params }).then((r) => r.data)
