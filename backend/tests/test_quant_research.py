import json

import numpy as np

from service.quant.research import build_research, simulation_summary
from service.quant.serialization import json_metrics


def test_chart_aggregates_match_exact_paths_without_exporting_paths():
    paths = np.array([[90., 80., 70.], [95., 90., 85.], [100., 110., 120.], [120., 130., 150.]])
    simulation = {"last_price": 100., "horizon": 3, "_mc_paths": paths}
    summary = simulation_summary(simulation)
    expected = np.percentile(paths, [5, 25, 50, 75, 95], axis=0)
    for i, row in enumerate(summary["cone"]):
        assert row["session"] == i + 1
        np.testing.assert_allclose([row[k] for k in ["p05", "p25", "p50", "p75", "p95"]], expected[:, i])
    assert sum(row["count"] for row in summary["distribution"]) == 4
    assert abs(sum(row["probabilityPct"] for row in summary["distribution"]) - 100) < 1e-9
    returns = (paths[:, -1] / 100 - 1) * 100
    assert summary["var95_pct"] == np.percentile(returns, 5)
    assert summary["cvar95_pct"] == returns[returns <= np.percentile(returns, 5)].mean()
    dates = ["2026-10-14", "2026-10-15", "2026-10-16"]
    simulation["visual_summary"] = summary
    research = build_research({"symbol": "FPT", "n": 3, "_price_dates": dates,
        "_price_close": [100, 120, 90], "simulation_risk": simulation,
        "hmm": {"_state_dates": dates[1:], "_state_labels": ["BULL", "BEAR"]},
        "rec": {"factor_details": {"cmf": {"z": -.5}}}})
    assert [p["valuePct"] for p in research["drawdown"]] == [0, 0, -25]
    assert research["regime"] == [{"date": dates[1], "price": 120., "state": "BULL"}, {"date": dates[2], "price": 90., "state": "BEAR"}]
    assert research["factors"][0] == {"id": "cmf", "label": "cmf", "value": -.5, "min": -1, "max": 1}
    encoded = json.dumps(json_metrics({"research": research, "simulation": simulation}), allow_nan=False)
    assert "_mc_paths" not in encoded
    assert "probabilities" not in encoded


def test_constant_and_missing_simulations_have_honest_bounded_payloads():
    assert simulation_summary({}) == {}
    assert simulation_summary({"_mc_paths": [[float("nan")]], "last_price": 100}) == {}
    summary = simulation_summary({"_mc_paths": np.full((100, 3), 100.), "last_price": 100.})
    assert len(summary["distribution"]) == 1
    assert summary["distribution"][0]["probabilityPct"] == 100
    assert summary["var95_pct"] == summary["cvar95_pct"] == 0
    legacy = build_research({"symbol": "FPT"})
    assert legacy["cone"] == legacy["distribution"] == legacy["regime"] == []


def test_new_report_preserves_research_through_snapshot_and_api_contract(monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone

    import pandas as pd

    from service.quant import engines
    from service.quant.baseline import compute_baseline
    from service.quant.configuration import QuantConfig
    from service.report_assembly import assemble_report, FIELD_MAPPING
    from service.reports import ReportResult

    # Exercise real Monte Carlo and the production serialization boundary; skip
    # unrelated expensive model fitting in this chart-contract check.
    for engine, method in [(engines.ARIMAEngine, "fit"), (engines.GARCHEngine, "fit"),
                           (engines.HMMEngine, "fit"), (engines.DistributionAnalyzer, "full_test")]:
        monkeypatch.setattr(engine, method, lambda *args, **kwargs: {"error": "UNAVAILABLE_IN_TEST"})
    dates = pd.bdate_range(end="2026-10-16", periods=252)
    close = 100000 * np.exp(np.arange(252) * .0002 + np.sin(np.arange(252)) * .005)
    frame = pd.DataFrame({"close": close, "open": close, "high": close * 1.01,
        "low": close * .99, "volume": np.full(252, 1000000)}, index=dates)
    config = replace(QuantConfig(), PRICE_INPUT_UNIT="VND", FORECAST_HORIZON=3, MONTE_CARLO_SIMS=100)
    evaluated = datetime(2026, 10, 17, 1, tzinfo=timezone.utc)
    baseline = compute_baseline("FPT", frame, exchange="HOSE", evaluated_at=evaluated, config=config)
    assert "error" not in baseline
    cached = json.loads(json.dumps(json_metrics(baseline), allow_nan=False))
    assert "_mc_paths" not in cached["simulation_risk"]
    for keys in FIELD_MAPPING.values():
        for key in keys:
            cached.setdefault(key, {})
    cached.update(sizing_mode="REFERENCE", fcast={"horizon": 3}, rec={"score": 50}, costs={"net_forecast_pct": 0},
        action={"action": "WATCH", "executable": False})
    report = assemble_report(cached, job_id="research-job", evaluated_at=evaluated,
        data_as_of=datetime(2026, 10, 16, 9, tzinfo=timezone.utc), versions={}, config=config)
    delivered = ReportResult.model_validate({**report, "report_id": "research-report"}).model_dump(mode="json")
    research = delivered["research"]
    assert len(research["cone"]) == 3 and research["simulation_count"] == 100
    assert research["end"] == "2026-10-16" and len(research["drawdown"]) == 252
    assert abs(sum(p["probabilityPct"] for p in research["distribution"]) - 100) < 1e-8
    assert research["units"] == {"price": "VND", "return": "pct"}
