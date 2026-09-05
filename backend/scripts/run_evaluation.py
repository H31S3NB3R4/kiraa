r"""CLI entry point for the Phase 12 evaluation harness.

Run from the ``backend/`` directory:

    ..\.venv\Scripts\python.exe scripts\run_evaluation.py
    ..\.venv\Scripts\python.exe scripts\run_evaluation.py --json-only
    ..\.venv\Scripts\python.exe scripts\run_evaluation.py --database-url sqlite:///./data/eval.db

Seeds the fixed benchmark dataset (``data/benchmark/benchmark.json`` — 500
synthetic transactions, seed 42) into a dedicated evaluation database,
scores the reconciliation engine, the ML anomaly layer, and the agent
loop against the seeded ground truth, prints the todo Phase 12 benchmark
table (real measured numbers — never invented), and writes a machine-
readable JSON report next to the database.

Exit codes: 0 success, 1 evaluation failure, 2 dataset/seed failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a plain script: make ``backend/`` importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import redact_credentials  # noqa: E402
from app.services.db_seed import (  # noqa: E402
    SeedError,
    build_engine,
    load_dataset_file,
    seed_database,
)
from app.services.evaluation import (  # noqa: E402
    EvaluationError,
    benchmark_table,
    evaluation_report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = REPO_ROOT / "data" / "benchmark" / "benchmark.json"
DEFAULT_DB = REPO_ROOT / "data" / "evaluation.db"
DEFAULT_REPORT = REPO_ROOT / "data" / "generated" / "evaluation_report.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Phase 12 evaluation harness over the fixed benchmark dataset."
    )
    parser.add_argument(
        "--dataset", type=str, default=str(DEFAULT_DATASET),
        help="benchmark dataset JSON (default: <repo>/data/benchmark/benchmark.json)",
    )
    parser.add_argument(
        "--database-url", type=str, default=None,
        help="evaluation database URL (default: sqlite:///<repo>/data/evaluation.db)",
    )
    parser.add_argument(
        "--report", type=str, default=str(DEFAULT_REPORT),
        help="machine-readable JSON report output path",
    )
    parser.add_argument(
        "--json-only", action="store_true",
        help="print only the JSON report (no human-readable table)",
    )
    args = parser.parse_args(argv)

    url = args.database_url or f"sqlite:///{DEFAULT_DB}"
    try:
        dataset = load_dataset_file(args.dataset)
        engine = build_engine(url)
        try:
            # The evaluation database is a dedicated scratch DB, so every
            # run rebuilds it from the fixed benchmark (idempotent,
            # deterministic — identical input, identical report).
            seed_database(engine, dataset, recreate=True)
        finally:
            pass
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Import lazily: the evaluation service pulls the whole agent stack.
    from sqlalchemy.orm import Session

    try:
        with Session(engine) as db:
            report = evaluation_report(db, include_agent=True)
    except EvaluationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )

    meta = dataset.get("metadata", {})
    if not args.json_only:
        print("Kiraa evaluation harness (all data is synthetic)")
        print(
            f"  dataset : {args.dataset} "
            f"(seed {meta.get('seed')}, {meta.get('transactions')} transactions)"
        )
        print(f"  database: {redact_credentials(url)}")
        print()
        print(benchmark_table(report))
        print()
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())