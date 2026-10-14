"""Keep metrics/series, omit raw Monte Carlo paths and explicitly report nonfinite values."""
from datetime import date, datetime

import numpy as np
import pandas as pd


def json_metrics(value, warnings: list[str] | None = None, path: str = ""):
    warnings = warnings if warnings is not None else []
    if isinstance(value, dict):
        return {str(key): json_metrics(item, warnings, f"{path}.{key}") for key, item in value.items()
            if key != "_mc_paths"}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [json_metrics(item, warnings, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, pd.Series):
        return {stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp): json_metrics(item, warnings, path)
            for stamp, item in value.items()}
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, (float, np.floating)):
        if not np.isfinite(value):
            warnings.append("NONFINITE_METRIC:" + path)
            return None
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise TypeError("Unsupported metric type: " + type(value).__name__)
