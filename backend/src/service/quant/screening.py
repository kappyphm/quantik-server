"""21 prototype criteria with explicit availability and stable full-universe ranking."""
import numpy as np
import pandas as pd

from service.quant.screening_legacy import AnalysisEngine, FeatureEngine, MarketAnalyzer, RegimeAdjustment, ScreeningEngine
from service.quant.serialization import json_metrics

POLICY_VERSION = "screening-tristate-1"
RULES = {
    "STRONG": [("S1_RS_market", 2, ["rs_vs_index_1d"]), ("S2_vol_spike", 20, ["vol_ratio_20"]),
        ("S3_near_high20", 20, ["pct_of_high20"]), ("S4_vol_dry_pullback", 20, ["vol_avg5", "vol_avg20"]),
        ("S5_breakout_high20", 20, ["high20"]), ("S6_hh_hl_structure", 21, []), ("S7_RS_10d", 11, ["rs_vs_index_10d"])],
    "ACCUMULATION": [("A1_tight_range", 20, ["range_20d"]), ("A2_vol_declining", 20, ["vol_avg5", "vol_avg20"]),
        ("A3_absorption", 39, []), ("A4_above_ma20", 20, ["ma20"]), ("A5_near_resistance", 20, ["pct_of_high20"]),
        ("A6_long_accumulation", 40, []), ("A7_spring", 30, [])],
    "DISTRIBUTION": [("D1_vol_no_price_up", 20, ["vol_ratio_20", "pct_change_1d"]),
        ("D2_long_upper_shadow", 1, ["upper_shadow_ratio"]), ("D3_failed_breakout", 29, []),
        ("D4_vol_on_down", 11, []), ("D5_below_ma20", 20, ["ma20"]), ("D6_drop_from_peak", 20, ["high20"]),
        ("D7_multi_dist_sessions", 29, [])],
}


def aggregate(criteria: dict, hard_pass: bool) -> dict:
    counts = {state: sum(c["state"] == state for c in criteria.values()) for state in ["TRUE", "FALSE", "UNAVAILABLE"]}
    return {"criteria": criteria, "true_count": counts["TRUE"], "false_count": counts["FALSE"],
        "unavailable_count": counts["UNAVAILABLE"], "coverage_ratio": (7-counts["UNAVAILABLE"])/7,
        "score": 100*counts["TRUE"]/7, "ranking_eligible": hard_pass and counts["UNAVAILABLE"] == 0}


