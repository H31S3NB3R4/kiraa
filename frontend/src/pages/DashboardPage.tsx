/**
 * Dashboard view (Phase 10): KPI cards from `GET /api/metrics` plus the
 * top open exceptions preview. Re-renders on every merchant/date scope
 * change; the read-only metrics run is deterministic so the cards can
 * never show invented or stale numbers.
 */

import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, IndianRupee, ShieldAlert } from 'lucide-react'
import { getExceptions, getMetrics } from '../api/endpoints'
import type { ExceptionRow, MetricsResponse } from '../api/types'
import { useScope } from '../state/scope'
import {
  exceptionLabel,
  formatDate,
  formatINR,
  formatINR2,
  formatNumber,
  formatPct,
  severityTone,
} from '../lib/format'
import { Badge, Card, EmptyState, ErrorState, Loading } from '../components/ui'

function KpiCard({
  label,
  value,
  sub,
  icon,
  tone = 'bg-indigo-50 text-indigo-600',
}: {
  label: string
  value: string
  sub?: string
  icon: React.ReactNode
  tone?: string
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
          {label}
        </span>
        <span className={`rounded-lg p-1.5 ${tone}`}>{icon}</span>
      </div>
      <p className="mt-2 text-2xl font-semibold text-slate-900">{value}</p>
      {sub && <p className="mt-1 text-xs text-slate-500">{sub}</p>}
    </div>
  )
}

export default function DashboardPage() {
  const { scope, params } = useScope()
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null)
  const [topExceptions, setTopExceptions] = useState<ExceptionRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    Promise.all([
      getMetrics(scope.merchantId ? { merchant_id: scope.merchantId } : undefined),
      getExceptions({ ...params, status: 'open', limit: 5, sort: 'impact_desc' }),
    ])
      .then(([m, exc]) => {
        if (cancelled) return
        setMetrics(m)
        setTopExceptions(exc.rows)
      })
      .catch((e: unknown) => {
        if (!cancelled)
          setError(
            e instanceof Error ? e.message : 'Failed to load dashboard — is the backend running?',
          )
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [scope.merchantId, params])

  if (loading) return <Loading label="Loading dashboard…" />
  if (error) return <ErrorState message={error} />
  if (!metrics) return <EmptyState message="No metrics available." />

  const recon = metrics.reconciliation

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Dashboard</h1>
        <p className="text-xs text-slate-500">
          Read-only overview for{' '}
          {scope.merchantId ?? 'all merchants'}
          {metrics.cash_as_of_date && ` · cash as of ${formatDate(metrics.cash_as_of_date)}`}
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard
          label="Total Cash"
          value={formatINR(metrics.total_cash)}
          sub={metrics.cash_as_of_date ? `as of ${formatDate(metrics.cash_as_of_date)}` : undefined}
          icon={<IndianRupee className="h-4 w-4" />}
        />
        <KpiCard
          label="Match Rate"
          value={formatPct(metrics.match_rate_pct)}
          sub={`${formatNumber(recon.matched)} of ${formatNumber(recon.transactions)} matched`}
          icon={<CheckCircle2 className="h-4 w-4" />}
          tone="bg-emerald-50 text-emerald-600"
        />
        <KpiCard
          label="Open Exceptions"
          value={formatNumber(metrics.exception_count)}
          sub={`${formatNumber(metrics.exception_transactions)} exception transactions`}
          icon={<AlertTriangle className="h-4 w-4" />}
          tone="bg-amber-50 text-amber-600"
        />
        <KpiCard
          label="Impact at Risk"
          value={formatINR(metrics.financial_impact_at_risk)}
          sub={`${metrics.pending_proposals} proposal${metrics.pending_proposals === 1 ? '' : 's'} awaiting review`}
          icon={<ShieldAlert className="h-4 w-4" />}
          tone="bg-rose-50 text-rose-600"
        />
      </div>

      <Card
        title="Top open exceptions"
        subtitle="Highest financial impact first — read-only engine view"
      >
        {topExceptions.length === 0 ? (
          <EmptyState message="No open exceptions in this scope. 🎉" />
        ) : (
          <ul className="divide-y divide-slate-100">
            {topExceptions.map((row) => (
              <li key={row.exception_id} className="flex items-center gap-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs font-medium text-slate-700">
                      {row.transaction_id}
                    </span>
                    <Badge tone={severityTone(row.severity)}>{row.severity}</Badge>
                    <span className="text-xs text-slate-500">
                      {exceptionLabel(row.exception_type)}
                    </span>
                  </div>
                  <p className="mt-0.5 truncate text-xs text-slate-500">{row.description}</p>
                </div>
                <div className="text-right">
                  <p className="text-xs font-semibold text-rose-700">
                    {formatINR2(row.financial_impact)}
                  </p>
                  <p className="text-[10px] text-slate-400">{formatDate(row.exception_date)}</p>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
