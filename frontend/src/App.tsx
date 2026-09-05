/**
 * App shell (Phase 10): sidebar navigation, global merchant selector
 * (from `GET /api/merchants`) + date range, main routed content, and the
 * always-visible agent chat panel on the right. The selected merchant /
 * date scope flows to every page and the agent via `useScope`.
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Activity,
  Brain,
  ChartLine,
  ClipboardCheck,
  IndianRupee,
  ScrollText,
} from 'lucide-react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { getMerchants } from './api/endpoints'
import type { MerchantRow } from './api/types'
import { AgentPanel } from './components/AgentPanel'
import DashboardPage from './pages/DashboardPage'
import ReconciliationPage from './pages/ReconciliationPage'
import ForecastPage from './pages/ForecastPage'
import AnomaliesPage from './pages/AnomaliesPage'
import ActionsPage from './pages/ActionsPage'
import AuditPage from './pages/AuditPage'
import { ScopeContext, type ScopeContextValue } from './state/scope'

const NAV = [
  { to: '/', label: 'Dashboard', icon: Activity, end: true },
  { to: '/reconciliation', label: 'Reconciliation', icon: ClipboardCheck },
  { to: '/forecast', label: 'Forecast', icon: ChartLine },
  { to: '/anomalies', label: 'Anomalies', icon: Brain },
  { to: '/actions', label: 'Actions', icon: IndianRupee },
  { to: '/audit', label: 'Audit', icon: ScrollText },
]

export default function App() {
  const [merchants, setMerchants] = useState<MerchantRow[]>([])
  const [merchantId, setMerchantId] = useState<string | null>(null)
  const [startDate, setStartDate] = useState<string | null>(null)
  const [endDate, setEndDate] = useState<string | null>(null)

  useEffect(() => {
    getMerchants()
      .then((m) => setMerchants(m.rows))
      .catch(() => setMerchants([]))
  }, [])

  const params = useMemo(() => {
    const p: Record<string, string> = {}
    if (merchantId) p.merchant_id = merchantId
    if (startDate) p.start_date = startDate
    if (endDate) p.end_date = endDate
    return p
  }, [merchantId, startDate, endDate])

  const scopeValue: ScopeContextValue = {
    scope: { merchantId, startDate, endDate },
    setMerchantId,
    setStartDate,
    setEndDate,
    params,
  }


  return (
    <ScopeContext.Provider value={scopeValue}>
      <div className="flex h-screen overflow-hidden bg-slate-100 text-slate-900">
        {/* Sidebar */}
        <aside className="flex w-52 shrink-0 flex-col border-r border-slate-200 bg-white">
          <div className="flex items-center gap-2 px-4 py-4">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-600 text-sm font-bold text-white">
              K
            </span>
            <div>
              <p className="text-sm font-semibold leading-tight">Kiraa</p>
              <p className="text-[10px] text-slate-500">reconciliation console</p>
            </div>
          </div>
          <nav className="flex-1 space-y-1 px-2 py-2">
            {NAV.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 rounded-lg px-3 py-2 text-xs font-medium transition ${
                    isActive
                      ? 'bg-indigo-50 text-indigo-700'
                      : 'text-slate-600 hover:bg-slate-100'
                  }`
                }
              >
                <Icon className="h-4 w-4" />
                {label}
              </NavLink>
            ))}
          </nav>
          <p className="px-4 py-3 text-[10px] leading-relaxed text-slate-400">
            Synthetic demo data — nothing here moves real money. Read-only by
            default; ledger writes happen only through the reviewed Actions
            flow.
          </p>
        </aside>

        {/* Main column */}
        <div className="flex min-w-0 flex-1 flex-col">
          {/* Global scope bar */}
          <header className="flex flex-wrap items-center gap-3 border-b border-slate-200 bg-white px-6 py-3">
            <label className="flex items-center gap-2 text-xs text-slate-600">
              Merchant
              <select
                value={merchantId ?? ''}
                onChange={(e) => setMerchantId(e.target.value || null)}
                className="rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-800 shadow-sm"
              >
                <option value="">All merchants</option>
                {merchants.map((m) => (
                  <option key={m.merchant_id} value={m.merchant_id}>
                    {m.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 text-xs text-slate-600">
              From
              <input
                type="date"
                value={startDate ?? ''}
                onChange={(e) => setStartDate(e.target.value || null)}
                className="rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-800 shadow-sm"
              />
            </label>
            <label className="flex items-center gap-2 text-xs text-slate-600">
              To
              <input
                type="date"
                value={endDate ?? ''}
                onChange={(e) => setEndDate(e.target.value || null)}
                className="rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-800 shadow-sm"
              />
            </label>
            <span className="ml-auto flex items-center gap-2">
              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 ring-1 ring-amber-200">
                synthetic demo data
              </span>
              <span className="font-mono text-[10px] text-slate-400">
                {merchantId ?? 'all merchants'}
              </span>
            </span>
          </header>

          {/* Content + agent panel */}
          <div className="flex min-h-0 flex-1">
            <main className="min-w-0 flex-1 overflow-y-auto px-6 py-6">
              <Routes>
                <Route path="/" element={<DashboardPage />} />
                <Route path="/reconciliation" element={<ReconciliationPage />} />
                <Route path="/forecast" element={<ForecastPage />} />
                <Route path="/anomalies" element={<AnomaliesPage />} />
                <Route path="/actions" element={<ActionsPage />} />
                <Route path="/audit" element={<AuditPage />} />
              </Routes>
            </main>
            <aside className="hidden w-96 shrink-0 border-l border-slate-200 bg-white lg:block">
              <AgentPanel />
            </aside>
          </div>
        </div>
      </div>
    </ScopeContext.Provider>
  )
}