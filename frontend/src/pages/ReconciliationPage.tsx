/**
 * Reconciliation view (Phase 10): the persisted exception list from
 * `GET /api/exceptions` with severity/status filters, plus a "run fresh
 * reconciliation" button that posts to `/reconciliation/run` (persist=true
 * so the engine upserts rows) — the one refresh write the dashboard
 * triggers; every ledger posting still requires the explicit Phase 8
 * action flow.
 */

import { useEffect, useState } from 'react'
import { PlayCircle } from 'lucide-react'
import { getExceptions, runReconciliation } from '../api/endpoints'
import type { ExceptionRow } from '../api/types'
import { useScope } from '../state/scope'
import { exceptionLabel, formatDate, formatINR2, formatNumber, severityTone } from '../lib/format'
import { Badge, Card, EmptyState, ErrorState, Loading } from '../components/ui'

const SEVERITIES = ['all', 'high', 'medium', 'low']
const STATUSES = ['all', 'open', 'resolved']

export default function ReconciliationPage() {
  const { scope, params } = useScope()
  const [rows, setRows] = useState<ExceptionRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [severity, setSeverity] = useState('all')
  const [status, setStatus] = useState('open')
  const [running, setRunning] = useState(false)
  const [runMsg, setRunMsg] = useState<string | null>(null)

  const load = () => {
    const q: Record<string, unknown> = { ...params, limit: 200 }
    if (severity !== 'all') q.severity = severity
    if (status !== 'all') q.status = status
    setError(null)
    getExceptions(q)
      .then((r) => setRows(r.rows))
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : 'Failed to load exceptions.'),
      )
  }

  // Re-load whenever scope or the two filter selects change.
  useEffect(load, [params, severity, status])

  async function runFresh() {
    setRunning(true)
    setRunMsg(null)
    setError(null)
    try {
      const result = await runReconciliation({
        merchant_id: scope.merchantId,
        start_date: scope.startDate,
        end_date: scope.endDate,
        persist: true,
      })
      const rec = (result as Record<string, unknown>).reconciliation as
        | Record<string, unknown>
        | undefined
      setRunMsg(
        rec
          ? `Fresh run: ${rec.matched}/${rec.transactions} matched, ${rec.exceptions} exceptions, impact ${formatINR2(rec.total_financial_impact as number)}.`
          : 'Fresh reconciliation completed.',
      )
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reconciliation run failed.')
    } finally {
      setRunning(false)
    }
  }


  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Reconciliation</h1>
          <p className="text-xs text-slate-500">
            Persisted engine exceptions for {scope.merchantId ?? 'all merchants'} — the same rows
            the agent's reconciliation tool reports.
          </p>
        </div>
        <button
          onClick={runFresh}
          disabled={running}
          className="inline-flex items-center gap-2 rounded-lg bg-slate-900 px-3 py-2 text-xs font-medium text-white shadow-sm transition hover:bg-slate-700 disabled:opacity-40"
        >
          <PlayCircle className="h-4 w-4" />
          {running ? 'Running…' : 'Run fresh reconciliation'}
        </button>
      </div>

      {runMsg && (
        <p className="rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-800 ring-1 ring-emerald-200">
          {runMsg}
        </p>
      )}

      <Card
        title="Exceptions"
        subtitle="Deterministic engine output — every row cites its transaction"
        actions={
          <div className="flex items-center gap-2">
            <select
              value={severity}
              onChange={(e) => setSeverity(e.target.value)}
              className="rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-700"
            >
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  severity: {s}
                </option>
              ))}
            </select>
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              className="rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-700"
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  status: {s}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {error ? (
          <ErrorState message={error} />
        ) : rows === null ? (
          <Loading label="Loading exceptions…" />
        ) : rows.length === 0 ? (
          <EmptyState message="No exceptions match this filter. 🎉" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-slate-200 text-[11px] uppercase tracking-wide text-slate-400">
                  <th className="py-2 pr-4 font-medium">Transaction</th>
                  <th className="py-2 pr-4 font-medium">Type</th>
                  <th className="py-2 pr-4 font-medium">Date</th>
                  <th className="py-2 pr-4 text-right font-medium">Expected</th>
                  <th className="py-2 pr-4 text-right font-medium">Recorded</th>
                  <th className="py-2 pr-4 text-right font-medium">Impact</th>
                  <th className="py-2 pr-4 font-medium">Severity</th>
                  <th className="py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.exception_id} className="border-b border-slate-50 last:border-0">
                    <td className="py-2.5 pr-4">
                      <span className="font-mono text-slate-700">{row.transaction_id}</span>
                      <span className="ml-2 text-slate-400">{row.merchant_id}</span>
                    </td>
                    <td className="py-2.5 pr-4 text-slate-700">
                      {exceptionLabel(row.exception_type)}
                    </td>
                    <td className="py-2.5 pr-4 text-slate-500">
                      {formatDate(row.exception_date)}
                    </td>
                    <td className="py-2.5 pr-4 text-right text-slate-700">
                      {formatINR2(row.expected_amount)}
                    </td>
                    <td className="py-2.5 pr-4 text-right text-slate-700">
                      {formatINR2(row.recorded_amount)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-medium text-rose-700">
                      {formatINR2(row.financial_impact)}
                    </td>
                    <td className="py-2.5 pr-4">
                      <Badge tone={severityTone(row.severity)}>{row.severity}</Badge>
                    </td>
                    <td className="py-2.5">
                      <Badge tone={severityTone(row.status === 'open' ? 'medium' : 'low')}>
                        {row.status}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-right text-[11px] text-slate-400">
              {formatNumber(rows.length)} rows
            </p>
          </div>
        )}
      </Card>
    </div>
  )
}