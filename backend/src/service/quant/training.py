"""Mature labels, temporal validation and reusable LightGBM artifacts."""
from dataclasses import dataclass

import lightgbm as lgb
import numpy as np
import pandas as pd

from service.quant.configuration import QuantConfig, engine_context
from service.quant.engines import CrossSectionalLightGBMEngine as Features


@dataclass(frozen=True)
class TrainingPolicy:
    version: str = "temporal-beta-1"
    holdout_sessions: int = 40
    minimum_train_rows: int = 400
    minimum_symbols: int = 5
    minimum_validation_rows: int = 150
    minimum_rank_days: int = 20
    minimum_rank_ic: float = 0.02
    maximum_baseline_rmse_ratio: float = 1.0
    threads: int = 1


def training_panel(data: dict[str, pd.DataFrame], index: pd.DataFrame,
                   sector_map: dict[str, str], config: QuantConfig) -> pd.DataFrame:
    if index.empty or not index.index.is_unique or not index.index.is_monotonic_increasing:
        raise ValueError("Invalid index sessions")
    if not data or set(data) != set(sector_map) or any(not s for s in sector_map.values()):
        raise ValueError("Explicit sector membership required")
    target_session = index.index[-1]
    sector_returns = Features.sector_return_series(data, sector_map)
    market_returns = index.close.pct_change()
    panels = []
    with engine_context(config):
        for symbol, frame in sorted(data.items()):
            if frame.index[-1] != target_session or not frame.index.is_unique or not frame.index.is_monotonic_increasing:
                raise ValueError("Mixed or invalid target sessions")
            if not frame.index.isin(index.index).all():
                raise ValueError("Missing index sessions")
            features = Features._feature_frame(frame, market_returns, sector_returns[sector_map[symbol]],
                config.FORECAST_HORIZON)
            features["label_end"] = pd.Series(frame.index, index=frame.index).shift(-config.FORECAST_HORIZON)
            features["date"], features["symbol"] = features.index, symbol
            features = features.dropna(subset=Features.FEATURE_COLUMNS + ["target_abs_pct", "target_alpha_pct", "label_end"])
            panels.append(features.reset_index(drop=True))
    panel = pd.concat(panels, ignore_index=True)
    if panel.empty or not np.isfinite(panel[Features.FEATURE_COLUMNS + ["target_abs_pct", "target_alpha_pct"]]).all().all():
        raise ValueError("No finite mature training rows")
    return panel.sort_values(["date", "symbol"]).reset_index(drop=True)


def _diagnostics(frame: pd.DataFrame, absolute: np.ndarray, alpha: np.ndarray) -> dict:
    actual_abs, actual_alpha = frame.target_abs_pct.to_numpy(), frame.target_alpha_pct.to_numpy()
    alpha_error, absolute_error = alpha - actual_alpha, absolute - actual_abs
    rank_ics = []
    diagnostic = frame[["date", "target_alpha_pct"]].copy()
    diagnostic["prediction"] = alpha
    for _, group in diagnostic.groupby("date"):
        if len(group) >= 5 and group.prediction.nunique() > 1 and group.target_alpha_pct.nunique() > 1:
            value = group.prediction.corr(group.target_alpha_pct, method="spearman")
            if pd.notna(value):
                rank_ics.append(float(value))
    return {"mae_abs_pct": float(np.mean(np.abs(absolute_error))),
        "rmse_abs_pct": float(np.sqrt(np.mean(absolute_error ** 2))),
        "mae_alpha_pct": float(np.mean(np.abs(alpha_error))),
        "rmse_alpha_pct": float(np.sqrt(np.mean(alpha_error ** 2))),
        "directional_accuracy_ratio": float(np.mean(np.sign(absolute) == np.sign(actual_abs))),
        "rank_ic": float(np.mean(rank_ics)) if rank_ics else None,
        "rank_days": len(rank_ics), "rows": len(frame), "sessions": frame.date.nunique()}


