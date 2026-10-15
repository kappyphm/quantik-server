"""Validated, immutable operation/calendar versions with audited compare-and-swap activation."""
import hashlib
import json
import time
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from service.billing import audit
from service.jobs import JobError
from service.models import ActiveRuntimeConfig, RuntimeConfigVersion


class OperationsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = True
    ingestion_enabled: bool = False
    screening_enabled: bool = True
    snapshots_enabled: bool = True
    training_enabled: bool = True
    auto_promote: bool = True
    retry_seconds: int = Field(default=300, ge=30, le=1800)
    maximum_attempts: int = Field(default=36, ge=1, le=100)
    task_timeout_seconds: int = Field(default=28800, ge=120, le=43200)
    history_calendar_days: int = Field(default=1900, ge=730, le=3650)


class TradingCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    sessions: list[date] = Field(min_length=1, max_length=800)
    source_urls: list[str] = Field(min_length=1, max_length=10)
    review_note: str = Field(min_length=20, max_length=1000)
    provenance: Literal["EXCHANGE_REFERENCE", "FIXTURE"] = "EXCHANGE_REFERENCE"

    @model_validator(mode="after")
    def validate_calendar(self):
        if self.end < self.start or (self.end-self.start).days > 1096:
            raise ValueError("Invalid calendar coverage")
        if self.sessions != sorted(set(self.sessions)) or self.sessions[0] < self.start or self.sessions[-1] > self.end:
            raise ValueError("Calendar sessions must be unique, ordered and within coverage")
        if any(not url.startswith("https://") or len(url) > 1000 for url in self.source_urls):
            raise ValueError("Source URLs must use HTTPS")
        return self


def insert_for(db):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def validate_payload(kind: str, payload: dict) -> dict:
    if kind == "OPERATIONS":
        return OperationsPolicy.model_validate(payload).model_dump(mode="json")
    if kind == "TRADING_CALENDAR":
        return TradingCalendar.model_validate(payload).model_dump(mode="json")
    raise JobError("CONFIG_KIND_UNSUPPORTED", 422)


def version_hash(kind: str, payload: dict) -> str:
    return hashlib.sha256(json.dumps({"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def create_version(db: Session, kind: str, payload: dict, actor_id: str | None) -> RuntimeConfigVersion:
    payload = validate_payload(kind, payload)
    version_id = version_hash(kind, payload)
    db.execute(insert_for(db)(RuntimeConfigVersion).values(id=version_id, kind=kind, payload=payload,
        created_at=int(time.time()), actor_id=actor_id).on_conflict_do_nothing(index_elements=["id"]))
    row = db.get(RuntimeConfigVersion, version_id)
    audit(db, "config.version_created", {"kind": kind, "version_id": version_id}, actor_id)
    return row


def checked_version(db: Session, version_id: str) -> RuntimeConfigVersion:
    row = db.get(RuntimeConfigVersion, version_id)
    if row is None:
        raise JobError("CONFIG_NOT_FOUND", 404)
    if version_hash(row.kind, row.payload) != row.id:
        raise JobError("CONFIG_CHECKSUM_MISMATCH", 409)
    validate_payload(row.kind, row.payload)
    return row


def active_version(db: Session, kind: str, *, lock: bool = False) -> RuntimeConfigVersion | None:
    pointer = db.scalar(select(ActiveRuntimeConfig).where(ActiveRuntimeConfig.kind == kind)
        .with_for_update().execution_options(populate_existing=True)) if lock else db.get(ActiveRuntimeConfig, kind)
    row = checked_version(db, pointer.version_id) if pointer else None
    if row and row.kind != kind:
        raise JobError("CONFIG_KIND_MISMATCH", 409)
    return row


def activate(db: Session, version_id: str, expected_version: str | None, reason: str, actor_id: str | None, *, allow_fixture: bool = False) -> RuntimeConfigVersion:
    if not 10 <= len(reason.strip()) <= 500:
        raise JobError("CONFIG_REASON_REQUIRED", 422)
    row = checked_version(db, version_id)
    if row.kind == "TRADING_CALENDAR" and row.payload["provenance"] == "FIXTURE" and not allow_fixture:
        raise JobError("FIXTURE_CALENDAR_FORBIDDEN", 409)
    # Insert-on-conflict is safe when no pointer exists and does not use an initial SAVEPOINT.
    inserted = db.scalar(insert_for(db)(ActiveRuntimeConfig).values(kind=row.kind, version_id=row.id)
        .on_conflict_do_nothing(index_elements=["kind"]).returning(ActiveRuntimeConfig.kind))
    pointer = db.scalar(select(ActiveRuntimeConfig).where(ActiveRuntimeConfig.kind == row.kind)
        .with_for_update().execution_options(populate_existing=True))
    before = None if inserted else pointer.version_id
    if before != expected_version:
        # The caller must roll back this transaction on conflict.
        raise JobError("CONFIG_ACTIVATION_CONFLICT", 409)
    pointer.version_id = row.id
    audit(db, "config.activated", {"kind": row.kind, "before": before, "after": row.id, "reason": reason}, actor_id)
    return row
