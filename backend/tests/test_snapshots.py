import service
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select, update
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from alembic import command
from alembic.config import Config

from service import snapshots
from service.db import make_engine, session_factory
from service.market_data import DataError, checksum, normalize_history, normalize_universe, publish_manifest, save_history, save_universe
from service.models import DataManifest, MarketSnapshot, PublishedSnapshot
from service.models import Account, AnalysisReport, DependencyRequest, DependencyWaiter, Inbox, Job, JobAttempt, Outbox, QuotaBucket, Reservation
from service.api import create_app
from service.analysis_worker import AnalysisWorker, Envelope, wake_waiters
from service.broker import BrokerUnavailable, RabbitTransport, dispatch_once
from service.settings import Settings
from service.quant import engines as e
from service.quant.assembly import finalize_member
from service.quant.configuration import QuantConfig, engine_context
from service.report_assembly import assemble_report


def test_snapshot_reference_cache_report_invariance_and_publication(tmp_path, monkeypatch, request):
    url = os.environ.get("QUANTIK_CHECK_SNAPSHOT_URL") or "sqlite:///" + (tmp_path / "snapshots.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    sessions = session_factory(engine)
    config = replace(QuantConfig(), PRICE_INPUT_UNIT="VND")
    days = pd.bdate_range(end="2026-10-14", periods=160)
    n = np.arange(160)
    frames = {}
    histories = {}
    with sessions() as db, db.begin():
        universe = save_universe(db, normalize_universe(pd.DataFrame({"symbol": [f"S{i:02}" for i in range(6)],
            "exchange": ["HOSE"] * 6, "icb_code_lv2": ["sector"] * 6})))
        for i in range(6):
            symbol = f"S{i:02}"
            price = (80+i*10)*np.exp(.001*n+.02*np.sin(n/12+i))
            raw = pd.DataFrame({"time": days, "open": price*.999, "high": price*1.02,
                "low": price*.98, "close": price, "volume": np.full(160, 2000000)})
            history = normalize_history(raw, symbol=symbol, kind="EQUITY", source="vci",
                target_session=days[-1].date(), observed_at=datetime(2026, 10, 15, tzinfo=timezone.utc))
            histories[symbol] = save_history(db, history)
            frames[symbol] = history.frame()
        index_raw = raw.copy()
        index_raw[["open", "high", "low", "close"]] *= 10
        histories["VNINDEX"] = save_history(db, normalize_history(index_raw, symbol="VNINDEX", kind="INDEX",
            source="kbs", target_session=days[-1].date(), observed_at=datetime(2026, 10, 15, tzinfo=timezone.utc)))
        manifest = publish_manifest(db, universe, histories, target_session=days[-1].date(), input_mode="COMPLETED_DAILY")
        manifest_id = manifest.id
    calls = []

    def baseline_fixture(symbol, frame, **kwargs):
        calls.append(symbol)
        with engine_context(config):
            return {"symbol": symbol, "exchange": "HOSE", "n": len(frame), "vni_regime": "BULL",
                "_price_dates": [stamp.isoformat() for stamp in frame.index], "_price_close": frame.close.tolist(),
                "data_quality": {"status": "PASS", "score": 100, "flags": []},
                "liquidity": e.LiquidityEngine.analyze(frame), "stats": e.StatEngine.returns(frame.close),
                "vol": e.StatEngine.vol_regime(frame.close), "ac": e.StatEngine.autocorr(frame.close),
                "dist": {"verdict": {"is_gaussian": True}}, "arima": {}, "garch": {},
                "hmm": {"current": "BULL", "prob_pct": 90, "state_probs": {"BULL": 90, "BEAR": 0, "SIDEWAY": 10}},
                "alpha": e.AlphaEngine.extract(frame, kwargs["index_frame"]),
                "sr": e.StructureEngine.sr_levels(frame), "trend": e.StructureEngine.trend(frame),
                "flow": e.StructureEngine.flow(frame), "hurst": e.HurstRegimeEngine.analyze(frame.close),
                "momentum_alpha": e.MomentumAlphaEngine.analyze(frame.close),
                "simulation_risk": e.FcastEngine.ensemble(frame.close, symbol=symbol),
                "kelly": {"disabled": True}, "meta_label": {"disabled": True}, "component_errors": {}}

    monkeypatch.setattr(snapshots, "compute_baseline", baseline_fixture)
    snapshot = snapshots.build_market_snapshot(sessions, manifest_id, config)
    assert snapshot.status == "READY" and len(calls) == 6
    assert snapshots.build_market_snapshot(sessions, manifest_id, config).id == snapshot.id
    assert len(calls) == 6
    payload = snapshot.payload
    assert payload["reference"]["symbols"] == sorted(frames)
    assert "_mc_paths" not in payload["members"]["S00"]["simulation_risk"]
    single = finalize_member(payload["members"]["S00"], frames["S00"], payload["reference"], config)
    batch = {s: finalize_member(payload["members"][s], frames[s], payload["reference"], config) for s in ["S02", "S00"]}
    assert single["rec"] == batch["S00"]["rec"] and single["action"] == batch["S00"]["action"]
    assert not single["action"]["executable"] and "MODEL_UNAVAILABLE" in single["action"]["reason_codes"]
    evaluation = datetime.now(timezone.utc)
    report = assemble_report(single, job_id="fixture-job", evaluated_at=evaluation,
        data_as_of=datetime(2026, 10, 14, 9, tzinfo=timezone.utc), config=config,
        versions={"data_manifest": manifest_id, "market_snapshot": snapshot.id, "model": None,
            "config": checksum(asdict(config)), "engines": {"quant": snapshots.ENGINE_VERSION}},
        warnings=payload["warnings"])
    assert len(report["sections"]) == 16 and report["commentary"]["detailed"]
    assert report["sections"]["arima"]["status"] == "UNAVAILABLE"
    assert report["sections"]["trade_plan"]["metrics"]["sizing_mode"] == "REFERENCE"
    pointer_name = "completed:2026-10-14:" + checksum(asdict(config))
    # Correction creates a new manifest/snapshot; a retried old builder cannot replace its pointer.
    with sessions() as db, db.begin():
        old = db.get(DataManifest, manifest_id)
        corrected_index = normalize_history(index_raw.assign(close=index_raw.close*.999), symbol="VNINDEX", kind="INDEX",
            source="kbs", target_session=days[-1].date(), observed_at=datetime(2026, 10, 15, 1, tzinfo=timezone.utc))
        histories["VNINDEX"] = save_history(db, corrected_index)
        newer = publish_manifest(db, universe, histories, target_session=days[-1].date(), input_mode="COMPLETED_DAILY")
        assert newer.revision_order > old.revision_order
        newer_id = newer.id
    corrected = snapshots.build_market_snapshot(sessions, newer_id, config)
    with sessions() as db, db.begin():
        db.get(MarketSnapshot, snapshot.id).status = "FAILED"
    snapshots.build_market_snapshot(sessions, manifest_id, config)
    with sessions() as db:
        assert db.get(PublishedSnapshot, pointer_name).snapshot_id == corrected.id
    # A build failure leaves the last published snapshot intact.
    with sessions() as db, db.begin():
        third_index = normalize_history(index_raw.assign(close=index_raw.close*1.001), symbol="VNINDEX", kind="INDEX",
            source="kbs", target_session=days[-1].date(), observed_at=datetime(2026, 10, 15, 2, tzinfo=timezone.utc))
        histories["VNINDEX"] = save_history(db, third_index)
        third = publish_manifest(db, universe, histories, target_session=days[-1].date(), input_mode="COMPLETED_DAILY")
        third_id = third.id
    def incomplete_baseline(symbol, frame, **kwargs):
        return {"error": "FIXTURE_FAILURE"} if symbol in {"S00", "S01"} else baseline_fixture(symbol, frame, **kwargs)
    monkeypatch.setattr(snapshots, "compute_baseline", incomplete_baseline)
    with pytest.raises(DataError) as error:
        snapshots.build_market_snapshot(sessions, third_id, config)
    assert error.value.code == "SNAPSHOT_COVERAGE_NOT_READY"
    with sessions() as db:
        assert db.get(PublishedSnapshot, pointer_name).snapshot_id == corrected.id

    # Complete customer flow using this snapshot; fitting is a fixture, transport confirms are injected.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test", jwt_private_key_path=private,
        jwt_public_key_path=public, allow_fixture_analysis=True, allow_degraded_analysis=True)
    worker = AnalysisWorker(sessions, settings)
    import time
    with sessions() as db, db.begin():
        account = Account(google_sub="worker-test", role="user")
        db.add(account)
        db.flush()
        account_id = account.id
        bucket = QuotaBucket(account_id=account_id, valid_from=int(time.time())-10,
            valid_until=int(time.time())+3600, quota_total=5, active_limit=2, entitlement_version="fixture")
        db.add(bucket)
        db.flush()
        bucket_id = bucket.id

    class ConfirmedTransport:
        fail = True
        events = []
        def publish(self, envelope):
            self.events.append(Envelope.decode(envelope.encode()))
            if self.fail:
                raise BrokerUnavailable("SIMULATED_CONFIRM_LOSS")

    transport = ConfirmedTransport()
    rabbit = None
    if os.environ.get("QUANTIK_CHECK_AMQP_URL"):
        from pydantic import SecretStr
        real_settings = settings.model_copy(update={"amqp_url": SecretStr(os.environ["QUANTIK_CHECK_AMQP_URL"])})
        from uuid import uuid4
        rabbit = RabbitTransport(real_settings, "quantik_snapshot_check_"+uuid4().hex)
        original_publish = transport.publish
        def real_publish(envelope):
            rabbit.publish(envelope)
            original_publish(envelope)
        transport.publish = real_publish
        def cleanup_broker():
            if rabbit.connection.is_open:
                rabbit.channel.queue_delete(queue=rabbit.queue)
                rabbit.channel.queue_delete(queue=rabbit.queue+".dead")
                rabbit.channel.exchange_delete(exchange=rabbit.namespace)
                rabbit.channel.exchange_delete(exchange=rabbit.namespace+".dead")
                rabbit.close()
        request.addfinalizer(cleanup_broker)
    with TestClient(create_app(settings)) as client:
        with sessions() as db, db.begin():
            access, _ = client.app.state.identity.new_session(db, db.get(Account, account_id))
        headers = {"Authorization": "Bearer " + access}
        body = {"symbol": "S00", "target_session": "2026-10-14"}
        first = client.post("/api/v1/analyses", json=body, headers=headers | {"Idempotency-Key": "worker-flow"})
        assert first.status_code == 202
        job_id = first.json()["id"]
        assert client.post("/api/v1/analyses", json=body,
            headers=headers | {"Idempotency-Key": "worker-flow"}).json()["id"] == job_id
        with pytest.raises(BrokerUnavailable):
            dispatch_once(sessions, transport)
        with sessions() as db:
            assert db.scalar(select(Outbox).where(Outbox.job_id == job_id)).published_at is None
        transport.fail = False
        assert dispatch_once(sessions, transport) == 1
        if rabbit:
            rabbit.consume(worker, once=True)
            rabbit.consume(worker, once=True)
            assert rabbit.channel.queue_declare(queue=rabbit.queue, passive=True).method.message_count == 0
        else:
            assert worker.process(transport.events[0]) == "SUCCEEDED"
            assert worker.process(transport.events[1]) == "DUPLICATE"
        status = client.get(f"/api/v1/analyses/{job_id}", headers=headers).json()
        assert status["status"] == "SUCCEEDED" and status["quota_effect"] == "CONSUMED"
        report = client.get("/api/v1/reports/" + status["report_id"], headers=headers)
        assert report.status_code == 200 and len(report.json()["sections"]) == 16
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(AnalysisReport)) == 1
            assert db.scalar(select(func.count()).select_from(Inbox)) == 1
            assert db.get(QuotaBucket, bucket_id).consumed == 1
        # Strict defaults wait for live data/model and do not start customer compute.
        waiting = client.post("/api/v1/analyses", json=body,
            headers=headers | {"Idempotency-Key": "strict-policy"}).json()["id"]
        strict = settings.model_copy(update={"allow_fixture_analysis": False, "allow_degraded_analysis": False})
        before = len(transport.events)
        dispatch_once(sessions, transport)
        assert AnalysisWorker(sessions, strict).process(transport.events[before]) == "WAITING_DATA"
        with sessions() as db:
            assert db.get(Job, waiting).compute_started_at is None
        assert wake_waiters(sessions, strict) == 0
        assert wake_waiters(sessions, settings) == 1
        before = len(transport.events)
        dispatch_once(sessions, transport)
        assert worker.process(transport.events[before]) == "SUCCEEDED"
        # Different symbol requests for the same market context share a dependency key.
        waiting_ids = []
        for symbol in ["S00", "S01"]:
            result = client.post("/api/v1/analyses", json={"symbol": symbol, "target_session": "2026-10-13"},
                headers=headers | {"Idempotency-Key": "missing-" + symbol})
            assert result.status_code == 202
            waiting_ids.append(result.json()["id"])
        before = len(transport.events)
        dispatch_once(sessions, transport)
        for event in transport.events[before:]:
            assert worker.process(event) == "WAITING_DATA"
        with sessions() as db:
            keys = list(db.scalars(select(DependencyWaiter.dependency_key).where(DependencyWaiter.job_id.in_(waiting_ids))))
            assert len(keys) == 2 and len(set(keys)) == 1
        for pending_id in waiting_ids:
            assert client.post(f"/api/v1/analyses/{pending_id}/cancel", headers=headers).json()["quota_effect"] == "RELEASED"
        # Security revocation after compute starts fences completion and releases the reservation.
        last = client.post("/api/v1/analyses", json=body,
            headers=headers | {"Idempotency-Key": "disabled-during-compute"}).json()["id"]
        from service import analysis_worker
        real_finalize = analysis_worker.finalize_member
        def disable_during_compute(*args, **kwargs):
            result = real_finalize(*args, **kwargs)
            with sessions() as db, db.begin():
                db.execute(update(Account).where(Account.id == account_id).values(enabled=False))
            return result
        monkeypatch.setattr(analysis_worker, "finalize_member", disable_during_compute)
        before = len(transport.events)
        dispatch_once(sessions, transport)
        assert worker.process(transport.events[before]) == "STOPPED"
        with sessions() as db:
            final_job = db.get(Job, last)
            assert final_job.status == "FAILED" and final_job.reason == "ACCOUNT_DISABLED"
            assert final_job.compute_started_at is not None
            quota = db.get(QuotaBucket, bucket_id)
            assert (quota.consumed, quota.reserved, quota.active_jobs) == (2, 0, 0)
            assert all(a.stopped_at is not None for a in db.scalars(select(JobAttempt)))
    engine.dispose()
