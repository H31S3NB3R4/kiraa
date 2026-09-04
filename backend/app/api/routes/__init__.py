"""API route modules.

Phase 0 registers `health`; Phase 6 adds the agent chat router; Phase 8
adds the actions router (approve/reject/rollback — the only ledger-mutating
surface). Later phases add reconciliation, ledger, forecast, anomalies,
audit, and metrics routers.
"""
