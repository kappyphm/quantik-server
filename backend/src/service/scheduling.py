"""Durable VN-session scheduling; no provider calls or numerical imports in the scheduler."""
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from service.market_data import DataError
from service.models import (ScheduledLane, ScheduledTask, SchedulerCursor,
    ScreeningRun, new_id)
from service.operational_config import OperationsPolicy, TradingCalendar, active_version, checked_version, insert_for
from service.settings import Settings

VN = ZoneInfo("Asia/Ho_Chi_Minh")
KINDS = ("INGEST", "SCREEN", "SNAPSHOT", "TRAIN", "BACKFILL")


def stamp(day: date, clock: str) -> int:
    return int(datetime.combine(day, day_time.fromisoformat(clock), VN).timestamp())


def policy_for(db: Session, task: ScheduledTask) -> OperationsPolicy:
    version = checked_version(db, task.config_id)
    if version.kind != "OPERATIONS":
        raise DataError("TASK_CONFIG_KIND_MISMATCH")
    return OperationsPolicy.model_validate(version.payload)


def allowed(kind: str, policy: OperationsPolicy, settings: Settings) -> bool:
    return policy.enabled and {"INGEST": policy.ingestion_enabled and settings.scheduled_provider_enabled,
        "SCREEN": policy.screening_enabled, "SNAPSHOT": policy.snapshots_enabled, "TRAIN": policy.training_enabled,
        "BACKFILL": policy.ingestion_enabled and policy.screening_enabled and settings.scheduled_provider_enabled}[kind]


def lane_names(kind: str) -> tuple[str, ...]:
    # A backfill includes ingestion and screening. Screen workers acquire the
    # same lanes in this order, so neither path races an ingest worker.
    return ("INGEST", "SCREEN") if kind in {"SCREEN", "BACKFILL"} else (kind,)


def mark_screening_missed(db: Session, task: ScheduledTask, now: int) -> None:
    if task.kind != "SCREEN":
        return
    from service.screening import POLICY
    reason = task.reason or "SCHEDULER_MISSED_SLOT"
    db.execute(insert_for(db)(ScreeningRun).values(id=new_id(), target_session=task.target_session, slot=task.slot,
        status="FAILED", policy=POLICY, created_at=now, lease_until=0, reason=reason)
        .on_conflict_do_nothing(index_elements=["target_session", "slot"]))
    db.execute(update(ScreeningRun).where(ScreeningRun.target_session == task.target_session,
        ScreeningRun.slot == task.slot, ScreeningRun.status != "READY",
        or_(ScreeningRun.execution_token.is_(None), ScreeningRun.lease_until <= now))
        .values(status="FAILED", reason=reason, execution_token=None, lease_until=0))


def recover(db: Session, now: int) -> int:
    rows = list(db.scalars(select(ScheduledTask).where(
        ScheduledTask.status.in_(["PENDING", "WAITING", "RUNNING"]),
        or_(ScheduledTask.deadline <= now, (ScheduledTask.status == "RUNNING") & (ScheduledTask.lease_until <= now)))
        .order_by(ScheduledTask.scheduled_at).limit(100).with_for_update(skip_locked=True)))
    for task in rows:
        policy = policy_for(db, task)
        terminal = task.deadline <= now or task.attempt >= policy.maximum_attempts
        task.status, task.reason = ("FAILED", "TASK_DEADLINE" if task.deadline <= now else "TASK_ATTEMPTS_EXHAUSTED") if terminal else ("WAITING", "TASK_LEASE_EXPIRED")
        task.execution_token, task.lease_until, task.next_attempt_at = None, 0, now + policy.retry_seconds
        if terminal:
            mark_screening_missed(db, task, now)
    return len(rows)


