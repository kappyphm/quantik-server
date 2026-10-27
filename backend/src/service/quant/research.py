"""Small chart aggregates from the exact analysis run; never export raw paths."""
import numpy as np


def simulation_summary(simulation: dict) -> dict:
    paths = np.asarray(simulation.get("_mc_paths", []), dtype=float)
    last = simulation.get("last_price", 0)
    if paths.ndim != 2 or not paths.size or not np.isfinite(paths).all() or not np.isfinite(last) or last <= 0:
        return {}
    quantiles = np.percentile(paths, [5, 25, 50, 75, 95], axis=0)
    returns = (paths[:, -1] / last - 1) * 100
    # Bounded payload, also for nearly constant or heavy-tailed distributions.
    iqr = float(np.ptp(np.percentile(returns, [25, 75])))
    width = 2 * iqr / np.cbrt(len(returns))
    bins = min(60, max(1, int(np.ceil(min(float(np.ptp(returns)) / width, 60))))) if width > 0 else 1
    counts, edges = np.histogram(returns, bins=bins)
    cutoff = float(np.percentile(returns, 5))
    return {
        "simulation_count": int(paths.shape[0]),
        "cone": [{"session": i + 1, **{key: float(quantiles[j, i])
            for j, key in enumerate(("p05", "p25", "p50", "p75", "p95"))}}
            for i in range(paths.shape[1])],
        "distribution": [{"returnPct": float((edges[i] + edges[i + 1]) / 2),
            "probabilityPct": float(count / len(returns) * 100),
            "lowerPct": float(edges[i]), "upperPct": float(edges[i + 1]), "count": int(count)}
            for i, count in enumerate(counts)],
        "var95_pct": cutoff, "cvar95_pct": float(returns[returns <= cutoff].mean()),
    }


def build_research(final: dict, config=None) -> dict:
    dates, closes = final.get("_price_dates", []), final.get("_price_close", [])
    prices = {str(day)[:10]: float(price) for day, price in zip(dates, closes)}
    peak, drawdown = 0., []
    for day, price in prices.items():
        peak = max(peak, price)
        drawdown.append({"date": day, "valuePct": (price / peak - 1) * 100})
    hmm = final.get("hmm") or {}
    simulation = final.get("simulation_risk") or {}
    summary = simulation.get("visual_summary") or {}
    if not summary and config is not None and len(closes) >= 30:
        import pandas as pd
        from service.quant.configuration import engine_context
        from service.quant.engines import FcastEngine
        with engine_context(config):
            replay = FcastEngine.ensemble(pd.Series(closes, index=pd.to_datetime(dates)),
                garch_result=final.get("garch"), symbol=final["symbol"],
                vni_regime=final.get("vni_regime", "NEUTRAL"))
        for key in ("median", "lower", "upper"):
            np.testing.assert_allclose(replay["mc_path"][key], simulation["mc_path"][key], rtol=1e-12, atol=1e-8)
        summary = simulation_summary(replay)
    regime = [{"date": str(day)[:10], "price": prices[str(day)[:10]], "state": state}
        for day, state in zip(hmm.get("_state_dates", []), hmm.get("_state_labels", []))
        if str(day)[:10] in prices]
    factors = [{"id": key, "label": key, "value": item["z"], "min": -1, "max": 1}
        for key, item in (final.get("rec", {}).get("factor_details") or {}).items()
        if item.get("z") is not None]
    lock = simulation.get("lock_risk", {})
    risk = [("var", "VaR95 mô phỏng", summary.get("var95_pct")),
        ("cvar", "CVaR95 mô phỏng", summary.get("cvar95_pct")),
        ("lock_drawdown", "Sụt giảm T-lock", lock.get("max_dd_lock_pct")),
        ("probability_loss", "P(lỗ trên 3% trong T-lock)", lock.get("prob_loss_gt_3pct"))]
    return {"schema_version": 1, "symbol": final["symbol"], "units": {"price": "VND", "return": "pct"},
        "observations": final.get("n"), "start": next(iter(prices), None),
        "end": next(reversed(prices), None) if prices else None,
        "horizon_sessions": simulation.get("horizon"), "simulation_count": summary.get("simulation_count"),
        "cone": summary.get("cone", []), "distribution": summary.get("distribution", []),
        "regime": regime, "drawdown": drawdown, "factors": factors,
        "risk_metrics": [{"id": key, "label": label, "value": value, "unit": "%"}
            for key, label, value in risk if value is not None]}
