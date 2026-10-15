import service
import ast
from datetime import datetime
from pathlib import Path
from typing import Dict
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from service.api import create_app
from service.db import make_engine, session_factory
from service.market_data import DataError, normalize_history, normalize_universe, publish_manifest, save_history, save_universe
from service.models import Account, PublishedScreening, ScreeningResult
from service.quant.screening import evaluate_symbol, rank_results
from service.screening import run_screening, validate_history
from service.settings import Settings

VN = ZoneInfo("Asia/Ho_Chi_Minh")


def prices(end="2026-10-14", n=80, volume=200000):
    x = np.arange(n)
    close = 90 + .03*x + 2*np.sin(x/2)
    return pd.DataFrame({"open": close*.999, "high": close*1.015, "low": close*.985,
        "close": close, "volume": volume + 10000*np.sin(x/3)}, index=pd.bdate_range(end=end, periods=n))


def test_21_criteria_parity_missing_inputs_and_provisional_volume():
    source = ast.parse(Path("docs/prototype/crawl_data.py").read_text(encoding="utf-8-sig"))
    nodes = [c for c in source.body if isinstance(c, ast.ClassDef) and c.name in {"FeatureEngine", "ScreeningEngine"}]
    namespace = {"pd": pd, "np": np, "Dict": Dict}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "screening-prototype-baseline", "exec"), namespace)
    frame, index = prices(), prices()*10
    features = namespace["FeatureEngine"].compute_features(frame.reset_index(names="time"))
    features = namespace["FeatureEngine"].compute_relative_strength(features, index.reset_index(names="time"))
    result = evaluate_symbol("AAA", frame, index)
    for group, method in [("STRONG", "screen_strong"), ("ACCUMULATION", "screen_accumulation"), ("DISTRIBUTION", "screen_distribution")]:
        original = getattr(namespace["ScreeningEngine"], method)(features)
        current = result["groups"][group]
        assert len(current["criteria"]) == 7 and current["unavailable_count"] == 0
        for key, expected in original["criteria"].items():
            assert current["criteria"][key]["state"] == ("TRUE" if expected else "FALSE"), key
        assert current["score"] == pytest.approx(original["met_count"]/7*100)
    short = evaluate_symbol("SHORT", frame.tail(15), index)
    assert short["groups"]["STRONG"]["criteria"]["S1_RS_market"]["state"] != "UNAVAILABLE"
    assert short["groups"]["ACCUMULATION"]["criteria"]["A6_long_accumulation"]["state"] == "UNAVAILABLE"
    assert short["groups"]["ACCUMULATION"]["score"] == short["groups"]["ACCUMULATION"]["true_count"]/7*100
    missing_index = evaluate_symbol("AAA", frame, index.drop(index.index[-11]))
    assert missing_index["groups"]["STRONG"]["criteria"]["S7_RS_10d"]["state"] == "UNAVAILABLE"
    assert not missing_index["ranking_eligible"]
    provisional = frame.copy()
    provisional["volume"] = 200000
    provisional.iloc[-1, provisional.columns.get_loc("volume")] = 2000000
    morning = evaluate_symbol("AAA", provisional, index, input_mode="PROVISIONAL_DAILY")
    assert morning["volume_context"]["baseline20_volume_shares"] == 200000
    assert morning["price_summary"]["vol_ratio_20"] == 10
    assert morning["volume_context"]["projected_volume"] is None
    assert evaluate_symbol("AAA", provisional.tail(20), index, input_mode="PROVISIONAL_DAILY")["groups"]["STRONG"]["criteria"]["S2_vol_spike"]["state"] == "UNAVAILABLE"
    empty = evaluate_symbol("EMPTY", None, index)
    assert sum(g["unavailable_count"] for g in empty["groups"].values()) == 21
    other = evaluate_symbol("BBB", frame, index)
    assert rank_results([other, result])["OVERALL"] == ["AAA", "BBB"]


