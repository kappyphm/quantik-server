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
        for index in range(3):
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
        assert current_bucket(db, account.id, now).daily_created == 3
        assert current_bucket(db, account.id, now).quota_total == 5
    engine.dispose()
