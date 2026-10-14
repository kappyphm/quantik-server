"""Versioned scoring against immutable full-universe reference distributions."""
import numpy as np

from service.quant.configuration import CFG
from service.quant.engines import AdaptiveScorer

REGIME_MULTIPLIER = {"BULL": 1.08, "NEUTRAL": 1.0, "WEAK": .92, "BEAR": .85, "CRISIS": .70}
SCORING_POLICY = "snapshot-robust-blend-1"


def build_reference(reports: dict[str, dict]) -> dict:
    factors = {symbol: AdaptiveScorer.extract_factors(report) for symbol, report in reports.items()}
    result = {}
    for factor in AdaptiveScorer.WEIGHTS:
        values = np.array([record[factor] for record in factors.values()], dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("Nonfinite reference factors")
        result[factor] = {"n": len(values), "median": float(np.median(values)),
            "q1": float(np.percentile(values, 25)), "q3": float(np.percentile(values, 75)),
            "degenerate": len(values) < 3 or float(np.ptp(np.percentile(values, [25, 75]))) < 1e-9}
    return {"policy": SCORING_POLICY, "minimum_sample": 3, "zero_iqr": "ZERO_CONTRIBUTION",
        "symbols": sorted(factors), "factors": result}


def score_member(report: dict, reference: dict) -> dict:
    if reference["policy"] != SCORING_POLICY or report["symbol"] not in reference["symbols"]:
        raise ValueError("Reference membership or policy mismatch")
    factors = AdaptiveScorer.extract_factors(report)
    regime = report["vni_regime"]
    multiplier = REGIME_MULTIPLIER.get(regime, 1.0)
    absolute = 50
    for name, scale, bound in (("sharpe", 10, 15), ("forecast_ret", 2, 10), ("cmf", 30, 7),
        ("trend_quality", 12, 7), ("alpha_composite", 10, 10), ("hmm_regime", 5, 5),
        ("lock_safety", 5, 5), ("liquidity", 6, 6), ("data_quality", 6, 6)):
        absolute += float(np.clip(factors[name] * scale, -bound, bound))
    # Match the legacy absolute branch's integer clipping before the blend, minus screener bonuses.
    absolute = int(np.clip(absolute * multiplier, 0, 100))
    weighted, details = 0.0, {}
    anchor = CFG.ABSOLUTE_SCORE_ANCHOR
    for name, weight in AdaptiveScorer.WEIGHTS.items():
        raw, distribution = float(factors[name]), reference["factors"][name]
        if not np.isfinite(raw):
            raise ValueError("Nonfinite member factor")
        z = 0.0 if distribution["degenerate"] else float(np.clip(
            (raw - distribution["median"]) / (distribution["q3"] - distribution["q1"]), -1, 1))
        weighted += weight * z
        details[name] = {"raw": round(raw, 4), "z": round(z, 3), "w": weight,
            "cs_contribution_points": (1 - anchor) * 40 * weight * z * multiplier,
            "normalization_reason": "ZERO_IQR_OR_SMALL_SAMPLE" if distribution["degenerate"] else None}
    cross_sectional = (50 + weighted * 40) * multiplier
    score = int(np.clip(anchor * absolute + (1 - anchor) * cross_sectional, 0, 100))
    rating = "⭐⭐⭐⭐⭐ SWING BUY" if score >= 80 else "⭐⭐⭐⭐ BUY & HOLD" if score >= 65 else (
        "⭐⭐⭐ THEO DÕI" if score >= 50 else "⭐⭐ YẾU" if score >= 35 else "⭐ TRÁNH")
    return {"score": score, "rating": rating, "vni_regime": regime, "scoring_method": SCORING_POLICY,
        "timing": report["fcast"].get("timing", ""), "hold_plan": report["fcast"].get("hold_plan_label", ""),
        "factor_details": details, "reference_n": len(reference["symbols"])}
