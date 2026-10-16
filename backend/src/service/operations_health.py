"""Read-only, bounded operational aggregates; no numerical payloads or customer identifiers."""
from datetime import datetime, timezone
import math
import shutil
import threading
import time
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from service.db import SCHEMA_REVISION
from service.jobs import TERMINAL
from service.models import (BankPayment, DataManifest, DependencyRequest, Job, JobAttempt, MarketSnapshot,
    ModelVersion, Outbox, ProcessHeartbeat, ProviderCall, ProviderGate, PublishedModel, PublishedScreening,
    QuotaBucket, QuotaRefund, Reservation, ScheduledTask, ScreeningRun)
from service.operational_config import TradingCalendar, active_version
from service.calendar_reference import expected_screening
from service.settings import Settings
from service.telemetry import labels


def grouped(db: Session, model, field="status") -> dict:
    column = getattr(model, field)
    return {state: count for state, count in db.execute(select(column, func.count()).group_by(column))}


def quota_inconsistencies(db: Session) -> int:
    reservations = select(Reservation.bucket_id, func.sum(case((Reservation.state == "RESERVED", 1), else_=0)).label("reserved"),
        func.sum(case((Reservation.state == "CONSUMED", 1), else_=0)).label("consumed")).group_by(Reservation.bucket_id).subquery()
    active = select(Job.bucket_id, func.count().label("active")).where(Job.status.not_in(TERMINAL)).group_by(Job.bucket_id).subquery()
    refunded = select(Reservation.bucket_id, func.count().label("refunded")).join(QuotaRefund,
        QuotaRefund.reservation_id == Reservation.id).group_by(Reservation.bucket_id).subquery()
    query = select(func.count()).select_from(QuotaBucket).outerjoin(reservations, reservations.c.bucket_id == QuotaBucket.id)
    query = query.outerjoin(active, active.c.bucket_id == QuotaBucket.id).outerjoin(refunded, refunded.c.bucket_id == QuotaBucket.id)
    return db.scalar(query.where(or_(QuotaBucket.reserved != func.coalesce(reservations.c.reserved, 0),
        QuotaBucket.active_jobs != func.coalesce(active.c.active, 0),
        QuotaBucket.consumed != func.coalesce(reservations.c.consumed, 0)-func.coalesce(refunded.c.refunded, 0))))


def broker_probe(settings: Settings) -> dict:
    connection = None
    try:
        import pika
        import logging
        logging.getLogger("pika").setLevel(logging.CRITICAL)
        parameters = pika.URLParameters(settings.amqp_url.get_secret_value())
        parameters.connection_attempts, parameters.socket_timeout, parameters.stack_timeout = 1, 2, 3
        parameters.blocked_connection_timeout, parameters.heartbeat = 2, 0
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        queue = channel.queue_declare(queue="quantik.analysis", passive=True)
        dead = channel.queue_declare(queue="quantik.analysis.dead", passive=True)
        return {"up": True, "ready_messages": queue.method.message_count, "consumers": queue.method.consumer_count,
            "dead_messages": dead.method.message_count}
    except ImportError:
        return {"up": False, "reason": "BROKER_CLIENT_UNAVAILABLE"}
    except Exception:
        return {"up": False, "reason": "BROKER_UNAVAILABLE_OR_UNINITIALIZED"}
    finally:
        if connection and connection.is_open:
            try:
                connection.close()
            except Exception:
                pass


