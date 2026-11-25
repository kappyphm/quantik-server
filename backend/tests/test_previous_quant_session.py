from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from service import analysis_readiness as readiness
from service.models import Base, DataManifest, UniverseVersion
from service.operational_config import activate, create_version
from service.settings import Settings


def test_previous_session_requires_completed_data_for_requested_symbol(monkeypatch):
    sessions = [date(2026, 10, n) for n in (12, 13, 14, 15, 16)]
    monkeypatch.setattr(readiness, "completed_sessions", lambda db: sessions)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, environment="test", database_url="sqlite://",
        allow_fixture_analysis=True, allow_degraded_analysis=True)
    with Session(engine) as db:
        universe = UniverseVersion(checksum="1"*64, payload={"members": {"PVS": {}, "FPT": {}}}, created_at=1)
        db.add(universe)
        db.flush()
        policy = create_version(db, "OPERATIONS", {"snapshots_enabled": True}, None)
        activate(db, policy.id, None, "Test snapshot processing enabled", None)
        for index, (day, symbol, mode) in enumerate([
                (sessions[0], "PVS", "COMPLETED_DAILY"), (sessions[1], "PVS", "COMPLETED_DAILY"),
                (sessions[2], "FPT", "COMPLETED_DAILY"), (sessions[3], "PVS", "PROVISIONAL_DAILY")]):
            db.add(DataManifest(universe_id=universe.id, target_session=day, input_mode=mode,
                checksum=str(index)*64, histories={symbol: "fixture"}, coverage={}, created_at=index))
        db.flush()
        assert readiness.options(db, settings, sessions[-1])["reason"] == "DATA_NOT_READY"
        assert readiness.previous_ready_session(db, settings, sessions[-1], "PVS") == sessions[1]
        assert readiness.previous_ready_session(db, settings, sessions[-1], "ABB") is None
        assert readiness.previous_ready_session(db, settings, sessions[0], "PVS") is None
