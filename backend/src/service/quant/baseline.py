"""Stock-specific computations; final agreement/plan depend on a pinned market snapshot."""
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from service.quant.configuration import QuantConfig, engine_context
from service.quant import engines as e


def compute_baseline(symbol: str, frame: pd.DataFrame, *, exchange: str, evaluated_at: datetime,
                     index_frame: pd.DataFrame | None = None, config: QuantConfig) -> dict:
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at requires timezone")
    if e.VNMarketRules.normalize_exchange(exchange) == "UNKNOWN":
        raise ValueError("Unknown exchange")
    with engine_context(config):
        quality = e.DataQualityEngine.audit(frame, exchange, as_of=evaluated_at.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")))
        if quality["status"] == "FAIL":
            return {"symbol": symbol, "exchange": exchange, "data_quality": quality,
                "error": "DATA_QUALITY_FAIL"}
        if index_frame is not None:
            # Index points have no exchange daily price-band checks. Validate its structure
            # upstream; regime detection checks numeric close/history without stock jump rules.
            closes = pd.to_numeric(index_frame["close"], errors="coerce")
            if not np.isfinite(closes).all() or (closes <= 0).any():
                raise ValueError("Invalid index history")
        regime = e.AdaptiveScorer._detect_vni_regime(index_frame)
        prices = frame.close
        report = {"symbol": symbol, "exchange": exchange, "n": len(frame),
            "range": f"{frame.index[0].date().isoformat()} -> {frame.index[-1].date().isoformat()}",
            "date": evaluated_at.isoformat(),
            "data_quality": quality, "liquidity": e.LiquidityEngine.analyze(frame, exchange),
            "vni_regime": regime, "_price_dates": [stamp.isoformat() for stamp in frame.index],
            "_price_close": prices.tolist(), "component_errors": {}}
        tasks = {
            "dist": lambda: e.DistributionAnalyzer.full_test(prices),
            "stats": lambda: e.StatEngine.returns(prices),
            "vol": lambda: e.StatEngine.vol_regime(prices),
            "ac": lambda: e.StatEngine.autocorr(prices),
            "arima": lambda: e.ARIMAEngine.fit(prices),
            "garch": lambda: e.GARCHEngine.fit(prices),
            "hmm": lambda: e.HMMEngine.fit(frame, symbol=symbol),
            "alpha": lambda: e.AlphaEngine.extract(frame, index_frame),
            "sr": lambda: e.StructureEngine.sr_levels(frame),
            "trend": lambda: e.StructureEngine.trend(frame),
            "flow": lambda: e.StructureEngine.flow(frame),
            "momentum_alpha": lambda: e.MomentumAlphaEngine.analyze(prices),
            "hurst": lambda: e.HurstRegimeEngine.analyze(prices),
        }
        for name, operation in tasks.items():
            try:
                report[name] = operation()
            except (ValueError, ArithmeticError, np.linalg.LinAlgError) as exc:
                report[name] = {}
                report["component_errors"][name] = type(exc).__name__
        report["simulation_risk"] = e.FcastEngine.ensemble(prices, garch_result=report.get("garch"),
            symbol=symbol, vni_regime=regime)
        report["kelly"] = {"disabled": True, "note": "Kelly requires separately calibrated trade outcomes"}
        report["meta_label"] = {"disabled": True, "reason": "PRODUCT_POLICY"}
        return report
