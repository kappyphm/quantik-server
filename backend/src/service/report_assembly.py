"""Preserve prototype nested outputs in version-1 sections and explicit compatibility metadata."""
from datetime import date, datetime

from service.quant.commentary import PrototypeCommentary
from service.quant.configuration import QuantConfig, engine_context
from service.quant.serialization import json_metrics
from service.reports import Report

FIELD_MAPPING = {
    "data_quality": ["data_quality"], "liquidity": ["liquidity"], "distribution": ["dist"],
    "statistics": ["stats", "vol", "ac", "n", "range", "date", "_price_dates", "_price_close"],
    "arima": ["arima"], "garch": ["garch"], "hmm": ["hmm"],
    "technical": ["alpha", "sr", "trend", "flow"], "context": ["sector", "cross_corr"],
    "directional_alpha": ["momentum_alpha", "hurst", "conditional_mr", "lightgbm_cross_sectional", "cross_sectional_model"],
    "agreement": ["fcast"], "simulation_risk": ["simulation_risk"], "costs": ["costs"],
    "trade_plan": ["sl", "pos", "sizing_mode", "kelly", "meta_label"],
    "decision": ["rec", "action"],
}
UNITS = {"price": "VND", "returns": "PERCENT", "ratios": "RATIO", "volume": "SHARES"}


def assemble_report(final: dict, *, job_id: str, evaluated_at: datetime, data_as_of: datetime,
                    versions: dict, config: QuantConfig, warnings: list[str] | None = None,
                    target_session: date | None = None, data_observed_at: datetime | None = None) -> dict:
    warnings = list(warnings or []) + final.get("serialization_warnings", [])
    sections = {}
    for section, keys in FIELD_MAPPING.items():
        metrics = {key: final.get(key) for key in keys}
        required_present = all(value is not None for value in metrics.values())
        if len(keys) == 1:
            value = metrics[keys[0]]
            required_present = isinstance(value, dict) and bool(value) and not value.get("error")
        status = "AVAILABLE" if required_present else "UNAVAILABLE"
        sections[section] = {"status": status, "reason_codes": [] if required_present else ["COMPONENT_UNAVAILABLE"],
            "units": UNITS.copy(), "metrics": json_metrics(metrics, warnings)}
    with engine_context(config):
        narrative = PrototypeCommentary()
        summary = narrative.generate_commentary(final)
        detailed = narrative.generate_commentary(final, detailed=True)
    narrative_metrics = {"summary": summary, "detailed": detailed,
        "compatibility": {"meta_label": "DISABLED", "outcome_tracking": "DISABLED", "kelly": "DISABLED",
            "raw_mc_paths": "REPLACED_BY_QUANTILE_TRAJECTORIES", "sizing_mode": final["sizing_mode"]}}
    sections["commentary"] = {"status": "AVAILABLE", "reason_codes": [], "units": {}, "metrics": narrative_metrics}
    result = {"schema_version": "1.0", "job_id": job_id, "symbol": final["symbol"],
        "evaluated_at": evaluated_at, "data_as_of": data_as_of, "input_mode": "COMPLETED_DAILY",
        "target_session": target_session, "data_observed_at": data_observed_at,
        "versions": versions, "sections": sections, "decision": final["action"],
        "commentary": narrative_metrics, "warnings": sorted(set(warnings))}
    return Report.model_validate(result).model_dump(mode="json")
