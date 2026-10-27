"""Read-only projections over immutable screening runs, with fixed top-five selection."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from service.models import ScreeningResult, ScreeningRun


def item_view(row: ScreeningResult) -> dict:
    payload = row.payload
    prices, volume = payload.get("price_summary", {}), payload.get("volume_context", {})
    groups = {name: {"score": group.get("score") if not group.get("unavailable_count") else None,
        "met_count": group.get("true_count", 0), "total_count": 7,
        "unavailable_count": group.get("unavailable_count", 0)}
        for name, group in payload["groups"].items()}
    eligible = bool(payload.get("ranking_eligible"))
    incomplete = any(group["unavailable_count"] for group in groups.values())
    return {"symbol": row.symbol, "exchange": payload.get("exchange"),
        "price_vnd": prices.get("close"), "change_ratio": prices.get("pct_change_1d"),
        "change_5d_ratio": prices.get("pct_change_5d"), "change_20d_ratio": prices.get("pct_change_20d"),
        "volume_shares": volume.get("raw_volume", prices.get("volume")),
        "average_volume_20_shares": volume.get("baseline20_volume_shares"),
        "volume_ratio_20": prices.get("vol_ratio_20"), "price_to_high20_ratio": prices.get("pct_of_high20"),
        "groups": groups, "eligible": eligible,
        "status": "INSUFFICIENT_DATA" if incomplete else "ELIGIBLE" if eligible else "LOW_LIQUIDITY"}


def top_results(db: Session, run_id: str, group: str) -> dict:
    rank = ScreeningResult.distribution_rank if group == "DISTRIBUTION" else ScreeningResult.overall_rank
    rows = db.scalars(select(ScreeningResult).where(ScreeningResult.run_id == run_id,
        rank >= 1, rank <= 5).order_by(rank, ScreeningResult.symbol).limit(5))
    return {"run_id": run_id, "group": group, "items": [item_view(row) for row in rows]}


def detail_view(run: ScreeningRun, row: ScreeningResult) -> dict:
    context = (run.summary or {}).get("market_context", {})
    detail = context.get("detail", {})
    trend, volatility = detail.get("trend", {}), detail.get("volatility", {})
    return {"run_id": run.id, "target_session": run.target_session.isoformat(), "published_at": run.published_at,
        "input_mode": (run.summary or {}).get("input_mode", "COMPLETED_DAILY"),
        "observed_at": row.payload.get("volume_context", {}).get("observed_at"),
        "item": item_view(row), "assessment": row.payload.get("assessment", {}),
        "market": {**{key: context.get(key) for key in ("last_close", "regime_label", "combined_score", "risk_label",
            "investor_strategy", "broker_strategy")},
            # Legacy market changes are percentage points; equity changes are ratios.
            "change_5d_ratio": trend["pct_change_5d"]/100 if trend.get("pct_change_5d") is not None else None,
            "change_20d_ratio": trend["pct_change_20d"]/100 if trend.get("pct_change_20d") is not None else None,
            "above_ma20": trend.get("above_ma20"), "atr_pct": volatility.get("atr_pct")}}