def tick(sessions: sessionmaker[Session], settings: Settings, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Scheduler clock must include a timezone")
    local, current = now.astimezone(VN).date(), int(now.timestamp())
    with sessions() as db, db.begin():
        recovered = recover(db, current)
        config, reference = active_version(db, "OPERATIONS"), active_version(db, "TRADING_CALENDAR")
        if not config or not reference:
            return {"state": "CONFIG_NOT_READY", "recovered": recovered}
        policy, calendar = OperationsPolicy.model_validate(config.payload), TradingCalendar.model_validate(reference.payload)
        if not policy.enabled:
            return {"state": "DISABLED", "recovered": recovered}
        if calendar.provenance == "FIXTURE" and settings.environment == "production":
            return {"state": "FIXTURE_CALENDAR_FORBIDDEN", "recovered": recovered}
        if not calendar.start <= local <= calendar.end:
            return {"state": "CALENDAR_OUT_OF_RANGE", "recovered": recovered}
        # Initial seven-day catch-up covers the previous weekly trigger. A persisted cursor
        # then catches up every missed day, in bounded 31-day transactions after long outages.
        initial = max(calendar.start, local-timedelta(days=7)) - timedelta(days=1)
        db.execute(insert_for(db)(SchedulerCursor).values(name="market", planned_through=initial,
            config_id=config.id, calendar_id=reference.id)
            .on_conflict_do_nothing(index_elements=["name"]))
        cursor = db.scalar(select(SchedulerCursor).where(SchedulerCursor.name == "market").with_for_update())
        if cursor.config_id != config.id or cursor.calendar_id != reference.id:
            cursor.planned_through, cursor.config_id, cursor.calendar_id = initial, config.id, reference.id
        start = max(calendar.start, cursor.planned_through + timedelta(days=1))
        end = min(local, start+timedelta(days=30))
        inserted = 0
        for session in calendar.sessions:
            if not start <= session <= end:
                continue
            following = session + timedelta(days=1)
            slots = []
            if policy.ingestion_enabled:
                slots += [("INGEST", "AM", stamp(session, "11:30"), stamp(session, "12:55")),
                    ("INGEST", "PM", stamp(session, "16:00"), stamp(following, "05:00"))]
            if policy.screening_enabled:
                slots += [("SCREEN", "AM", stamp(session, "11:30"), stamp(session, "12:55")),
                    ("SCREEN", "PM", stamp(session, "15:00"), stamp(session, "17:30"))]
            if policy.snapshots_enabled:
                slots += [("SNAPSHOT", "PM", stamp(session, "16:10"), stamp(following, "06:00"))]
            week_end = session + timedelta(days=6-session.weekday())
            # Only the last exchange session of an ISO week is the weekly training cutoff.
            last_of_week = not any(session < candidate <= week_end for candidate in calendar.sessions)
            if policy.training_enabled and last_of_week and week_end <= calendar.end:
                slots += [("TRAIN", "WEEK", stamp(week_end, "18:00"), stamp(week_end+timedelta(days=1), "06:00"))]
            for kind, slot, scheduled, deadline in slots:
                result = db.execute(insert_for(db)(ScheduledTask).values(id=new_id(), kind=kind, target_session=session,
                    slot=slot, config_id=config.id, calendar_id=reference.id, scheduled_at=scheduled, deadline=deadline,
                    next_attempt_at=scheduled, created_at=current, status="PENDING", attempt=0, lease_until=0)
                    .on_conflict_do_nothing(index_elements=["kind", "target_session", "slot"]))
                inserted += result.rowcount
        if start <= end:
            cursor.planned_through = end
        recovered += recover(db, current)
        return {"state": "READY", "created": inserted, "recovered": recovered,
            "calendar_id": reference.id, "config_id": config.id, "planned_through": cursor.planned_through.isoformat()}


def claim(sessions: sessionmaker[Session], settings: Settings, kind: str, *, now: int | None = None) -> tuple[ScheduledTask, str] | None:
    if kind not in KINDS:
        raise ValueError("Unsupported task kind")
    now = int(time.time()) if now is None else now
    with sessions() as db, db.begin():
        version = active_version(db, "OPERATIONS")
        if not version or not allowed(kind, OperationsPolicy.model_validate(version.payload), settings):
            return None
        lanes = []
        for name in lane_names(kind):
            db.execute(insert_for(db)(ScheduledLane).values(kind=name, lease_until=0)
                .on_conflict_do_nothing(index_elements=["kind"]))
            lanes.append(db.scalar(select(ScheduledLane).where(ScheduledLane.kind == name).with_for_update()))
        if any(lane.lease_until > now for lane in lanes):
            return None
        candidates = ("SCREEN", "BACKFILL") if kind == "SCREEN" and allowed("BACKFILL", OperationsPolicy.model_validate(version.payload), settings) else (kind,)
        task = db.scalar(select(ScheduledTask).where(ScheduledTask.kind.in_(candidates),
            ScheduledTask.status.in_(["PENDING", "WAITING"]), ScheduledTask.next_attempt_at <= now,
            ScheduledTask.scheduled_at <= now, ScheduledTask.deadline > now)
            .order_by(ScheduledTask.scheduled_at, ScheduledTask.id).limit(1).with_for_update(skip_locked=True))
        if task is None:
            return None
        policy = policy_for(db, task)
        if task.attempt >= policy.maximum_attempts:
            task.status, task.reason = "FAILED", "TASK_ATTEMPTS_EXHAUSTED"
            mark_screening_missed(db, task, now)
            return None
        token = new_id()
        task.status, task.reason, task.execution_token = "RUNNING", None, token
        task.lease_until, task.started_at, task.attempt = now+120, now, task.attempt+1
        for lane in lanes:
            lane.execution_token, lane.lease_until = token, task.lease_until
        return task, token


def assert_current(db: Session, task_id: str, token: str, settings: Settings, *, now: int | None = None) -> ScheduledTask:
    now = int(time.time()) if now is None else now
    task = db.scalar(select(ScheduledTask).where(ScheduledTask.id == task_id).with_for_update()
        .execution_options(populate_existing=True))
    if task is None or task.status != "RUNNING" or task.execution_token != token or task.lease_until <= now:
        raise DataError("TASK_LEASE_LOST", True)
    policy = policy_for(db, task)
    if task.deadline <= now or now-task.started_at >= policy.task_timeout_seconds:
        raise DataError("TASK_EXECUTION_DEADLINE")
    if task.kind == "BACKFILL":
        due = db.scalar(select(ScheduledTask.id).where(ScheduledTask.kind.in_(["INGEST", "SCREEN"]),
            ScheduledTask.status.in_(["PENDING", "WAITING"]), ScheduledTask.scheduled_at <= now,
            ScheduledTask.next_attempt_at <= now, ScheduledTask.deadline > now).limit(1))
        if due:
            raise DataError("BACKFILL_YIELD_TO_SCHEDULE", True, 120)
    active = active_version(db, "OPERATIONS", lock=True)
    if not active or not allowed(task.kind, OperationsPolicy.model_validate(active.payload), settings):
        raise DataError("TASK_OPERATOR_PAUSED", True)
    calendar = checked_version(db, task.calendar_id)
    if calendar.kind != "TRADING_CALENDAR" or task.target_session.isoformat() not in calendar.payload["sessions"]:
        raise DataError("TASK_SESSION_NOT_IN_CALENDAR")
    if calendar.payload["provenance"] == "FIXTURE" and settings.environment == "production":
        raise DataError("FIXTURE_CALENDAR_FORBIDDEN")
    current_calendar = active_version(db, "TRADING_CALENDAR", lock=True)
    if current_calendar:
        current_calendar = TradingCalendar.model_validate(current_calendar.payload)
        if current_calendar.start <= task.target_session <= current_calendar.end and task.target_session not in current_calendar.sessions:
            raise DataError("TASK_SESSION_REMOVED_FROM_CALENDAR")
    return task


def heartbeat(sessions: sessionmaker[Session], settings: Settings, task_id: str, token: str) -> None:
    now = int(time.time())
    with sessions() as db, db.begin():
        # Same lane->task lock order as claim. Finish releases the lane in a second transaction.
        lanes = list(db.scalars(select(ScheduledLane).where(ScheduledLane.execution_token == token)
            .order_by(ScheduledLane.kind).with_for_update()))
        if not lanes or any(lane.lease_until <= now for lane in lanes):
            raise DataError("TASK_LANE_LOST", True)
        task = assert_current(db, task_id, token, settings, now=now)
        if tuple(lane.kind for lane in lanes) != lane_names(task.kind):
            raise DataError("TASK_LANE_LOST", True)
        task.lease_until = now+120
        for lane in lanes:
            lane.lease_until = task.lease_until


def finish(sessions: sessionmaker[Session], task_id: str, token: str, *, result: dict | None = None, error: DataError | None = None) -> None:
    now = int(time.time())
    with sessions() as db, db.begin():
        task = db.scalar(select(ScheduledTask).where(ScheduledTask.id == task_id,
            ScheduledTask.execution_token == token, ScheduledTask.status == "RUNNING").with_for_update())
        if task and task.lease_until > now:
            policy = policy_for(db, task)
            if now >= task.deadline and error is None:
                error = DataError("TASK_DEADLINE")
            retry = error and error.retryable and task.attempt < policy.maximum_attempts and now < task.deadline
            task.status = "WAITING" if retry else ("FAILED" if error else "SUCCEEDED")
            task.reason, task.result = error.code if error else None, result
            task.next_attempt_at = now + max(policy.retry_seconds, (error.retry_after or 0) if error else 0)
            task.execution_token, task.lease_until = None, 0
            if error and not retry:
                mark_screening_missed(db, task, now)
    with sessions() as db, db.begin():
        db.execute(update(ScheduledLane).where(ScheduledLane.execution_token == token)
            .values(execution_token=None, lease_until=0))
