"""Durable customer analysis execution; ingestion/training never run inside a request."""
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from service import jobs
from service.market_data import DataError, DailyHistory, checksum
from service.canonical_json import semantic_checksum
from service.models import Account, DataManifest, DependencyRequest, DependencyWaiter, HistoryRevision, Inbox, Job, JobAttempt, MarketSnapshot, ModelVersion, Outbox, PublishedSnapshot
from service.quant.configuration import QuantConfig
from service.settings import Settings
from service.quant.versions import ENGINE_VERSION, SNAPSHOT_POLICY
from service.delivery import Envelope, PoisonMessage

DEFAULT_CONFIG = replace(QuantConfig(), PRICE_INPUT_UNIT="VND")


def finalize_member(*args, **kwargs):
    from service.quant.assembly import finalize_member as implementation
    return implementation(*args, **kwargs)


def assemble_report(*args, **kwargs):
    from service.report_assembly import assemble_report as implementation
    return implementation(*args, **kwargs)


def dialect_insert(db):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def resolve_inputs(db, job: Job, settings: Settings) -> dict:
    account = db.get(Account, job.account_id)
    if not account or not account.enabled:
        raise DataError("ACCOUNT_DISABLED")
    pinned = job.pinned_versions
    if pinned:
        snapshot_id = pinned["market_snapshot"]
    else:
        name = "completed:" + job.target_session.isoformat() + ":" + checksum(asdict(DEFAULT_CONFIG))
        pointer = db.get(PublishedSnapshot, name)
        if not pointer:
            raise DataError("SNAPSHOT_NOT_READY", True)
        snapshot_id = pointer.snapshot_id
    # JSON/JSONB projection keeps every job from decoding the whole market snapshot.
    stmt = select(MarketSnapshot.manifest_id, MarketSnapshot.model_id, MarketSnapshot.config,
        MarketSnapshot.dependency_key, MarketSnapshot.payload["versions"].label("dependency"),
        MarketSnapshot.payload["members"][job.symbol].label("member"),
        MarketSnapshot.payload["member_checksums"][job.symbol].as_string().label("member_checksum"),
        MarketSnapshot.payload["reference"].label("reference"),
        MarketSnapshot.payload["reference_checksum"].as_string().label("reference_checksum"),
        MarketSnapshot.payload["warnings"].label("warnings"))
    row = db.execute(stmt.where(MarketSnapshot.id == snapshot_id, MarketSnapshot.status == "READY",
        MarketSnapshot.target_session == job.target_session)).first()
    if not row:
        raise DataError("SNAPSHOT_NOT_READY", True)
    content_hash = semantic_checksum if row.dependency and row.dependency.get("policy") == SNAPSHOT_POLICY else checksum
    if not row.dependency or content_hash(row.dependency) != row.dependency_key:
        raise DataError("SNAPSHOT_DEPENDENCY_CORRUPTED")
    if row.dependency["engine"] != ENGINE_VERSION or row.dependency["policy"] not in {"market-snapshot-beta-2", SNAPSHOT_POLICY}:
        raise DataError("SNAPSHOT_ENGINE_UNAVAILABLE", not bool(pinned))
    if not row.member:
        raise DataError("SYMBOL_SNAPSHOT_INELIGIBLE")
    if content_hash(row.member) != row.member_checksum or content_hash(row.reference) != row.reference_checksum:
        raise DataError("SNAPSHOT_MEMBER_CORRUPTED")
    manifest = db.get(DataManifest, row.manifest_id)
    if not manifest or manifest.target_session != job.target_session:
        raise DataError("MANIFEST_MISMATCH")
    history = db.get(HistoryRevision, manifest.histories[job.symbol])
    if not history or history.symbol != job.symbol or history.target_session != job.target_session:
        raise DataError("HISTORY_MISMATCH")
    if checksum(history.payload) != history.checksum:
        raise DataError("HISTORY_CHECKSUM_MISMATCH")
    if not settings.allow_fixture_analysis and history.payload["metadata"].get("provenance") != "LIVE_VALIDATED":
        raise DataError("LIVE_DATA_NOT_VALIDATED", True)
    warnings = list(row.warnings or []) + history.payload["metadata"].get("warnings", [])
    evaluated_at = job.evaluated_at or int(time.time())
    if not row.model_id and not settings.allow_degraded_analysis:
        raise DataError("MODEL_NOT_READY", True)
    if row.model_id:
        model = db.execute(select(ModelVersion.cutoff_session, ModelVersion.status, ModelVersion.evidence)
            .where(ModelVersion.id == row.model_id)).first()
        if not model or model.status != "PUBLISHED" or not model.evidence.get("live_data_validated"):
            raise DataError("MODEL_NOT_READY", True)
        age = (datetime.fromtimestamp(evaluated_at, timezone.utc).astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date()
            - model.cutoff_session).days
        if age < 0 or age >= 28 or model.cutoff_session > job.target_session:
            raise DataError("MODEL_EXPIRED_OR_FUTURE", True)
        if age >= 14:
            warnings.append("MODEL_AGE_WARNING")
        row.member["cross_sectional_model"]["model_age_days"] = age
    config = QuantConfig(**row.config)
    versions = {"data_manifest": manifest.id, "market_snapshot": snapshot_id, "model": row.model_id,
        "config": checksum(row.config), "engines": {"quant": ENGINE_VERSION, "report": "1.0"}}
    if pinned and pinned != versions:
        raise DataError("PINNED_INPUTS_MISMATCH")
    return {"member": row.member, "reference": row.reference, "config": config, "versions": versions,
        "history": DailyHistory(job.symbol, history.target_session, history.input_mode, history.payload),
        "warnings": warnings, "evaluated_at": evaluated_at}


