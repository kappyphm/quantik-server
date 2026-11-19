"""Report-bound radar scoring inputs for the client radar (display only).

The client derives seven of the fourteen module scores itself from report
sections. The backend supplies the remaining seven plus the same-session
liquidity universe used for its percentile ranks. Every supplied score follows
a documented 0-100 rule over the module's own published outputs; a module
whose inputs are missing or invalid gets a null score instead of an invented
one. These inputs never feed trade or action gates.
"""
import numpy as np


def _bounded(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return round(min(max(number, 0.0), 100.0), 1)


def _entry(module_id: str, score: float | None, method: str) -> dict:
    return {"id": module_id, "score": score, "method": method}


def module_scores(final: dict) -> list[dict]:
    """0-100 display scores for the modules the client cannot derive itself."""
    entries = []
    verdict = (final.get("dist") or {}).get("verdict") or {}
    rejects = verdict.get("reject_count")
    entries.append(_entry("distribution",
        _bounded(100 * (1 - rejects / 5)) if isinstance(rejects, (int, float)) and 0 <= rejects <= 5 else None,
        "100 × (1 − số kiểm định chuẩn hoá bị bác bỏ / 5 kiểm định)."))
    arima = final.get("arima") or {}
    coefficients = [c for c in (arima.get("coefficients") or {}).values()
        if isinstance(c, dict) and "sig" in c]
    stationary = (arima.get("stationarity") or {}).get("stationary")
    entries.append(_entry("arima",
        _bounded(100 * sum(1 for c in coefficients if c["sig"]) / len(coefficients))
        if stationary and coefficients else None,
        "Tỷ lệ hệ số ARIMA có ý nghĩa thống kê (p<0,05) × 100, chỉ khi chuỗi dừng."))
    params = [p for p in ((final.get("garch") or {}).get("garch") or {}).get("params", {}).values()
        if isinstance(p, dict) and isinstance(p.get("p"), (int, float)) and np.isfinite(p["p"])]
    entries.append(_entry("garch",
        _bounded(100 * sum(1 for p in params if p["p"] < 0.05) / len(params)) if params else None,
        "Tỷ lệ tham số GARCH có ý nghĩa thống kê (p<0,05) × 100."))
    ret20 = ((final.get("sector") or {}).get("trend") or {}).get("ret_20D_pct")
    entries.append(_entry("context",
        _bounded(50 + 5 * max(-10.0, min(10.0, float(ret20))))
        if isinstance(ret20, (int, float)) and np.isfinite(ret20) else None,
        "50 + 5 × lợi suất ngành 20 phiên (%), cắt tại ±10%."))
    momentum = final.get("momentum_alpha") or {}
    mscore = momentum.get("score")
    entries.append(_entry("directional_alpha",
        _bounded(50 + 50 * max(-1.0, min(1.0, float(mscore))))
        if momentum.get("active") and isinstance(mscore, (int, float)) and np.isfinite(mscore) else None,
        "Ánh xạ tuyến tính điểm momentum_alpha của engine từ [−1, 1] sang [0, 100]."))
    prob_up = ((final.get("simulation_risk") or {}).get("mc") or {}).get("prob_up")
    entries.append(_entry("simulation_risk", _bounded(prob_up),
        "Xác suất mô phỏng Monte Carlo cho giá tăng sau horizon (%)."))
    stops = final.get("sl") or {}
    rr = stops.get("rr_optimal")
    entries.append(_entry("trade_plan",
        _bounded(100 * float(rr) / (float(rr) + 1))
        if stops.get("plan_valid") and isinstance(rr, (int, float)) and np.isfinite(rr) and rr > 0 else None,
        "100 × RR tối ưu / (RR tối ưu + 1), chỉ khi kế hoạch giao dịch hợp lệ."))
    return entries


def liquidity_universe(frames: dict, *, universe_id: str, sessions: int = 20,
                       minimum: int = 10) -> dict:
    """ADV20 value and Amihud20 for every frame, from its own last sessions.

    Amihud20 is the mean of |log return| / traded value over the window; the
    client only ranks members against each other, so the raw illiquidity
    ratio is published without a scaling constant.
    """
    members = []
    for symbol, frame in sorted(frames.items()):
        try:
            close = frame["close"].astype(float).tail(sessions + 1)
            volume = frame["volume"].astype(float).tail(sessions + 1)
        except Exception:
            continue
        if len(close) < minimum + 1:
            continue
        value = (close * volume).iloc[1:]
        log_return = np.log(close / close.shift(1)).abs().iloc[1:]
        adv = float(value.mean()) if len(value) else 0.0
        valid = (value > 0) & np.isfinite(log_return)
        if adv <= 0 or int(valid.sum()) < minimum:
            continue
        amihud = float((log_return[valid] / value[valid]).mean())
        if not np.isfinite(amihud) or amihud < 0:
            continue
        members.append({"symbol": symbol, "adv20_value_vnd": adv, "amihud20": amihud})
    return {"universe_id": universe_id, "members": members}


def build_radar_scoring(final: dict, *, target_session, liquidity_universe: dict | None) -> dict:
    result = {"schema_version": 1, "as_of": target_session.isoformat(),
        "module_scores": module_scores(final)}
    if liquidity_universe and len(liquidity_universe.get("members") or []) >= 2:
        result["liquidity_universe"] = liquidity_universe
    return result
