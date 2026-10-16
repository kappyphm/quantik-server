"""Paid entitlements and worker outcomes for isolated test databases only."""
from datetime import datetime, timezone
import time
from uuid import uuid4

from service import billing, jobs
from service.models import BankPayment, Job, PlanVersion, PurchaseOrder, QuotaBucket, Subscription
from service.reports import SECTION_NAMES


def paid_bucket(db, account_id, *, quota_total=20, active_limit=2, valid_from=None, valid_until=None):
    start = int(time.time())-10 if valid_from is None else valid_from
    end = start+3600 if valid_until is None else valid_until
    key = uuid4().hex
    plan = PlanVersion(name="Isolated entitlement fixture", price_vnd=10000, duration_days=30,
        analysis_quota=quota_total, active_limit=active_limit, features={"screening": True, "analysis": True}, created_at=start)
    db.add(plan); db.flush()
    order = PurchaseOrder(account_id=account_id, plan_id=plan.id, idempotency_key=key, reference="QTK"+key[:16].upper(),
        amount_vnd=10000, bank_code="TPBANK", bank_account="fixture", account_name="Fixture", transfer_content=key,
        created_at=start, expires_at=start+1800)
    db.add(order); db.flush()
    payment = BankPayment(id=key, fingerprint=key*2, order_id=order.id, bank_code=order.bank_code,
        bank_account=order.bank_account, direction="in", amount_vnd=10000, transaction_at=start, received_at=start,
        references=[order.reference], status="RECONCILIATION")
    db.add(payment); db.flush(); billing.grant(db, payment, order, start)
    assert payment.status == "APPLIED"
    from sqlalchemy import select
    period = db.scalar(select(Subscription).where(Subscription.payment_id == key))
    period.valid_until = end
    bucket = db.get(QuotaBucket, period.bucket_id)
    bucket.valid_until = min(bucket.valid_until, end)
    return bucket


def fail_job(db, job_id):
    job = db.get(Job, job_id)
    if job.status in jobs.TERMINAL:
        return job
    if job.status == "WAITING_DATA":
        jobs.fail_waiting(db, job_id, "FIXTURE_DATA_UNAVAILABLE")
    else:
        token = job.execution_token
        if job.status in {"QUEUED", "RETRY_WAIT"}:
            job, token = jobs.claim(db, job_id)
        jobs.failed_attempt(db, job_id, token, "FIXTURE_PROVIDER_ERROR", False)
    return job


def complete_job(db, job_id):
    job = db.get(Job, job_id)
    token = job.execution_token
    if job.status == "QUEUED":
        job, token = jobs.claim(db, job_id)
    versions = job.pinned_versions or {"data_manifest": "fixture", "market_snapshot": "fixture", "model": "fixture", "config": "fixture", "engines": {}}
    jobs.begin_compute(db, job_id, token, versions)
    stamp = datetime.now(timezone.utc).isoformat()
    return jobs.complete(db, job_id, token, {"schema_version": "1.0", "job_id": job.id,
        "symbol": job.symbol, "evaluated_at": stamp, "data_as_of": stamp, "input_mode": "COMPLETED_DAILY", "versions": versions,
        "sections": {name: {"status": "AVAILABLE", "metrics": {"fixture": True}} for name in SECTION_NAMES},
        "decision": {"action": "WATCH"}, "commentary": {"fixture": "isolated test"}})
