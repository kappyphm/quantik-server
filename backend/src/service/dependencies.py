"""Shared background dependency requests and recovery control loop."""
import time
import threading
from datetime import date

from sqlalchemy import select, update

from service.analysis_worker import wake_waiters
from service.market_data import DataError
from service.models import Account, DependencyRequest, DependencyWaiter, Job, new_id
from service.quant.configuration import QuantConfig
from service.analysis_readiness import ready_inputs
from service.operational_config import OperationsPolicy, active_version

LEASE_SECONDS = 120


def current_dependency(db, key: str, token: str):
    now = int(time.time())
    request = db.scalar(select(DependencyRequest).where(DependencyRequest.key == key).with_for_update()
        .execution_options(populate_existing=True))
    if not request or request.status != "RUNNING" or request.execution_token != token or request.lease_until <= now:
        raise DataError("DEPENDENCY_LEASE_LOST", True)
    operations = active_version(db, "OPERATIONS")
    if not operations:
        raise DataError("DEPENDENCY_OPERATIONS_NOT_READY", True)
    policy = OperationsPolicy.model_validate(operations.payload)
    if not policy.enabled or not policy.snapshots_enabled:
        raise DataError("DEPENDENCY_OPERATOR_PAUSED", True)
    waiter = db.scalar(select(Job.id).join(DependencyWaiter, DependencyWaiter.job_id == Job.id)
        .join(Account, Account.id == Job.account_id).where(DependencyWaiter.dependency_key == key,
            Job.status == "WAITING_DATA", Job.deadline > now, Account.enabled.is_(True)).limit(1))
    if not waiter:
        raise DataError("DEPENDENCY_NO_WAITERS")
    return request


def heartbeat(sessions, key: str, token: str) -> None:
    with sessions() as db, db.begin():
        current_dependency(db, key, token).lease_until = int(time.time()) + LEASE_SECONDS


class DependencyHeartbeat:
    def __init__(self, sessions, key: str, token: str):
        self.sessions, self.key, self.token = sessions, key, token
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.wait(20):
            try:
                heartbeat(self.sessions, self.key, self.token)
            except Exception:
                return  # Builder effects still require the fenced guard.

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=5)


def run_dependency_once(sessions, settings) -> int:
    now, token = int(time.time()), new_id()
    with sessions() as db, db.begin():
        request = db.scalar(select(DependencyRequest).where(
            DependencyRequest.status.in_(["PENDING", "WAITING"]), DependencyRequest.next_attempt_at <= now)
            .order_by(DependencyRequest.next_attempt_at).limit(1).with_for_update(skip_locked=True))
        if not request:
            return 0
        request.status, request.execution_token, request.lease_until, request.dirty = "RUNNING", token, now + LEASE_SECONDS, False
        key, payload = request.key, request.payload
    try:
        target = date.fromisoformat(payload["target_session"])
        config = QuantConfig(**payload["config"])
        with sessions() as db:
            current_dependency(db, key, token)
            manifest, _ = ready_inputs(db, target, config, settings)
            manifest_id = manifest.id
        from service.snapshots import build_market_snapshot
        with DependencyHeartbeat(sessions, key, token):
            snapshot = build_market_snapshot(sessions, manifest_id, config,
                operation_guard=lambda db: current_dependency(db, key, token))
            with sessions() as db:
                current_dependency(db, key, token)
            if snapshot.status != "READY" or (not snapshot.model_id and not settings.allow_degraded_analysis):
                raise DataError("SNAPSHOT_NOT_READY", True)
        status, reason, delay = "READY", None, 0
    except DataError as exc:
        status = "CLOSED" if exc.code == "DEPENDENCY_NO_WAITERS" else "WAITING" if exc.retryable else "FAILED"
        reason, delay = exc.code, 300
    except Exception:
        status, reason, delay = "WAITING", "DEPENDENCY_COMPUTE_ERROR", 300
    with sessions() as db, db.begin():
        request = db.scalar(select(DependencyRequest).where(DependencyRequest.key == key,
            DependencyRequest.execution_token == token, DependencyRequest.lease_until > int(time.time())).with_for_update())
        if request:
            request.status = "PENDING" if request.dirty and status != "CLOSED" else status
            request.next_attempt_at = int(time.time()) + (0 if request.dirty else delay)
            request.dirty, request.reason, request.execution_token, request.lease_until = False, reason, None, 0
    return 1


def reconcile_control_once(sessions, settings) -> dict:
    from service import jobs
    with sessions() as db, db.begin():
        recovered = jobs.reconcile(db)
        db.execute(update(DependencyRequest).where(DependencyRequest.status == "RUNNING",
            DependencyRequest.lease_until <= int(time.time())).values(status="PENDING", execution_token=None,
                lease_until=0, next_attempt_at=int(time.time()), reason="DEPENDENCY_LEASE_EXPIRED"))
        active_waiter = select(DependencyWaiter.job_id).join(Job, Job.id == DependencyWaiter.job_id).where(
            DependencyWaiter.dependency_key == DependencyRequest.key, Job.status.not_in(jobs.TERMINAL)).exists()
        db.execute(update(DependencyRequest).where(DependencyRequest.status != "RUNNING", ~active_waiter)
            .values(status="CLOSED", execution_token=None, lease_until=0))
    woken = wake_waiters(sessions, settings)
    return {"jobs_recovered": recovered, "jobs_woken": woken}