def test_screening_publication_deadline_all_results_and_api(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path/"screening.sqlite").as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine, symbols = make_engine(url), ["AAA", "BBB", "LOW", "NEW"]
    sessions = session_factory(engine)
    with sessions() as db, db.begin():
        universe = save_universe(db, normalize_universe(pd.DataFrame({"symbol": symbols,
            "exchange": ["HOSE"]*4})))

    def manifest_for(day, mode, observed):
        histories = {}
        with sessions() as db, db.begin():
            for symbol in [*symbols, "VNINDEX"]:
                frame = prices(day, n=10 if symbol == "NEW" else 80, volume=100 if symbol == "LOW" else 200000)
                # LOW must remain nonnegative while failing the liquidity hard filter.
                if symbol == "LOW":
                    frame["volume"] = 100
                frame["volume"] = frame.volume.round()
                history = normalize_history(frame.reset_index(names="time"), symbol=symbol,
                    kind="INDEX" if symbol == "VNINDEX" else "EQUITY", source="kbs" if symbol == "VNINDEX" else "vci",
                    target_session=pd.Timestamp(day).date(), observed_at=observed, input_mode=mode, minimum_bars=1)
                histories[symbol] = save_history(db, history)
            manifest = publish_manifest(db, universe, histories, target_session=pd.Timestamp(day).date(), input_mode=mode)
            return manifest.id, histories

    observed = datetime(2026, 10, 14, 16, tzinfo=VN)
    manifest_id, histories = manifest_for("2026-10-14", "COMPLETED_DAILY", observed)
    first = run_screening(sessions, observed.date(), "PM", manifest_id=manifest_id,
        now=datetime(2026, 10, 14, 16, 5, tzinfo=VN), allow_fixture=True)
    assert first.status == "READY", first.reason
    assert first.summary["top_symbols"] == ["AAA", "BBB"]
    assert first.summary["expected_symbols"] == 4
    assert run_screening(sessions, observed.date(), "PM", manifest_id=manifest_id,
        now=datetime(2026, 10, 14, 16, 6, tzinfo=VN), allow_fixture=True).id == first.id
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ScreeningResult)) == 4
        assert not db.get(ScreeningResult, (first.id, "LOW")).payload["ranking_eligible"]
        assert db.get(ScreeningResult, (first.id, "NEW")).payload["groups"]["ACCUMULATION"]["unavailable_count"] > 0
    waiting = run_screening(sessions, pd.Timestamp("2026-10-15").date(), "AM",
        now=datetime(2026, 10, 15, 11, 31, tzinfo=VN), allow_fixture=True)
    assert waiting.status == "WAITING_DATA"
    morning_id, morning_histories = manifest_for("2026-10-15", "PROVISIONAL_DAILY", datetime(2026, 10, 15, 11, 45, tzinfo=VN))
    morning = run_screening(sessions, pd.Timestamp("2026-10-15").date(), "AM", manifest_id=morning_id,
        now=datetime(2026, 10, 15, 12, 5, tzinfo=VN), allow_fixture=True)
    assert morning.status == "READY" and morning.id == waiting.id, morning.reason
    failed = run_screening(sessions, pd.Timestamp("2026-10-15").date(), "PM",
        now=datetime(2026, 10, 15, 17, 31, tzinfo=VN), allow_fixture=True)
    assert failed.status == "FAILED" and failed.reason == "SCREENING_DEADLINE"
    with sessions() as db:
        assert db.get(PublishedScreening, "ALL").run_id == morning.id
        assert db.get(PublishedScreening, "PM").run_id == first.id
    with pytest.raises(DataError) as error:
        validate_history(morning_histories["AAA"], pd.Timestamp("2026-10-15").date(), "PROVISIONAL_DAILY", "AM",
            datetime(2026, 10, 15, 12, 5, tzinfo=VN), False)
    assert error.value.code == "LIVE_DATA_NOT_VALIDATED"
    private, public = tmp_path/"private.pem", tmp_path/"public.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test", jwt_private_key_path=private,
        jwt_public_key_path=public, allow_fixture_analysis=True)
    with TestClient(create_app(settings)) as client:
        with sessions() as db, db.begin():
            account = Account(google_sub="screening-fixture", role="user")
            db.add(account)
            db.flush()
            access, _ = client.app.state.identity.new_session(db, account)
        headers = {"Authorization": "Bearer "+access}
        latest = client.get("/api/v1/screenings/latest", headers=headers)
        assert latest.status_code == 200 and latest.json()["id"] == morning.id
        assert latest.json()["stale"] and latest.json()["latest_attempt"]["status"] == "FAILED"
        page = client.get(f"/api/v1/screenings/{morning.id}/results?limit=2", headers=headers).json()
        assert [r["symbol"] for r in page["items"]] == ["AAA", "BBB"]
        rest = client.get(f"/api/v1/screenings/{morning.id}/results", params={"cursor": page["next_cursor"]}, headers=headers).json()
        assert [r["symbol"] for r in rest["items"]] == ["LOW", "NEW"]
        assert len(client.get(f"/api/v1/screenings/{morning.id}/results?group=OVERALL", headers=headers).json()["items"]) == 2
        assert client.get("/api/v1/instruments?limit=2", headers=headers).json()["next_cursor"] == "BBB"
        assert client.get("/api/v1/screenings/latest").status_code == 401
    engine.dispose()
