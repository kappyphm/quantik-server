"""Backfill API, shared lanes and publication over an isolated migrated database."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from service import scheduling, screening, screening_backfill, scheduled_worker, ingestion
from service.api import create_app
from service.market_data import DataError, normalize_history, normalize_universe, publish_manifest, save_history, save_universe
from service.models import Account, AuditEvent, PublishedScreening, ScheduledTask, ScreeningResult, ScreeningRun
from service.operational_config import activate, create_version
from service.settings import Settings
from test_scheduling import database
from test_screening import prices


def test_backfill_api_worker_and_monotonic_publication(tmp_path, monkeypatch):
    url, engine, sessions = database(tmp_path, monkeypatch)
    instant = datetime(2026, 10, 16, 14, tzinfo=scheduling.VN)
    clock = SimpleNamespace(time=lambda: instant.timestamp())
    monkeypatch.setattr(scheduling, "time", clock)
    monkeypatch.setattr(screening_backfill, "time", clock)
    private, public = tmp_path/"private.pem", tmp_path/"public.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test", scheduled_provider_enabled=True,
        allow_fixture_analysis=True, jwt_private_key_path=private, jwt_public_key_path=public)
    with sessions() as db, db.begin():
        config = create_version(db, "OPERATIONS", {"enabled": True, "ingestion_enabled": True}, None)
        activate(db, config.id, None, "Enable isolated backfill checks", None)
        calendar = create_version(db, "TRADING_CALENDAR", {"start": "2026-10-01", "end": "2026-10-31",
            "sessions": ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"], "source_urls": ["https://example.org/fixture"],
            "review_note": "Deterministic calendar for isolated backfill checks", "provenance": "FIXTURE"}, None)
        activate(db, calendar.id, None, "Activate isolated test calendar", None, allow_fixture=True)
    original_run = screening.run_screening
    monkeypatch.setattr(screening, "run_screening", lambda *a, **kw: original_run(*a, **kw, now=instant))
    ingested = []

    def ingest(_settings, start, target, **kwargs):
        assert kwargs["input_mode"] == "COMPLETED_DAILY" and kwargs["resume"]
        with sessions() as db, db.begin():
            kwargs["operation_guard"](db)
            universe = save_universe(db, normalize_universe(pd.DataFrame({"symbol": ["AAA", "BBB"], "exchange": ["HOSE", "HOSE"]})))
            histories = {}
            for symbol in ["AAA", "BBB", "VNINDEX"]:
                frame = prices(target.isoformat()).reset_index(names="time")
                frame["volume"] = frame.volume.round()
                payload = normalize_history(frame, symbol=symbol,
                    kind="INDEX" if symbol == "VNINDEX" else "EQUITY", source="vci", target_session=target,
                    observed_at=instant, input_mode="COMPLETED_DAILY", minimum_bars=1, provenance="FIXTURE")
                histories[symbol] = save_history(db, payload)
            manifest = publish_manifest(db, universe, histories, target_session=target, input_mode="COMPLETED_DAILY")
            ingested.append(target)
            return {"manifest_id": manifest.id}

    monkeypatch.setattr(ingestion, "ingest_market", ingest)
    with TestClient(create_app(settings)) as client:
        with sessions() as db, db.begin():
            admin, user = Account(role="admin", username="operator"), Account(role="user", google_sub="backfill-user")
            db.add_all([admin, user]); db.flush()
            headers = {"Authorization": "Bearer "+client.app.state.identity.new_session(db, admin)[0]}
            user_headers = {"Authorization": "Bearer "+client.app.state.identity.new_session(db, user)[0]}
        body = {"target_session": "2026-10-15", "reason": "Inspect completed market screening"}
        path = "/api/v1/admin/screening/backfills"
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers=user_headers).status_code == 401
        opts = client.get("/api/v1/admin/screening/backfill-options", headers=headers).json()
        assert opts["sessions"] == ["2026-10-15", "2026-10-14", "2026-10-13"] and opts["enabled"]
        assert client.post(path, json=body | {"target_session": "2026-10-16"},
            headers=headers | {"Idempotency-Key": "unfinished"}).status_code == 422
        response = client.post(path, json=body, headers=headers | {"Idempotency-Key": "request-1"})
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]
        assert client.post(path, json=body, headers=headers | {"Idempotency-Key": "request-1"}).json()["id"] == task_id
        assert client.post(path, json=body | {"reason": "Different request contents"},
            headers=headers | {"Idempotency-Key": "request-1"}).status_code == 409
        assert client.post(path, json=body | {"target_session": "2026-10-14"},
            headers=headers | {"Idempotency-Key": "other-pending"}).status_code == 409

        task, token = scheduling.claim(sessions, settings, "SCREEN")
        assert task.id == task_id and task.kind == "BACKFILL"
        with sessions() as db, db.begin():
            automatic = ScheduledTask(kind="INGEST", target_session=date(2026, 10, 16), slot="PM",
                config_id=config.id, calendar_id=calendar.id, scheduled_at=int(instant.timestamp()),
                next_attempt_at=int(instant.timestamp()), deadline=int(instant.timestamp())+3600,
                created_at=int(instant.timestamp()), status="PENDING")
            db.add(automatic); db.flush(); automatic_id = automatic.id
        assert scheduling.claim(sessions, settings, "INGEST") is None
        assert scheduling.claim(sessions, settings, "SCREEN") is None
        with sessions() as db, pytest.raises(DataError, match="BACKFILL_YIELD_TO_SCHEDULE"):
            scheduling.assert_current(db, task_id, token, settings)
        scheduling.finish(sessions, task_id, token, error=DataError("BACKFILL_YIELD_TO_SCHEDULE", True))
        automatic_task, automatic_token = scheduling.claim(sessions, settings, "INGEST")
        assert automatic_task.id == automatic_id
        scheduling.finish(sessions, automatic_id, automatic_token, result={"isolated_fixture": True})
        instant += timedelta(seconds=301)
        task, token = scheduling.claim(sessions, settings, "SCREEN")
        assert task.id == task_id
        scheduling.heartbeat(sessions, settings, task_id, token)
        result = scheduled_worker.execute(sessions, settings, task, token)
        scheduling.finish(sessions, task_id, token, result=result)
        detail = client.get(f"/api/v1/admin/scheduled-tasks/{task_id}", headers=headers).json()
        assert detail["status"] == "SUCCEEDED" and result["status"] == "READY"
        assert client.get(f"/api/v1/admin/scheduled-tasks/{task_id}", headers=user_headers).status_code == 401
        assert ingested == [date(2026, 10, 15)]
        with sessions() as db:
            latest_id = db.get(PublishedScreening, "ALL").run_id
            assert db.get(ScreeningRun, latest_id).summary["execution_mode"] == "BACKFILL"
            assert db.scalar(select(func.count()).select_from(ScreeningResult)) == 2
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "screening.backfill_requested")) == 1
        assert client.post(path, json=body, headers=headers | {"Idempotency-Key": "already-done"}).json()["status"] == "SUCCEEDED"

        old = client.post(path, json=body | {"target_session": "2026-10-14"},
            headers=headers | {"Idempotency-Key": "older-session"})
        assert old.status_code == 202
        assert scheduled_worker.run_once(sessions, settings, "SCREEN") == 1
        with sessions() as db:
            assert db.get(PublishedScreening, "ALL").run_id == latest_id
            assert db.get(PublishedScreening, "PM").run_id == latest_id
            assert db.get(ScheduledTask, old.json()["id"]).status == "SUCCEEDED"
        # Automatic execution still respects the original slot deadline.
        denied = original_run(sessions, date(2026, 10, 12), "PM", now=instant, allow_fixture=True)
        assert denied.reason == "SCREENING_DEADLINE"

        def unavailable(*args, **kwargs):
            raise DataError("PROVIDER_TEMPORARILY_UNAVAILABLE", True)

        monkeypatch.setattr(ingestion, "ingest_market", unavailable)
        retry_body = body | {"target_session": "2026-10-13"}
        retry_id = client.post(path, json=retry_body, headers=headers | {"Idempotency-Key": "retry-session"}).json()["id"]
        assert scheduled_worker.run_once(sessions, settings, "SCREEN") == 1
        assert client.get(f"/api/v1/admin/scheduled-tasks/{retry_id}", headers=headers).json()["status"] == "WAITING"
        with sessions() as db, db.begin():
            paused = create_version(db, "OPERATIONS", {"enabled": False}, None)
            activate(db, paused.id, config.id, "Pause during provider failure", None)
        instant += timedelta(seconds=301)
        assert scheduling.claim(sessions, settings, "SCREEN") is None
        denied = client.post(path, json=retry_body, headers=headers | {"Idempotency-Key": "paused-new-request"})
        assert denied.status_code == 409 and denied.json()["code"] == "BACKFILL_OPERATIONS_DISABLED"
        with sessions() as db, db.begin():
            activate(db, config.id, paused.id, "Resume after provider recovery", None)
            task = db.get(ScheduledTask, retry_id)
            task.deadline = int(instant.timestamp())-1
            scheduling.recover(db, int(instant.timestamp()))
        # The same request cannot reset an expired task; an explicit new retry can.
        assert client.post(path, json=retry_body, headers=headers | {"Idempotency-Key": "retry-session"}).json()["status"] == "FAILED"
        restarted = client.post(path, json=retry_body, headers=headers | {"Idempotency-Key": "retry-session-again"})
        assert restarted.json()["id"] == retry_id and restarted.json()["attempt"] == 0
        monkeypatch.setattr(ingestion, "ingest_market", ingest)
        assert scheduled_worker.run_once(sessions, settings, "SCREEN") == 1
        assert client.get(f"/api/v1/admin/scheduled-tasks/{retry_id}", headers=headers).json()["status"] == "SUCCEEDED"
