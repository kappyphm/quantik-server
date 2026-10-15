"""Explicit audited customer controls and compensating quota refunds."""
import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from service.billing import audit
from service.jobs import JobError, TERMINAL
from service.models import Account, Job, QuotaBucket, QuotaRefund, RefreshSession, Reservation, UsageLedger


def set_enabled(db: Session, account_id: str, enabled: bool, reason: str, actor_id: str) -> Account:
    if not 10 <= len(reason.strip()) <= 500:
        raise JobError("ADMIN_REASON_REQUIRED", 422)
    account = db.scalar(select(Account).where(Account.id == account_id, Account.role == "user")
        .with_for_update().execution_options(populate_existing=True))
    if not account:
        raise JobError("NOT_FOUND", 404)
    before = account.enabled
    account.enabled = enabled
    if not enabled:
        db.execute(update(RefreshSession).where(RefreshSession.account_id == account_id).values(revoked=True))
    if before != enabled:
        audit(db, "admin.customer_state", {"account_id": account_id, "before": before, "after": enabled,
            "reason": reason}, actor_id)
    return account


def refund_quota(db: Session, job_id: str, actor_id: str, key: str, reason: str, evidence: str) -> QuotaRefund:
    if not 1 <= len(key) <= 128 or not 10 <= len(reason.strip()) <= 500 or not 10 <= len(evidence.strip()) <= 1000:
        raise JobError("INVALID_REFUND_REQUEST", 422)
    # Serialize keys per admin; this account is distinct from the customer's worker lock.
    actor = db.scalar(select(Account).where(Account.id == actor_id, Account.role == "admin")
        .with_for_update().execution_options(populate_existing=True))
    if not actor or not actor.enabled:
        raise JobError("FORBIDDEN", 403)
    previous = db.scalar(select(QuotaRefund).where(QuotaRefund.actor_id == actor_id, QuotaRefund.idempotency_key == key))
    if previous:
        reservation = db.get(Reservation, previous.reservation_id)
        if reservation.job_id != job_id or previous.reason != reason or previous.evidence != evidence:
            raise JobError("IDEMPOTENCY_CONFLICT", 409)
        return previous
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if not job:
        raise JobError("NOT_FOUND", 404)
    reservation = db.scalar(select(Reservation).where(Reservation.job_id == job_id).with_for_update())
    if job.status not in TERMINAL or not reservation or reservation.state != "CONSUMED":
        raise JobError("JOB_NOT_REFUNDABLE", 409)
    if db.scalar(select(QuotaRefund.id).where(QuotaRefund.reservation_id == reservation.id)):
        raise JobError("QUOTA_ALREADY_REFUNDED", 409)
    changed = db.execute(update(QuotaBucket).where(QuotaBucket.id == reservation.bucket_id, QuotaBucket.consumed >= 1)
        .values(consumed=QuotaBucket.consumed-1))
    if changed.rowcount != 1:
        raise JobError("QUOTA_LEDGER_INCONSISTENT", 409)
    refund = QuotaRefund(actor_id=actor_id, reservation_id=reservation.id, idempotency_key=key,
        reason=reason, evidence=evidence, created_at=int(time.time()))
    db.add(refund)
    db.add(UsageLedger(reservation_id=reservation.id, effect="REFUND", occurred_at=refund.created_at, reason="ADMIN_REFUND"))
    db.flush()
    audit(db, "admin.quota_refund", {"refund_id": refund.id, "job_id": job.id, "bucket_id": reservation.bucket_id,
        "units": 1, "reason": reason, "evidence": evidence}, actor_id)
    return refund
