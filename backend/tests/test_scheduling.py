from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import func, select

from service import scheduling
from service.calendar_reference import calendar_2026, expected_screening
from service.db import make_engine, session_factory
from service.jobs import JobError
from service.market_data import DataError
from service.models import ActiveRuntimeConfig, ScheduledLane, ScheduledTask, ScreeningRun
from service.operational_config import OperationsPolicy, TradingCalendar, activate, create_version
from service.settings import Settings


def database(tmp_path, monkeypatch):
    url = "sqlite:///"+(tmp_path/"scheduler.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    return url, engine, session_factory(engine)


def test_calendar_config_schedule_and_fenced_recovery(tmp_path, monkeypatch):
    reference = TradingCalendar.model_validate(calendar_2026())
    assert len(reference.sessions) == 248
    for closed in (date(2026, 1, 2), date(2026, 1, 10), date(2026, 2, 18), date(2026, 8, 22), date(2026, 8, 31)):
        assert closed not in reference.sessions
    assert expected_screening(reference, "ALL", datetime(2026, 9, 2, 18, tzinfo=scheduling.VN)) == (date(2026, 8, 28), "PM")
    assert expected_screening(reference, "ALL", datetime(2026, 9, 3, 13, tzinfo=scheduling.VN)) == (date(2026, 9, 3), "AM")
    with pytest.raises(ValidationError):
        OperationsPolicy(unknown_runtime_flag=True)
    with pytest.raises(ValidationError):
        TradingCalendar.model_validate(calendar_2026() | {"sessions": ["2026-01-05", "2026-01-05"]})
    url, engine, sessions = database(tmp_path, monkeypatch)
    settings = Settings(_env_file=None, environment="test", database_url=url)
    now = datetime(2026, 10, 15, 11, 31, tzinfo=scheduling.VN)
    clock = [int(now.timestamp())]
    monkeypatch.setattr(scheduling, "time", SimpleNamespace(time=lambda: clock[0]))
    calendar = {"start": "2026-10-01", "end": "2026-10-31", "sessions": ["2026-10-09", "2026-10-15", "2026-10-16"],
        "source_urls": ["https://example.org/fixture"], "review_note": "Explicit deterministic fixture exchange calendar", "provenance": "FIXTURE"}
    with sessions() as db, db.begin():
        config = create_version(db, "OPERATIONS", {}, None)
        original_id = config.id
        activate(db, original_id, None, "Activate validated operation policy", None)
        reference = create_version(db, "TRADING_CALENDAR", calendar, None)
        with pytest.raises(JobError) as error:
            activate(db, reference.id, None, "Fixture forbidden by default", None)
        assert error.value.code == "FIXTURE_CALENDAR_FORBIDDEN"
        activate(db, reference.id, None, "Activate fixture for focused test", None, allow_fixture=True)
    report = scheduling.tick(sessions, settings, now=now)
    assert report["state"] == "READY" and report["created"] > 0
    assert scheduling.tick(sessions, settings, now=now)["created"] == 0
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ScheduledTask).where(ScheduledTask.target_session == date(2026, 10, 14))) == 0
        assert db.scalar(select(func.count()).select_from(ScheduledTask).where(ScheduledTask.target_session == date(2026, 10, 16))) == 0
        old = db.scalar(select(ScheduledTask).where(ScheduledTask.kind == "SCREEN", ScheduledTask.slot == "AM",
            ScheduledTask.target_session == date(2026, 10, 9)))
        assert old.status == "FAILED" and old.reason == "TASK_DEADLINE"
    assert scheduling.claim(sessions, settings, "INGEST") is None  # Provider pause cannot log in.
    task, token = scheduling.claim(sessions, settings, "SCREEN")
    assert task.slot == "AM" and task.target_session == now.date()
    assert scheduling.claim(sessions, settings, "SCREEN") is None  # Global lane also excludes another replica.
    with sessions() as db, db.begin():
        scheduling.assert_current(db, task.id, token, settings)
    clock[0] += 121
    with sessions() as db, db.begin():
        assert scheduling.recover(db, clock[0]) == 1
    with sessions() as db, pytest.raises(DataError):
        scheduling.assert_current(db, task.id, token, settings)
    assert scheduling.claim(sessions, settings, "SCREEN") is None
    clock[0] += 301
    retried, new_token = scheduling.claim(sessions, settings, "SCREEN")
    assert retried.id == task.id and retried.attempt == 2 and new_token != token
    scheduling.heartbeat(sessions, settings, task.id, new_token)
    with sessions() as db, db.begin():
        paused = create_version(db, "OPERATIONS", {"enabled": False}, None)
        activate(db, paused.id, original_id, "Pause operations for review", None)
    with sessions() as db, pytest.raises(DataError) as error:
        scheduling.assert_current(db, task.id, new_token, settings)
    assert error.value.code == "TASK_OPERATOR_PAUSED"
    scheduling.finish(sessions, task.id, new_token, error=DataError("TASK_OPERATOR_PAUSED", True))
    assert scheduling.claim(sessions, settings, "SCREEN") is None
    # Stale expected version cannot overwrite an operator change, including rollback.
    with sessions() as db, db.begin(), pytest.raises(JobError) as error:
        activate(db, original_id, original_id, "Stale activation must fail", None)
    assert error.value.code == "CONFIG_ACTIVATION_CONFLICT"
    with sessions() as db, db.begin():
        assert db.get(ActiveRuntimeConfig, "OPERATIONS").version_id == paused.id
        activate(db, original_id, paused.id, "Rollback to previously validated policy", None)
    clock[0] += 301
    retried, final_token = scheduling.claim(sessions, settings, "SCREEN")
    scheduling.finish(sessions, task.id, final_token, result={"screening_run_id": "fixture"})
    scheduling.finish(sessions, task.id, token, result={"screening_run_id": "stale"})
    with sessions() as db:
        current = db.get(ScheduledTask, task.id)
        assert current.status == "SUCCEEDED" and current.result == {"screening_run_id": "fixture"}
    # Sunday trigger targets the week's last actual session; no Saturday/weekend screening.
    sunday = datetime(2026, 10, 18, 18, 0, tzinfo=scheduling.VN)
    clock[0] = int(sunday.timestamp())
    scheduling.tick(sessions, settings, now=sunday)
    weekly, weekly_token = scheduling.claim(sessions, settings, "TRAIN")
    assert weekly.target_session == date(2026, 10, 16) and weekly.slot == "WEEK"
    with sessions() as db, db.begin():
        old_calendar = db.get(ActiveRuntimeConfig, "TRADING_CALENDAR").version_id
        corrected = create_version(db, "TRADING_CALENDAR", calendar | {"sessions": ["2026-10-09", "2026-10-15"]}, None)
        activate(db, corrected.id, old_calendar, "Explicit exchange closure correction", None, allow_fixture=True)
    with sessions() as db, pytest.raises(DataError) as error:
        scheduling.assert_current(db, weekly.id, weekly_token, settings)
    assert error.value.code == "TASK_SESSION_REMOVED_FROM_CALENDAR"
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ScheduledTask).where(ScheduledTask.kind == "SCREEN",
            ScheduledTask.target_session.in_([date(2026, 10, 17), date(2026, 10, 18)]))) == 0
    beyond = scheduling.tick(sessions, settings, now=datetime(2026, 11, 1, 12, tzinfo=scheduling.VN))
    assert beyond["state"] == "CALENDAR_OUT_OF_RANGE"
    engine.dispose()


