import time

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
from service.models import Account
from service.settings import Settings


def test_admin_lifecycle_and_token_security(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "auth.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    sessions = session_factory(engine)
    password = "test-password-not-a-secret"
    with sessions() as db, db.begin():
        assert seed_admin(db, "admin", password)
        assert not seed_admin(db, "admin", "different-password-must-not-reset")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test", public_origin="http://localhost:8000",
        jwt_private_key_path=private, jwt_public_key_path=public)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/ready").status_code == 200
        invalid = client.post("/api/v1/admin/auth/login", json={"password": "never-echo-this"})
        assert invalid.status_code == 422 and "never-echo-this" not in invalid.text
        assert client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
        result = client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": password})
        assert result.status_code == 200
        access = result.json()["access_token"]
        headers = {"Authorization": f"Bearer {access}"}
        assert client.get("/api/v1/admin/me", headers=headers).status_code == 200
        claims = jwt.decode(access, public.read_bytes(), algorithms=["RS256"], audience="quantik-admin")
        for change in ({"aud": "quantik-user"}, {"iss": "attacker"}, {"exp": int(time.time()) - 60}, {"role": "user"}):
            bad = jwt.encode(claims | change, private.read_bytes(), algorithm="RS256")
            assert client.get("/api/v1/admin/me", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
        bad = jwt.encode(claims, "wrong-secret-for-rejected-algorithm" * 2, algorithm="HS256")
        assert client.get("/api/v1/admin/me", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
        old_cookie = client.cookies.get("quantik_admin_refresh")
        assert client.post("/api/v1/admin/auth/refresh").status_code == 403
        origin = {"Origin": settings.public_origin}
        rotated = client.post("/api/v1/admin/auth/refresh", headers=origin)
        assert rotated.status_code == 200
        new_access = rotated.json()["access_token"]
        # Every used token remains detectable, even after several rotations.
        assert client.post("/api/v1/admin/auth/refresh", headers=origin).status_code == 200
        replay = client.post("/api/v1/admin/auth/refresh", headers=origin | {"Cookie": f"quantik_admin_refresh={old_cookie}"})
        assert replay.status_code == 401
        assert client.get("/api/v1/admin/me", headers={"Authorization": f"Bearer {new_access}"}).status_code == 401
        result = client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": password})
        headers = {"Authorization": "Bearer " + result.json()["access_token"]}
        assert client.post("/api/v1/admin/auth/logout", headers=origin).status_code == 204
        assert client.get("/api/v1/admin/me", headers=headers).status_code == 401
        for _ in range(5):
            assert client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
        assert client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": password}).status_code == 429
    with sessions() as db:
        assert len(list(db.scalars(select(Account)))) == 1
    engine.dispose()
