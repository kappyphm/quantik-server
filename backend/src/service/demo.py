"""Shared demo account for exhibition booths.

The demo account is a normal customer account flagged ``is_demo``. It is
never reachable through Google signup: booth machines obtain a session
only by exchanging an admin-minted demo code, and an admin can revoke
codes or reset the demo account at any time.

Limits are bypassed for the demo account in the admission and screening
checks (see ``jobs.admit`` and ``screening_access``). The demo plan below
still exists so quota buckets and subscriptions have a real backing
record; its quota is deliberately far above any booth-day usage, and the
explicit bypasses mean a demo session is never blocked by a quota,
active-job or subscription check. Demo accounts may not create paid
orders (see ``billing.create_order``).
"""

import hashlib
import secrets
import time

from sqlalchemy import delete, select, update

from service.daily_limits import day_bounds
from service.models import Account, DemoCode, PlanVersion, QuotaBucket, Subscription

DEMO_USERNAME = "demo"
DEMO_PLAN_ID = "05dfe4bc-755c-4dc2-9891-b3c0e13c8003"
DEMO_PLAN_QUOTA = 999_999
DEMO_PLAN_ACTIVE_LIMIT = 100
# Subscription timestamps are Postgres INTEGER (32-bit, max 2147483647),
# so a 100-year subscription overflows. 10 years from any date in the
# 2020s stays safely inside the range, and the explicit demo bypasses
# mean the account is unlimited regardless of the subscription end.
DEMO_PLAN_YEARS = 10
INT32_MAX = 2_147_483_647


def is_demo_account(account) -> bool:
    return bool(account) and bool(getattr(account, "is_demo", False))


def ensure_demo_account(db) -> Account:
    """Create (or return) the shared demo account with a demo plan."""
    account = db.scalar(select(Account).where(Account.username == DEMO_USERNAME))
    if account is None:
        account = Account(username=DEMO_USERNAME, display_name="Tài khoản demo",
                          role="user", profile_required=False, is_demo=True, enabled=True)
        db.add(account)
        db.flush()
    elif not account.is_demo or account.role != "user":
        account.is_demo = True
        account.role = "user"
        account.profile_required = False
        db.flush()
    plan = db.get(PlanVersion, DEMO_PLAN_ID)
    if plan is None:
        plan = PlanVersion(id=DEMO_PLAN_ID, name="Demo", price_vnd=0,
                           duration_days=DEMO_PLAN_YEARS * 365, analysis_quota=DEMO_PLAN_QUOTA,
                           active_limit=DEMO_PLAN_ACTIVE_LIMIT,
                           features={"screening": True, "analysis": True},
                           available=False, created_at=int(time.time()))
        db.add(plan)
        db.flush()
    now = int(time.time())
    existing = db.scalar(select(Subscription).where(Subscription.account_id == account.id,
                                                    Subscription.grant_key == "demo:" + account.id))
    if existing is None or existing.valid_until <= now:
        start, end = day_bounds(now)
        bucket = QuotaBucket(account_id=account.id, valid_from=start, valid_until=end,
                             quota_total=plan.analysis_quota, active_limit=plan.active_limit,
                             entitlement_version=plan.id, daily_created=0)
        db.add(bucket)
        db.flush()
        period = Subscription(account_id=account.id, plan_id=plan.id, bucket_id=bucket.id,
                              grant_key="demo:" + account.id, valid_from=now,
                              valid_until=min(now + DEMO_PLAN_YEARS * 365 * 86400,
                                              INT32_MAX - 86400))
        db.add(period)
        db.flush()
        bucket.subscription_id = period.id
    return account


def create_code(db, actor_id: str, expires_in_days: int | None = None) -> tuple[DemoCode, str]:
    """Mint a reusable booth code. The plaintext code is returned once."""
    ensure_demo_account(db)
    code = "demo-" + secrets.token_urlsafe(24)
    now = int(time.time())
    row = DemoCode(code_hash=hashlib.sha256(code.encode()).hexdigest(), created_by=actor_id,
                   created_at=now,
                   expires_at=now + expires_in_days * 86400 if expires_in_days else None)
    db.add(row)
    db.flush()
    return row, code


def code_view(row: DemoCode) -> dict:
    return {"id": row.id, "created_by": row.created_by, "created_at": row.created_at,
            "expires_at": row.expires_at, "revoked": row.revoked,
            "used_count": row.used_count, "last_used_at": row.last_used_at}


def find_code(db, code: str) -> DemoCode | None:
    return db.scalar(select(DemoCode).where(
        DemoCode.code_hash == hashlib.sha256(code.encode()).hexdigest()))


def reset_demo(db) -> dict:
    """Revoke demo sessions and clear demo jobs/reports for the next booth.

    Non-terminal jobs are cancelled and their reservations released first;
    terminal jobs and their reports are then deleted. Paid orders are never
    touched (demo accounts cannot create orders in the first place), and
    demo codes themselves are left alone so a revoked code stays revoked.
    """
    from service import jobs as job_service
    from service.models import (AnalysisReport, Inbox, Job, JobAttempt, Outbox,
                                RefreshSession, Reservation, UsageLedger)
    account = ensure_demo_account(db)
    account.display_name = "Tài khoản demo"
    account.profile_required = False
    sessions_revoked = db.execute(
        update(RefreshSession)
        .where(RefreshSession.account_id == account.id, RefreshSession.revoked.is_(False))
        .values(revoked=True)).rowcount or 0
    jobs_deleted = 0
    for job in list(db.scalars(select(Job).where(Job.account_id == account.id))):
        if job.status not in job_service.TERMINAL:
            try:
                job_service.settle(db, job, consume=False, reason="DEMO_RESET")
            except Exception:
                pass
            job.status = "CANCELLED"
            db.flush()
        reservation = db.scalar(select(Reservation).where(Reservation.job_id == job.id))
        if reservation is not None:
            db.execute(delete(UsageLedger).where(
                UsageLedger.reservation_id == reservation.id))
            db.delete(reservation)
        db.execute(delete(Inbox).where(Inbox.job_id == job.id))
        db.execute(delete(Outbox).where(Outbox.job_id == job.id))
        db.execute(delete(JobAttempt).where(JobAttempt.job_id == job.id))
        db.execute(delete(AnalysisReport).where(AnalysisReport.job_id == job.id))
        db.delete(job)
        jobs_deleted += 1
    return {"account_id": account.id, "sessions_revoked": sessions_revoked,
            "jobs_deleted": jobs_deleted}
