/**
 * Anomalies view (Phase 10 + 11): ML anomaly scores from
 * `GET /api/anomalies`, clearly labelled as ML (not deterministic
 * exceptions — those live on the Reconciliation page). Each anomaly row
 * renders the model's "normal vs unusual" comparison built from the
 * `features` and `model` dicts in the response payload, plus its
 * reconciliation cross-link and reason string.
 */

import { useEffect, useState } from 'react'
import { Brain } from 'lucide-react'
import { getAnomalies } from '../api/endpoints'
import type { AnomalyResponse, AnomalyScoreRow } from '../api/types'
import { useScope } from '../state/scope'
import { Badge, Card, EmptyState, ErrorState, Loading } from '../components/ui'

/** Render one feature value at a glance (number/bool/string). */
function featureValue(v: unknown): string {
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2)
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (v == null) return '—'
  return String(v)
}

/**
 * Build the "normal vs unusual" comparison for one anomaly row: the ML
 * model's baseline stats (from the response `model` dict) next to this
 * transaction's `features`. Only includes features present in the row.
 */
function comparisonFor(
  row: AnomalyScoreRow,
  model: Record<string, unknown> | null,
): { feature: string; normal: string; observed: string }[] {
  const feats = row.features ?? {}
  const stats = (model ?? {}) as Record<string, unknown>
  const out: { feature: string; normal: string; observed: string }[] = []
  for (const [key, value] of Object.entries(feats)) {
    if (key === 'transaction_id') continue
    // Model dict carries per-feature baselines as nested stats; fall
    // back to observed-only when no baseline is published.
    const nested = stats[key]
    if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
      const n = nested as Record<string, unknown>
      const mean = n.mean ?? n.avg ?? n.center
      if (mean != null) {
        out.push({ feature: key, normal: featureValue(mean), observed: featureValue(value) })
        continue
      }
    }
    out.push({ feature: key, normal: '—', observed: featureValue(value) })
  }
  return out
}

export default function AnomaliesPage() {
  const { params } = useScope()
  const [data, setData] = useState<AnomalyResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [onlyAnomalies, setOnlyAnomalies] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getAnomalies({ ...params, include_scores: 'true', limit: 100 })
      .then((r) => {
        if (!cancelled) setData(r)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load anomaly scores.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [params])

  if (loading) return <Loading label="Scoring transactions…" />
  if (error) return <ErrorState message={error} />
  if (!data) return <EmptyState message="No anomaly data available." />

  const anomalies = data.scores.filter((s) => s.is_anomaly === true)
  const rows = onlyAnomalies ? anomalies : data.scores


  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold text-slate-900">
            <Brain className="h-5 w-5 text-violet-600" />
            Anomalies
            <span className="rounded bg-violet-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-violet-700">
              ML anomaly
            </span>
          </h1>
          <p className="text-xs text-slate-500">
            Statistical outlier scores — labelled "ML anomaly", not deterministic exceptions.
            Deterministic engine exceptions are on the Reconciliation page.
          </p>
        </div>
        <label className="flex items-center gap-2 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={onlyAnomalies}
            onChange={(e) => setOnlyAnomalies(e.target.checked)}
            className="h-3.5 w-3.5 accent-violet-600"
          />
          flagged only ({anomalies.length} of {data.scores.length})
        </label>
      </div>

      {rows.length === 0 ? (
        <Card>
          <EmptyState message="No flagged anomalies in this scope. 🎉" />
        </Card>
      ) : (
        <div className="space-y-3">
          {rows.map((row) => {
            const comparison = comparisonFor(row, data.model)
            return (
              <Card key={row.transaction_id}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs font-semibold text-slate-800">
                    {row.transaction_id}
                  </span>
                  {row.merchant_id && (
                    <span className="text-xs text-slate-400">{row.merchant_id}</span>
                  )}
                  {row.severity && (
                    <Badge
                      tone={
                        row.severity === 'high'
                          ? 'bg-rose-50 text-rose-700 ring-rose-200'
                          : 'bg-violet-50 text-violet-700 ring-violet-200'
                      }
                    >
                      {row.severity}
                    </Badge>
                  )}
                  <span className="ml-auto font-mono text-xs text-slate-500">
                    score {row.anomaly_score.toFixed(3)}
                  </span>
                </div>
                {row.reason && <p className="mt-2 text-xs text-slate-600">{row.reason}</p>}
                {comparison.length > 0 && (
                  <div className="mt-3 overflow-hidden rounded-lg border border-slate-200">
                    <table className="w-full text-left text-[11px]">
                      <thead>
                        <tr className="bg-slate-50 text-slate-500">
                          <th className="px-3 py-1.5 font-medium">Feature</th>
                          <th className="px-3 py-1.5 font-medium">Normal baseline</th>
                          <th className="px-3 py-1.5 font-medium">This transaction</th>
                        </tr>
                      </thead>
                      <tbody>
                        {comparison.map((c) => (
                          <tr key={c.feature} className="border-t border-slate-100">
                            <td className="px-3 py-1.5 font-mono text-slate-600">{c.feature}</td>
                            <td className="px-3 py-1.5 text-slate-500">{c.normal}</td>
                            <td className="px-3 py-1.5 font-medium text-slate-800">{c.observed}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {row.reconciliation_pass != null && (
                  <p className="mt-2 text-[11px] text-slate-400">
                    deterministic engine: {row.reconciliation_pass ? 'passed' : 'flagged'} — ML and
                    engine verdicts are independent
                  </p>
                )}
              </Card>
            )
          })}
        </div>
      )}
    </div>
  )
}