def register_dependency(db, job: Job, reason: str) -> None:
    payload = {"target_session": job.target_session.isoformat(), "config": asdict(DEFAULT_CONFIG),
        "engine": ENGINE_VERSION, "snapshot_policy": SNAPSHOT_POLICY}
    key = checksum(payload)
    now = int(time.time())
    db.execute(dialect_insert(db)(DependencyRequest).values(key=key, kind="analysis_snapshot", payload=payload,
        status="PENDING", next_attempt_at=now, lease_until=0, reason=reason)
        .on_conflict_do_nothing(index_elements=["key"]))
    db.execute(update(DependencyRequest).where(DependencyRequest.key == key,
        DependencyRequest.status.in_(["READY", "FAILED", "CLOSED"]))
        .values(status="PENDING", next_attempt_at=now, reason=reason, dirty=False))
    db.execute(dialect_insert(db)(DependencyWaiter).values(job_id=job.id, dependency_key=key)
        .on_conflict_do_update(index_elements=["job_id"], set_={"dependency_key": key}))


class AttemptHeartbeat:
    def __init__(self, sessions, job_id: str, token: str):
        self.sessions, self.job_id, self.token = sessions, job_id, token
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.wait(20):
            try:
                with self.sessions() as db, db.begin():
                    job = jobs.live_attempt(db, self.job_id, self.token)
                    attempt = db.get(JobAttempt, self.token)
                    account = db.get(Account, job.account_id)
                    if not account or not account.enabled:
                        jobs.failed_attempt(db, job.id, self.token, "ACCOUNT_DISABLED", False)
                        self.lost.set()
                        return
                    if int(time.time()) - attempt.started_at >= job.policy.get("execution_timeout_seconds", 900):
                        jobs.failed_attempt(db, job.id, self.token, "EXECUTION_TIMEOUT", False)
                        self.lost.set()
                        return
                    jobs.heartbeat(db, job.id, self.token)
            except Exception:
                self.lost.set()
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=5)


