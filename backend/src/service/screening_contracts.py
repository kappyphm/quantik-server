from typing import Literal
from pydantic import BaseModel, Field


class CriterionRecord(BaseModel):
    state: Literal["TRUE", "FALSE", "UNAVAILABLE"]
    reason_codes: list[str]
    minimum_bars: int
    detail: str
    input_fields: list[str]


class ScreeningGroup(BaseModel):
    criteria: dict[str, CriterionRecord]
    true_count: int
    false_count: int
    unavailable_count: int
    coverage_ratio: float
    score: float
    ranking_eligible: bool


class ScreeningItem(BaseModel):
    symbol: str
    groups: dict[str, ScreeningGroup]
    assessment: dict
    price_summary: dict
    overall_score: float
    ranking_eligible: bool
    rejection_reasons: list[str]
    volume_context: dict
    policy_version: str
    warnings: list[str]
    units: dict[str, str]
    exchange: str | None = None
    ranks: dict[str, int | None] = Field(default_factory=dict)


class ScreeningPage(BaseModel):
    run_id: str
    group: str
    items: list[ScreeningItem]
    next_cursor: str | None
