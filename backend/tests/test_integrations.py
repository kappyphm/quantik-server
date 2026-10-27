from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import threading
from zoneinfo import ZoneInfo
import json

from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from service import billing, integrations, ingestion
from service.api import create_app
from service.db import make_engine, session_factory
from service.market_data import DataError
from service.models import Account, AuditEvent, IntegrationConfig
from service.settings import Settings


@pytest.fixture
def context(tmp_path, monkeypatch):
    url = "sqlite:///"+(tmp_path/"integrations.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    private, public, master = [tmp_path/name for name in ("private.pem", "public.pem", "integration.key")]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    master.write_bytes(Fernet.generate_key())
    settings = Settings(_env_file=None, environment="test", database_url=url, jwt_private_key_path=private,
        jwt_public_key_path=public, integration_encryption_key_path=master, VNSTOCK_API_KEY="env-vnstock-key",
        SEPAY_WEBHOOK_API_KEY="env-webhook-key-123", SEPAY_BANK_CODE="TPBANK", SEPAY_ACCOUNT_NUMBER="00001111",
        SEPAY_ACCOUNT_NAME="Fixture", SEPAY_REQUIRES_TRANSFER_PREFIX=True, payments_enabled=False)
    engine = make_engine(url)
    sessions = session_factory(engine)
    with TestClient(create_app(settings)) as client:
        with sessions() as db, db.begin():
            staff, owner = Account(role="admin", username="staff"), Account(role="user", google_sub="fixture-owner")
            db.add_all([staff, owner])
            db.flush()
            headers = [{"Authorization": "Bearer "+client.app.state.identity.new_session(db, a)[0]} for a in (staff, owner)]
            staff_id = staff.id
        yield client, settings, sessions, headers[0], headers[1], staff_id
    engine.dispose()


def test_admin_vnstock_rotation_encryption_and_ingestion(context, monkeypatch):
    client, settings, sessions, admin, user, _ = context
    url = "/api/v1/admin/integrations/vnstock"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=user).status_code == 401
    initial = client.get(url, headers=admin).json()
    assert initial["revision"] == 0 and initial["source"] == "ENVIRONMENT" and initial["api_key_configured"]
    assert "env-vnstock-key" not in json.dumps(initial)
    body = {"expected_revision": 0, "reason": "Rotate provider credentials", "api_key": "new-vnstock-key-never-return"}
    assert client.patch(url, json=body, headers=user).status_code == 401
    changed = client.patch(url, json=body, headers=admin)
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 1
    assert client.patch(url, json=body, headers=admin).status_code == 409
    invalid = client.patch(url, json=body | {"expected_revision": 1, "api_key": "\ninvalid-secret-value"}, headers=admin)
    assert invalid.status_code == 422 and "invalid-secret-value" not in invalid.text
    with sessions() as db:
        raw = db.get(IntegrationConfig, "vnstock").encrypted_payload
        audits = json.dumps([event.details for event in db.scalars(select(AuditEvent))])
        assert body["api_key"] not in raw+audits+changed.text
        assert integrations.read(db, "vnstock", settings)[0]["api_key"] == body["api_key"]
    calls = []
    class Provider:
        def __init__(self, key, *_):
            calls.append(key)
        def universe(self):
            raise DataError("PROBE_FINISHED_WITHOUT_NETWORK")
    monkeypatch.setattr(ingestion, "VnstockProvider", Provider)
    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    with pytest.raises(DataError, match="PROBE_FINISHED_WITHOUT_NETWORK"):
        ingestion.ingest_market(settings, today, today)
    assert calls == [body["api_key"]]
    cleared = client.patch(url, json={"expected_revision": 1, "reason": "Revoke provider credential", "api_key": None}, headers=admin)
    assert cleared.status_code == 200 and cleared.json()["api_key_configured"] is False
    with pytest.raises(DataError, match="PROVIDER_API_KEY_MISSING"):
        ingestion.ingest_market(settings, today, today)
    assert len(calls) == 1  # Explicit clear must never fall back to the old environment key.


