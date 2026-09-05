/**
 * Actions view (Phase 10): the human review queue. `GET /api/proposals`
 * lists journal proposals (pending + history); approve / reject /
 * rollback posts to the Phase 8 action routes with a client-generated
 * idempotency key and the acting approver — the only paths that write
 * the ledger, and every result lands on the audit trail.
 */

import { useEffect, useMemo, useState } from 'react'
import { Check, RotateCcw, X } from 'lucide-react'
import {
  approveProposal,
  getProposals,
  rejectProposal,
  rollbackProposal,
} from '../api/endpoints'
import type { ActionResponse, ProposalRow } from '../api/types'
import { useScope } from '../state/scope'
import { formatDate, formatINR2, formatPct, statusTone } from '../lib/format'
import { Badge, Card, EmptyState, Loading } from '../components/ui'

const FILTERS = ['all', 'pending', 'approved', 'rejected', 'rolled_back'] as const

/** Client-generated idempotency key (PRD section 14). */
function newKey(): string {
  const rand =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random()}`
  return `ui-${rand}`.slice(0, 64)
}

export default function ActionsPage() {
  const { params } = useScope()
  const [rows, setRows] = useState<ProposalRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>('all')
  const [approver, setApprover] = useState('analyst@kiraa')
  const [busyId, setBusyId] = useState<string | null>(null)
  const [result, setResult] = useState<ActionResponse | null>(null)
  const [resultFor, setResultFor] = useState<string | null>(null)

  const load = () => {
    getProposals({ ...params, limit: 200 })
      .then((r) => setRows(r.rows))
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : 'Failed to load proposals.'),
      )
  }

  useEffect(load, [params])

  const filtered = useMemo(
    () => (rows ?? []).filter((r) => filter === 'all' || r.status === filter),
    [rows, filter],
  )

  async function decide(
    row: ProposalRow,
    kind: 'approve' | 'reject' | 'rollback',
  ): Promise<void> {
    setBusyId(row.proposal_id)
    setError(null)
    setResult(null)
    setResultFor(null)
    try {
      const body = { idempotency_key: newKey(), approver }
      const fn =
        kind === 'approve' ? approveProposal : kind === 'reject' ? rejectProposal : rollbackProposal
      const res = await fn(row.proposal_id, body)
      setResult(res)
      setResultFor(row.proposal_id)
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : `Failed to ${kind} proposal.`)
    } finally {
      setBusyId(null)
    }
  }


  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Actions</h1>
        <p className="text-xs text-slate-500">
          Human review queue — approving posts the correction to the ledger via the idempotent
          Phase 8 action routes; every decision lands on the audit trail.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1 shadow-sm">
          {FILTERS.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`rounded px-2.5 py-1 text-xs font-medium transition ${
                filter === f ? 'bg-indigo-600 text-white' : 'text-slate-600 hover:bg-slate-100'
              }`}
            >
              {f}
            </button>
          ))}
        </div>
        <label className="ml-auto flex items-center gap-2 text-xs text-slate-600">
          approver
          <input
            value={approver}
            onChange={(e) => setApprover(e.target.value)}
            className="w-44 rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-800 shadow-sm outline-none focus:border-indigo-400"
          />
        </label>
      </div>

      {error && (
        <p className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-800 ring-1 ring-rose-200">
          {error}
        </p>
      )}
      {result && (
        <p
          className={`rounded-lg px-3 py-2 text-xs ring-1 ${
            result.idempotent_replay
              ? 'bg-sky-50 text-sky-800 ring-sky-200'
              : 'bg-emerald-50 text-emerald-800 ring-emerald-200'
          }`}
        >
          {result.message}
          {result.idempotent_replay && ' (idempotent replay — no duplicate post)'}
          {result.ledger_entry_id && (
            <>
              {' '}
              · ledger entry <span className="font-mono">{result.ledger_entry_id}</span>
            </>
          )}
          {' '}
          · audit event <span className="font-mono">{result.audit_event_id}</span>
        </p>
      )}

      {rows === null ? (
        <Loading label="Loading proposals…" />
      ) : filtered.length === 0 ? (
        <Card>
          <EmptyState
            message={
              rows.length === 0
                ? 'No journal proposals yet — ask the agent to investigate an exception.'
                : 'No proposals match this filter.'
            }
          />
        </Card>
      ) : (
        <div className="space-y-3">
          {filtered.map((row) => (
            <Card key={row.proposal_id}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs font-semibold text-slate-800">
                  {row.proposal_id}
                </span>
                <Badge tone={statusTone(row.status)}>{row.status}</Badge>
                {row.merchant_name && (
                  <span className="text-xs text-slate-500">{row.merchant_name}</span>
                )}
                {row.transaction_id && (
                  <span className="font-mono text-[11px] text-slate-400">
                    for {row.transaction_id}
                  </span>
                )}
                <span className="ml-auto text-sm font-semibold text-slate-900">
                  {formatINR2(row.amount)}
                </span>
              </div>

              <p className="mt-2 text-xs text-slate-600">{row.narrative}</p>

              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-500">
                <span>
                  Dr <span className="font-mono">{row.debit_account}</span> / Cr{' '}
                  <span className="font-mono">{row.credit_account}</span>
                </span>
                <span>confidence {formatPct(row.confidence * 100, 0)}</span>
                {row.entry_date && <span>entry {formatDate(row.entry_date)}</span>}
                <span>created {formatDate(row.created_at)}</span>
                {row.evidence_ids.length > 0 && (
                  <span className="flex flex-wrap items-center gap-1">
                    evidence:
                    {row.evidence_ids.map((id) => (
                      <span
                        key={id}
                        className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600"
                      >
                        {id}
                      </span>
                    ))}
                  </span>
                )}
              </div>

              <div className="mt-3 flex items-center gap-2">
                <button
                  onClick={() => decide(row, 'approve')}
                  disabled={busyId !== null || row.status !== 'pending'}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-emerald-700 disabled:opacity-40"
                >
                  <Check className="h-3.5 w-3.5" />
                  Approve
                </button>
                <button
                  onClick={() => decide(row, 'reject')}
                  disabled={busyId !== null || row.status !== 'pending'}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-rose-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-rose-700 disabled:opacity-40"
                >
                  <X className="h-3.5 w-3.5" />
                  Reject
                </button>
                <button
                  onClick={() => decide(row, 'rollback')}
                  disabled={busyId !== null || row.status !== 'approved'}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-violet-700 disabled:opacity-40"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  Rollback
                </button>
                {resultFor === row.proposal_id && result && (
                  <span className="ml-auto text-[11px] text-emerald-700">done</span>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
