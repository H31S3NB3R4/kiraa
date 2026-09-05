"""CLI entry point for the Phase 2 database seeder.

Run from the ``backend/`` directory:

    python scripts/seed_db.py                       # default dataset + DATABASE_URL
    python scripts/seed_db.py --recreate            # drop & rebuild first
    python scripts/seed_db.py --dataset ../data/benchmark/benchmark.json
    python scripts/seed_db.py --database-url sqlite:///./data/demo.db

Exit codes: 0 success, 1 database already seeded, 2 dataset load failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script: make ``backend/`` importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings, redact_credentials  # noqa: E402
from app.services.db_seed import (  # noqa: E402
    SeedError,
    build_engine,
    load_dataset_file,
    seed_database,
    summarize_counts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Create the schema and seed the database from a dataset JSON."
    )
    parser.add_argument(
        "--dataset", type=str,
        default=str(REPO_ROOT / "data" / "generated" / "dataset.json"),
        help="dataset JSON file (default: <repo>/data/generated/dataset.json)",
    )
    parser.add_argument(
        "--database-url", type=str, default=None,
        help="SQLAlchemy URL (default: DATABASE_URL from the environment)",
    )
    parser.add_argument(
        "--recreate", action="store_true",
        help="drop all tables before seeding (destructive)",
    )
    args = parser.parse_args(argv)

    try:
        dataset = load_dataset_file(args.dataset)
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    url = args.database_url or settings.database_url
    engine = build_engine(url)
    try:
        counts = seed_database(engine, dataset, recreate=args.recreate)
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print("Seeded database (all data is synthetic)")
    print(f"  dataset: {args.dataset}")
    print(f"  url    : {redact_credentials(url)}")
    print(summarize_counts(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