class AnalysisWorker:
    def __init__(self, sessions, settings: Settings):
        self.sessions, self.settings = sessions, settings

    def process(self, envelope: Envelope) -> str:
        with self.sessions() as db, db.begin():
            event = db.get(Outbox, envelope.event_id)
            if not event or event.job_id != envelope.job_id or event.event != "analysis.ready":
                raise PoisonMessage("UNKNOWN_OUTBOX_EVENT")
            if db.get(Inbox, envelope.event_id):
                return "DUPLICATE"
            claimed = jobs.claim(db, envelope.job_id)
            if not claimed:
                return "NOT_CLAIMED"  # Durable reconciler/outbox handles active lease or delayed retry.
            job, token = claimed
        try:
            with AttemptHeartbeat(self.sessions, job.id, token) as heartbeat:
                with self.sessions() as db, db.begin():
                    current = jobs.live_attempt(db, job.id, token)
                    try:
                        inputs = resolve_inputs(db, current, self.settings)
                    except DataError as exc:
                        if exc.retryable:
                            register_dependency(db, current, exc.code)
                            jobs.wait_data(db, job.id, token, exc.code)
                            return "WAITING_DATA"
                        raise
                    jobs.begin_compute(db, job.id, token, inputs["versions"])
                    current.evaluated_at = inputs["evaluated_at"]
                final = finalize_member(inputs["member"], inputs["history"].frame(), inputs["reference"],
                    inputs["config"], job.inputs.get("sizing") or None)
                report = assemble_report(final, job_id=job.id, evaluated_at=datetime.fromtimestamp(inputs["evaluated_at"], timezone.utc),
                    data_as_of=datetime.combine(job.target_session, datetime.min.time(), timezone.utc),
                    target_session=job.target_session,
                    data_observed_at=datetime.fromisoformat(inputs["history"].payload["metadata"]["observed_at"]),
                    versions=inputs["versions"], config=inputs["config"], warnings=inputs["warnings"])
                if heartbeat.lost.is_set():
                    raise jobs.JobError("STALE_EXECUTION")
                with self.sessions() as db, db.begin():
                    jobs.complete(db, job.id, token, report)
                return "SUCCEEDED"
        except (DataError, jobs.JobError) as exc:
            with self.sessions() as db, db.begin():
                try:
                    jobs.failed_attempt(db, job.id, token, exc.code, False)
                except jobs.JobError as stale:
                    if stale.code != "STALE_EXECUTION":
                        raise
            return "STOPPED"
        except (ValueError, TypeError, ArithmeticError):
            with self.sessions() as db, db.begin():
                jobs.failed_attempt(db, job.id, token, "REPORT_COMPUTE_INVALID", False)
            return "FAILED"
        except Exception:
            with self.sessions() as db, db.begin():
                try:
                    jobs.failed_attempt(db, job.id, token, "WORKER_SYSTEM_ERROR", True)
                except jobs.JobError:
                    pass
            return "RETRY_OR_FAILED"
        finally:
            with self.sessions() as db, db.begin():
                current = db.get(Job, job.id)
                attempt = db.get(JobAttempt, token)
                if attempt:
                    attempt.stopped_at = int(time.time())
                    attempt.outcome = "LEASE_LOST" if current.attempt != attempt.attempt_number else current.status
                db.execute(dialect_insert(db)(Inbox).values(event_id=envelope.event_id, job_id=job.id,
                    completed_at=int(time.time())).on_conflict_do_nothing(index_elements=["event_id"]))


def wake_waiters(sessions, settings: Settings) -> int:
    count = 0
    with sessions() as db, db.begin():
        waiting = list(db.scalars(select(Job).where(Job.status == "WAITING_DATA")
            .order_by(Job.created_at).limit(100).with_for_update(skip_locked=True)))
        for job in waiting:
            try:
                resolve_inputs(db, job, settings)
            except DataError as exc:
                if not exc.retryable:
                    jobs.fail_waiting(db, job.id, exc.code)
                else:
                    job.reason = exc.code
                continue
            if jobs.wake_data(db, job.id):
                count += 1
    return count


def local_worker_once(sessions, worker: AnalysisWorker, limit: int = 10) -> int:
    if worker.settings.environment == "production":
        raise ValueError("Production requires RabbitMQ transport")
    with sessions() as db:
        events = list(db.scalars(select(Outbox).join(Job, Job.id == Outbox.job_id)
            .where(Outbox.published_at.is_(None), Job.ready_at <= int(time.time()))
            .order_by(Outbox.created_at, Outbox.id).limit(limit)))
    for event in events:
        worker.process(Envelope(event.id, event.job_id))
        with sessions() as db, db.begin():
            db.execute(update(Outbox).where(Outbox.id == event.id).values(published_at=int(time.time())))
    return len(events)
