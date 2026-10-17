"""Critical payment checks with real JWT roles and a temporary migrated DB; no bank calls."""
from entitlement_fixtures import fail_job
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

from service import billing, jobs
from service.api import create_app
from service.db import make_engine, session_factory
from service.models import Account, BankPayment, PlanVersion, PurchaseOrder, QuotaBucket, Subscription
from service.settings import Settings


def test_prepaid_payment_contract_and_atomic_entitlement(tmp_path, monkeypatch):
    url = "sqlite:///"+(tmp_path/"billing.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    sessions = session_factory(engine)
    private, public = tmp_path/"private.pem", tmp_path/"public.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test", jwt_private_key_path=private,
        jwt_public_key_path=public, payments_enabled=True, SEPAY_WEBHOOK_API_KEY="fixture-webhook-key",
        SEPAY_BANK_CODE="TPBank", SEPAY_ACCOUNT_NUMBER="00001111", SEPAY_ACCOUNT_NAME="Fixture Account")
    with TestClient(create_app(settings)) as client:
        with sessions() as db, db.begin():
            owner, other, admin = Account(google_sub="buyer", role="user", display_name="Verified Buyer", email="buyer@example.test"), Account(google_sub="other", role="user"), Account(username="staff", role="admin")
            db.add_all([owner, other, admin])
            db.flush()
            owner_id, admin_id = owner.id, admin.id
            user_auth, other_auth, admin_auth = [{"Authorization": "Bearer "+client.app.state.identity.new_session(db, a)[0]} for a in (owner, other, admin)]
        assert client.get("/api/v1/admin/users", params={"q": "BUYER@"}, headers=admin_auth).json()["items"][0]["email"] == "buyer@example.test"
        assert client.get("/api/v1/admin/users", params={"q": "%"}, headers=admin_auth).json()["items"] == []
        assert client.get("/api/v1/admin/orders", params={"cursor": "invalid"}, headers=admin_auth).status_code == 422
        plan_body = {"name": "Beta 30", "price_vnd": 99000, "duration_days": 30, "analysis_quota": 30, "active_limit": 2}
        assert client.post("/api/v1/admin/plans", json=plan_body, headers=user_auth).status_code == 401
        created = client.post("/api/v1/admin/plans", json=plan_body, headers=admin_auth | {"Idempotency-Key": "plan-first"})
        assert created.status_code == 201, created.text
        plan_id = created.json()["id"]
        assert not created.json()["available"]
        assert created.json()["daily_request_limit"] == 30
        repeat = client.post("/api/v1/admin/plans", json=plan_body, headers=admin_auth | {"Idempotency-Key": "plan-first"})
        assert repeat.json()["id"] == plan_id
        assert client.post("/api/v1/admin/plans", json=plan_body | {"price_vnd": 100000}, headers=admin_auth | {"Idempotency-Key": "plan-first"}).status_code == 409
        assert client.post("/api/v1/orders", json={"plan_id": plan_id}, headers=user_auth | {"Idempotency-Key": "first"}).status_code == 409
        assert client.post(f"/api/v1/admin/plans/{plan_id}/availability", json={"available": True}, headers=admin_auth).status_code == 200

        def order(key):
            result = client.post("/api/v1/orders", json={"plan_id": plan_id, "new_order": True}, headers=user_auth | {"Idempotency-Key": key})
            assert result.status_code == 201, result.text
            return result.json()

        first = order("first")
        # Order administration is read-only and never available to customer JWTs.
        for path in ("/api/v1/admin/orders", f"/api/v1/admin/orders/{first['id']}"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers=user_auth).status_code == 401
        rows = client.get("/api/v1/admin/orders", params={"status": "PENDING", "reference": first["reference"]}, headers=admin_auth).json()
        assert [item["id"] for item in rows["items"]] == [first["id"]]
        assert rows["items"][0]["plan_name"] == "Beta 30"
        assert client.get("/api/v1/admin/orders", params={"account_id": "absent"}, headers=admin_auth).json()["items"] == []
        assert client.get("/api/v1/admin/orders", params={"status": "INVALID"}, headers=admin_auth).status_code == 422
        assert client.get("/api/v1/admin/orders/missing", headers=admin_auth).status_code == 404
        reused = client.post("/api/v1/orders", json={"plan_id": plan_id}, headers=user_auth | {"Idempotency-Key": "resume-first"})
        assert reused.json()["id"] == first["id"]
        assert order("first")["id"] == first["id"]
        assert first["transfer_content"] == first["reference"] and "amount=99000" in first["qr_url"]
        assert client.get(f"/api/v1/orders/{first['id']}", headers=other_auth).status_code == 404
        assert client.get("/api/v1/me/entitlements", headers=user_auth).json()["active"] is False

        def payload(order, number, **changes):
            return {"id": number, "gateway": "TPBank", "transactionDate": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d %H:%M:%S"),
                "accountNumber": "00001111", "code": None, "content": order["reference"], "transferType": "in",
                "transferAmount": 99000, "referenceCode": "BANK"+str(number), **changes}

        hook_auth = {"Authorization": "Apikey fixture-webhook-key"}

        def send(body):
            result = client.post("/api/v1/webhooks/sepay", json=body, headers=hook_auth)
            assert result.status_code == 200 and result.json() == {"success": True}, result.text

        body = payload(first, 1)
        assert client.post("/api/v1/webhooks/sepay", json=body).status_code == 401
        assert client.post("/api/v1/webhooks/sepay", json=body | {"id": 0}, headers=hook_auth).status_code == 422
        settings.payments_enabled = False
        assert client.post("/api/v1/orders", json={"plan_id": plan_id}, headers=user_auth | {"Idempotency-Key": "stopped"}).status_code == 503
        send(body)
        settings.payments_enabled = True
        send(body)
        send(body | {"id": 2})  # Same bank reference, different SePay ID must not grant twice.
        assert client.get(f"/api/v1/orders/{first['id']}", headers=user_auth).json()["status"] == "PAID"
        assert client.get("/api/v1/me/entitlements", headers=user_auth).json()["remaining"] == 30
        assert client.post("/api/v1/orders", json={"plan_id": plan_id}, headers=user_auth | {"Idempotency-Key": "resume-first"}).json()["id"] == first["id"]
        detail = client.get(f"/api/v1/admin/orders/{first['id']}", headers=admin_auth).json()
        assert detail["customer"]["id"] == owner_id and detail["plan"]["price_vnd"] == 99000
        assert detail["subscription"]["bucket_id"]
        assert any(p["status"] == "APPLIED" for p in detail["payments"])
        assert "idempotency_key" not in detail and "fingerprint" not in str(detail)
        assert client.post(f"/api/v1/orders/{first['id']}/cancel", headers=user_auth).status_code == 409
        send(payload(first, 3))  # A genuinely second transfer goes to reconciliation.
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(Subscription)) == 1
            assert db.scalar(select(func.count()).select_from(QuotaBucket)) == 1
            assert db.get(BankPayment, "3").reason == "ORDER_CLOSED_OR_LATE"

        second = order("renewal")
        send(payload(second, 4))
        with sessions() as db:
            periods = list(db.scalars(select(Subscription).order_by(Subscription.valid_from)))
            assert len(periods) == 2 and periods[1].valid_from == periods[0].valid_until
            bucket = db.get(QuotaBucket, periods[0].bucket_id)
            assert bucket.quota_total == 30 and bucket.active_limit == 2
        # Real admission consumes the active period, never the prepaid future period.
        with sessions() as db, db.begin():
            job = jobs.admit(db, owner_id, "FPT", datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date(), "paid-job")
            assert job.bucket_id == periods[0].bucket_id
            fail_job(db, job.id)

        negative = order("mismatches")
        for number, changes, expected in [
            (10, {"transferAmount": 98000}, "AMOUNT_MISMATCH"),
            (11, {"transferAmount": 100000}, "AMOUNT_MISMATCH"),
            (12, {"accountNumber": "9999"}, "WRONG_BANK_ACCOUNT"),
            (13, {"transferType": "out"}, "OUTBOUND_TRANSFER"),
            (14, {"content": "no order reference"}, "REFERENCE_MISSING_OR_AMBIGUOUS"),
            (15, {"content": negative["reference"]+" "+first["reference"]}, "REFERENCE_MISSING_OR_AMBIGUOUS"),
        ]:
            send(payload(negative, number, **changes))
            with sessions() as db:
                assert db.get(BankPayment, str(number)).reason == expected
                assert db.scalar(select(func.count()).select_from(Subscription)) == 2
        pending_view = client.get(f"/api/v1/orders/{negative['id']}", headers=user_auth).json()
        assert pending_view["payment_resolution"] == "RECONCILIATION" and pending_view["qr_url"] is None
        assert client.post(f"/api/v1/orders/{negative['id']}/cancel", headers=user_auth).status_code == 409
        # An administrator cannot force an underpayment into an exact-price purchase.
        assert client.post("/api/v1/admin/reconciliation/10/apply", json={"order_id": negative["id"], "reason": "Verified manually with bank"}, headers=admin_auth).status_code == 409

        late = order("late")
        with sessions() as db, db.begin():
            db.get(PurchaseOrder, late["id"]).expires_at = int(time.time())-1
        send(payload(late, 20))
        assert client.get(f"/api/v1/orders/{late['id']}", headers=user_auth).json()["status"] == "EXPIRED"
        expired = client.get("/api/v1/admin/orders", params={"status": "EXPIRED"}, headers=admin_auth).json()["items"]
        assert late["id"] in {o["id"] for o in expired}
        pending = client.get("/api/v1/admin/orders", params={"status": "PENDING"}, headers=admin_auth).json()["items"]
        assert late["id"] not in {o["id"] for o in pending}
        page1 = client.get("/api/v1/admin/orders", params={"limit": 1, "plan_id": plan_id}, headers=admin_auth).json()
        page2 = client.get("/api/v1/admin/orders", params={"limit": 1, "plan_id": plan_id, "cursor": page1["next_cursor"]}, headers=admin_auth).json()
        assert len(page1["items"]) == len(page2["items"]) == 1
        assert page1["items"][0]["id"] != page2["items"][0]["id"]
        resolution = {"order_id": late["id"], "reason": "Verified late transfer against bank statement"}
        for _ in range(2):
            result = client.post("/api/v1/admin/reconciliation/20/apply", json=resolution, headers=admin_auth)
            assert result.status_code == 200 and result.json()["status"] == "APPLIED", result.text

        # Withdrawing the plan blocks new orders but preserves already created order terms.
        pending = order("before-withdrawal")
        client.post(f"/api/v1/admin/plans/{plan_id}/availability", json={"available": False}, headers=admin_auth)
        assert client.post("/api/v1/orders", json={"plan_id": plan_id}, headers=user_auth | {"Idempotency-Key": "withdrawn"}).status_code == 409
        send(payload(pending, 21))
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(Subscription)) == 4

        # Failure after grant must roll back payment + order + bucket + subscription together.
        with sessions() as db, db.begin():
            plan = db.get(PlanVersion, plan_id)
            plan.available = True
        rollback_order = order("rollback")
        with pytest.raises(RuntimeError):
            with sessions() as db, db.begin():
                billing.receive(db, billing.Webhook.model_validate(payload(rollback_order, 30)))
                db.flush()
                raise RuntimeError("simulated failure before commit")
        with sessions() as db:
            assert db.get(BankPayment, "30") is None
            assert db.get(PurchaseOrder, rollback_order["id"]).status == "PENDING"
            assert db.scalar(select(func.count()).select_from(Subscription)) == 4
        assert client.post(f"/api/v1/orders/{rollback_order['id']}/cancel", headers=user_auth).status_code == 200
        send(payload(rollback_order, 31))
        with sessions() as db:
            assert db.get(BankPayment, "31").reason == "ORDER_CANCELLED"
        assert client.get("/api/v1/admin/reconciliation", headers=admin_auth).status_code == 200
        assert client.get("/api/v1/payments", headers=other_auth).json()["items"] == []
        # Admission budget counts created requests, not successful compute or reservations.
        from service.daily_limits import day_bounds
        with sessions() as db, db.begin():
            bucket = db.get(QuotaBucket, periods[0].bucket_id)
            assert bucket.daily_created == 1
            for index in range(28):
                item = jobs.admit(db, owner_id, "FPT", datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date(), f"daily-{index}")
                fail_job(db, item.id)  # Internal settlement does not return admission budget.
            old_day = jobs.admit(db, owner_id, "FPT", datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date(), "daily-last")
            old_id = old_day.id
            assert jobs.admit(db, owner_id, "FPT", datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date(), "daily-last").id == old_id
        with sessions() as db, pytest.raises(jobs.JobError):
            jobs.admit(db, owner_id, "FPT", datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date(), "daily-over")
        next_day = day_bounds(int(time.time()))[1] + 1
        with monkeypatch.context() as clock:
            clock.setattr(time, "time", lambda: next_day)
            with sessions() as db, db.begin():
                new_day = jobs.admit(db, owner_id, "FPT", datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date(), "next-day-first")
                assert new_day.bucket_id != old_day.bucket_id
            with sessions() as db, pytest.raises(jobs.JobError) as limit:
                jobs.admit(db, owner_id, "FPT", datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date(), "cross-day-active-limit")
            assert limit.value.code == "ACTIVE_JOB_LIMIT"
            with sessions() as db, db.begin():
                fail_job(db, old_id)
                jobs.admit(db, owner_id, "FPT", datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date(), "next-day-second")
        assert client.post(f"/api/v1/analyses/{old_id}/cancel", headers=user_auth).status_code == 404
        assert client.post(f"/api/v1/admin/jobs/{old_id}/quota-refund", json={}, headers=admin_auth).status_code == 404
        replacement = client.post("/api/v1/admin/plans", json=plan_body | {"replaces_plan_id": plan_id}, headers=admin_auth | {"Idempotency-Key": "replace-plan"})
        assert replacement.status_code == 201
        replacement_id = replacement.json()["id"]
        assert client.post(f"/api/v1/admin/plans/{replacement_id}/availability", json={"available": True}, headers=admin_auth).status_code == 409
        assert client.post(f"/api/v1/admin/plans/{replacement_id}/availability", json={"available": True, "replace_existing": True}, headers=admin_auth).status_code == 200
        from service.onboarding import FREE_PLAN_ID
        public = client.get("/api/v1/plans").json()["items"]
        assert [p["id"] for p in public] == [FREE_PLAN_ID, replacement_id]
        assert public[0]["price_vnd"] == 0 and public[0]["daily_request_limit"] == 3
        assert client.post(f"/api/v1/admin/plans/{FREE_PLAN_ID}/availability", json={"available": True}, headers=admin_auth).json()["code"] == "SIGNUP_PLAN_MANAGED"

    engine.dispose()
