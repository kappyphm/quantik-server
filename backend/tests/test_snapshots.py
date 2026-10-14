import service
from dataclasses import asdict, replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from alembic import command
from alembic.config import Config

from service import snapshots
from service.db import make_engine, session_factory
from service.market_data import DataError, checksum, normalize_history, normalize_universe, publish_manifest, save_history, save_universe
from service.models import DataManifest, MarketSnapshot, PublishedSnapshot
from service.quant import engines as e
from service.quant.assembly import finalize_member
from service.quant.configuration import QuantConfig, engine_context
from service.report_assembly import assemble_report


def test_snapshot_reference_cache_report_invariance_and_publication(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "snapshots.sqlite").as_posix()
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
    engine.dispose()
