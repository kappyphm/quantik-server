from entitlement_fixtures import paid_bucket, fail_job
import time
from datetime import date, datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from service import jobs
from service.db import make_engine, session_factory
from service.models import Account, AnalysisReport, Job, Outbox, QuotaBucket, Reservation, UsageLedger, Subscription
from service.reports import SECTION_NAMES


def test_job_admission_fencing_and_settlement(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "jobs.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    factory = session_factory(engine)
    now = int(time.time())
    with factory() as db, db.begin():
        owner, other = Account(google_sub="owner", role="user"), Account(google_sub="other", role="user")
        db.add_all([owner, other])
        db.flush()
        owner_id, other_id = owner.id, other.id
        bucket = paid_bucket(db, owner_id, valid_from=now-10, valid_until=now+1000, quota_total=10, active_limit=2)
        db.add(bucket)
        db.flush()
        bucket_id = bucket.id

    def run(fn, *args, **kwargs):
        with factory() as db, db.begin():
            return fn(db, *args, **kwargs)

    def admit(key):
        return run(jobs.admit, owner_id, "FPT", date(2026, 10, 14), key)

    first = admit("failure-before-compute")
    assert admit("failure-before-compute").id == first.id
    with pytest.raises(jobs.JobError) as error:
        run(jobs.admit, owner_id, "VNM", date(2026, 10, 14), "failure-before-compute")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(jobs.JobError) as error:
        run(jobs.owned_job, other_id, first.id)
    assert error.value.status == 404
    second = admit("failure-after-compute")
    with pytest.raises(jobs.JobError) as error:
        admit("overflow")
    assert error.value.code == "ACTIVE_JOB_LIMIT"
    claim, token = run(jobs.claim, first.id)
    assert run(jobs.claim, first.id) is None
    run(jobs.wait_data, first.id, token, "SNAPSHOT_NOT_READY")
    run(fail_job, first.id)
    assert not run(jobs.wake_data, first.id)
    run(fail_job, first.id)
    with pytest.raises(jobs.JobError):
        run(jobs.begin_compute, first.id, token, {})
    versions = {"data_manifest": "fixture-manifest", "market_snapshot": "fixture-snapshot",
        "model": "fixture-model", "config": "fixture-config", "engines": {"version": "test"}}
    _, token = run(jobs.claim, second.id)
    run(jobs.begin_compute, second.id, token, versions)
    run(jobs.failed_attempt, second.id, token, "PROVIDER_ERROR", False)
    with pytest.raises(jobs.JobError):
        run(jobs.complete, second.id, token, {})
    third = admit("success")
    _, old_token = run(jobs.claim, third.id)
    run(jobs.begin_compute, third.id, old_token, versions)
    # Simulate an expired worker lease, without sleeping.
    with factory() as db, db.begin():
        db.get(Job, third.id).lease_until = now - 1
    assert run(jobs.reconcile) == 1
    _, token = run(jobs.claim, third.id)
    assert old_token != token
    with pytest.raises(jobs.JobError):
        run(jobs.complete, third.id, old_token, {})
    with pytest.raises(jobs.JobError) as error:
        run(jobs.begin_compute, third.id, token, versions | {"model": "changed"})
    assert error.value.code == "PINNED_VERSION_CONFLICT"
    run(jobs.begin_compute, third.id, token, versions)
    stamp = datetime.now(timezone.utc).isoformat()
    # Explicit report fixture verifies persistence/settlement, not scientific accuracy.
    report = {"schema_version": "1.0", "job_id": third.id, "symbol": "FPT", "evaluated_at": stamp,
        "data_as_of": stamp, "input_mode": "COMPLETED_DAILY", "versions": versions,
        "sections": {name: {"status": "AVAILABLE", "metrics": {"fixture": True}} for name in SECTION_NAMES},
        "decision": {"action": "WATCH"}, "commentary": {"fixture": "test only"}}
    with pytest.raises(jobs.JobError) as error:
        run(jobs.complete, third.id, token, report | {"sections": {}})
    assert error.value.code == "INVALID_REPORT"
    record = run(jobs.complete, third.id, token, report)
    assert record.account_id == owner_id
    assert run(jobs.owned_job, owner_id, third.id).status == "SUCCEEDED"
    fourth = admit("failure-old-bucket")
    _, token = run(jobs.claim, fourth.id)
    with factory() as db, db.begin():
        db.get(QuotaBucket, bucket_id).valid_until = now - 1
        db.scalar(select(Subscription).where(Subscription.bucket_id == bucket_id)).valid_until = now-1
        paid_bucket(db, owner_id, valid_from=now, valid_until=now+2000, quota_total=5, active_limit=2)
    run(jobs.failed_attempt, fourth.id, token, "PROVIDER_ERROR", False)
    repair = admit("terminal-repair")
    with factory() as db, db.begin():
        job = db.get(Job, repair.id)
        job.status, job.reason = "FAILED", "LEGACY_FAILURE"
    assert run(jobs.reconcile) == 1
    assert run(jobs.reconcile) == 0
    with factory() as db:
        old = db.get(QuotaBucket, bucket_id)
        assert (old.reserved, old.consumed, old.active_jobs) == (0, 1, 0)
        new = db.scalar(select(QuotaBucket).where(QuotaBucket.id != bucket_id))
        assert (new.reserved, new.consumed, new.active_jobs) == (0, 0, 0)
        effects = list(db.scalars(select(Reservation.state)))
        assert sorted(effects) == ["CONSUMED", "RELEASED", "RELEASED", "RELEASED", "RELEASED"]
        assert db.scalar(select(func.count()).select_from(UsageLedger)) == 10
        assert db.scalar(select(func.count()).select_from(AnalysisReport)) == 1
        assert db.scalar(select(func.count()).select_from(Outbox)) == 6
    engine.dispose()
