"""Reading shared screenings is a subscription feature, never a job admission.

Each plan also carries a daily screening quota (Free 5, Pro 20): loading the
results board or one symbol's screening detail costs one view. The latest
pointer stays unmetered so clients can poll publication state and read the
remaining quota from its response.
"""
import time
from sqlalchemy import select
from service.models import Account, PlanVersion, ScreeningUsage, Subscription
from service.jobs import JobError


def _is_demo(db, account_id: str) -> bool:
    account = db.get(Account, account_id)
    return account is not None and bool(getattr(account, "is_demo", False))


def _active_plan(db, account_id: str) -> PlanVersion | None:
    now = int(time.time())
    return db.scalar(select(PlanVersion).join(Subscription, Subscription.plan_id == PlanVersion.id)
        .where(Subscription.account_id == account_id, Subscription.valid_from <= now,
            Subscription.valid_until > now).order_by(Subscription.valid_from.desc()).limit(1))


def _screening_plan(db, account_id: str) -> PlanVersion | None:
    plan = _active_plan(db, account_id)
    if not plan or not plan.features.get("screening", False):
        return None
    return plan


def require_access(db, account_id: str) -> None:
    if _is_demo(db, account_id):
        return
    if not _screening_plan(db, account_id):
        raise JobError("SCREENING_SUBSCRIPTION_REQUIRED", 403)


def _today_usage(db, account_id: str, *, lock: bool = False) -> ScreeningUsage | None:
    from service.daily_limits import day_bounds
    start, _ = day_bounds(int(time.time()))
    query = select(ScreeningUsage).where(ScreeningUsage.account_id == account_id,
        ScreeningUsage.day_start == start)
    if lock:
        query = query.with_for_update()
    return db.scalar(query)


def quota_status(db, account_id: str) -> dict | None:
    """Today's screening quota for the response payload; None when unmetered
    (demo) or when the account has no screening entitlement to report."""
    if _is_demo(db, account_id):
        return None
    plan = _screening_plan(db, account_id)
    if not plan:
        return None
    row = _today_usage(db, account_id)
    used = row.used if row else 0
    return {"limit": plan.screening_quota, "used": used,
        "remaining": max(plan.screening_quota - used, 0)}


def consume_view(db, account_id: str) -> None:
    """Charge one screening view against today's plan quota."""
    if _is_demo(db, account_id):
        return
    plan = _screening_plan(db, account_id)
    if not plan:
        raise JobError("SCREENING_SUBSCRIPTION_REQUIRED", 403)
    db.scalar(select(Account).where(Account.id == account_id).with_for_update())
    row = _today_usage(db, account_id, lock=True)
    if row is None:
        from service.daily_limits import day_bounds
        start, _ = day_bounds(int(time.time()))
        row = ScreeningUsage(account_id=account_id, day_start=start, used=0)
        db.add(row)
        db.flush()
    if row.used >= plan.screening_quota:
        raise JobError("SCREENING_QUOTA_EXCEEDED", 403)
    row.used += 1
    db.flush()
