"""Admin-minted demo codes, demo login, demo limits bypass and reset."""

import time

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from service.api import create_app
from service.db import make_engine, session_factory
from service.demo import ensure_demo_account
from service.identity import seed_admin
from service.models import Account, Job, Subscription
from service.screening_access import require_access
from service.settings import Settings


def _app(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "demo.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test",
        public_origin="http://testserver", jwt_private_key_path=private,
        jwt_public_key_path=public)
    engine = make_engine(url)
    with session_factory(engine)() as db, db.begin():
        assert seed_admin(db, "admin", "admin-password-long-enough")
    return create_app(settings), engine


def test_demo_code_login_bypass_order_block_and_reset(tmp_path, monkeypatch):
    app, engine = _app(tmp_path, monkeypatch)
    origin = {"Origin": "http://testserver"}
    with TestClient(app) as client:
        ready = client.get("/health/ready").json()
        assert ready == {"status": "ready", "schema_revision": "0018"}
        admin = client.post("/api/v1/admin/auth/login",
            json={"username": "admin", "password": "admin-password-long-enough"}).json()
        headers = {"Authorization": f"Bearer {admin['access_token']}"}
        created = client.post("/api/v1/admin/demo/codes", json={}, headers=headers)
        assert created.status_code == 200
        code = created.json()["code"]
        assert code
        listed = client.get("/api/v1/admin/demo/codes", headers=headers).json()
        assert "code" not in listed["items"][0]
        exchanged = client.post("/api/v1/auth/demo/exchange", json={"code": code}, headers=origin)
        assert exchanged.status_code == 200
        user = {"Authorization": f"Bearer {exchanged.json()['access_token']}"}
        profile = client.get("/api/v1/me", headers=user).json()
        assert profile["is_demo"] is True
        # Demo accounts cannot create real paid orders.
        order = client.post("/api/v1/orders", json={"plan_id": "x" * 36},
            headers={**user, **origin, "Idempotency-Key": "demo-order-1"})
        assert order.status_code == 403
        assert order.json()["code"] == "DEMO_ORDER_NOT_ALLOWED"
        # Demo subscription timestamps must fit Postgres INTEGER columns.
        with session_factory(engine)() as db, db.begin():
            account = ensure_demo_account(db)
            periods = db.scalars(select(Subscription).where(
                Subscription.account_id == account.id)).all()
            assert periods and all(p.valid_until <= 2_147_483_647 for p in periods)
        # Screening bypass holds even without a subscription.
        with session_factory(engine)() as db, db.begin():
            account = ensure_demo_account(db)
            from sqlalchemy import update
            from service.models import QuotaBucket
            db.execute(update(QuotaBucket).where(QuotaBucket.account_id == account.id)
                .values(subscription_id=None))
            db.execute(Subscription.__table__.delete().where(
                Subscription.account_id == account.id))
            db.execute(QuotaBucket.__table__.delete().where(
                QuotaBucket.account_id == account.id))
            require_access(db, account.id)
        # Revoke the code, then reset: the old demo session must die.
        code_id = created.json()["id"]
        assert client.post(f"/api/v1/admin/demo/codes/{code_id}/revoke",
            json={}, headers=headers).status_code == 200
        assert client.post("/api/v1/auth/demo/exchange", json={"code": code},
            headers=origin).status_code == 401
        reset = client.post("/api/v1/admin/demo/reset", json={}, headers=headers)
        assert reset.status_code == 200
        assert client.get("/api/v1/me", headers=user).status_code == 401
    engine.dispose()
