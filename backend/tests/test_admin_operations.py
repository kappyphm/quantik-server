import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from service import admin, jobs
from service.api import create_app
from service.db import make_engine, session_factory
from service.models import Account, AuditEvent, Job, QuotaBucket, QuotaRefund, Reservation, UsageLedger
from service.settings import Settings


def test_admin_suspension_and_compensating_refund(tmp_path, monkeypatch):
    url = "sqlite:///"+(tmp_path/"operations.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    sessions = session_factory(engine)
    private, public = tmp_path/"private.pem", tmp_path/"public.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test", jwt_private_key_path=private, jwt_public_key_path=public)
    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    versions = {"data_manifest": "fixture", "market_snapshot": "fixture", "model": "fixture", "config": "fixture", "engines": {}}
    with TestClient(create_app(settings)) as client:
        with sessions() as db, db.begin():
            owner, staff = Account(role="user", google_sub="customer"), Account(role="admin", username="operator")
            db.add_all([owner, staff])
            db.flush()
            owner_id, staff_id = owner.id, staff.id
            user_auth, admin_auth = [{"Authorization": "Bearer "+client.app.state.identity.new_session(db, a)[0]} for a in (owner, staff)]
            bucket = QuotaBucket(account_id=owner_id, valid_from=int(time.time())-10, valid_until=int(time.time())+1000,
                quota_total=5, active_limit=3, entitlement_version="fixture-period")
            db.add(bucket)
            db.flush()
            bucket_id = bucket.id
        consumed = []
        for index in range(2):
            with sessions() as db, db.begin():
                job = jobs.admit(db, owner_id, "FPT", today, f"consume-{index}")
                claimed, token = jobs.claim(db, job.id)
                jobs.begin_compute(db, job.id, token, versions)
                jobs.cancel(db, owner_id, job.id)
                consumed.append(job.id)
        body = {"reason": "Customer service correction", "evidence": "Verified original job execution record"}
        path = f"/api/v1/admin/jobs/{consumed[0]}/quota-refund"
        headers = admin_auth | {"Idempotency-Key": "refund-1"}
        assert client.post(path, json=body, headers=user_auth | {"Idempotency-Key": "unauthorized"}).status_code == 401
        refunded = client.post(path, json=body, headers=headers)
        assert refunded.status_code == 200, refunded.text
        assert client.post(path, json=body, headers=headers).json()["id"] == refunded.json()["id"]
        assert client.post(path, json=body | {"reason": "Different correction reason"}, headers=headers).status_code == 409
        assert client.post(path, json=body, headers=admin_auth | {"Idempotency-Key": "refund-again"}).status_code == 409
        assert client.post(f"/api/v1/admin/jobs/{consumed[1]}/quota-refund", json=body, headers=headers).status_code == 409
        with sessions() as db:
            assert db.get(QuotaBucket, bucket_id).consumed == 1
            reservation = db.scalar(select(Reservation).where(Reservation.job_id == consumed[0]))
            assert reservation.state == "CONSUMED"  # Do not rewrite original settlement.
            assert set(db.scalars(select(UsageLedger.effect).where(UsageLedger.reservation_id == reservation.id))) == {"RESERVE", "CONSUMED", "REFUND"}
        detail = client.get(f"/api/v1/analyses/{consumed[0]}", headers=user_auth).json()
        assert detail["quota_effect"] == "CONSUMED" and detail["quota_refund_id"] == refunded.json()["id"]

        # Expired bucket refunds do not manufacture available quota in a new period.
        with sessions() as db, db.begin():
            db.get(QuotaBucket, bucket_id).valid_until = int(time.time())-1
            current = QuotaBucket(account_id=owner_id, valid_from=int(time.time())-1, valid_until=int(time.time())+1000,
                quota_total=4, active_limit=3, entitlement_version="next-fixture-period")
            db.add(current)
            db.flush()
            current_id = current.id
        with pytest.raises(RuntimeError):
            with sessions() as db, db.begin():
                admin.refund_quota(db, consumed[1], staff_id, "rollback", body["reason"], body["evidence"])
                raise RuntimeError("failure before commit")
        with sessions() as db:
            assert db.get(QuotaBucket, bucket_id).consumed == 1
            assert db.scalar(select(func.count()).select_from(QuotaRefund)) == 1
        late_refund = client.post(f"/api/v1/admin/jobs/{consumed[1]}/quota-refund", json=body,
            headers=admin_auth | {"Idempotency-Key": "expired-refund"})
        assert late_refund.status_code == 200 and late_refund.json()["bucket_id"] == bucket_id
        assert client.get("/api/v1/me/entitlements", headers=user_auth).json()["remaining"] == 4

        with sessions() as db, db.begin():
            waiting = jobs.admit(db, owner_id, "FPT", today, "pending-disable")
            running = jobs.admit(db, owner_id, "VNM", today, "running-disable")
            _, token = jobs.claim(db, running.id)
            jobs.begin_compute(db, running.id, token, versions)
            pending_id, running_id = waiting.id, running.id
        suspension = {"enabled": False, "reason": "Security review requested by operator"}
        assert client.post(f"/api/v1/admin/users/{staff_id}/state", json=suspension, headers=admin_auth).status_code == 404
        assert client.post(f"/api/v1/admin/users/{owner_id}/state", json=suspension, headers=admin_auth).status_code == 200
        assert client.get("/api/v1/me", headers=user_auth).status_code == 401
        with sessions() as db, db.begin():
            assert jobs.reconcile(db) == 2
            assert jobs.reconcile(db) == 0
        with sessions() as db:
            for job_id in (pending_id, running_id):
                job = db.get(Job, job_id)
                assert job.status == "FAILED" and job.reason == "ACCOUNT_DISABLED" and job.execution_token is None
            bucket = db.get(QuotaBucket, current_id)
            assert (bucket.consumed, bucket.reserved, bucket.active_jobs) == (0, 0, 0)
        with sessions() as db, pytest.raises(jobs.JobError):
            jobs.live_attempt(db, running_id, token)
        assert client.post(f"/api/v1/admin/users/{owner_id}/state", json=suspension | {"enabled": True}, headers=admin_auth).status_code == 200
        assert client.get("/api/v1/me", headers=user_auth).status_code == 401  # Re-enable cannot revive revoked sessions.
        for route in ("users", "jobs", "payments", "subscriptions", "models", "data-health", "audit"):
            response = client.get("/api/v1/admin/"+route, headers=admin_auth)
            assert response.status_code == 200, (route, response.text)
        with sessions() as db:
            events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "admin.customer_state")))
            assert any(e.actor_id == staff_id and e.details["before"] is True and e.details["after"] is False for e in events)
    engine.dispose()
