/**
 * Forecast view (Phase 10): cash-flow projection line chart from
 * `GET /api/forecast` (recharts). Shows the balance walk with inflow/
 * outflow components, the operating threshold, and the risk verdict —
 * every figure straight from the deterministic trend tool.
 */

import { useEffect, useState } from 'react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { getForecast } from '../api/endpoints'
import type { ForecastResponse } from '../api/types'
import { useScope } from '../state/scope'
import { formatDate, formatINR, formatPct, riskTone } from '../lib/format'
import { Card, EmptyState, ErrorState, Loading } from '../components/ui'

const HORIZONS = [7, 14, 30]

export default function ForecastPage() {
  const { scope } = useScope()
  const [horizon, setHorizon] = useState(14)
  const [data, setData] = useState<ForecastResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getForecast(
      scope.merchantId
        ? { merchant_id: scope.merchantId, horizon_days: horizon }
        : { horizon_days: horizon },
    )
      .then((r) => {
        if (!cancelled) setData(r)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load forecast.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [scope.merchantId, horizon])

  if (loading) return <Loading label="Projecting cash flow…" />
  if (error) return <ErrorState message={error} />
  if (!data) return <EmptyState message="No forecast available." />

  const hasSeries = data.forecast.length > 0

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Cash Forecast</h1>
          <p className="text-xs text-slate-500">
            {data.merchant_name ?? data.merchant_id ?? 'All merchants'} · model{' '}
            <span className="font-mono">{data.model ?? '—'}</span> · anchor{' '}
            {formatINR(data.anchor_balance)} on {formatDate(data.anchor_date)}
          </p>
        </div>
        <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1 shadow-sm">
          {HORIZONS.map((h) => (
            <button
              key={h}
              onClick={() => setHorizon(h)}
              className={`rounded px-2.5 py-1 text-xs font-medium transition ${
                horizon === h ? 'bg-indigo-600 text-white' : 'text-slate-600 hover:bg-slate-100'
              }`}
            >
              {h}d
            </button>
          ))}
        </div>
      </div>

      {data.risk && (
        <div
          className={`flex flex-wrap items-center gap-3 rounded-xl px-4 py-3 text-xs ring-1 ${riskTone(data.risk)}`}
        >
          <span className="font-semibold">Risk: {data.risk}</span>
          <span>{data.risk_reason}</span>
          {data.headroom != null && (
            <span className="ml-auto">
              headroom {formatINR(data.headroom)} ({formatPct(data.headroom_pct)})
            </span>
          )}
          {data.confidence && (
            <span className="rounded bg-white/60 px-1.5 py-0.5 text-[10px] font-semibold text-slate-700 ring-1 ring-slate-200">
              confidence: {data.confidence}
            </span>
          )}
        </div>
      )}

      <Card
        title="Projected daily cash"
        subtitle="Balance walk with inflow/outflow components (deterministic trend model)"
      >
        {!hasSeries ? (
          <EmptyState
            message={
              data.status === 'no_history'
                ? 'No cash history for this scope yet — run a reconciliation first.'
                : `No forecast rows (${data.status}).`
            }
          />
        ) : (
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data.forecast} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 11, fill: '#64748b' }}
                  tickFormatter={(v: string) => formatDate(v).slice(0, 6)}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: '#64748b' }}
                  tickFormatter={(v: number) => `₹${Math.round(v / 1000)}k`}
                  width={64}
                />
                <Tooltip
                  formatter={(value) => formatINR(Number(value))}
                  labelFormatter={(l) => formatDate(String(l))}
                />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Line
                  type="monotone"
                  dataKey="projected_cash"
                  name="Projected cash"
                  stroke="#4f46e5"
                  strokeWidth={2}
                  dot={false}
                />
                <Line
                  type="monotone"
                  dataKey="projected_inflow"
                  name="Inflow"
                  stroke="#059669"
                  strokeWidth={1}
                  dot={false}
                />
                <Line
                  type="monotone"
                  dataKey="projected_outflow"
                  name="Outflow"
                  stroke="#e11d48"
                  strokeWidth={1}
                  dot={false}
                />
                {data.operating_threshold != null && (
                  <ReferenceLine
                    y={data.operating_threshold}
                    stroke="#f59e0b"
                    strokeDasharray="6 4"
                    label={{
                      value: 'threshold',
                      fontSize: 10,
                      fill: '#b45309',
                      position: 'insideTopRight',
                    }}
                  />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card title="Ending balance">
          <p className="text-lg font-semibold text-slate-900">
            {formatINR(data.projected_ending_balance)}
          </p>
        </Card>
        <Card title="Min projected cash">
          <p className="text-lg font-semibold text-slate-900">
            {formatINR(data.min_projected_cash)}
          </p>
          <p className="text-xs text-slate-500">on {formatDate(data.min_projected_date)}</p>
        </Card>
        <Card title="First breach">
          <p className="text-lg font-semibold text-slate-900">
            {data.first_breach_date ? formatDate(data.first_breach_date) : '—'}
          </p>
          <p className="text-xs text-slate-500">
            {data.breach_days ? `${data.breach_days} days below threshold` : 'no breach projected'}
          </p>
        </Card>
        <Card title="Net per day">
          <p className="text-lg font-semibold text-slate-900">
            {formatINR(data.projected_net_per_day)}
          </p>
          <p className="text-xs text-slate-500">
            trend {formatINR(data.net_trend_per_day)}/day · vol {data.volatility_cv?.toFixed(2)}
          </p>
        </Card>
      </div>
    </div>
  )
}
