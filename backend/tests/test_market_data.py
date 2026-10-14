from datetime import date, datetime, timezone

import pandas as pd
import pytest
from alembic import command
from alembic.config import Config

from service.db import make_engine, session_factory
from service.market_data import DataError, normalize_history, normalize_universe, publish_manifest, save_history, save_universe
from service.vnstock_provider import reserve_provider_call


def test_history_units_readiness_revisions_and_shared_budget(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "market.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    sessions = session_factory(engine)
    frame = pd.DataFrame({"time": pd.date_range("2026-10-12", periods=3),
        "open": [100, 101, 102], "high": [102, 103, 104], "low": [99, 100, 101],
        "close": [101, 102, 103], "volume": [1000, 2000, 3000]})
    args = {"symbol": "FPT", "kind": "EQUITY", "source": "vci", "target_session": date(2026, 10, 14),
        "observed_at": datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc), "minimum_bars": 3}
    equity = normalize_history(frame, **args)
    assert equity.frame().close.iloc[-1] == 103000
    index = normalize_history(frame, **(args | {"symbol": "VNINDEX", "kind": "INDEX", "source": "kbs"}))
    assert index.frame().close.iloc[-1] == 103
    with pytest.raises(DataError) as error:
        normalize_history(frame, **(args | {"observed_at": datetime(2026, 10, 14, 5, tzinfo=timezone.utc)}))
    assert error.value.code == "TARGET_SESSION_NOT_READY"
    provisional = normalize_history(frame, **(args | {"observed_at": datetime(2026, 10, 14, 5, tzinfo=timezone.utc),
        "input_mode": "PROVISIONAL_DAILY"}))
    assert provisional.payload["metadata"]["input_mode"] == "PROVISIONAL_DAILY"
    for mutate, code in [
        (lambda f: f.assign(volume=[1000, -1, 3000]), "INVALID_PRICE_OR_VOLUME"),
        (lambda f: f.assign(close=[101, float("nan"), 103]), "NONFINITE_BARS"),
        (lambda f: f.assign(high=[90, 103, 104]), "INVALID_OHLC"),
        (lambda f: pd.concat([f, f.tail(1)]), "DUPLICATE_OR_INVALID_SESSIONS"),
    ]:
        with pytest.raises(DataError) as error:
            normalize_history(mutate(frame), **args)
        assert error.value.code == code
    capped = frame.copy()
    capped.attrs["tier_cap_history"] = 120
    with pytest.raises(DataError) as error:
        normalize_history(capped, **args)
    assert error.value.code == "PROVIDER_HISTORY_CAPPED"
    universe = normalize_universe(pd.DataFrame({"symbol": ["FPT", "VNM"], "exchange": ["HOSE", "HOSE"]}))
    with sessions() as db, db.begin():
        reference = save_universe(db, universe)
        first = save_history(db, equity)
        assert save_history(db, equity).id == first.id
        corrected = normalize_history(frame.assign(close=[101, 102, 103.5]), **args)
        revision = save_history(db, corrected)
        assert revision.id != first.id
        assert first.payload["bars"][-1]["close"] == 103000
        index_record = save_history(db, index)
        refs = {"FPT": first, "VNINDEX": index_record}
        with pytest.raises(DataError) as error:
            publish_manifest(db, reference, refs, target_session=args["target_session"], input_mode="COMPLETED_DAILY")
        assert error.value.code == "MANIFEST_COVERAGE_NOT_READY"
        manifest = publish_manifest(db, reference, refs, target_session=args["target_session"],
            input_mode="COMPLETED_DAILY", minimum_coverage_ratio=0.5)
        assert manifest.coverage["excluded"] == ["VNM"]
        assert manifest.histories["FPT"] == first.id
        assert publish_manifest(db, reference, refs, target_session=args["target_session"],
            input_mode="COMPLETED_DAILY", minimum_coverage_ratio=0.5).id == manifest.id
        with pytest.raises(DataError) as error:
            publish_manifest(db, reference, refs | {"FPT": save_history(db, provisional)},
                target_session=args["target_session"], input_mode="COMPLETED_DAILY", minimum_coverage_ratio=0.5)
        assert error.value.code == "MIXED_MANIFEST_VERSIONS"
    limits = {"per_minute": 1, "per_hour": 10, "per_day": 20, "per_month": 100}
    with sessions() as db, db.begin():
        reserve_provider_call(db, limits, now=1000)
    with sessions() as db, db.begin():
        with pytest.raises(DataError) as error:
            reserve_provider_call(db, limits, now=1001)
        assert error.value.code == "PROVIDER_BUDGET_EXHAUSTED" and error.value.retry_after == 59
    with sessions() as db, db.begin():
        reserve_provider_call(db, limits, now=1060)
    engine.dispose()
