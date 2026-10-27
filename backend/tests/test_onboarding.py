"""Signup entitlement never fabricates banking records or resets daily admissions."""
import time
from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from entitlement_fixtures import paid_bucket
from service import jobs
from service.db import make_engine, session_factory
from service.daily_limits import current_bucket, day_bounds
from service.models import Account, BankPayment, PurchaseOrder, Subscription
from service.onboarding import grant_signup


def test_signup_daily_limits_expiry_and_paid_upgrade(tmp_path, monkeypatch):
    url = "sqlite:///"+(tmp_path/"signup.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    now = int(time.time())
    with session_factory(engine)() as db, db.begin():
        account = Account(role="user", google_sub="signup-test")
        db.add(account); db.flush()
        free = grant_signup(db, account, now-10)
        assert grant_signup(db, account).id == free.id
        assert free.valid_until-free.valid_from == 30*86400
        assert db.scalar(select(func.count()).select_from(PurchaseOrder)) == 0
        assert db.scalar(select(func.count()).select_from(BankPayment)) == 0
        for index in range(5):
            job = jobs.admit(db, account.id, "FPT", date(2026, 10, 14), str(index))
            db.flush()
            claimed, token = jobs.claim(db, job.id)
            jobs.failed_attempt(db, job.id, token, "TEST_TERMINAL", False)
        with pytest.raises(jobs.JobError, match="") as denied:
            jobs.admit(db, account.id, "FPT", date(2026, 10, 14), "fourth")
        assert denied.value.code == "DAILY_REQUEST_LIMIT"
        assert current_bucket(db, account.id, day_bounds(now)[1]+1).daily_created == 0
        assert current_bucket(db, account.id, free.valid_until) is None
        paid = paid_bucket(db, account.id, quota_total=5, valid_from=now, valid_until=now+86400)
        period = db.get(Subscription, paid.subscription_id)
        assert period.valid_from == now and free.valid_until == now
        assert current_bucket(db, account.id, now).daily_created == 5
        assert current_bucket(db, account.id, now).quota_total == 5
    engine.dispose()


def test_all_customer_free_offer_is_once_only_and_preserves_usage_and_paid_terms(tmp_path, monkeypatch):
    from service.onboarding import FREE_PLAN_ID, grant_all_customers
    from service.models import PlanVersion
    url = "sqlite:///"+(tmp_path/"free-offer.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url); now = int(time.time())
    with session_factory(engine)() as db, db.begin():
        customer = Account(role="user", google_sub="existing")
        paid = Account(role="user", google_sub="paid")
        disabled = Account(role="user", google_sub="disabled", enabled=False)
        admin = Account(role="admin", username="staff")
        db.add_all([customer, paid, disabled, admin]); db.flush()
        old = grant_signup(db, customer, now-100)
        job = jobs.admit(db, customer.id, "FPT", date(2026, 10, 16), "before-offer")
        _, token = jobs.claim(db, job.id)
        jobs.failed_attempt(db, job.id, token, "TEST_TERMINAL", False)
        paid_bucket(db, paid.id, quota_total=9, valid_from=now-10, valid_until=now+1000)
        assert grant_all_customers(db, now) == {"created": 3, "existing": 0, "daily_limit": 5, "duration_days": 30}
        bucket = current_bucket(db, customer.id, now)
        period = db.get(Subscription, bucket.subscription_id)
        assert bucket.quota_total == 5 and bucket.daily_created == 1
        assert period.valid_until-period.valid_from == 30*86400 and old.valid_until == now
        assert current_bucket(db, paid.id, now).quota_total == 9
        gift = db.scalar(select(Subscription).where(Subscription.account_id == paid.id,
            Subscription.grant_key.like("free5-launch:%")))
        assert gift.valid_from == now+1000 and gift.valid_until-gift.valid_from == 30*86400
        newcomer = Account(role="user", google_sub="after-rollout")
        db.add(newcomer); db.flush()
        signup = grant_signup(db, newcomer, now+10)
        assert grant_all_customers(db, now+100)["created"] == 0
        assert db.scalar(select(func.count()).select_from(Subscription)
            .where(Subscription.account_id == newcomer.id)) == 1
        assert signup.valid_until == now+10+30*86400
        assert period.valid_until == now+30*86400
        assert db.scalar(select(func.count()).select_from(Subscription).where(Subscription.account_id == admin.id)) == 0
        assert db.get(PlanVersion, FREE_PLAN_ID).analysis_quota == 5
        assert db.get(PlanVersion, "04dfe4bc-755c-4dc2-9891-b3c0e13c8001").analysis_quota == 3
        assert db.scalar(select(func.count()).select_from(PurchaseOrder)) == 1  # Paid fixture only.
        assert db.scalar(select(func.count()).select_from(BankPayment)) == 1
    engine.dispose()
