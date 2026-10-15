import json
import time
import threading

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from service.api import create_app
from service.db import SCHEMA_REVISION, make_engine, session_factory
from service.models import Account, ProcessHeartbeat, QuotaBucket
from service.operations_health import BrokerProbe, collect
from service.settings import Settings
from service.telemetry import HttpMetrics, ProcessMonitor


def test_private_metrics_and_operational_integrity(tmp_path, monkeypatch):
    url = "sqlite:///"+(tmp_path/"monitor.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    private, public = tmp_path/"private.pem", tmp_path/"public.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test", jwt_private_key_path=private,
        jwt_public_key_path=public, metrics_token="private-scrape-test-credential")
    monkeypatch.setattr("service.operations_health.broker_probe", lambda _: {"up": True, "ready_messages": 2, "consumers": 1, "dead_messages": 0})
    events = []
    monkeypatch.setattr("service.api.emit", lambda event, **fields: events.append({"event": event, **fields}))
    engine = make_engine(url)
    sessions = session_factory(engine)
    app = create_app(settings)
    with TestClient(app) as client:
        with sessions() as db, db.begin():
            staff, owner = Account(role="admin", username="operator"), Account(role="user", google_sub="fixture-user")
            db.add_all([staff, owner])
            db.flush()
            admin_auth, user_auth = [{"Authorization": "Bearer "+app.state.identity.new_session(db, a)[0]} for a in (staff, owner)]
            bucket = QuotaBucket(account_id=owner.id, valid_from=int(time.time())-10, valid_until=int(time.time())+1000,
                quota_total=5, active_limit=2, entitlement_version="test")
            db.add(bucket)
            db.flush()
            bucket_id = bucket.id
            now = int(time.time())
            db.add(ProcessHeartbeat(id="stale", role="worker", state="RUNNING", started_at=now-200, last_seen=now-100, progress_at=now-200))
        assert client.get("/api/v1/admin/operations/health").status_code == 401
        assert client.get("/api/v1/admin/operations/health", headers=user_auth).status_code == 401
        health = client.get("/api/v1/admin/operations/health", headers=admin_auth)
        assert health.status_code == 200, health.text
        data = health.json()
        assert data["schema_ready"] and data["inconsistent_quota_buckets"] == 0
        assert data["broker"]["ready_messages"] == 2
        assert data["processes"] == [{"role": "api", "state": "RUNNING", "count": 1}]
        assert client.get("/health/ready").json()["schema_revision"] == SCHEMA_REVISION
        for headers in ({}, admin_auth, {"Authorization": "Bearer wrong"}):
            assert client.get("/internal/metrics", headers=headers).status_code == 401
        client.get("/api/v1/analyses/private-job-id?token=never-log-query", headers=user_auth)
        client.get("/not-a-route/private-path-secret")
        client.post("/api/v1/admin/auth/login", json={"password": "never-log-body-secret"})
        metrics = client.get("/internal/metrics", headers={"Authorization": "Bearer private-scrape-test-credential"})
        assert metrics.status_code == 200
        assert 'route="/api/v1/analyses/{job_id}"' in metrics.text
        assert 'route="unmatched"' in metrics.text
        assert "quantik_db_inconsistent_quota_buckets 0" in metrics.text
        recorded = metrics.text+json.dumps(events)
        for secret in ("private-job-id", "never-log-query", "private-path-secret", "never-log-body-secret", "private-scrape-test-credential", admin_auth["Authorization"]):
            assert secret not in recorded
        settings.metrics_token = SecretStr("")
        assert client.get("/internal/metrics").status_code == 503
        with sessions() as db, db.begin():
            db.get(QuotaBucket, bucket_id).reserved = 1
        with sessions() as db:
            assert collect(db)["inconsistent_quota_buckets"] == 1
        # Once the cache expires, a failed collector must return 503 rather than old success.
        app.state.operations_collector.expires_at = 0
        def broken(_):
            raise RuntimeError("never-log-database-secret")
        monkeypatch.setattr("service.operations_health.collect", broken)
        failed = client.get("/api/v1/admin/operations/health", headers=admin_auth)
        assert failed.status_code == 503 and "never-log-database-secret" not in failed.text
    with sessions() as db:
        assert db.get(ProcessHeartbeat, app.state.monitor.id).state == "STOPPED"
    with ProcessMonitor(sessions, "reconciler") as monitor:
        monitor.progress(error_type="ControlledFailure")
        monitor.pulse()
        with sessions() as db:
            row = db.get(ProcessHeartbeat, monitor.id)
            assert (row.state, row.error_type) == ("ERROR", "ControlledFailure")
    engine.dispose()


def test_http_histogram_is_cumulative_and_bounded():
    metrics = HttpMetrics()
    metrics.observe("GET", "/fixture/{id}", 200, .07)
    metrics.observe("GET", "/fixture/{id}", 201, .2)
    metrics.observe("UNRECOGNIZED", "unmatched", 404, .001)
    metrics.observe("GET", "/internal/metrics", 200, 1)
    result = metrics.render()
    assert 'le="0.025",method="GET",route="/fixture/{id}",status="2xx"} 0' in result
    assert 'le="0.1",method="GET",route="/fixture/{id}",status="2xx"} 1' in result
    assert 'le="+Inf",method="GET",route="/fixture/{id}",status="2xx"} 2' in result
    assert 'quantik_http_request_duration_seconds_count{method="GET",route="/fixture/{id}",status="2xx"} 2' in result
    assert 'method="OTHER"' in result and "/internal/metrics" not in result


def test_stalled_broker_probe_is_bounded_and_not_duplicated(monkeypatch):
    release = threading.Event()
    calls = []
    def stalled(_):
        calls.append(1)
        release.wait(5)
        return {"up": True}
    monkeypatch.setattr("service.operations_health.broker_probe", stalled)
    probe = BrokerProbe(Settings(_env_file=None), timeout=.01)
    try:
        assert probe.snapshot() == {"up": False, "reason": "BROKER_PROBE_TIMEOUT"}
        original = probe.thread
        assert probe.snapshot()["up"] is False
        assert probe.thread is original and len(calls) == 1
    finally:
        release.set()
        probe.thread.join(timeout=2)
    assert not probe.thread.is_alive()
