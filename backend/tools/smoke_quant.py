"""Full statistical baseline smoke on explicit synthetic inputs, never market evidence."""
import service  # Configure numerical threads before importing scientific libraries.
import contextlib
import io
import time
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from service.quant.baseline import compute_baseline
from service.quant.configuration import QuantConfig

n = np.arange(252)
prices = 90000 * np.exp(0.0004 * n + 0.01 * np.sin(n / 5))
frame = pd.DataFrame({"open": prices * .999, "high": prices * 1.01, "low": prices * .99,
    "close": prices, "volume": np.full(252, 1000000)}, index=pd.bdate_range(end="2026-10-14", periods=252))
started = time.monotonic()
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    result = compute_baseline("FPT", frame, exchange="HOSE", evaluated_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
        index_frame=frame / 100, config=replace(QuantConfig(), PRICE_INPUT_UNIT="VND"))
assert not result.get("error") and not result["component_errors"]
for name in ("arima", "garch", "hmm", "simulation_risk"):
    assert result[name] and not result[name].get("error"), name
print({"input_mode": "SYNTHETIC_FIXTURE", "seconds": round(time.monotonic() - started, 2),
    "components": len(result), "errors": result["component_errors"]})
