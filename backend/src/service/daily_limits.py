"""Admission budgets reset at midnight in Vietnam; subscription duration is separate."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from service.models import Account, Job, PlanVersion, QuotaBucket, Subscription

ZONE = ZoneInfo("Asia/Ho_Chi_Minh")


def day_bounds(now: int) -> tuple[int, int]:
    start = datetime.fromtimestamp(now, ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def created_today(db: Session, account_id: str, now: int) -> int:
    start, end = day_bounds(now)
    return db.scalar(select(func.count()).select_from(Job).where(Job.account_id == account_id,
        Job.created_at >= start, Job.created_at < end)) or 0


def current_bucket(db: Session, account_id: str, now: int) -> QuotaBucket | None:
    db.scalar(select(Account).where(Account.id == account_id).with_for_update())
    period = db.scalar(select(Subscription).where(Subscription.account_id == account_id,
        Subscription.valid_from <= now, Subscription.valid_until > now)
        .order_by(Subscription.valid_from.desc(), Subscription.id).limit(1))
    if not period:
        return None
    start, end = day_bounds(now)
    start, end = max(start, period.valid_from), min(end, period.valid_until)
    bucket = db.scalar(select(QuotaBucket).where(QuotaBucket.subscription_id == period.id,
        QuotaBucket.valid_from == start).with_for_update())
    used = created_today(db, account_id, now)
    if not bucket:
        plan = db.get(PlanVersion, period.plan_id)
        bucket = QuotaBucket(account_id=account_id, subscription_id=period.id,
            valid_from=start, valid_until=end, quota_total=plan.analysis_quota,
            active_limit=plan.active_limit, entitlement_version=plan.id, daily_created=used)
        db.add(bucket)
        db.flush()
    else:
        # A period may start mid-day; switching periods cannot reset today's admissions.
        bucket.daily_created = max(bucket.daily_created, used)
    return bucket
