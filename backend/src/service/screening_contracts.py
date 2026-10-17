"""Customer screening outputs; calculation details stay in internal persistence."""
from typing import Literal
from pydantic import BaseModel


class ScreeningItem(BaseModel):
    symbol: str
    exchange: str | None = None
    score: float
    price_vnd: float | None = None
    change_ratio: float | None = None
    recommendation: str | None = None


class ScreeningPage(BaseModel):
    run_id: str
    group: Literal["OVERALL", "DISTRIBUTION"]
    items: list[ScreeningItem]
    next_cursor: None = None
