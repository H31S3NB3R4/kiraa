"""ML package (Phase 5): anomaly feature engineering, training, and artifacts.

- ``ml.features``:      pure feature pipeline shared by training and serving
- ``ml.train_anomaly``: Isolation-Forest model, calibration, persistence

The directory is deliberately outside ``app/``: the model can be trained
from generated datasets without the app stack, while ``app.tools.anomalies``
consumes the fitted artifact (or the deterministic in-process fallback) at
request time. Trained binaries live under ``ml/artifacts/`` (gitignored).
"""
