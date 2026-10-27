"""Reading shared screenings is a subscription feature, never a job admission."""
import time
from sqlalchemy import select
from service.models import PlanVersion, Subscription
from service.jobs import JobError


def require_access(db, account_id: str) -> None:
    from service.models import Account
    account = db.get(Account, account_id)
    if account is not None and bool(getattr(account, "is_demo", False)):
        return
    now = int(time.time())
    plan = db.scalar(select(PlanVersion).join(Subscription, Subscription.plan_id == PlanVersion.id)
        .where(Subscription.account_id == account_id, Subscription.valid_from <= now,
            Subscription.valid_until > now).order_by(Subscription.valid_from.desc()).limit(1))
    if not plan or not plan.features.get("screening", False):
        raise JobError("SCREENING_SUBSCRIPTION_REQUIRED", 403)