def train_artifact(data: dict[str, pd.DataFrame], index: pd.DataFrame, sector_map: dict[str, str],
                   config: QuantConfig, policy: TrainingPolicy | None = None, incumbent_artifact: dict | None = None) -> dict:
    policy = policy or TrainingPolicy()
    if not 1 <= policy.threads <= 8 or policy.holdout_sessions < 20:
        raise ValueError("Invalid training policy")
    panel = training_panel(data, index, sector_map, config)
    dates = sorted(panel.date.unique())
    split = len(dates) - policy.holdout_sessions
    if split <= config.FORECAST_HORIZON + 60:
        raise ValueError("Insufficient temporal history")
    validation_start = pd.Timestamp(dates[split])
    embargo_end = pd.Timestamp(dates[split - config.FORECAST_HORIZON - 1])
    train = panel[panel.label_end <= embargo_end]
    validation = panel[panel.date >= validation_start]
    if len(train) < policy.minimum_train_rows or train.symbol.nunique() < policy.minimum_symbols:
        raise ValueError("Insufficient training rows/symbols")
    if len(validation) < policy.minimum_validation_rows or validation.symbol.nunique() < policy.minimum_symbols:
        raise ValueError("Insufficient validation coverage")
    if train.label_end.max() >= validation.date.min() or panel.label_end.max() > index.index[-1]:
        raise ValueError("Temporal leakage or immature labels")
    params = dict(n_estimators=180, learning_rate=0.035, num_leaves=15, max_depth=4,
        min_child_samples=35, subsample=0.85, colsample_bytree=0.85, reg_alpha=0.2,
        reg_lambda=1.0, random_state=42, n_jobs=policy.threads, verbosity=-1)
    absolute, alpha = lgb.LGBMRegressor(**params), lgb.LGBMRegressor(**params)
    x = train[Features.FEATURE_COLUMNS].astype(float)
    absolute.fit(x, train.target_abs_pct)
    alpha.fit(x, train.target_alpha_pct)
    validation_x = validation[Features.FEATURE_COLUMNS].astype(float)
    metrics = _diagnostics(validation, absolute.predict(validation_x), alpha.predict(validation_x))
    baseline = _diagnostics(validation, np.zeros(len(validation)), np.zeros(len(validation)))
    incumbent_comparison = None
    if incumbent_artifact:
        if incumbent_artifact["horizon"] != config.FORECAST_HORIZON or incumbent_artifact["feature_columns"] != Features.FEATURE_COLUMNS:
            raise ValueError("Incumbent contract mismatch")
        incumbent_abs = lgb.Booster(model_str=incumbent_artifact["absolute_model"])
        incumbent_alpha = lgb.Booster(model_str=incumbent_artifact["alpha_model"])
        incumbent_comparison = {"metrics": _diagnostics(validation,
            incumbent_abs.predict(validation_x, num_threads=policy.threads),
            incumbent_alpha.predict(validation_x, num_threads=policy.threads)),
            "training_overlaps_holdout": incumbent_artifact["cutoff_session"] >= validation_start.date().isoformat(),
            "usage": "DIAGNOSTIC_ONLY_IF_TRAINING_OVERLAPS_HOLDOUT"}
    reasons = []
    if metrics["rank_days"] < policy.minimum_rank_days or metrics["rank_ic"] is None:
        reasons.append("RANK_COVERAGE")
    elif metrics["rank_ic"] < policy.minimum_rank_ic:
        reasons.append("RANK_IC")
    for field in ("rmse_abs_pct", "rmse_alpha_pct"):
        if metrics[field] > baseline[field] * policy.maximum_baseline_rmse_ratio:
            reasons.append("BASELINE_" + field.upper())
    # Refit mature rows only after holdout metrics were frozen. Serialized boosters
    # avoid pickle/code execution and are shared through model persistence.
    full_x = panel[Features.FEATURE_COLUMNS].astype(float)
    absolute.fit(full_x, panel.target_abs_pct)
    alpha.fit(full_x, panel.target_alpha_pct)
    return {"format": "lightgbm-text-1", "lightgbm_version": lgb.__version__,
        "feature_columns": Features.FEATURE_COLUMNS, "horizon": config.FORECAST_HORIZON,
        "cutoff_session": index.index[-1].date().isoformat(),
        "absolute_model": absolute.booster_.model_to_string(), "alpha_model": alpha.booster_.model_to_string(),
        "validation": {"policy": policy.__dict__, "accepted": not reasons, "reasons": reasons,
            "metrics": metrics, "baseline": baseline, "incumbent_comparison": incumbent_comparison,
            "train_rows": len(train), "refit_rows": len(panel),
            "train_feature_end": train.date.max().date().isoformat(),
            "train_label_end": train.label_end.max().date().isoformat(),
            "validation_start": validation_start.date().isoformat(),
            "validation_end": validation.date.max().date().isoformat(),
            "purge_sessions": config.FORECAST_HORIZON, "embargo_sessions": config.FORECAST_HORIZON}}


