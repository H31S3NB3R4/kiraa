"""API route modules.

Phase 0 registers `health`; Phase 6 adds the agent chat router; Phase 8
adds the actions router (approve/reject/rollback — the only ledger-mutating
surface); Phase 9 adds the read/report routers: reconciliation, ledger,
forecast, anomalies, exceptions, runs, audit, and metrics; Phase 10 adds
the proposals and merchants listing routers behind the dashboard.
"""
