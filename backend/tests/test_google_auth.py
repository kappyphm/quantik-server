from entitlement_fixtures import paid_bucket
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from service.api import create_app
from service.db import make_engine, session_factory
from service.identity import seed_admin
from service.models import Account, QuotaBucket, Subscription
from service.settings import Settings


def test_google_exchange_browser_binding_and_role_isolation(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "google.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test",
        jwt_private_key_path=private, jwt_public_key_path=public,
        GOOGLE_CLIENT_ID="test-client", GOOGLE_CLIENT_SECRET="fixture-secret")
    engine = make_engine(url)
    with session_factory(engine)() as db, db.begin():
        seed_admin(db, "admin", "test-password-not-a-secret")
    with TestClient(create_app(settings)) as client:
        provider = client.app.state.google
        provider.http.close()
        active_nonce = ""
        invalid_nonce = False
        token_calls = 0
        google_profile = {"name": "Người dùng kiểm thử", "email": "verified@example.test", "email_verified": True}

        def token_endpoint(request):
            nonlocal token_calls
            token_calls += 1
            form = parse_qs(request.content.decode())
            assert form["client_secret"] == ["fixture-secret"]
            assert len(form["code_verifier"][0]) >= 43
            claims = {"sub": "google-stable-sub", "iss": "https://accounts.google.com",
                "aud": "test-client", "iat": int(time.time()), "exp": int(time.time()) + 600,
                "nonce": "wrong-nonce" if invalid_nonce else active_nonce}
            claims.update(google_profile)
            return httpx.Response(200, json={"id_token": jwt.encode(claims, private.read_bytes(), algorithm="RS256")})

        provider.http = httpx.Client(transport=httpx.MockTransport(token_endpoint))
        # Only JWKS retrieval is substituted; JWT signature/issuer/audience/nonce checks still execute.
        provider.jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public.read_bytes()))
        start = client.get("/api/v1/auth/google/start").json()
        params = parse_qs(urlsplit(start["authorization_url"]).query)
        active_nonce = params["nonce"][0]
        assert params["code_challenge_method"] == ["S256"]
        assert "profile" in params["scope"][0].split()
        state = params["state"][0]
        browser = client.cookies.get("quantik_oauth_browser")
        denied = client.get("/api/v1/auth/google/callback", params={"state": state, "code": "provider-code"},
            headers={"Cookie": "quantik_oauth_browser=attacker"})
        assert denied.status_code == 401 and token_calls == 0
        result = client.get("/api/v1/auth/google/callback", params={"state": state, "code": "provider-code"})
        assert result.status_code == 200
        exchange = result.json()["exchange_code"]
        assert client.get("/api/v1/auth/google/callback", params={"state": state, "code": "provider-code"}).status_code == 401
        assert token_calls == 1
        assert client.post("/api/v1/auth/exchange", json={"code": exchange}).status_code == 403
        origin = {"Origin": settings.public_origin}
        assert client.post("/api/v1/auth/exchange", json={"code": exchange},
            headers=origin | {"Cookie": "quantik_oauth_browser=attacker"}).status_code == 401
        result = client.post("/api/v1/auth/exchange", json={"code": exchange}, headers=origin)
        assert result.status_code == 200
        access = result.json()["access_token"]
        headers = {"Authorization": f"Bearer {access}"}
        assert client.get("/api/v1/me", headers=headers).status_code == 200
        assert client.get("/api/v1/admin/me", headers=headers).status_code == 401
        assert client.get("/health/ready").status_code == 200
        owner_id = client.get("/api/v1/me", headers=headers).json()["id"]
        assert client.get("/api/v1/me", headers=headers).json() == {
            "id": owner_id, "role": "user", "display_name": "Người dùng kiểm thử", "email": "verified@example.test"}
        # A legacy/orphan bucket must not grant product access in any environment.
        with session_factory(engine)() as db, db.begin():
            db.add(QuotaBucket(account_id=owner_id, valid_from=int(time.time())-10,
                valid_until=int(time.time())+1000, quota_total=99, active_limit=2, entitlement_version="orphan"))
        orphan_rights = client.get("/api/v1/me/entitlements", headers=headers).json()
        assert orphan_rights["active"] is False and orphan_rights["remaining"] == 0
        denied = client.post("/api/v1/analyses", json={"symbol": "FPT", "target_session": "2026-10-14"},
            headers=headers | {"Idempotency-Key": "orphan-denied"})
        assert denied.status_code == 403 and denied.json()["code"] == "NO_ENTITLEMENT"
        with session_factory(engine)() as db, db.begin():
            bucket_id = paid_bucket(db, owner_id, valid_from=int(time.time())-10, valid_until=int(time.time())+1000, quota_total=5, active_limit=2).id
        analysis_body = {"symbol": "FPT", "target_session": "2026-10-14"}
        job_headers = headers | {"Idempotency-Key": "http-fixture-job"}
        result = client.post("/api/v1/analyses", json=analysis_body, headers=job_headers)
        assert result.status_code == 202
        job_id = result.json()["id"]
        assert client.post("/api/v1/analyses", json=analysis_body, headers=job_headers).json()["id"] == job_id
        assert client.get("/api/v1/analyses", headers=headers).json()["items"][0]["id"] == job_id
        assert client.get("/api/v1/analyses/unknown", headers=headers).status_code == 404
        result = client.post(f"/api/v1/analyses/{job_id}/cancel", headers=headers)
        assert result.status_code == 404
        buckets = client.get("/api/v1/me/usage", headers=headers).json()["buckets"]
        assert next(bucket for bucket in buckets if bucket["id"] == bucket_id)["reserved"] == 1
        with session_factory(engine)() as db, db.begin():
            period = db.scalar(select(Subscription).where(Subscription.account_id == owner_id))
            period.valid_until = int(time.time())-1
        assert client.get("/api/v1/me/entitlements", headers=headers).json()["active"] is False
        denied = client.post("/api/v1/analyses", json=analysis_body,
            headers=headers | {"Idempotency-Key": "expired-subscription-denied"})
        assert denied.status_code == 403 and denied.json()["code"] == "NO_ENTITLEMENT"
        assert client.post("/api/v1/auth/exchange", json={"code": exchange},
            headers=origin | {"Cookie": f"quantik_oauth_browser={browser}"}).status_code == 401
        assert client.post("/api/v1/auth/refresh", headers=origin).status_code == 200
        assert client.post("/api/v1/auth/logout", headers=origin).status_code == 204
        assert client.get("/api/v1/me", headers=headers).status_code == 401
        admin_token = client.post("/api/v1/admin/auth/login", json={"username": "admin",
            "password": "test-password-not-a-secret"}).json()["access_token"]
        assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {admin_token}"}).status_code == 401
        params = parse_qs(urlsplit(client.get("/api/v1/auth/google/start").json()["authorization_url"]).query)
        active_nonce, invalid_nonce = params["nonce"][0], True
        assert client.get("/api/v1/auth/google/callback", params={"state": params["state"][0], "code": "bad-nonce"}).status_code == 401
        # Same Google sub remains the same account; unverified email is never displayed.
        invalid_nonce = False
        google_profile.update(name="Tên mới đã xác minh", email="unverified@example.test", email_verified=False)
        params = parse_qs(urlsplit(client.get("/api/v1/auth/google/start").json()["authorization_url"]).query)
        active_nonce = params["nonce"][0]
        result = client.get("/api/v1/auth/google/callback", params={"state": params["state"][0], "code": "updated-profile"})
        result = client.post("/api/v1/auth/exchange", json={"code": result.json()["exchange_code"]}, headers=origin)
        current = client.get("/api/v1/me", headers={"Authorization": "Bearer " + result.json()["access_token"]}).json()
        assert current == {"id": owner_id, "role": "user", "display_name": "Tên mới đã xác minh", "email": None}
    with session_factory(engine)() as db:
        assert len(list(db.scalars(select(Account).where(Account.google_sub.is_not(None))))) == 1
    engine.dispose()
