from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from service.jobs import JobError
from service.models import RuntimeConfigVersion, ScheduledTask
from service.operational_config import activate, active_version, create_version
from service.settings import Settings

Kind = Literal["OPERATIONS", "TRADING_CALENDAR"]


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict


class ActivationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: str | None = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=10, max_length=500)


def view(row: RuntimeConfigVersion) -> dict:
    return {"id": row.id, "kind": row.kind, "payload": row.payload, "created_at": row.created_at}


def router(settings: Settings, admin: Callable, db_session: Callable) -> APIRouter:
    routes = APIRouter(prefix="/api/v1/admin")

    @routes.post("/configs/{kind}/versions", status_code=201)
    def create(kind: Kind, body: VersionInput, actor=Depends(admin), db=Depends(db_session)):
        try:
            row = create_version(db, kind, body.payload, actor.id)
        except ValidationError:
            raise JobError("INVALID_CONFIG", 422) from None
        db.commit()
        return view(row)

    @routes.get("/configs/{kind}")
    def configs(kind: Kind, cursor: str | None = None, limit: int = Query(20, ge=1, le=100),
                actor=Depends(admin), db=Depends(db_session)):
        query = select(RuntimeConfigVersion).where(RuntimeConfigVersion.kind == kind).order_by(RuntimeConfigVersion.id)
        if cursor:
            query = query.where(RuntimeConfigVersion.id > cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        current = active_version(db, kind)
        return {"active_version": current.id if current else None, "items": [view(row) for row in rows[:limit]],
            "next_cursor": rows[limit-1].id if len(rows)>limit else None,
            "runtime_gates": {"scheduled_provider_enabled": settings.scheduled_provider_enabled} if kind == "OPERATIONS" else {}}

    @routes.post("/configs/{kind}/{version_id}/activate")
    def activation(kind: Kind, version_id: str, body: ActivationInput, actor=Depends(admin), db=Depends(db_session)):
        row = db.get(RuntimeConfigVersion, version_id)
        if not row or row.kind != kind:
            raise JobError("CONFIG_NOT_FOUND", 404)
        row = activate(db, version_id, body.expected_version, body.reason, actor.id,
            allow_fixture=settings.environment != "production")
        db.commit()
        return view(row)

    @routes.get("/scheduled-tasks")
    def tasks(cursor: str | None = None, status: str | None = None, kind: str | None = None,
              limit: int = Query(50, ge=1, le=100), actor=Depends(admin), db=Depends(db_session)):
        query = select(ScheduledTask).order_by(ScheduledTask.id)
        if cursor:
            query = query.where(ScheduledTask.id > cursor)
        if status:
            query = query.where(ScheduledTask.status == status)
        if kind:
            query = query.where(ScheduledTask.kind == kind)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [{key: getattr(row, key) for key in ("id", "kind", "target_session", "slot", "config_id",
            "calendar_id", "scheduled_at", "deadline", "status", "attempt", "next_attempt_at", "reason", "result")}
            for row in rows[:limit]], "next_cursor": rows[limit-1].id if len(rows)>limit else None}

    return routes
