"""Daily screening quota: Free plan meters 5 views/day, Pro 20, demo unmetered."""

import time

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from service.db import make_engine, session_factory
from service.jobs import JobError
from service.models import Account, PlanVersion, QuotaBucket, ScreeningUsage, Subscription
from service.screening_access import consume_view, quota_status, require_access


def _db(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "quota.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    command.upgrade(Config("backend/alembic.ini"), "head")
    return session_factory(make_engine(url))


def _account_with_plan(db, name: str, screening_quota: int) -> Account:
    now = int(time.time())
    plan = PlanVersion(name=name, price_vnd=0, duration_days=30, analysis_quota=5,
        screening_quota=screening_quota, active_limit=1,
        features={"screening": True, "analysis": True}, available=False, created_at=now)
    account = Account(username=f"u-{name}", password_hash="x", role="user")
    db.add_all([plan, account])
    db.flush()
    bucket = QuotaBucket(account_id=account.id, valid_from=now - 60, valid_until=now + 86400,
        quota_total=plan.analysis_quota, active_limit=plan.active_limit,
        entitlement_version=plan.id)
    db.add(bucket)
    db.flush()
    db.add(Subscription(account_id=account.id, plan_id=plan.id, bucket_id=bucket.id,
        grant_key=f"test:{account.id}", valid_from=now - 60, valid_until=now + 86400))
    db.flush()
    return account


def test_free_plan_allows_five_views_then_blocks(tmp_path, monkeypatch):
    factory = _db(tmp_path, monkeypatch)
    with factory() as db, db.begin():
        account = _account_with_plan(db, "Free", 5)
        for _ in range(5):
            consume_view(db, account.id)
        assert quota_status(db, account.id) == {"limit": 5, "used": 5, "remaining": 0}
        with pytest.raises(JobError) as caught:
            consume_view(db, account.id)
        assert caught.value.code == "SCREENING_QUOTA_EXCEEDED"
        assert db.scalar(select(ScreeningUsage.used).where(
            ScreeningUsage.account_id == account.id)) == 5


def test_pro_plan_allows_twenty_views(tmp_path, monkeypatch):
    factory = _db(tmp_path, monkeypatch)
    with factory() as db, db.begin():
        account = _account_with_plan(db, "QuanTik Pro", 20)
        for _ in range(20):
            consume_view(db, account.id)
        assert quota_status(db, account.id)["remaining"] == 0
        with pytest.raises(JobError):
            consume_view(db, account.id)


def test_no_subscription_still_requires_subscription(tmp_path, monkeypatch):
    factory = _db(tmp_path, monkeypatch)
    with factory() as db, db.begin():
        account = Account(username="bare", password_hash="x", role="user")
        db.add(account)
        db.flush()
        with pytest.raises(JobError) as caught:
            consume_view(db, account.id)
        assert caught.value.code == "SCREENING_SUBSCRIPTION_REQUIRED"
        with pytest.raises(JobError):
            require_access(db, account.id)
        assert quota_status(db, account.id) is None