def predict_snapshot(artifact: dict, data: dict[str, pd.DataFrame], index: pd.DataFrame,
                     sector_map: dict[str, str], config: QuantConfig) -> dict[str, dict]:
    if artifact["format"] != "lightgbm-text-1" or artifact["feature_columns"] != Features.FEATURE_COLUMNS:
        raise ValueError("Model feature contract mismatch")
    if artifact["horizon"] != config.FORECAST_HORIZON:
        raise ValueError("Model horizon mismatch")
    if index.index[-1].date().isoformat() < artifact["cutoff_session"]:
        raise ValueError("Model trained after inference target session")
    absolute = lgb.Booster(model_str=artifact["absolute_model"])
    alpha = lgb.Booster(model_str=artifact["alpha_model"])
    sector_returns = Features.sector_return_series(data, sector_map)
    rows, symbols = [], []
    with engine_context(config):
        for symbol, frame in sorted(data.items()):
            if frame.index[-1] != index.index[-1] or symbol not in sector_map:
                continue
            features = Features._feature_frame(frame, index.close.pct_change(), sector_returns[sector_map[symbol]])
            row = features.iloc[[-1]][Features.FEATURE_COLUMNS]
            if np.isfinite(row).all().all():
                rows.append(row)
                symbols.append(symbol)
    if not rows:
        return {}
    x = pd.concat(rows).astype(float)
    abs_predictions = absolute.predict(x, num_threads=1)
    alpha_predictions = alpha.predict(x, num_threads=1)
    ranks = pd.Series(alpha_predictions).rank(method="average", pct=True).to_numpy() * 100
    result = {}
    for symbol, pred_abs, pred_alpha, rank in zip(symbols, abs_predictions, alpha_predictions, ranks):
        direction = 1 if pred_abs > config.ALPHA_NEUTRAL_RET_PCT and pred_alpha > 0 and rank >= 60 else (
            -1 if pred_abs < -config.ALPHA_NEUTRAL_RET_PCT and pred_alpha < 0 and rank <= 40 else 0)
        strength = float(np.clip(.6 * abs(rank - 50) / 50 + .4 * min(abs(pred_abs) / 5, 1), 0, 1)) if direction else 0
        result[symbol] = {"available": True, "signal": "UP" if direction > 0 else "DOWN" if direction < 0 else "NEUTRAL",
            "direction": direction, "strength": round(strength, 4), "active": bool(direction),
            "predicted_abs_ret_pct": round(float(pred_abs), 3), "predicted_alpha_pct": round(float(pred_alpha), 3),
            "rank_pct": round(float(rank), 2), "proj_pct": round(float(pred_abs), 3),
            "as_of": index.index[-1].date().isoformat(), "model": "lightgbm_cross_sectional", "n_ranked_symbols": len(symbols)}
    return result
