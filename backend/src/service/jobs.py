"""Durable admission, worker fencing and terminal settlement.

All mutations run inside the caller's single DB transaction. No network I/O here.
"""
import hashlib
import json
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from service.models import Account, AnalysisReport, Job, JobAttempt, Outbox, QuotaBucket, Reservation, UsageLedger, new_id
from service.reports import Report
from pydantic import ValidationError

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
DEFAULT_POLICY = {"version": "daily-admission-1",
    "dependency_deadline_seconds": 86400, "execution_timeout_seconds": 900, "max_attempts": 3}


class JobError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status


def owned_job(db: Session, account_id: str, job_id: str, lock: bool = False) -> Job:
    stmt = select(Job).where(Job.id == job_id, Job.account_id == account_id)
    job = db.scalar(stmt.with_for_update() if lock else stmt)
    if not job:
        raise JobError("NOT_FOUND", 404)
    return job


def admit(db: Session, account_id: str, symbol: str, target_session: date,
          idempotency_key: str, sizing: dict | None = None) -> Job:
    now = int(time.time())
    if not re.fullmatch(r"[A-Z0-9]{2,16}", symbol) or not 1 <= len(idempotency_key) <= 128:
        raise JobError("INVALID_REQUEST", 422)
    if target_session > datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date():
        raise JobError("FUTURE_SESSION", 422)
    payload = {"symbol": symbol, "target_session": target_session.isoformat(), "sizing": sizing or {}}
    hashed = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    account = db.scalar(select(Account).where(Account.id == account_id).with_for_update())
    if not account or not account.enabled or account.role != "user":
        raise JobError("FORBIDDEN", 403)
    existing = db.scalar(select(Job).where(Job.account_id == account_id, Job.idempotency_key == idempotency_key))
    if existing:
        if existing.payload_hash != hashed:
            raise JobError("IDEMPOTENCY_CONFLICT")
        return existing
    from service.daily_limits import current_bucket
    bucket = current_bucket(db, account_id, now)
    if not bucket:
        raise JobError("NO_ENTITLEMENT", 403)
    if bucket.daily_created >= bucket.quota_total:
        raise JobError("DAILY_REQUEST_LIMIT", 429)
    active_count = db.scalar(select(func.count()).select_from(Job).where(Job.account_id == account_id,
        Job.status.not_in(TERMINAL)))
    if active_count >= bucket.active_limit:
        raise JobError("ACTIVE_JOB_LIMIT", 429)
    changed = db.execute(update(QuotaBucket).where(QuotaBucket.id == bucket.id,
        QuotaBucket.daily_created < QuotaBucket.quota_total,
        QuotaBucket.consumed + QuotaBucket.reserved < QuotaBucket.quota_total,
        QuotaBucket.active_jobs < QuotaBucket.active_limit)
        .values(reserved=QuotaBucket.reserved + 1, active_jobs=QuotaBucket.active_jobs + 1,
            daily_created=QuotaBucket.daily_created + 1))
    if changed.rowcount != 1:
        raise JobError("QUOTA_OR_JOB_LIMIT", 429)
    job = Job(account_id=account_id, bucket_id=bucket.id, idempotency_key=idempotency_key,
        payload_hash=hashed, symbol=symbol, target_session=target_session, inputs=payload,
        policy=DEFAULT_POLICY.copy(), created_at=now, ready_at=now,
        deadline=now + DEFAULT_POLICY["dependency_deadline_seconds"])
    db.add(job)
    db.flush()
    reservation = Reservation(job_id=job.id, bucket_id=bucket.id)
    db.add(reservation)
    db.flush()
    db.add(UsageLedger(reservation_id=reservation.id, effect="RESERVE", occurred_at=now, reason="ADMISSION"))
    db.add(Outbox(job_id=job.id, event="analysis.ready", created_at=now))
    return job


