/**
 * Global app state: the selected merchant + date range every view shares
 * (Phase 10 shell requirements) — kept in React context so the sidebar
 * selector, KPI cards, and agent panel all read the same scope.
 */

import { createContext, useContext } from 'react'

export interface ScopeState {
  merchantId: string | null
  startDate: string | null
  endDate: string | null
}

export interface ScopeContextValue {
  scope: ScopeState
  setMerchantId: (id: string | null) => void
  setStartDate: (d: string | null) => void
  setEndDate: (d: string | null) => void
  /** Query params object for GET endpoints (undefined values dropped). */
  params: Record<string, string>
}

export const ScopeContext = createContext<ScopeContextValue | null>(null)

export const useScope = (): ScopeContextValue => {
  const ctx = useContext(ScopeContext)
  if (!ctx) throw new Error('useScope must be used inside <ScopeProvider>')
  return ctx
}
