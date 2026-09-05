/**
 * Audit view (Phase 10): two read-only panes.
 *
 * 1. Agent-run history (`GET /api/runs`) — click a run to expand its
 *    full tool sequence + transcript from `GET /api/runs/{run_id}`,
 *    including each tool's arguments, result, status, latency, and the
 *    record ids / monetary impact extracted from the result payload.
 * 2. Action history (`GET /api/audit`) — every human decision, post,
 *    and rollback with actor + object + agent-run linkage (FR-9).
 *
 * Phase 11: every tool row carries an ML-anomaly vs deterministic
 * label, status icon, and latency; record-id chips and impact figures
 * come straight from the persisted tool results.
 */

import { useEffect, useState } from 'react'
import { Check, ChevronDown, History, ScrollText, Wrench, X } from 'lucide-react'
import { getAudit, getRunDetail, getRuns } from '../api/endpoints'
import type { AuditEventRow, RunDetailResponse, RunSummaryRow, RunToolCall } from '../api/types'
import { formatDateTime, formatINR, formatINR2, statusTone } from '../lib/format'
import { Badge, Card, EmptyState, ErrorState, Loading } from '../components/ui'

const ML_TOOLS = new Set(['detect_anomalies'])
const DETERMINISTIC_TOOLS = new Set([
  'run_reconciliation',
  'query_ledger',
  'query_exceptions',
  'propose_journal_entry',
  'verify_gst_invoice',
  'forecast_cashflow',
])

/** Pull a monetary-impact figure out of one tool result dict. */
function impactFromResult(result: Record<string, unknown> | null): string | null {
  if (result == null) return null
  if (typeof result.total_financial_impact === 'number') {
    return `impact ${formatINR(result.total_financial_impact)}`
  }
  if (typeof result.financial_impact === 'number') {
    return `impact ${formatINR(result.financial_impact)}`
  }
  if (typeof result.amount === 'number') return `amount ${formatINR2(result.amount)}`
  if (typeof result.min_projected_cash === 'number') {
    return `min cash ${formatINR(result.min_projected_cash)}`
  }
  if (typeof result.projected_ending_balance === 'number') {
    return `ends ${formatINR(result.projected_ending_balance)}`
  }
  return null
}

/** Extract record ids (TXN / SET / INV / PROP prefixed) from a tool result. */
function recordIdsFromResult(result: Record<string, unknown> | null): string[] {
  if (result == null) return []
  const ids: string[] = []
  const push = (v: unknown) => {
    if (typeof v === 'string' && /^(TXN-|SET-|INV-|PROP-)/.test(v)) ids.push(v)
  }
  push(result.transaction_id)
  push(result.settlement_id)
  push(result.invoice_id)
  push(result.proposal_id)
  if (Array.isArray(result.exceptions)) {
    for (const exc of result.exceptions as Record<string, unknown>[]) push(exc.transaction_id)
  }
  if (Array.isArray(result.proposals)) {
    for (const p of result.proposals as Record<string, unknown>[]) push(p.proposal_id)
  }
  return [...new Set(ids)].slice(0, 4)
}