def settle(db: Session, job: Job, consume: bool, reason: str) -> None:
    reservation = db.scalar(select(Reservation).where(Reservation.job_id == job.id).with_for_update())
    if not reservation:
        raise JobError("MISSING_RESERVATION", 500)
    if reservation.state != "RESERVED":
        return
    state = "CONSUMED" if consume else "RELEASED"
    changed = db.execute(update(Reservation).where(Reservation.id == reservation.id,
        Reservation.state == "RESERVED").values(state=state))
    if changed.rowcount != 1:
        return
    db.execute(update(QuotaBucket).where(QuotaBucket.id == reservation.bucket_id)
        .values(reserved=QuotaBucket.reserved - 1, consumed=QuotaBucket.consumed + int(consume),
            active_jobs=QuotaBucket.active_jobs - 1))
    db.add(UsageLedger(reservation_id=reservation.id, effect=state, occurred_at=int(time.time()), reason=reason))


def claim(db: Session, job_id: str, lease_seconds: int = 120) -> tuple[Job, str] | None:
    now, token = int(time.time()), new_id()
    if lease_seconds <= 0:
        raise ValueError("Lease must be positive")
    result = db.execute(update(Job).where(Job.id == job_id, Job.status.in_(["QUEUED", "RETRY_WAIT"]),
        Job.ready_at <= now, Job.deadline > now)
        .values(status="RUNNING", execution_token=token, lease_until=now + lease_seconds, attempt=Job.attempt + 1))
    if result.rowcount != 1:
        return None
    job = db.get(Job, job_id)
    db.add(JobAttempt(token=token, job_id=job_id, attempt_number=job.attempt, started_at=now, outcome="RUNNING"))
    return job, token


def live_attempt(db: Session, job_id: str, token: str) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if not job or job.status != "RUNNING" or job.execution_token != token or not job.lease_until or job.lease_until <= int(time.time()):
        raise JobError("STALE_EXECUTION")
    return job


def heartbeat(db: Session, job_id: str, token: str, lease_seconds: int = 120) -> None:
    job = live_attempt(db, job_id, token)
    if lease_seconds <= 0:
        raise ValueError("Lease must be positive")
    job.lease_until = int(time.time()) + lease_seconds


def begin_compute(db: Session, job_id: str, token: str, versions: dict) -> None:
    job = live_attempt(db, job_id, token)
    account = db.scalar(select(Account).where(Account.id == job.account_id).with_for_update())
    if not account or not account.enabled:
        raise JobError("ACCOUNT_DISABLED", 403)
    required = {"data_manifest", "market_snapshot", "model", "config", "engines"}
    if not required.issubset(versions):
        raise JobError("MISSING_INPUT_VERSIONS", 422)
    if job.pinned_versions and job.pinned_versions != versions:
        raise JobError("PINNED_VERSION_CONFLICT")
    job.pinned_versions = versions.copy()
    if job.compute_started_at is None:
        job.compute_started_at = int(time.time())
    if job.evaluated_at is None:
        job.evaluated_at = int(time.time())


def wait_data(db: Session, job_id: str, token: str, reason: str) -> None:
    job = live_attempt(db, job_id, token)
    job.status, job.reason, job.execution_token, job.lease_until = "WAITING_DATA", reason, None, None


def wake_data(db: Session, job_id: str) -> bool:
    now = int(time.time())
    result = db.execute(update(Job).where(Job.id == job_id, Job.status == "WAITING_DATA", Job.deadline > now)
        .values(status="QUEUED", ready_at=now))
    if result.rowcount == 1:
        db.add(Outbox(job_id=job_id, event="analysis.ready", created_at=now))
        return True
    return False


