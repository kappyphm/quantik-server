"""Shared background dependency requests and recovery control loop."""
import time

from sqlalchemy import select, update

from service.analysis_worker import wake_waiters
from service.market_data import DataError
from service.models import DataManifest, DependencyRequest, DependencyWaiter, Job, new_id
from service.quant.configuration import QuantConfig


def run_dependency_once(sessions) -> int:
    now, token = int(time.time()), new_id()
    with sessions() as db, db.begin():
        request = db.scalar(select(DependencyRequest).where(
            DependencyRequest.status.in_(["PENDING", "WAITING"]), DependencyRequest.next_attempt_at <= now)
            .order_by(DependencyRequest.next_attempt_at).limit(1).with_for_update(skip_locked=True))
        if not request:
            return 0
        request.status, request.execution_token, request.lease_until, request.dirty = "RUNNING", token, now + 86400, False
        key, payload = request.key, request.payload
    try:
        from datetime import date
        target = date.fromisoformat(payload["target_session"])
        with sessions() as db:
            manifest = db.scalar(select(DataManifest).where(DataManifest.target_session == target,
                DataManifest.input_mode == "COMPLETED_DAILY").order_by(DataManifest.revision_order.desc()).limit(1))
            if not manifest:
                raise DataError("DATA_NOT_READY", True)
            manifest_id = manifest.id
        from service.snapshots import build_market_snapshot
        build_market_snapshot(sessions, manifest_id, QuantConfig(**payload["config"]))
        status, reason, delay = "READY", None, 0
    except DataError as exc:
        status, reason, delay = ("WAITING" if exc.retryable else "FAILED"), exc.code, 300
    except Exception:
        status, reason, delay = "WAITING", "DEPENDENCY_COMPUTE_ERROR", 300
    with sessions() as db, db.begin():
        request = db.scalar(select(DependencyRequest).where(DependencyRequest.key == key,
            DependencyRequest.execution_token == token).with_for_update())
        if request:
            request.status = "PENDING" if request.dirty else status
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