def evaluate_symbol(symbol: str, frame: pd.DataFrame | None, index: pd.DataFrame | None, *,
                    input_mode: str = "COMPLETED_DAILY", minimum_average_volume: int = 100000) -> dict:
    if input_mode not in {"COMPLETED_DAILY", "PROVISIONAL_DAILY"}:
        raise ValueError("Unknown screening input mode")
    n, warnings = len(frame) if frame is not None else 0, []
    features = None
    valid = frame is not None and not frame.empty and {"open", "high", "low", "close", "volume"}.issubset(frame.columns)
    if valid:
        try:
            valid = isinstance(frame.index, pd.DatetimeIndex) and frame.index.is_unique and frame.index.is_monotonic_increasing
            valid = valid and np.isfinite(frame[["open", "high", "low", "close", "volume"]]).all().all()
            valid = valid and (frame[["open", "high", "low", "close"]] > 0).all().all() and (frame.volume >= 0).all()
            valid = valid and (frame.high >= frame[["open", "close", "low"]].max(axis=1)).all()
            valid = valid and (frame.low <= frame[["open", "close", "high"]].min(axis=1)).all()
        except (TypeError, ValueError):
            valid = False
    if valid:
        features = FeatureEngine.compute_features(frame.reset_index(names="time"), input_mode=input_mode)
        if index is not None and not index.empty:
            aligned = index.close.reindex(frame.index)
            for horizon in [1, 10]:
                reference_return = aligned / aligned.shift(horizon) - 1  # No forward-filling a missing index session.
                features[f"rs_vs_index_{horizon}d"] = (frame.close/frame.close.shift(horizon)-1-reference_return).to_numpy()
    legacy = {"STRONG": {}, "ACCUMULATION": {}, "DISTRIBUTION": {}}
    if features is not None and n >= 20:
        legacy = {"STRONG": ScreeningEngine.screen_strong(features), "ACCUMULATION": ScreeningEngine.screen_accumulation(features),
            "DISTRIBUTION": ScreeningEngine.screen_distribution(features)}
        if input_mode == "PROVISIONAL_DAILY":
            absorption, distribution_count = False, 0
            for position in range(max(0, n-20), n):
                row = features.iloc[position]
                end = position if position == n-1 else position+1
                baseline = features.volume.iloc[max(0, end-20):end].mean()
                ratio = row.volume/baseline if baseline > 0 else np.nan
                if np.isfinite(ratio) and ratio >= 2 and row.pct_change_1d > -.02:
                    absorption = True
                if position >= n-10 and np.isfinite(ratio) and ratio >= 1.5 and abs(row.pct_change_1d) <= .015:
                    distribution_count += 1
            legacy["ACCUMULATION"]["criteria"]["A3_absorption"] = absorption
            legacy["DISTRIBUTION"]["criteria"]["D7_multi_dist_sessions"] = distribution_count >= 3
            legacy["DISTRIBUTION"]["detail"]["D7"] = f"{distribution_count} phiên phân phối"
    baseline_volume = None
    if valid and n >= (21 if input_mode == "PROVISIONAL_DAILY" else 20):
        completed = frame.iloc[:-1] if input_mode == "PROVISIONAL_DAILY" else frame
        baseline_volume = float(completed.volume.tail(20).mean())
    hard_pass = valid and baseline_volume is not None and baseline_volume >= minimum_average_volume
    groups = {}
    for group, rules in RULES.items():
        criteria = {}
        for name, minimum, fields in rules:
            if input_mode == "PROVISIONAL_DAILY" and name in {"S2_vol_spike", "S4_vol_dry_pullback", "A2_vol_declining", "D1_vol_no_price_up"}:
                minimum += 1
            reason = "INVALID_OR_MISSING_BARS" if not valid else "INSUFFICIENT_HISTORY" if n < minimum else None
            if not reason and any(not np.isfinite(features.iloc[-1].get(field, np.nan)) for field in fields):
                reason = "INPUT_UNAVAILABLE"
            if not reason and name == "D4_vol_on_down":
                changes = features.pct_change_1d.tail(10)
                if not (changes > 0).any() or not (changes < 0).any():
                    reason = "NO_COMPARISON_SESSIONS"
            value = legacy[group].get("criteria", {}).get(name)
            if not reason and value is None:
                # Legacy group entry guards at 20; D2 itself needs only one valid candle.
                if name == "D2_long_upper_shadow":
                    value = features.iloc[-1].upper_shadow_ratio >= .5
                elif name in {"S1_RS_market", "S7_RS_10d"}:
                    value = features.iloc[-1][fields[0]] > 0
                elif name == "D4_vol_on_down":
                    recent = features.tail(10)
                    value = recent[recent.pct_change_1d < 0].volume.mean() > recent[recent.pct_change_1d > 0].volume.mean()
                else:
                    reason = "INPUT_UNAVAILABLE"
            criteria[name] = {"state": "UNAVAILABLE" if reason else "TRUE" if bool(value) else "FALSE",
                "reason_codes": [reason] if reason else [], "minimum_bars": minimum,
                "detail": legacy[group].get("detail", {}).get(name.split("_")[0], reason or ""),
                "input_fields": fields}
        groups[group] = aggregate(criteria, bool(hard_pass))
    complete = all(g["unavailable_count"] == 0 for g in groups.values())
    if complete and hard_pass:
        assessment = AnalysisEngine.generate_assessment(symbol, features,
            *[{"score": round(groups[g]["score"], 1), "criteria": {k: v["state"] == "TRUE" for k, v in groups[g]["criteria"].items()}}
              for g in ["STRONG", "ACCUMULATION", "DISTRIBUTION"]])
        assessment["status"] = "AVAILABLE"
    else:
        assessment = {"status": "UNAVAILABLE", "signal_code": "INCOMPLETE" if not complete else "REJECTED",
            "reason_codes": ["CRITERION_COVERAGE"] if not complete else ["LIQUIDITY_FILTER"]}
    if groups["DISTRIBUTION"]["unavailable_count"]:
        warnings.append("DISTRIBUTION_COVERAGE_INCOMPLETE")
    result = {"symbol": symbol, "groups": groups, "assessment": assessment,
        "price_summary": AnalysisEngine.get_price_summary(features) if features is not None else {},
        "overall_score": max(groups[g]["score"] for g in ["STRONG", "ACCUMULATION"]),
        "ranking_eligible": groups["STRONG"]["ranking_eligible"] and groups["ACCUMULATION"]["ranking_eligible"],
        "rejection_reasons": [] if hard_pass else ["INSUFFICIENT_HISTORY_OR_LIQUIDITY"],
        "volume_context": {"raw_volume": float(frame.volume.iloc[-1]) if valid else None,
            "baseline20_volume_shares": baseline_volume, "input_mode": input_mode,
            "projected_volume": None, "projected_volume_reason": "DISABLED",
            "rolling_volume_policy": "CURRENT_RAW_IN_5D_PRIOR_COMPLETED_20D" if input_mode == "PROVISIONAL_DAILY" else "COMPLETED_ROLLING_20D"},
        "policy_version": POLICY_VERSION, "warnings": warnings}
    result["units"] = {"price": "VND", "volume": "SHARES", "price_changes": "RATIO", "score": "PERCENT"}
    return json_metrics(result)


def rank_results(results: list[dict], top_n: int = 70) -> dict:
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    ranking = {}
    for group in RULES:
        eligible = [r for r in results if r["groups"][group]["ranking_eligible"]]
        ordered = sorted(eligible, key=lambda r: (-r["groups"][group]["true_count"], r["symbol"]))
        ranking[group] = [r["symbol"] for r in ordered]
    ordered = sorted([r for r in results if r["ranking_eligible"]], key=lambda r: (-r["overall_score"], r["symbol"]))
    ranking["OVERALL"] = [r["symbol"] for r in ordered]
    ranking["TOP"] = ranking["OVERALL"][:top_n]
    return ranking


def market_context(index: pd.DataFrame, input_mode: str) -> dict:
    raw = MarketAnalyzer().analyze_frame(index.reset_index(names="time"), input_mode=input_mode)
    features = raw.pop("raw_data", None)
    raw["series"] = features.to_dict("records") if features is not None else []
    raw["screening_adjustments"] = MarketAnalyzer().get_screening_adjustments(raw)
    raw["input_mode"] = input_mode
    raw["price_unit"], raw["volume_unit"] = "POINTS", "SHARES"
    return json_metrics(raw)