def complete(db: Session, job_id: str, token: str, report: dict) -> AnalysisReport:
    job = live_attempt(db, job_id, token)
    account = db.scalar(select(Account).where(Account.id == job.account_id).with_for_update())
    if not account or not account.enabled:
        raise JobError("ACCOUNT_DISABLED", 403)
    if job.compute_started_at is None or report.get("versions") != job.pinned_versions:
        raise JobError("REPORT_VERSION_CONFLICT", 422)
    if report.get("symbol") != job.symbol or report.get("job_id") != job.id:
        raise JobError("REPORT_IDENTITY_CONFLICT", 422)
    try:
        validated = Report.model_validate(report).model_dump(mode="json")
        json.dumps(validated, allow_nan=False)
    except (ValidationError, ValueError):
        raise JobError("INVALID_REPORT", 422) from None
    record = AnalysisReport(job_id=job.id, account_id=job.account_id, payload=validated)
    db.add(record)
    job.status, job.reason, job.execution_token, job.lease_until = "SUCCEEDED", None, None, None
    settle(db, job, True, "SUCCESS")
    db.flush()
    return record


def fail_waiting(db: Session, job_id: str, reason: str) -> None:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job and job.status == "WAITING_DATA":
        job.status, job.reason = "FAILED", reason
        settle(db, job, False, reason)


def failed_attempt(db: Session, job_id: str, token: str, reason: str, retryable: bool) -> None:
    job = live_attempt(db, job_id, token)
    now = int(time.time())
    if retryable and job.attempt < job.policy["max_attempts"] and job.deadline > now:
        job.status, job.ready_at = "RETRY_WAIT", now + min(300, 2 ** job.attempt)
        db.add(Outbox(job_id=job.id, event="analysis.ready", created_at=now))
    else:
        job.status = "FAILED"
        settle(db, job, False, reason)
    job.reason, job.execution_token, job.lease_until = reason, None, None


def reconcile(db: Session) -> int:
    now = int(time.time())
    # Account suspension commits without taking job locks, avoiding account->job versus
    # completion's job->account deadlock. This sweep fences/releases pending work promptly.
    disabled = list(db.scalars(select(Job).join(Account, Account.id == Job.account_id)
        .where(Account.enabled.is_(False), Job.status.not_in(TERMINAL))
        .order_by(Job.created_at).limit(100).with_for_update(skip_locked=True, of=Job)))
    for job in disabled:
        job.status, job.reason, job.execution_token, job.lease_until = "FAILED", "ACCOUNT_DISABLED", None, None
        settle(db, job, False, job.reason)
    jobs = list(db.scalars(select(Job).where(Job.status.not_in(TERMINAL),
        or_(Job.deadline <= now, (Job.status == "RUNNING") & (Job.lease_until <= now)))
        .order_by(Job.created_at).limit(100).with_for_update(skip_locked=True)))
    changed = len(disabled)
    for job in jobs:
        if job.deadline <= now:
            job.status, job.reason = "FAILED", "DEPENDENCY_DEADLINE"
            settle(db, job, False, job.reason)
        elif job.status == "RUNNING" and job.lease_until and job.lease_until <= now:
            if job.attempt >= job.policy["max_attempts"]:
                job.status, job.reason = "FAILED", "ATTEMPTS_EXHAUSTED"
                settle(db, job, False, job.reason)
            else:
                job.status, job.ready_at, job.reason = "QUEUED", now, "LEASE_EXPIRED"
                db.add(Outbox(job_id=job.id, event="analysis.ready", created_at=now))
        else:
            continue
        job.execution_token, job.lease_until = None, None
        changed += 1
    unsettled = list(db.scalars(select(Job).join(Reservation, Reservation.job_id == Job.id)
        .where(Job.status.in_(TERMINAL), Reservation.state == "RESERVED")
        .order_by(Job.created_at).limit(100).with_for_update(skip_locked=True)))
    for job in unsettled:
        # Historical CANCELLED rows remain readable; releasing a reservation
        # never restores the daily admission count. No new cancellation exists.
        consume = job.status == "SUCCEEDED"
        settle(db, job, consume, job.reason or "TERMINAL_REPAIR")
        changed += 1
    return changed