def collect(db: Session, *, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    def count(model, *conditions):
        return db.scalar(select(func.count()).select_from(model).where(*conditions))
    def age(stamp):
        return max(0, now-stamp) if stamp is not None else 0
    ready = db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
    oldest_queue = db.scalar(select(func.min(Job.created_at)).where(Job.status.in_(["QUEUED", "RETRY_WAIT"])))
    oldest_wait = db.scalar(select(func.min(Job.created_at)).where(Job.status == "WAITING_DATA"))
    oldest_outbox = db.scalar(select(func.min(Outbox.created_at)).where(Outbox.published_at.is_(None)))
    leaks = db.scalar(select(func.count()).select_from(Reservation).join(Job, Job.id == Reservation.job_id)
        .where(Reservation.state == "RESERVED", Job.status.in_(TERMINAL)))
    attempts = list(db.execute(select(JobAttempt.stopped_at-JobAttempt.started_at).where(JobAttempt.stopped_at.is_not(None))
        .order_by(JobAttempt.stopped_at.desc()).limit(1000)).scalars())
    attempts = sorted(max(0, value) for value in attempts)
    waits = sorted(max(0, value) for value in db.execute(select(JobAttempt.started_at-Job.created_at)
        .join(Job, Job.id == JobAttempt.job_id).where(JobAttempt.stopped_at.is_not(None))
        .order_by(JobAttempt.stopped_at.desc()).limit(1000)).scalars())
    def quantile(values, ratio):
        return values[min(len(values)-1, math.ceil(len(values)*ratio)-1)] if values else None
    processes = [{"role": role, "state": state, "count": total} for role, state, total in db.execute(
        select(ProcessHeartbeat.role, ProcessHeartbeat.state, func.count()).where(ProcessHeartbeat.last_seen >= now-90,
            ProcessHeartbeat.state != "STOPPED")
        .group_by(ProcessHeartbeat.role, ProcessHeartbeat.state))]
    provider = {str(window): count(ProviderCall, ProviderCall.occurred_at > now-window) for window in (60, 3600, 86400)}
    gate = db.get(ProviderGate, "vnstock")
    pointer = db.get(PublishedModel, "weekly-lightgbm")
    model = db.execute(select(ModelVersion.cutoff_session, ModelVersion.evidence).where(ModelVersion.id == pointer.model_id)).first() if pointer else None
    model_age = (datetime.fromtimestamp(now, ZoneInfo("Asia/Ho_Chi_Minh")).date()-model.cutoff_session).days if model else None
    latest_data = db.scalar(select(func.max(DataManifest.created_at)))
    calendar = active_version(db, "TRADING_CALENDAR")
    calendar_status, screening_missed = "NOT_CONFIGURED", None
    if calendar:
        calendar = TradingCalendar.model_validate(calendar.payload)
        instant = datetime.fromtimestamp(now, timezone.utc)
        if calendar.start <= instant.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date() <= calendar.end:
            calendar_status = "READY"
            expected = expected_screening(calendar, "ALL", instant)
            publication = db.get(PublishedScreening, "ALL")
            run = db.get(ScreeningRun, publication.run_id) if publication else None
            screening_missed = bool(expected and (not run or (run.target_session, run.slot) < expected))
        else:
            calendar_status = "OUT_OF_RANGE"
    reconciliation_since = db.scalar(select(func.min(BankPayment.received_at)).where(BankPayment.status == "RECONCILIATION"))
    disk = shutil.disk_usage(".")
    publication = db.get(PublishedScreening, "ALL")
    screening = db.get(ScreeningRun, publication.run_id) if publication else None
    attempt = db.scalar(select(ScreeningRun).order_by(ScreeningRun.target_session.desc(), ScreeningRun.slot.desc()).limit(1))
    operations = active_version(db, "OPERATIONS")
    return {"collected_at": now, "schema_ready": ready, "jobs": grouped(db, Job),
        "screening": {"published": {"id": screening.id, "target_session": screening.target_session.isoformat(),
            "slot": screening.slot, "published_at": screening.published_at,
            "coverage": (screening.summary or {}).get("history_coverage_ratio")} if screening else None,
            "latest_attempt": {"status": attempt.status, "reason": attempt.reason, "target_session": attempt.target_session.isoformat(), "slot": attempt.slot} if attempt else None,
            "operations": {key: operations.payload.get(key) for key in ("enabled", "ingestion_enabled", "screening_enabled")} if operations else None},
        "queue_oldest_seconds": age(oldest_queue), "waiting_data_oldest_seconds": age(oldest_wait),
        "expired_job_leases": count(Job, Job.status == "RUNNING", Job.lease_until <= now),
        "outbox_pending": count(Outbox, Outbox.published_at.is_(None)), "outbox_oldest_seconds": age(oldest_outbox),
        "terminal_reservation_leaks": leaks, "inconsistent_quota_buckets": quota_inconsistencies(db),
        "attempt_runtime_seconds": {"sample_count": len(attempts), "p95": quantile(attempts, .95), "p99": quantile(attempts, .99)},
        "attempt_wait_seconds": {"sample_count": len(waits), "p95": quantile(waits, .95), "p99": quantile(waits, .99)},
        "dependencies": grouped(db, DependencyRequest), "snapshots": grouped(db, MarketSnapshot),
        "screening_runs": grouped(db, ScreeningRun), "scheduled_tasks": grouped(db, ScheduledTask),
        "processes": processes, "provider_calls": provider, "provider_cooldown_seconds": max(0, gate.cooldown_until-now) if gate else 0,
        "models": grouped(db, ModelVersion), "published_model_present": model is not None, "published_model_age_days": model_age,
        "published_model_live_validated": bool(model and model.evidence.get("live_data_validated")),
        "latest_manifest_present": latest_data is not None, "latest_manifest_age_seconds": age(latest_data),
        "calendar_status": calendar_status, "screening_expected_slot_missed": screening_missed,
        "payments": grouped(db, BankPayment), "reconciliation_oldest_seconds": age(reconciliation_since),
        "disk_free_bytes": disk.free, "disk_total_bytes": disk.total}


class BrokerProbe:
    """At most one diagnostic connection/thread, even if an AMQP RPC stalls forever."""
    def __init__(self, settings: Settings, timeout: float = 4):
        self.settings, self.timeout = settings, timeout
        self.lock = threading.Lock()
        self.thread, self.result = None, None
        self.done = threading.Event()

    def run(self) -> None:
        try:
            result = broker_probe(self.settings)
        except Exception:
            result = {"up": False, "reason": "BROKER_PROBE_FAILED"}
        with self.lock:
            self.result = result
        self.done.set()

    def snapshot(self) -> dict:
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.result = None
                self.done.clear()
                self.thread = threading.Thread(target=self.run, daemon=True, name="broker-health-probe")
                self.thread.start()
        if not self.done.wait(self.timeout):
            return {"up": False, "reason": "BROKER_PROBE_TIMEOUT"}
        with self.lock:
            return dict(self.result)


class Collector:
    def __init__(self, sessions: sessionmaker[Session], settings: Settings):
        self.sessions, self.settings = sessions, settings
        self.lock, self.cached, self.expires_at = threading.Lock(), None, 0.0
        self.broker = BrokerProbe(settings)

    def snapshot(self) -> dict:
        with self.lock:
            if time.monotonic() < self.expires_at and self.cached is not None:
                return self.cached
            with self.sessions() as db:
                data = collect(db)
            data["screening"]["scheduled_provider_enabled"] = self.settings.scheduled_provider_enabled
            data["broker"] = self.broker.snapshot()
            self.cached, self.expires_at = data, time.monotonic()+10
            return data


def prometheus(data: dict, release_id: str) -> str:
    lines = []
    declared = set()
    def gauge(name, value, **dimensions):
        if value is not None and isinstance(value, (int, float, bool)) and math.isfinite(value):
            if name not in declared:
                lines.append("# TYPE quantik_"+name+" gauge")
                declared.add(name)
            lines.append("quantik_"+name+labels(dimensions)+" "+str(int(value) if isinstance(value, bool) else value))
    gauge("release_info", 1, release=release_id)
    for key in ("schema_ready", "queue_oldest_seconds", "waiting_data_oldest_seconds", "expired_job_leases", "outbox_pending",
                "outbox_oldest_seconds", "terminal_reservation_leaks", "inconsistent_quota_buckets", "provider_cooldown_seconds",
                "published_model_present", "published_model_age_days", "published_model_live_validated", "latest_manifest_present",
                "latest_manifest_age_seconds", "screening_expected_slot_missed", "reconciliation_oldest_seconds", "disk_free_bytes", "disk_total_bytes"):
        gauge("db_"+key, data[key])
    for key in ("jobs", "dependencies", "snapshots", "screening_runs", "scheduled_tasks", "models", "payments"):
        for status, total in sorted(data[key].items()):
            gauge("db_"+key, total, status=status)
    processes = {(row["role"], row["state"]): row["count"] for row in data["processes"]}
    roles = ("api", "dispatcher", "worker", "dependencies", "reconciler", "scheduler",
        "schedule-worker:INGEST", "schedule-worker:SCREEN", "schedule-worker:SNAPSHOT", "schedule-worker:TRAIN")
    for role in roles:
        for state in ("RUNNING", "ERROR"):
            gauge("processes_alive", processes.get((role, state), 0), role=role, state=state)
    for window, total in data["provider_calls"].items():
        gauge("db_provider_calls", total, window_seconds=window)
    for kind in ("runtime", "wait"):
        for name, value in data["attempt_"+kind+"_seconds"].items():
            gauge("db_attempt_"+kind+"_"+name+("_seconds" if name != "sample_count" else ""), value)
    gauge("db_calendar_ready", data["calendar_status"] == "READY")
    for key, value in data["broker"].items():
        if key != "reason":
            gauge("broker_"+key, value)
    return "\n".join(lines)+"\n"
