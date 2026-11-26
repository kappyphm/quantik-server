"""Read-only projections over immutable screening runs, with fixed top-five selection."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from service.models import AnalysisReport, Job, DataManifest, ScreeningResult, ScreeningRun, UniverseVersion


def run_members(db: Session, run: ScreeningRun) -> dict:
    """Universe members of a run's manifest, for name/sector projection."""
    manifest_id = run.manifest_id or (run.summary or {}).get("manifest_id")
    manifest = db.get(DataManifest, manifest_id) if manifest_id else None
    universe = db.get(UniverseVersion, manifest.universe_id) if manifest else None
    return (universe.payload or {}).get("members", {}) if universe else {}


def item_view(row: ScreeningResult, *, name: str | None = None, sector: str | None = None,
              index_trend: str | None = None) -> dict:
    payload = row.payload
    prices, volume = payload.get("price_summary", {}), payload.get("volume_context", {})
    stats, sector_context = payload.get("sheet_stats") or {}, payload.get("sector_context") or {}
    groups = {name: {"score": group.get("score") if not group.get("unavailable_count") else None,
        "met_count": group.get("true_count", 0), "total_count": 7,
        "unavailable_count": group.get("unavailable_count", 0)}
        for name, group in payload["groups"].items()}
    eligible = bool(payload.get("ranking_eligible"))
    incomplete = any(group["unavailable_count"] for group in groups.values())
    return {"symbol": row.symbol, "name": name, "exchange": payload.get("exchange"),
        "sector": sector, "index_trend": index_trend,
        "commentary": (payload.get("assessment") or {}).get("investor_note"),
        "price_vnd": prices.get("close"), "change_ratio": prices.get("pct_change_1d"),
        "change_5d_ratio": prices.get("pct_change_5d"), "change_20d_ratio": prices.get("pct_change_20d"),
        "volume_shares": volume.get("raw_volume", prices.get("volume")),
        "average_volume_20_shares": volume.get("baseline20_volume_shares"),
        "volume_ratio_20": prices.get("vol_ratio_20"), "price_to_high20_ratio": prices.get("pct_of_high20"),
        "adv20_value_vnd": stats.get("adv20_value_vnd"), "liquidity_tier": stats.get("liquidity_tier"),
        "capacity_shares": stats.get("capacity_shares"), "gap_abs_p95_pct": stats.get("gap_abs_p95_pct"),
        "sharpe": stats.get("sharpe"), "up_day_ratio_pct": stats.get("up_day_ratio_pct"),
        "max_drawdown_pct": stats.get("max_drawdown_pct"), "annual_return_pct": stats.get("annual_return_pct"),
        "sector_trend": sector_context.get("sector_trend"),
        "sector_return_20d_pct": sector_context.get("sector_return_20d_pct"),
        "sector_correlation": sector_context.get("sector_correlation"), "sector_lead_lag": None,
        "groups": groups, "eligible": eligible,
        "data_session": prices.get("data_session"), "stale_sessions": prices.get("stale_sessions"),
        "status": "INSUFFICIENT_DATA" if incomplete else "ELIGIBLE" if eligible else "LOW_LIQUIDITY"}


def top_results(db: Session, run_id: str, group: str) -> dict:
    rank = ScreeningResult.distribution_rank if group == "DISTRIBUTION" else ScreeningResult.overall_rank
    rows = db.scalars(select(ScreeningResult).where(ScreeningResult.run_id == run_id,
        rank >= 1, rank <= 5).order_by(rank, ScreeningResult.symbol).limit(5))
    run = db.get(ScreeningRun, run_id)
    members = run_members(db, run) if run else {}
    index_trend = ((run.summary or {}).get("market_context", {}) if run else {}).get("regime_label")
    return {"run_id": run_id, "group": group, "items": [
        item_view(row, name=members.get(row.symbol, {}).get("name"),
            sector=members.get(row.symbol, {}).get("sector"), index_trend=index_trend)
        for row in rows]}


