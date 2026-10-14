"""Final dependency graph: directional inputs -> agreement -> costs -> stops -> sizing -> score -> action."""
from copy import deepcopy
from dataclasses import replace

import pandas as pd

from service.quant.configuration import QuantConfig, engine_context
from service.quant import engines as e
from service.quant.scoring import score_member


def directional_context(baseline: dict, frame: pd.DataFrame, index: pd.DataFrame,
                        sector_returns: pd.Series, prediction: dict | None, context: dict,
                        config: QuantConfig) -> dict:
    report = deepcopy(baseline)
    if report.get("error"):
        raise ValueError("Baseline failed data-quality gate")
    with engine_context(config):
        report["sector"], report["cross_corr"] = context["sector"], context["cross_corr"]
        report["conditional_mr"] = e.ConditionalResidualReversionEngine.analyze(frame,
            idx_df=index, sector_returns=sector_returns, hurst=report["hurst"], hmm=report["hmm"],
            vni_regime=report["vni_regime"])
        report["lightgbm_cross_sectional"] = prediction or {"available": False, "active": False,
            "direction": 0, "strength": 0.0, "proj_pct": 0.0, "reason": "MODEL_UNAVAILABLE"}
        report["fcast"] = e.AgreementEngine.combine(report["simulation_risk"], report["momentum_alpha"],
            report["lightgbm_cross_sectional"], report["conditional_mr"], report["hurst"], report["hmm"],
            vni_regime=report["vni_regime"])
        report["fcast"].update(meta_status="DISABLED", meta_trust_probability=None, calibrated_prob_up=None)
        report["costs"] = e.CostEngine.analyze(report["fcast"], report["liquidity"])
    return report


def finalize_member(report: dict, frame: pd.DataFrame, reference: dict, config: QuantConfig,
                    sizing: dict | None = None) -> dict:
    report = deepcopy(report)
    if sizing:
        config = replace(config, ACCOUNT_SIZE=sizing["account_size_vnd"],
            MAX_POSITION_RISK=sizing.get("max_position_risk_ratio", config.MAX_POSITION_RISK))
    with engine_context(config):
        report["sl"] = e.RiskEng.stops(frame, rs=report["stats"], fc=report["fcast"],
            exchange=report["exchange"], liquidity=report["liquidity"], costs=report["costs"])
        stops = report["sl"]
        report["pos"] = e.RiskEng.sizing(stops["entry"], stops["sl_swing"], acct=config.ACCOUNT_SIZE,
            hold_sessions=report["fcast"].get("hold_plan_sessions"), exchange=report["exchange"],
            liquidity=report["liquidity"]) if stops and stops.get("sl_swing") else {"shares": 0}
        report["sizing_mode"] = "PERSONALIZED" if sizing else "REFERENCE"
        report["rec"] = score_member(report, reference)
        report["action"] = e.ActionEngine.decide(report)
        if not report["lightgbm_cross_sectional"].get("available"):
            report["action"]["reason_codes"].append("MODEL_UNAVAILABLE")
            report["action"]["executable"] = False
            report["action"]["gate_pass"] = False
            if report["action"]["action"] == "BUY_NOW":
                report["action"]["action"] = "WATCH"
    return report