def test_sepay_hot_configuration_preserves_existing_orders(context):
    client, settings, sessions, admin, user, staff_id = context
    path = "/api/v1/admin/integrations/sepay"
    initial = client.get(path, headers=admin).json()
    assert initial["missing_fields"] == ["transfer_prefix"] and not initial["payments_enabled"]
    base = {"expected_revision": 0, "reason": "Configure bank integration"}
    assert client.patch(path, json=base | {"payments_enabled": True}, headers=admin).status_code == 422
    with sessions() as db:
        assert db.get(IntegrationConfig, "sepay") is None
    hook_key = "new-webhook-key-not-returned"
    configured = client.patch(path, json=base | {"webhook_api_key": hook_key, "transfer_prefix": "VA123", "payments_enabled": True}, headers=admin)
    assert configured.status_code == 200, configured.text
    assert hook_key not in configured.text and configured.json()["configuration_complete"]
    assert client.get("/api/v1/plans").json()["payments_enabled"]
    with sessions() as db, db.begin():
        plan = billing.create_plan(db, billing.PlanInput(name="Integration fixture", price_vnd=99000,
            duration_days=30, analysis_quota=10, active_limit=1), staff_id)
        plan.available = True
        plan_id = plan.id
    def order(key):
        response = client.post("/api/v1/orders", json={"plan_id": plan_id, "new_order": True}, headers=user | {"Idempotency-Key": key})
        assert response.status_code == 201, response.text
        return response.json()
    first = order("before-bank-change")
    assert first["account_number"] == "00001111" and first["transfer_content"].startswith("VA123 ")
    assert client.patch(path, json={"expected_revision": 1, "reason": "Change bank receiving account", "account_number": "99992222"}, headers=admin).status_code == 200
    second = order("after-bank-change")
    assert second["account_number"] == "99992222"
    assert client.get("/api/v1/orders/"+first["id"], headers=user).json()["account_number"] == "00001111"
    payload = {"id": 120, "gateway": "TPBank", "transactionDate": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d %H:%M:%S"),
        "accountNumber": "00001111", "content": first["transfer_content"], "transferType": "in", "transferAmount": 99000, "referenceCode": "FIXTURE_BANK_120"}
    assert client.post("/api/v1/webhooks/sepay", json=payload, headers={"Authorization": "Apikey env-webhook-key-123"}).status_code == 401
    paid = client.post("/api/v1/webhooks/sepay", json=payload, headers={"Authorization": "Apikey "+hook_key})
    assert paid.status_code == 200, paid.text
    assert client.get("/api/v1/orders/"+first["id"], headers=user).json()["status"] == "PAID"
    assert client.get("/api/v1/me/entitlements", headers=user).json()["remaining"] == 10
    # Active payments cannot lose their webhook secret without an explicit simultaneous disable.
    revoke = {"expected_revision": 2, "reason": "Disable receiving payments", "webhook_api_key": None}
    assert client.patch(path, json=revoke, headers=admin).status_code == 409
    assert client.patch(path, json=revoke | {"payments_enabled": False}, headers=admin).status_code == 409
    assert client.post("/api/v1/orders/" + second["id"] + "/cancel", headers=user).status_code == 200
    assert client.patch(path, json=revoke | {"payments_enabled": False}, headers=admin).status_code == 200
    assert not client.get("/api/v1/plans").json()["payments_enabled"]
    assert client.post("/api/v1/orders", json={"plan_id": plan_id}, headers=user | {"Idempotency-Key": "disabled"}).status_code == 503
    with sessions() as db:
        assert hook_key not in db.get(IntegrationConfig, "sepay").encrypted_payload
        assert hook_key not in json.dumps([event.details for event in db.scalars(select(AuditEvent))])


def test_missing_wrong_key_and_tampered_ciphertext_fail_closed(context):
    client, settings, sessions, admin, _, _ = context
    path = "/api/v1/admin/integrations/vnstock"
    body = {"expected_revision": 0, "reason": "Configure provider account", "api_key": "stored-provider-key"}
    original = settings.integration_encryption_key_path.read_bytes()
    settings.integration_encryption_key_path.unlink()
    assert client.get(path, headers=admin).json()["encryption_ready"] is False
    assert client.patch(path, json=body, headers=admin).status_code == 503
    settings.integration_encryption_key_path.write_bytes(original)
    assert client.patch(path, json=body, headers=admin).status_code == 200
    assert client.patch("/api/v1/admin/integrations/sepay", json={"expected_revision": 0,
        "reason": "Persist receiving configuration", "payments_enabled": False}, headers=admin).status_code == 200
    settings.integration_encryption_key_path.write_bytes(Fernet.generate_key())
    failed = client.get(path, headers=admin)
    assert failed.status_code == 503 and body["api_key"] not in failed.text
    assert client.get("/api/v1/plans").status_code == 503  # No environment fallback when a stored override cannot decrypt.
    settings.integration_encryption_key_path.write_bytes(original)
    with sessions() as db, db.begin():
        db.get(IntegrationConfig, "vnstock").encrypted_payload = "tampered"
    assert client.get(path, headers=admin).status_code == 503


def test_concurrent_admin_updates_have_one_winner(context):
    client, settings, sessions, admin, _, _ = context
    barrier = threading.Barrier(2)
    def change(index):
        barrier.wait(timeout=5)
        return client.patch("/api/v1/admin/integrations/vnstock", json={"expected_revision": 0,
            "reason": "Concurrent operator rotation", "api_key": "fixture-concurrent-key-"+str(index)}, headers=admin).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(change, range(2)))
    assert sorted(statuses) == [200, 409]
    with sessions() as db:
        assert db.get(IntegrationConfig, "vnstock").revision == 1
        assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == "integration.updated")))) == 1
