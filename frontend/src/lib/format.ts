/**
 * Shared formatting helpers (INR money, percents, dates) and the
 * deterministic-exception vs ML-anomaly label vocabulary the whole
 * dashboard reuses (Phase 11: "clearly label ML anomaly vs
 * deterministic exception").
 */

export const formatINR = (value: number | null | undefined): string => {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 0,
  }).format(value)
}

export const formatINR2 = (value: number | null | undefined): string => {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
}

export const formatPct = (value: number | null | undefined, digits = 1): string =>
  value == null ? '—' : `${value.toFixed(digits)}%`

export const formatDate = (value: string | null | undefined): string => {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

export const formatDateTime = (value: string | null | undefined): string => {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export const formatNumber = (value: number | null | undefined): string =>
  value == null ? '—' : new Intl.NumberFormat('en-IN').format(value)

/** Human label for the deterministic exception taxonomy (engine output). */
export const EXCEPTION_LABELS: Record<string, string> = {
  MISSING_SETTLEMENT: 'Missing settlement',
  FEE_MISMATCH: 'Fee mismatch',
  REFUND_MISMATCH: 'Refund mismatch',
  DUPLICATE_TRANSACTION: 'Duplicate transaction',
  SETTLEMENT_TIMING_MISMATCH: 'Settlement timing',
  AMOUNT_MISMATCH: 'Amount mismatch',
  LEDGER_MISMATCH: 'Ledger mismatch',
  GST_MISMATCH: 'GST mismatch',
  FAILED_LEDGER_WRITE: 'Failed ledger write',
}

export const exceptionLabel = (type: string): string =>
  EXCEPTION_LABELS[type] ?? type.replace(/_/g, ' ').toLowerCase()

/** Badge palette shared by severity / status / risk chips. */
export const severityTone = (severity: string): string =>
  ({
    high: 'bg-rose-50 text-rose-700 ring-rose-200',
    medium: 'bg-amber-50 text-amber-700 ring-amber-200',
    low: 'bg-slate-100 text-slate-600 ring-slate-200',
  })[severity] ?? 'bg-slate-100 text-slate-600 ring-slate-200'

export const statusTone = (status: string): string =>
  ({
    open: 'bg-amber-50 text-amber-700 ring-amber-200',
    resolved: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    pending: 'bg-amber-50 text-amber-700 ring-amber-200',
    approved: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    rejected: 'bg-rose-50 text-rose-700 ring-rose-200',
    rolled_back: 'bg-violet-50 text-violet-700 ring-violet-200',
    completed: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    model_error: 'bg-rose-50 text-rose-700 ring-rose-200',
    running: 'bg-sky-50 text-sky-700 ring-sky-200',
  })[status] ?? 'bg-slate-100 text-slate-600 ring-slate-200'

export const riskTone = (risk: string | null): string =>
  ({
    HIGH: 'bg-rose-50 text-rose-700 ring-rose-200',
    MEDIUM: 'bg-amber-50 text-amber-700 ring-amber-200',
    LOW: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  })[risk ?? ''] ?? 'bg-slate-100 text-slate-600 ring-slate-200'