def test_worker_waits_without_provider_calls_or_training(tmp_path, monkeypatch):
    import time
    from service.scheduled_worker import run_once
    url, engine, sessions = database(tmp_path, monkeypatch)
    settings = Settings(_env_file=None, environment="test", database_url=url, scheduled_provider_enabled=False)
    now = int(time.time())
    today = datetime.now(scheduling.VN).date()
    with sessions() as db, db.begin():
        config = create_version(db, "OPERATIONS", {"ingestion_enabled": True}, None)
        activate(db, config.id, None, "Activate scheduler fixture policy", None)
        calendar = create_version(db, "TRADING_CALENDAR", {"start": today.isoformat(), "end": today.isoformat(),
            "sessions": [today.isoformat()], "source_urls": ["https://example.org/fixture"],
            "review_note": "Single explicit session for worker readiness test", "provenance": "FIXTURE"}, None)
        activate(db, calendar.id, None, "Activate worker fixture calendar", None, allow_fixture=True)
        ids = {}
        for kind in ("INGEST", "TRAIN", "SNAPSHOT"):
            task = ScheduledTask(kind=kind, target_session=today, slot="PM", config_id=config.id, calendar_id=calendar.id,
                scheduled_at=now-1, deadline=now+3600, next_attempt_at=now-1, created_at=now,
                status="PENDING", attempt=0, lease_until=0)
            db.add(task)
            db.flush()
            ids[kind] = task.id
    assert run_once(sessions, settings, "INGEST") == 0
    assert run_once(sessions, settings, "TRAIN") == 1
    assert run_once(sessions, settings, "SNAPSHOT") == 1
    with sessions() as db:
        assert db.get(ScheduledTask, ids["INGEST"]).attempt == 0
        for kind in ("TRAIN", "SNAPSHOT"):
            task = db.get(ScheduledTask, ids[kind])
            assert task.status == "WAITING" and task.reason == "COMPLETED_MANIFEST_NOT_READY"
    engine.dispose()


