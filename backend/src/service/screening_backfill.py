"""Audited, idempotent requests to screen completed exchange sessions."""
import hashlib
import json
import time
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from service.jobs import JobError
from service.models import AuditEvent, RuntimeConfigVersion, ScheduledLane, ScheduledTask, new_id
from service.operational_config import OperationsPolicy, TradingCalendar, active_version, audit, insert_for
from service.scheduling import VN, allowed, stamp
from service.settings import Settings


def completed_sessions(db: Session, settings: Settings, now: int) -> tuple[RuntimeConfigVersion, list[date]]:
    reference = active_version(db, "TRADING_CALENDAR")
    if not reference:
        raise JobError("CALENDAR_NOT_READY", 409)
    calendar = TradingCalendar.model_validate(reference.payload)
    if calendar.provenance == "FIXTURE" and settings.environment == "production":
        raise JobError("FIXTURE_CALENDAR_FORBIDDEN", 409)
    today = datetime.fromtimestamp(now, VN).date()
    if not calendar.start <= today <= calendar.end:
        raise JobError("CALENDAR_OUT_OF_RANGE", 409)
    sessions = [day for day in calendar.sessions if stamp(day, "16:00") <= now]
    return reference, sessions


def task_view(task: ScheduledTask) -> dict:
    return {key: getattr(task, key) for key in ("id", "kind", "target_session", "slot", "status",
        "attempt", "scheduled_at", "deadline", "next_attempt_at", "reason", "result")}


def request_backfill(db: Session, settings: Settings, actor_id: str, target: date | None,
                     reason: str, key: str) -> ScheduledTask:
    now = int(time.time())
    payload_hash = hashlib.sha256(json.dumps({"target": target.isoformat() if target else None,
        "reason": reason}, sort_keys=True).encode()).hexdigest()
    # Serialize operator requests independently from execution lanes. The unique
    # task identity also deduplicates automatic retries for the same session.
    db.execute(insert_for(db)(ScheduledLane).values(kind="BACKFILL", lease_until=0)
        .on_conflict_do_nothing(index_elements=["kind"]))
    db.scalar(select(ScheduledLane).where(ScheduledLane.kind == "BACKFILL").with_for_update())
    previous = db.scalar(select(AuditEvent).where(AuditEvent.actor_id == actor_id,
        AuditEvent.action == "screening.backfill_requested", AuditEvent.details["request_key"].as_string() == key))
    if previous:
        if previous.details["payload_hash"] != payload_hash:
            raise JobError("IDEMPOTENCY_CONFLICT")
        return db.get(ScheduledTask, previous.details["task_id"])
    reference, sessions = completed_sessions(db, settings, now)
    target = target or (sessions[-1] if sessions else None)
    if target not in sessions:
        raise JobError("BACKFILL_SESSION_NOT_COMPLETED", 422)
    config = active_version(db, "OPERATIONS")
    if not config or not allowed("BACKFILL", OperationsPolicy.model_validate(config.payload), settings):
        raise JobError("BACKFILL_OPERATIONS_DISABLED", 409)
    pending = db.scalar(select(ScheduledTask).where(ScheduledTask.kind == "BACKFILL",
        ScheduledTask.status.in_(["PENDING", "WAITING", "RUNNING"])))
    if pending and pending.target_session != target:
        raise JobError("BACKFILL_ALREADY_PENDING", 409)
    task = db.scalar(select(ScheduledTask).where(ScheduledTask.kind == "BACKFILL",
        ScheduledTask.target_session == target, ScheduledTask.slot == "PM").with_for_update())
    old_state = task_view(task) if task else None
    if task is None:
        task = ScheduledTask(id=new_id(), kind="BACKFILL", target_session=target, slot="PM", created_at=now)
        db.add(task)
    if task.status not in {"PENDING", "WAITING", "RUNNING", "SUCCEEDED"}:
        policy = OperationsPolicy.model_validate(config.payload)
        task.config_id, task.calendar_id = config.id, reference.id
        task.scheduled_at = task.next_attempt_at = now
        task.deadline = now + policy.task_timeout_seconds
        task.status, task.attempt, task.reason, task.result = "PENDING", 0, None, None
        task.execution_token, task.lease_until, task.started_at = None, 0, None
    db.flush()
    audit(db, "screening.backfill_requested", {"task_id": task.id, "target_session": target.isoformat(),
        "reason": reason, "request_key": key, "payload_hash": payload_hash,
        "previous_status": old_state["status"] if old_state else None,
        "previous_attempts": old_state["attempt"] if old_state else None,
        "previous_reason": old_state["reason"] if old_state else None}, actor_id)
    return task
