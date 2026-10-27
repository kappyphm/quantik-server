"""Customer views; internal formulas and provenance stay private."""
from typing import Literal
from pydantic import BaseModel


class ScreeningScore(BaseModel):
    score: float | None = None
    met_count: int = 0
    total_count: int = 7
    unavailable_count: int = 0


class ScreeningItem(BaseModel):
    symbol: str
    exchange: str | None = None
    price_vnd: float | None = None
    change_ratio: float | None = None
    change_5d_ratio: float | None = None
    change_20d_ratio: float | None = None
    volume_shares: float | None = None
    average_volume_20_shares: float | None = None
    volume_ratio_20: float | None = None
    price_to_high20_ratio: float | None = None
    groups: dict[Literal["STRONG", "ACCUMULATION", "DISTRIBUTION"], ScreeningScore]
    eligible: bool
    status: Literal["ELIGIBLE", "INSUFFICIENT_DATA", "LOW_LIQUIDITY"]


class ScreeningPage(BaseModel):
    run_id: str
    group: Literal["OVERALL", "DISTRIBUTION"]
    items: list[ScreeningItem]


class ScreeningAssessment(BaseModel):
    overall_signal: str | None = None
    adjusted_signal: str | None = None
    money_flow: str | None = None
    trend: str | None = None
    strength: str | None = None
    investor_note: str | None = None
    broker_note: str | None = None


class ScreeningMarket(BaseModel):
    last_close: float | None = None
    regime_label: str | None = None
    combined_score: float | None = None
    risk_label: str | None = None
    change_5d_ratio: float | None = None
    change_20d_ratio: float | None = None
    above_ma20: bool | None = None
    atr_pct: float | None = None
    investor_strategy: str | None = None
    broker_strategy: str | None = None


class ScreeningDetail(BaseModel):
    run_id: str
    target_session: str
    published_at: int
    input_mode: str
    observed_at: str | None = None
    item: ScreeningItem
    assessment: ScreeningAssessment
    market: ScreeningMarket
