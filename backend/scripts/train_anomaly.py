r"""CLI entry point for the Phase 5 anomaly-model trainer.

Run from the ``backend/`` directory:

    ..\.venv\Scripts\python.exe scripts\train_anomaly.py
    ..\.venv\Scripts\python.exe scripts\train_anomaly.py --out ml/artifacts/iforest-v1.joblib

Trains the Isolation Forest on the fixed pure-normal history, calibrates
the flag threshold on the pooled normal scores, prints a summary, and
persists the versioned joblib bundle under ``ml/artifacts/`` (gitignored).
Exit codes: 0 success, 1 save failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script: make ``backend/`` importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml.train_anomaly import (  # noqa: E402
    N_ESTIMATORS,
    THRESHOLD_PERCENTILE,
    TRAIN_KWS,
    VALIDATION_KWS,
    train_model,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Train and persist the Isolation-Forest anomaly model (Phase 5)."
    )
    parser.add_argument(
        "--out", type=str, default=None,
        help="artifact path (default: ml/artifacts/<MODEL_VERSION>.joblib)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="print only the artifact path",
    )
    args = parser.parse_args(argv)

    bundle = train_model()
    path = bundle_save(bundle, args.out)

    if not args.quiet:
        training = bundle["training"]
        print("Trained anomaly model (all training data is synthetic)")
        print(f"  model           : {bundle['model_name']} ({bundle['model_version']})")
        print(f"  n_estimators    : {N_ESTIMATORS}")
        print(f"  features        : {', '.join(bundle['feature_names'])}")
        print(f"  train records   : {training['train_records']} (seed {TRAIN_KWS['seed']})")
        print(
            f"  calibration     : {training['calibration_records']} normal records "
            f"at p{THRESHOLD_PERCENTILE * 100:g}"
        )
        print(f"  threshold       : {bundle['threshold']}")
        print(f"  sklearn         : {bundle['sklearn_version']}")
    print(f"artifact: {path}")
    return 0


def bundle_save(bundle: dict, out: str | None) -> Path:
    from ml.train_anomaly import save_model

    return save_model(bundle, out)


if __name__ == "__main__":
    raise SystemExit(main())