def test_ingestion_resume_preserves_provider_budget(tmp_path, monkeypatch):
    from service import ingestion
    from service.market_data import DailyHistory
    url, engine, sessions = database(tmp_path, monkeypatch)
    monkeypatch.setenv("VNSTOCK_API_KEY", "fixture-not-a-secret")
    settings = Settings(_env_file=None, environment="test", database_url=url)
    calls, should_limit, should_wait = [], [True], [True]
    target, start = date(2026, 10, 15), date(2021, 1, 1)

    class Provider:
        def __init__(self, *_):
            pass

        def universe(self):
            return {"members": {s: {"exchange": "HOSE", "sector": "10"} for s in ("AAA", "BBB", "CCC", "DDD")}, "provenance": "LIVE_UNVERIFIED"}

        def history(self, symbol, start, target_session, **kwargs):
            calls.append(symbol)
            if symbol == "BBB" and should_limit[0]:
                should_limit[0] = False
                raise DataError("PROVIDER_BUDGET_EXHAUSTED", True, 600)
            if symbol == "CCC" and should_wait[0]:
                should_wait[0] = False
                raise DataError("TARGET_SESSION_NOT_READY", True)
            from service.screening_validation import certify_history
            equity = kwargs["kind"] == "EQUITY"
            return certify_history(DailyHistory(symbol, target_session, kwargs["input_mode"], {"metadata": {"kind": kwargs["kind"],
                "provider": "vnstock", "provider_version": "5.0.1", "source": "vci" if equity else "kbs",
                "price_unit": "VND" if equity else "POINTS", "source_price_unit": "THOUSAND_VND" if equity else "POINTS", "volume_unit": "SHARES",
                "target_session": target_session.isoformat(), "input_mode": kwargs["input_mode"],
                "requested_start": start.isoformat(), "provenance": "LIVE_UNVERIFIED", "observed_at": "2026-10-15T09:01:00+00:00"},
                "bars": [{"session": target_session.isoformat(), "open": 10000, "high": 10100, "low": 9900, "close": 10000, "volume": 100000}]}))

    monkeypatch.setattr(ingestion, "VnstockProvider", Provider)
    first = ingestion.ingest_market(settings, start, target, resume=True)
    assert first["manifest_id"] is None and first["retry_after"] == 600
    assert calls == ["VNINDEX", "AAA", "BBB"]
    second = ingestion.ingest_market(settings, start, target, resume=True)
    assert second["manifest_id"] is None and second["histories_available"] == 4
    assert calls == ["VNINDEX", "AAA", "BBB", "BBB", "CCC", "DDD"]
    third = ingestion.ingest_market(settings, start, target, resume=True)
    assert third["manifest_id"] and third["histories_available"] == 5
    assert calls == ["VNINDEX", "AAA", "BBB", "BBB", "CCC", "DDD", "CCC"]
    fourth = ingestion.ingest_market(settings, start, target, resume=True)
    assert fourth["manifest_id"] == third["manifest_id"] and len(calls) == 7
    guarded = []

    def yield_after_index(db):
        guarded.append(True)
        if len(guarded) >= 6:
            raise DataError("BACKFILL_YIELD_TO_SCHEDULE", True)

    with pytest.raises(DataError, match="BACKFILL_YIELD_TO_SCHEDULE"):
        ingestion.ingest_market(settings, start, target, operation_guard=yield_after_index)
    assert calls[7:] == ["VNINDEX"]  # Stop at the boundary, before fetching equities.
    engine.dispose()
