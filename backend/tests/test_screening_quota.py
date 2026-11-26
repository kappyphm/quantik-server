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
        account_id = _account_with_plan(db, "Free", 5).id
    with factory() as db:
        for _ in range(5):
            consume_view(db, account_id)
        assert quota_status(db, account_id) == {"limit": 5, "used": 5, "remaining": 0}
        with pytest.raises(JobError) as caught:
            consume_view(db, account_id)
        assert caught.value.code == "SCREENING_QUOTA_EXCEEDED"
        assert db.scalar(select(ScreeningUsage.used).where(
            ScreeningUsage.account_id == account_id)) == 5


def test_pro_plan_allows_twenty_views(tmp_path, monkeypatch):
    factory = _db(tmp_path, monkeypatch)
    with factory() as db, db.begin():
        account_id = _account_with_plan(db, "QuanTik Pro", 20).id
    with factory() as db:
        for _ in range(20):
            consume_view(db, account_id)
        assert quota_status(db, account_id)["remaining"] == 0
        with pytest.raises(JobError):
            consume_view(db, account_id)


def test_views_persist_across_request_sessions(tmp_path, monkeypatch):
    """Each request gets its own session; the charge must survive its close."""
    factory = _db(tmp_path, monkeypatch)
    with factory() as db, db.begin():
        account = _account_with_plan(db, "Free", 5)
        account_id = account.id
    for _ in range(5):
        with factory() as db:
            consume_view(db, account_id)
    with factory() as db:
        assert quota_status(db, account_id) == {"limit": 5, "used": 5, "remaining": 0}
        with pytest.raises(JobError) as caught:
            consume_view(db, account_id)
        assert caught.value.code == "SCREENING_QUOTA_EXCEEDED"


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


def test_board_quant_matches_account_manifest_and_symbol(tmp_path, monkeypatch):
    from datetime import date
    from types import SimpleNamespace
    from service.models import AnalysisReport, Job
    from service.screening_views import quant_columns

    factory = _db(tmp_path, monkeypatch)
    day = date(2026, 10, 16)
    run = SimpleNamespace(manifest_id="published", summary={"input_mode": "COMPLETED_DAILY"}, target_session=day)
    with factory() as db, db.begin():
        viewer = _account_with_plan(db, "Free", 5)
        other = _account_with_plan(db, "Pro", 20)
        for seq, (account, revision, score) in enumerate([
            (viewer, "published", 12), (viewer, "published", 0), (viewer, "older", 98), (other, "published", 99)
        ]):
            job = Job(account_id=account.id, bucket_id=db.scalar(select(QuotaBucket.id).where(
                QuotaBucket.account_id == account.id)), idempotency_key=str(seq), payload_hash="0"*64,
                symbol="AAA", target_session=day, inputs={}, policy={}, status="SUCCEEDED", created_at=seq,
                ready_at=seq, deadline=seq+1, evaluated_at=seq)
            db.add(job)
            db.flush()
            db.add(AnalysisReport(job_id=job.id, account_id=account.id, payload={"versions": {"data_manifest": revision},
                "sections": {"decision": {"status": "AVAILABLE", "metrics": {"rec": {"score": score}}},
                    "hmm": {"status": "UNAVAILABLE", "metrics": {"hmm": {"current": "unused"}}}}}))
        db.flush()
        rows = quant_columns(db, run, viewer.id, {"AAA", "BBB"})
        assert set(rows) == {"AAA"}
        assert rows["AAA"]["score"] == 0
        assert rows["AAA"]["hmm_regime"] is None
        run.summary["input_mode"] = "PROVISIONAL_DAILY"
        assert quant_columns(db, run, viewer.id, {"AAA"}) == {}