def detail_view(run: ScreeningRun, row: ScreeningResult, member: dict | None = None) -> dict:
    context = (run.summary or {}).get("market_context", {})
    detail = context.get("detail", {})
    trend, volatility = detail.get("trend", {}), detail.get("volatility", {})
    return {"run_id": run.id, "target_session": run.target_session.isoformat(), "published_at": run.published_at,
        "input_mode": (run.summary or {}).get("input_mode", "COMPLETED_DAILY"),
        "observed_at": row.payload.get("volume_context", {}).get("observed_at"),
        "item": item_view(row, name=(member or {}).get("name"), sector=(member or {}).get("sector"),
            index_trend=context.get("regime_label")), "assessment": row.payload.get("assessment", {}),
        "market": {**{key: context.get(key) for key in ("last_close", "regime_label", "combined_score", "risk_label",
            "investor_strategy", "broker_strategy")},
            # Legacy market changes are percentage points; equity changes are ratios.
            "change_5d_ratio": trend["pct_change_5d"]/100 if trend.get("pct_change_5d") is not None else None,
            "change_20d_ratio": trend["pct_change_20d"]/100 if trend.get("pct_change_20d") is not None else None,
            "above_ma20": trend.get("above_ma20"), "atr_pct": volatility.get("atr_pct")}}


def quant_columns(db: Session, run: ScreeningRun, account_id: str, symbols: set[str]) -> dict:
    """Use only the viewer's completed reports on the exact screening input revision."""
    manifest = run.manifest_id or (run.summary or {}).get("manifest_id")
    if not manifest or not symbols or (run.summary or {}).get("input_mode") == "PROVISIONAL_DAILY":
        return {}
    report = AnalysisReport.payload
    ranked = select(AnalysisReport.id.label("id"), Job.symbol,
        func.row_number().over(partition_by=Job.symbol, order_by=(Job.evaluated_at.desc(), Job.id.desc())).label("rank")
        ).join(Job, Job.id == AnalysisReport.job_id).where(
        AnalysisReport.account_id == account_id, Job.account_id == account_id, Job.status == "SUCCEEDED",
        Job.target_session == run.target_session, Job.symbol.in_(symbols),
        report["versions"]["data_manifest"].as_string() == manifest).subquery()
    sections = report["sections"]
    names = ("decision", "agreement", "costs", "trade_plan", "statistics", "hmm", "context")
    # Read only the metrics used by the sheet; research and price series can be large.
    sources = {"decision": ("action", "rec"), "agreement": ("fcast",), "costs": ("costs",),
        "trade_plan": ("sl",), "statistics": ("vol",), "hmm": ("hmm",), "context": ("cross_corr",)}
    columns = [sections[name]["status"].as_string().label(name + "_status") for name in names]
    for name, keys in sources.items():
        columns.extend(sections[name]["metrics"][key].label(key) for key in keys)
    rows = db.execute(select(ranked.c.symbol, *columns).join_from(ranked, AnalysisReport,
        AnalysisReport.id == ranked.c.id).where(ranked.c.rank == 1)).mappings()
    values = {}
    for row in rows:
        metrics = {key: (row[key] or {}) if row[name + "_status"] == "AVAILABLE" else {}
            for name, keys in sources.items() for key in keys}
        action, rec, forecast = metrics["action"], metrics["rec"], metrics["fcast"]
        costs, stops = metrics["costs"], metrics["sl"]
        lock = forecast.get("lock_risk") or {}
        relations = metrics["cross_corr"].get("lead_lag") or []
        values[row["symbol"]] = {
            "action": action.get("action"), "gate_reasons": action.get("reason_codes"),
            "score": rec.get("score"), "rating": rec.get("rating"),
            "holding_sessions": forecast.get("hold_plan_sessions"), "hmm_regime": metrics["hmm"].get("current"),
            "volatility_regime": metrics["vol"].get("regime"), "forecast_direction": forecast.get("consensus"),
            "ensemble_return_pct": forecast.get("ensemble_ret_pct"), "roundtrip_cost_pct": costs.get("roundtrip_cost_pct"),
            "net_forecast_pct": costs.get("net_forecast_pct"), "lock_drawdown_pct": lock.get("max_dd_lock_pct"),
            "probability_loss_gt_3pct": lock.get("prob_loss_gt_3pct"), "entry_vnd": stops.get("entry"),
            "stop_loss_vnd": stops.get("sl_swing"), "take_profit_1_vnd": stops.get("tp1"),
            "take_profit_2_vnd": stops.get("tp_optimal") or stops.get("tp2") or stops.get("tp_2r"),
            "trade_model_agreement_pct": stops.get("model_agreement_pct"),
            "sector_lead_lag": relations[0].get("relation") if relations else None,
        }
    return values
