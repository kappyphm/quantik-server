"""Report transport and minimum completeness contract; numerical parity is separate."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SECTION_NAMES = {"data_quality", "liquidity", "distribution", "statistics", "arima", "garch", "hmm",
    "technical", "context", "directional_alpha", "agreement", "simulation_risk", "costs", "trade_plan",
    "decision", "commentary"}
REQUIRED_AVAILABLE = {"data_quality", "liquidity", "statistics", "directional_alpha", "agreement",
    "simulation_risk", "costs", "trade_plan", "decision", "commentary"}


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    status: Literal["AVAILABLE", "UNAVAILABLE", "DISABLED"]
    reason_codes: list[str] = Field(default_factory=list)
    units: dict[str, str] = Field(default_factory=dict)
    metrics: dict

    @model_validator(mode="after")
    def valid_availability(self) -> "Section":
        if self.status != "AVAILABLE" and not self.reason_codes:
            raise ValueError("Unavailable sections require reasons")
        if self.status == "AVAILABLE" and not self.metrics:
            raise ValueError("Available sections require metrics")
        return self


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal["1.0"]
    job_id: str
    symbol: str
    evaluated_at: datetime
    data_as_of: datetime
    target_session: date | None = None
    data_observed_at: datetime | None = None
    input_mode: Literal["COMPLETED_DAILY"]
    versions: dict
    sections: dict[str, Section]
    decision: dict
    commentary: dict
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def completeness(self) -> "Report":
        if set(self.sections) != SECTION_NAMES:
            raise ValueError("Report must contain all version-1 sections")
        if any(self.sections[name].status != "AVAILABLE" for name in REQUIRED_AVAILABLE):
            raise ValueError("Required sections are unavailable")
        for stamp in (self.evaluated_at, self.data_as_of):
            if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
                raise ValueError("Report timestamps must be UTC")
        if not self.decision or not self.commentary:
            raise ValueError("Missing decision or commentary")
        return self


class ReportResult(Report):
    report_id: str
