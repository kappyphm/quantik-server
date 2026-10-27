"""Once-only signup entitlement and customer supplied profile."""
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from service.billing import audit
from service.models import Account, AuditEvent, PlanVersion, QuotaBucket, Subscription

FREE_PLAN_ID = "04dfe4bc-755c-4dc2-9891-b3c0e13c8002"


def ensure_signup_plan(db) -> PlanVersion:
    """New immutable version; prior Free terms remain in historical records."""
    from service.operational_config import insert_for
    db.execute(insert_for(db)(PlanVersion).values(id=FREE_PLAN_ID, name="Free", price_vnd=0,
        duration_days=30, analysis_quota=5, active_limit=1,
        features={"screening": True, "analysis": True}, available=False, created_at=int(time.time()))
        .on_conflict_do_nothing(index_elements=["id"]))
    return db.get(PlanVersion, FREE_PLAN_ID)


def grant_signup(db, account: Account, now: int | None = None, *, grant_key: str | None = None) -> Subscription:
    now = int(time.time()) if now is None else now
    db.scalar(select(Account).where(Account.id == account.id).with_for_update())
    key = grant_key or "signup:" + account.id
    previous = db.scalar(select(Subscription).where(Subscription.grant_key == key))
    if previous:
        return previous
    plan = ensure_signup_plan(db)
    paid_end = db.scalar(select(func.max(Subscription.valid_until)).where(
        Subscription.account_id == account.id, Subscription.grant_key.is_(None), Subscription.valid_until > now))
    start = max(now, paid_end or now)
    for previous_free in db.scalars(select(Subscription).where(Subscription.account_id == account.id,
            Subscription.grant_key.is_not(None), Subscription.valid_until > now)):
        previous_free.valid_until = max(previous_free.valid_from, now)
    from service.daily_limits import day_bounds, created_today
    bucket = QuotaBucket(account_id=account.id, valid_from=start,
        valid_until=min(start+plan.duration_days*86400, day_bounds(start)[1]), quota_total=plan.analysis_quota,
        active_limit=plan.active_limit, entitlement_version=plan.id,
        daily_created=created_today(db, account.id, now) if start == now else 0)
    db.add(bucket)
    db.flush()
    period = Subscription(account_id=account.id, plan_id=plan.id, bucket_id=bucket.id,
        grant_key=key, valid_from=start, valid_until=start+plan.duration_days*86400)
    db.add(period)
    db.flush()
    bucket.subscription_id = period.id
    audit(db, "user.signup_free_granted", {"subscription_id": period.id, "plan_id": plan.id}, account.id)
    return period


def grant_all_customers(db, now: int | None = None) -> dict:
    """Once-only Free offer for every customer, explicitly requested by owner."""
    ensure_signup_plan(db)
    db.scalar(select(PlanVersion).where(PlanVersion.id == FREE_PLAN_ID).with_for_update())
    if db.scalar(select(AuditEvent.id).where(AuditEvent.action == "operations.free5_offer_completed").limit(1)):
        count = db.scalar(select(func.count()).select_from(Subscription)
            .where(Subscription.grant_key.like("free5-launch:%")))
        return {"created": 0, "existing": count, "daily_limit": 5, "duration_days": 30}
    created = existing = 0
    for account in db.scalars(select(Account).where(Account.role == "user").order_by(Account.id)):
        key = "free5-launch:" + account.id
        previous = db.scalar(select(Subscription.id).where(Subscription.grant_key == key))
        grant_signup(db, account, now, grant_key=key)
        existing += int(previous is not None)
        created += int(previous is None)
    result = {"created": created, "existing": existing, "daily_limit": 5, "duration_days": 30}
    audit(db, "operations.free5_offer_completed", result)
    return result


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=255)
    date_of_birth: date
    phone_number: str = Field(min_length=8, max_length=30)

    @field_validator("display_name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("Invalid name")
        return value

    @field_validator("date_of_birth")
    @classmethod
    def valid_birth(cls, value):
        if not date(today().year-120, 1, 1) <= value <= today():
            raise ValueError("Invalid birth date")
        return value

    @field_validator("phone_number")
    @classmethod
    def valid_phone(cls, value):
        value = re.sub(r"[\s().-]", "", value)
        if re.fullmatch(r"0[35789]\d{8}", value):
            value = "+84" + value[1:]
        if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError("Use a valid international or Vietnamese mobile number")
        return value


def profile_view(account: Account) -> dict:
    birth = account.date_of_birth
    current = today()
    age = current.year-birth.year-((current.month, current.day) < (birth.month, birth.day)) if birth else None
    return {"id": account.id, "role": account.role, "display_name": account.display_name,
        "email": account.email, "date_of_birth": birth, "age": age,
        "phone_number": account.phone_number, "profile_required": account.profile_required,
        "is_demo": bool(getattr(account, "is_demo", False))}
