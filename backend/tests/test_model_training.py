import service
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from alembic import command
from alembic.config import Config

from service.db import make_engine, session_factory
from service.market_data import DataError, normalize_history, normalize_universe, publish_manifest, save_history, save_universe
from service.model_lifecycle import promote_model, save_candidate
from service.models import PublishedModel
from service.quant.configuration import QuantConfig
from service.quant.training import predict_snapshot, train_artifact


def test_temporal_training_artifact_and_fixture_publication_gate(tmp_path, monkeypatch):
    days = pd.bdate_range(end="2026-10-14", periods=400)
    n = np.arange(400)
    data = {}
    for i in range(6):
        prices = (80000 + i * 10000) * np.exp(.0004 * n + .04 * np.sin(n / 12 + i))
        data[f"S{i:02}"] = pd.DataFrame({"open": prices * .999, "high": prices * 1.015,
            "low": prices * .985, "close": prices, "volume": 1000000 + 100000 * np.sin(n / 8 + i)}, index=days)
    index = data["S00"].copy() / 100
    sector_map = {symbol: "sector-1" for symbol in data}
    config = replace(QuantConfig(), PRICE_INPUT_UNIT="VND")
    artifact = train_artifact(data, index, sector_map, config)
    validation = artifact["validation"]
    assert validation["train_label_end"] < validation["validation_start"]
    assert artifact["cutoff_session"] == "2026-10-14"
    assert validation["purge_sessions"] == config.FORECAST_HORIZON
    assert validation["embargo_sessions"] == config.FORECAST_HORIZON
    assert validation["metrics"]["rows"] == 240
    assert validation["metrics"]["rank_days"] > 0
    predictions = predict_snapshot(artifact, data, index, sector_map, config)
    assert len(predictions) == 6
    assert predictions == predict_snapshot(artifact, dict(reversed(list(data.items()))), index, sector_map, config)
    assert {p["n_ranked_symbols"] for p in predictions.values()} == {6}
    assert {p["as_of"] for p in predictions.values()} == {"2026-10-14"}
    stale = data | {"S00": data["S00"].iloc[:-1]}
    assert "S00" not in predict_snapshot(artifact, stale, index, sector_map, config)
    url = "sqlite:///" + (tmp_path / "model.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    sessions = session_factory(engine)
    with sessions() as db, db.begin():
        universe = save_universe(db, normalize_universe(pd.DataFrame({"symbol": list(data),
            "exchange": ["HOSE"] * 6, "icb_code_lv2": ["sector-1"] * 6})))
        histories = {}
        for symbol, frame in data.items():
            raw = frame.reset_index(names="time")
            raw[["open", "high", "low", "close"]] /= 1000
            raw["volume"] = raw.volume.round()
            history = normalize_history(raw, symbol=symbol, kind="EQUITY", source="vci",
                target_session=days[-1].date(), observed_at=datetime(2026, 10, 15, tzinfo=timezone.utc))
            histories[symbol] = save_history(db, history)
        raw_index = index.reset_index(names="time")
        raw_index["volume"] = raw_index.volume.round()
        histories["VNINDEX"] = save_history(db, normalize_history(raw_index, symbol="VNINDEX", kind="INDEX",
            source="kbs", target_session=days[-1].date(), observed_at=datetime(2026, 10, 15, tzinfo=timezone.utc)))
        manifest = publish_manifest(db, universe, histories, target_session=days[-1].date(), input_mode="COMPLETED_DAILY")
        candidate = save_candidate(db, manifest, artifact, config)
        assert not candidate.evidence["live_data_validated"]
        assert save_candidate(db, manifest, artifact, config).id == candidate.id
        with pytest.raises(DataError) as error:
            promote_model(db, candidate.id)
        assert error.value.code in {"MODEL_LIVE_DATA_UNVERIFIED", "MODEL_VALIDATION_REJECTED"}
        assert db.get(PublishedModel, "weekly-lightgbm") is None
    engine.dispose()
