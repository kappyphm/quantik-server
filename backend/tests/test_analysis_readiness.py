"""Readiness and dependency fencing; all data/model evidence here is synthetic."""
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from entitlement_fixtures import analysis_calendar, paid_bucket
from service import dependencies, jobs
from service.analysis_readiness import DEFAULT_CONFIG, model_for_session, options, ready_inputs
from service.analysis_worker import register_dependency
from service.market_data import DataError
from service.models import Account, DataManifest, DependencyRequest, ModelVersion, UniverseVersion
from service.operational_config import activate, create_version
from service.settings import Settings
from test_scheduling import database


def test_readiness_model_history_and_dependency_restart(tmp_path, monkeypatch):
    url, engine, sessions = database(tmp_path, monkeypatch)
    settings = Settings(_env_file=None, database_url=url, environment="test")
    target = datetime.now().date()-timedelta(days=3)
    with sessions() as db, db.begin():
        analysis_calendar(db)
        config = create_version(db, "OPERATIONS", {}, None)
        activate(db, config.id, None, "Enable isolated dependency verification", None)
        owner = Account(role="user", google_sub="readiness")
        db.add(owner); db.flush()
        paid_bucket(db, owner.id)
        job = jobs.admit(db, owner.id, "FPT", target, "dependency-inputs")
        _, token = jobs.claim(db, job.id)
        jobs.wait_data(db, job.id, token, "SNAPSHOT_NOT_READY")
        register_dependency(db, job, "SNAPSHOT_NOT_READY")
        dependency = db.scalar(select(DependencyRequest))
        key = dependency.key

    from service import snapshots
    built = []

    def build(_sessions, manifest_id, config, operation_guard):
        with sessions() as db:
            operation_guard(db)
        built.append(manifest_id)
        return SimpleNamespace(status="READY", model_id="validated-model")

    monkeypatch.setattr(snapshots, "build_market_snapshot", build)
    assert dependencies.run_dependency_once(sessions, settings) == 1
    with sessions() as db, db.begin():
        request = db.get(DependencyRequest, key)
        assert request.status == "WAITING" and request.reason == "DATA_NOT_READY" and not built
        universe = UniverseVersion(checksum="u"*64, payload={"members": {"FPT": {"sector": "10"}}, "provenance": "LIVE_UNVERIFIED"}, created_at=0)
        db.add(universe); db.flush()
        manifest = DataManifest(checksum="m"*64, universe_id=universe.id, target_session=target,
            input_mode="COMPLETED_DAILY", histories={}, coverage={}, created_at=0, revision_order=1)
        db.add(manifest); db.flush(); manifest_id = manifest.id
        request.next_attempt_at = 0
    assert dependencies.run_dependency_once(sessions, settings) == 1
    with sessions() as db, db.begin():
        request = db.get(DependencyRequest, key)
        assert request.reason == "UNIVERSE_NOT_VALIDATED" and not built
        # Isolate model readiness from the separately tested provider validation.
        def model_inputs(db, target, config, settings):
            model_id = model_for_session(db, target, config)
            if not model_id:
                raise DataError("MODEL_NOT_READY", True)
            return db.get(DataManifest, manifest_id), model_id
        monkeypatch.setattr(dependencies, "ready_inputs", model_inputs)
        request.next_attempt_at = 0
    assert dependencies.run_dependency_once(sessions, settings) == 1
    with sessions() as db, db.begin():
        assert db.get(DependencyRequest, key).reason == "MODEL_NOT_READY" and not built
        older = ModelVersion(checksum="o"*64, manifest_id=manifest_id, cutoff_session=target-timedelta(days=1),
            created_at=0, published_at=1, status="PUBLISHED", config=asdict(DEFAULT_CONFIG), artifact={},
            evidence={"live_data_validated": True, "validation_accepted": True})
        future = ModelVersion(checksum="n"*64, manifest_id=manifest_id, cutoff_session=target+timedelta(days=1),
            created_at=0, published_at=2, status="PUBLISHED", config=asdict(DEFAULT_CONFIG), artifact={}, evidence=older.evidence)
        db.add_all([older, future]); db.flush()
        assert model_for_session(db, target, DEFAULT_CONFIG) == older.id
        request = db.get(DependencyRequest, key)
        request.status, request.execution_token, request.lease_until = "RUNNING", "dead-process", int(time.time())-1
    dependencies.reconcile_control_once(sessions, settings)
    with sessions() as db:
        assert db.get(DependencyRequest, key).status == "PENDING"
        with pytest.raises(DataError, match="DEPENDENCY_LEASE_LOST"):
            dependencies.current_dependency(db, key, "dead-process")
    assert dependencies.run_dependency_once(sessions, settings) == 1
    with sessions() as db, db.begin():
        request = db.get(DependencyRequest, key)
        assert request.status == "READY" and built == [manifest_id]
        request.status, request.execution_token, request.lease_until = "RUNNING", "heartbeat", int(time.time())+60
    dependencies.heartbeat(sessions, key, "heartbeat")
    with sessions() as db, db.begin():
        assert 100 <= db.get(DependencyRequest, key).lease_until-int(time.time()) <= 120
        paused = create_version(db, "OPERATIONS", {"enabled": False}, None)
        activate(db, paused.id, config.id, "Pause isolated dependency operation", None)
    with pytest.raises(DataError, match="DEPENDENCY_OPERATOR_PAUSED"):
        dependencies.heartbeat(sessions, key, "heartbeat")
    engine.dispose()
