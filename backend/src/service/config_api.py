from collections.abc import Callable
from datetime import date
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query
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


class BackfillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_session: date | None = None
    reason: str = Field(min_length=10, max_length=500)


def view(row: RuntimeConfigVersion) -> dict:
    return {"id": row.id, "kind": row.kind, "payload": row.payload, "created_at": row.created_at}


def router(settings: Settings, admin: Callable, db_session: Callable) -> APIRouter:
    routes = APIRouter(prefix="/api/v1/admin")

    @routes.get("/screening/backfill-options")
    def backfill_options(actor=Depends(admin), db=Depends(db_session)):
        from service.screening_backfill import completed_sessions, task_view
        from service.scheduling import allowed
        from service.operational_config import OperationsPolicy
        _, sessions = completed_sessions(db, settings, int(time.time()))
        config = active_version(db, "OPERATIONS")
        latest = db.scalar(select(ScheduledTask).where(ScheduledTask.kind == "BACKFILL")
            .order_by(ScheduledTask.scheduled_at.desc(), ScheduledTask.id.desc()).limit(1))
        return {"sessions": list(reversed(sessions[-60:])), "latest_task": task_view(latest) if latest else None,
            "enabled": bool(config and allowed("BACKFILL", OperationsPolicy.model_validate(config.payload), settings))}

    @routes.post("/screening/backfills", status_code=202)
    def backfill(body: BackfillInput, idempotency_key: str = Header(min_length=1, max_length=128),
                 actor=Depends(admin), db=Depends(db_session)):
        from service.screening_backfill import request_backfill, task_view
        if len(body.reason.strip()) < 10:
            raise JobError("BACKFILL_REASON_REQUIRED", 422)
        task = request_backfill(db, settings, actor.id, body.target_session, body.reason.strip(), idempotency_key)
        db.commit()
        return task_view(task)

    @routes.get("/scheduled-tasks/{task_id}")
    def task_detail(task_id: str, actor=Depends(admin), db=Depends(db_session)):
        from service.screening_backfill import task_view
        task = db.get(ScheduledTask, task_id)
        if not task:
            raise JobError("TASK_NOT_FOUND", 404)
        return task_view(task)

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
