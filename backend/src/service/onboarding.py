"""Once-only signup entitlement and customer supplied profile."""
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from service.billing import audit
from service.models import Account, PlanVersion, QuotaBucket, Subscription

FREE_PLAN_ID = "04dfe4bc-755c-4dc2-9891-b3c0e13c8001"


def grant_signup(db, account: Account, now: int | None = None) -> Subscription:
    now = int(time.time()) if now is None else now
    db.scalar(select(Account).where(Account.id == account.id).with_for_update())
    key = "signup:" + account.id
    previous = db.scalar(select(Subscription).where(Subscription.grant_key == key))
    if previous:
        return previous
    plan = db.get(PlanVersion, FREE_PLAN_ID)
    if not plan:
        raise RuntimeError("Signup plan migration is missing")
    from service.daily_limits import day_bounds
    bucket = QuotaBucket(account_id=account.id, valid_from=now,
        valid_until=min(now+plan.duration_days*86400, day_bounds(now)[1]), quota_total=plan.analysis_quota,
        active_limit=plan.active_limit, entitlement_version=plan.id)
    db.add(bucket)
    db.flush()
    period = Subscription(account_id=account.id, plan_id=plan.id, bucket_id=bucket.id,
        grant_key=key, valid_from=now, valid_until=now+plan.duration_days*86400)
    db.add(period)
    db.flush()
    bucket.subscription_id = period.id
    audit(db, "user.signup_free_granted", {"subscription_id": period.id, "plan_id": plan.id}, account.id)
    return period


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
        "phone_number": account.phone_number, "profile_required": account.profile_required}