/** One expanded tool row: status, args, result impact + record ids. */
function ToolRow({ call }: { call: RunToolCall }) {
  const [open, setOpen] = useState(false)
  const ml = ML_TOOLS.has(call.tool_name)
  const deterministic = DETERMINISTIC_TOOLS.has(call.tool_name)
  const failed = call.status !== 'ok'
  const ids = recordIdsFromResult(call.result ?? null)
  const impact = impactFromResult(call.result ?? null)
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50/60">
      <button
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        onClick={() => setOpen(!open)}
      >
        {failed ? (
          <X className="h-3.5 w-3.5 shrink-0 text-rose-600" />
        ) : (
          <Check className="h-3.5 w-3.5 shrink-0 text-emerald-600" />
        )}
        <span className="font-mono text-xs font-medium text-slate-700">{call.tool_name}</span>
        {ml && (
          <span className="rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-semibold text-violet-700">
            ML anomaly
          </span>
        )}
        {deterministic && (
          <span className="rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700">
            deterministic
          </span>
        )}
        {impact && <span className="text-[10px] text-slate-500">{impact}</span>}
        {ids.length > 0 && (
          <span className="flex items-center gap-1">
            {ids.map((id) => (
              <span
                key={id}
                className="rounded bg-slate-200/70 px-1.5 py-0.5 font-mono text-[10px] text-slate-600"
              >
                {id}
              </span>
            ))}
          </span>
        )}
        <span className="ml-auto text-[10px] text-slate-400">
          {Math.round(call.latency_ms)} ms
        </span>
        <ChevronDown
          className={`h-3.5 w-3.5 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>
      {open && (
        <div className="space-y-1 border-t border-slate-200 px-3 py-2 text-[11px] text-slate-600">
          <div>
            <span className="text-slate-400">args: </span>
            <code className="break-all">{JSON.stringify(call.arguments)}</code>
          </div>
          <div>
            <span className="text-slate-400">result: </span>
            <code className="break-all">{JSON.stringify(call.result)}</code>
          </div>
          {call.error && (
            <div className="text-rose-600">
              <span className="text-slate-400">error: </span>
              {call.error}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** One run row: expandable tool sequence + final answer + transcript. */
function RunRow({ run }: { run: RunSummaryRow }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<RunDetailResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  function toggle() {
    setOpen(!open)
    if (!open && detail === null) {
      getRunDetail(run.run_id)
        .then((d) => setDetail(d))
        .catch((e: unknown) =>
          setError(e instanceof Error ? e.message : 'Failed to load run detail.'),
        )
    }
  }

  return (
    <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
      <button onClick={toggle} className="flex w-full items-center gap-3 px-4 py-3 text-left">
        <span className="font-mono text-[11px] text-slate-400">{run.run_id}</span>
        <span className="min-w-0 flex-1 truncate text-xs text-slate-700">{run.user_query}</span>
        <Badge
          tone={
            run.status === 'completed'
              ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
              : 'bg-rose-50 text-rose-700 ring-rose-200'
          }
        >
          {run.status}
        </Badge>
        <span className="shrink-0 text-[10px] text-slate-400">
          {run.turn_count} turns · {run.tool_call_count} tools
        </span>
        <ChevronDown
          className={`h-4 w-4 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>
      {open && (
        <div className="space-y-3 border-t border-slate-100 px-4 py-3">
          {error ? (
            <ErrorState message={error} />
          ) : detail === null ? (
            <Loading label="Loading run…" />
          ) : (
            <>
              <div className="space-y-1">
                <div className="flex items-center gap-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">
                  <Wrench className="h-3 w-3" /> tool sequence ({detail.tool_call_count})
                </div>
                {detail.tool_calls.length === 0 ? (
                  <p className="text-xs text-slate-500">No tool calls.</p>
                ) : (
                  detail.tool_calls.map((c) => <ToolRow key={c.seq} call={c} />)
                )}
              </div>
              {detail.final_response && (
                <div>
                  <div className="text-[10px] font-medium uppercase tracking-wide text-slate-400">
                    final answer
                  </div>
                  <p className="mt-1 whitespace-pre-wrap rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-700 ring-1 ring-slate-200">
                    {detail.final_response}
                  </p>
                </div>
              )}
              <div>
                <div className="text-[10px] font-medium uppercase tracking-wide text-slate-400">
                  transcript
                </div>
                <div className="mt-1 space-y-1">
                  {detail.messages.map((m) => (
                    <div key={m.seq} className="flex gap-2 text-[11px]">
                      <span
                        className={`w-14 shrink-0 font-medium ${
                          m.role === 'user'
                            ? 'text-indigo-600'
                            : m.role === 'model'
                              ? 'text-emerald-600'
                              : 'text-slate-400'
                        }`}
                      >
                        {m.role}
                      </span>
                      <span className="min-w-0 flex-1 break-words text-slate-600">
                        {JSON.stringify(m.content)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
              <p className="text-right text-[10px] text-slate-400">
                started {formatDateTime(detail.started_at)} ·{' '}
                {Math.round(detail.total_llm_latency_ms)} ms LLM
              </p>
            </>
          )}
        </div>
      )}
    </div>
  )
}

export default function AuditPage() {
  const [runs, setRuns] = useState<RunSummaryRow[] | null>(null)
  const [audit, setAudit] = useState<AuditEventRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setError(null)
    Promise.all([
      getRuns({ limit: 50 }),
      getAudit({ limit: 50 }),
    ])
      .then(([r, a]) => {
        if (cancelled) return
        setRuns(r.rows)
        setAudit(a.rows)
      })
      .catch((e: unknown) => {
        if (!cancelled)
          setError(e instanceof Error ? e.message : 'Failed to load audit data.')
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (error) return <ErrorState message={error} />

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Audit</h1>
        <p className="text-xs text-slate-500">
          Every agent run with its full tool sequence, and every human decision with before/after
          states — append-only (FR-9).
        </p>
      </div>

      <section>
        <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-800">
          <History className="h-4 w-4 text-indigo-600" />
          Agent run history
        </h2>
        {runs === null ? (
          <Loading label="Loading runs…" />
        ) : runs.length === 0 ? (
          <Card>
            <EmptyState message="No agent runs yet — start a conversation in the chat panel." />
          </Card>
        ) : (
          <div className="space-y-2">
            {runs.map((run) => (
              <RunRow key={run.run_id} run={run} />
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-800">
          <ScrollText className="h-4 w-4 text-indigo-600" />
          Action history
        </h2>
        {audit === null ? (
          <Loading label="Loading audit events…" />
        ) : audit.length === 0 ? (
          <Card>
            <EmptyState message="No audit events yet — approve or reject a proposal on the Actions page." />
          </Card>
        ) : (
          <Card>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-slate-200 text-[11px] uppercase tracking-wide text-slate-400">
                    <th className="py-2 pr-4 font-medium">When</th>
                    <th className="py-2 pr-4 font-medium">Actor</th>
                    <th className="py-2 pr-4 font-medium">Action</th>
                    <th className="py-2 pr-4 font-medium">Object</th>
                    <th className="py-2 font-medium">Run</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.map((ev) => (
                    <tr key={ev.event_id} className="border-b border-slate-50 last:border-0">
                      <td className="py-2.5 pr-4 text-slate-500">{formatDateTime(ev.created_at)}</td>
                      <td className="py-2.5 pr-4 text-slate-700">{ev.actor}</td>
                      <td className="py-2.5 pr-4">
                        <Badge tone={statusTone(ev.action)}>{ev.action}</Badge>
                      </td>
                      <td className="py-2.5 pr-4">
                        <span className="font-mono text-[11px] text-slate-600">{ev.object_id}</span>
                        <span className="ml-2 text-slate-400">{ev.object_type}</span>
                      </td>
                      <td className="py-2.5">
                        {ev.agent_run_id ? (
                          <span className="font-mono text-[11px] text-slate-500">
                            {ev.agent_run_id}
                          </span>
                        ) : (
                          <span className="text-slate-400">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </section>
    </div>
  )
}
