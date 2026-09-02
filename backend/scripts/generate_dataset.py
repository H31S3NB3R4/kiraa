"""CLI entry point for the synthetic dataset generator.

Run from the ``backend/`` directory:

    python scripts/generate_dataset.py
    python scripts/generate_dataset.py --transactions 500 --window-days 56 \
        --exceptions-per-type 5 --out ../data/benchmark --name benchmark

The generator itself lives in ``app/services/dataset_generator.py`` so it can
be imported and unit-tested like any other module.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Allow running as a plain script: make ``backend/`` importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.dataset_generator import (  # noqa: E402
    DEFAULT_END_DATE,
    generate_dataset,
    summarize,
    write_dataset,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the deterministic synthetic finance dataset."
    )
    parser.add_argument("--transactions", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--window-days", type=int, default=28)
    parser.add_argument("--end-date", type=str, default=DEFAULT_END_DATE.isoformat())
    parser.add_argument("--exceptions-per-type", type=int, default=1)
    parser.add_argument("--customers", type=int, default=80)
    parser.add_argument(
        "--out", type=str, default=None,
        help="output directory (default: <repo>/data/generated)",
    )
    parser.add_argument("--name", type=str, default="dataset", help="base file name")
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else REPO_ROOT / "data" / "generated"
    end_date = date.fromisoformat(args.end_date)

    dataset = generate_dataset(
        transactions=args.transactions,
        seed=args.seed,
        end_date=end_date,
        window_days=args.window_days,
        exceptions_per_type=args.exceptions_per_type,
        customers=args.customers,
    )
    json_path, labels_path = write_dataset(dataset, out_dir, args.name)

    print(summarize(dataset))
    print(f"dataset: {json_path}")
    print(f"labels : {labels_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
