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
from service.models import Account, PublishedScreening, ScreeningResult, PlanVersion, PurchaseOrder, BankPayment, Subscription, QuotaBucket
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

    def manifest_for(day, mode, observed, certified=False, omit=()):
        histories = {}
        with sessions() as db, db.begin():
            reference = universe
            if certified:
                from service.screening_validation import certify_universe, certify_history
                reference = save_universe(db, certify_universe({**universe.payload, "provenance": "LIVE_UNVERIFIED"}))
            for symbol in [*symbols, "VNINDEX"]:
                if symbol in omit:
                    continue
                frame = prices(day, n=10 if symbol == "NEW" else 80, volume=100 if symbol == "LOW" else 200000)
                # LOW must remain nonnegative while failing the liquidity hard filter.
                if symbol == "LOW":
                    frame["volume"] = 100
                frame["volume"] = frame.volume.round()
                history = normalize_history(frame.reset_index(names="time"), symbol=symbol,
                    kind="INDEX" if symbol == "VNINDEX" else "EQUITY", source="kbs" if symbol == "VNINDEX" else "vci",
                    target_session=pd.Timestamp(day).date(), observed_at=observed, input_mode=mode, minimum_bars=1,
                    provenance="LIVE_UNVERIFIED" if certified else "FIXTURE")
                if certified:
                    history = certify_history(history)
                histories[symbol] = save_history(db, history)
            manifest = publish_manifest(db, reference, histories, target_session=pd.Timestamp(day).date(), input_mode=mode)
            return manifest.id, histories

    observed = datetime(2026, 10, 14, 16, tzinfo=VN)
    manifest_id, histories = manifest_for("2026-10-14", "COMPLETED_DAILY", observed, omit={"LOW"})
    first = run_screening(sessions, observed.date(), "PM", manifest_id=manifest_id,
        now=datetime(2026, 10, 14, 16, 5, tzinfo=VN), allow_fixture=True)
    assert first.status == "READY", first.reason
    assert first.summary["top_symbols"] == ["AAA", "BBB"]
    assert first.summary["expected_symbols"] == 4
    assert first.summary["ready_symbols"] == 3 and first.summary["history_coverage_ratio"] == .75
    assert first.summary["unavailable"]["LOW"] == "HISTORY_NOT_READY"
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
        assert client.get("/api/v1/screenings/latest", headers=headers).status_code == 403
        assert client.get(f"/api/v1/screenings/{morning.id}/results", headers=headers).status_code == 403
        import time
        from service import billing
        with sessions() as db, db.begin():
            now = int(time.time())
            plan = PlanVersion(name="Screening test", price_vnd=10000, duration_days=30, analysis_quota=20,
                active_limit=1, features={"screening": True, "analysis": True}, available=True, created_at=now)
            db.add(plan); db.flush()
            order = PurchaseOrder(account_id=account.id, plan_id=plan.id, idempotency_key="screen", reference="QTKSCREEN",
                amount_vnd=10000, bank_code="TPBANK", bank_account="111", account_name="Test", transfer_content="QTKSCREEN", created_at=now, expires_at=now+600)
            db.add(order); db.flush()
            payment = BankPayment(id="screen-payment", fingerprint="f"*64, order_id=order.id, bank_code="TPBANK", bank_account="111", direction="in", amount_vnd=10000,
                transaction_at=now, received_at=now, references=["QTKSCREEN"], status="RECONCILIATION")
            db.add(payment); db.flush()
            billing.grant(db, payment, order, now)
        latest = client.get("/api/v1/screenings/latest", headers=headers)
        assert latest.status_code == 200 and latest.json()["id"] == morning.id
        assert latest.json()["stale"]
        assert not {"latest_attempt", "manifest_id", "summary", "schedule", "reason"} & latest.json().keys()
        base = f"/api/v1/screenings/{morning.id}"
        page = client.get(base+"/results?group=OVERALL", headers=headers).json()
        assert [r["symbol"] for r in page["items"]] == ["AAA", "BBB"]
        assert page["group"] == "OVERALL" and "total_pages" not in page
        assert page["items"][0]["groups"]["STRONG"]["total_count"] == 7
        assert "criteria" not in str(page) and "manifest_id" not in str(page)
        assert client.get(base+"/results?group=ALL", headers=headers).status_code == 422
        assert client.get(base+"/symbols/NEW", headers=headers).status_code == 404
        detail = client.get(base+"/symbols/aaa", headers=headers)
        assert detail.status_code == 200, detail.text
        detail = detail.json()
        assert detail["run_id"] == morning.id and detail["item"]["symbol"] == "AAA"
        assert detail["assessment"]["investor_note"]
        assert detail["market"]["change_5d_ratio"] == pytest.approx(morning.summary["market_context"]["detail"]["trend"]["pct_change_5d"]/100)
        assert not {"series", "screening_adjustments", "criteria", "input_fields", "manifest_id"} & detail["market"].keys()
        assert client.get(base+"/symbols/NOPE", headers=headers).status_code == 404
        assert client.get(base+"/symbols/AAA").status_code == 401
        assert client.get(f"/api/v1/screenings/{failed.id}/symbols/AAA", headers=headers).status_code == 409
        with sessions() as db, db.begin():
            from copy import deepcopy
            seed = db.get(ScreeningResult, (morning.id, "AAA"))
            seed.distribution_rank = 7
            db.get(ScreeningResult, (morning.id, "BBB")).distribution_rank = 8
            for rank in range(3, 79):
                payload = deepcopy(seed.payload)
                payload["symbol"] = f"CAP{rank:03}"
                # Best and distribution selections must follow their independent persisted ranks.
                payload["groups"]["STRONG"]["score"] = rank
                db.add(ScreeningResult(run_id=morning.id, symbol=payload["symbol"],
                    overall_rank=rank, strong_rank=None, accumulation_rank=None,
                    distribution_rank=9-rank if rank <= 8 else rank, payload=payload))
        best = client.get(base+"/results?group=OVERALL&page_size=100&page=2&cursor=5", headers=headers).json()
        worst = client.get(base+"/results?group=DISTRIBUTION", headers=headers).json()
        assert [r["symbol"] for r in best["items"]] == ["AAA", "BBB", "CAP003", "CAP004", "CAP005"]
        assert [r["symbol"] for r in worst["items"]] == ["CAP008", "CAP007", "CAP006", "CAP005", "CAP004"]
        assert "CAP078" not in str(best) + str(worst)
        assert client.get(base+"/symbols/CAP008", headers=headers).status_code == 200
        assert client.get(base+"/symbols/CAP078", headers=headers).status_code == 404
        assert client.get("/api/v1/instruments?limit=2", headers=headers).json()["next_cursor"] == "BBB"
        assert client.get("/api/v1/screenings/latest").status_code == 401
        # The automatic screening contract accepts normalized live inputs without
        # promoting their provenance/adjustment for Quant or model publication.
        live_manifest, _ = manifest_for("2026-10-16", "PROVISIONAL_DAILY", datetime(2026, 10, 16, 11, 45, tzinfo=VN), certified=True)
        live = run_screening(sessions, pd.Timestamp("2026-10-16").date(), "AM", manifest_id=live_manifest,
            now=datetime(2026, 10, 16, 12, 5, tzinfo=VN))
        assert live.status == "READY", live.reason
        assert live.summary["source_mode"] == "LIVE_SCREENING_VALIDATED"
        settings.allow_fixture_analysis = False
        assert client.get("/api/v1/screenings/latest", headers=headers).json()["id"] == live.id
        live_results = client.get(f"/api/v1/screenings/{live.id}/results?group=OVERALL", headers=headers)
        assert live_results.status_code == 200 and live_results.json()["items"]
        with sessions() as db, db.begin():
            bucket = db.scalar(select(QuotaBucket).where(QuotaBucket.account_id == account.id))
            assert bucket.daily_created == bucket.consumed == bucket.reserved == 0
            period = db.scalar(select(Subscription).where(Subscription.account_id == account.id))
            period.valid_until = now - 1
        assert client.get(f"/api/v1/screenings/{morning.id}/results", headers=headers).status_code == 403
        assert client.get(f"/api/v1/screenings/{morning.id}/symbols/AAA", headers=headers).status_code == 403
    engine.dispose()


def test_screening_contract_evidence_binds_content_without_promoting_quant():
    from copy import deepcopy
    from service.market_data import DailyHistory
    from service.screening_validation import certify_history, certify_universe, eligible
    day = pd.Timestamp("2026-10-15").date()
    h = normalize_history(prices(day).round().reset_index(names="time"), symbol="FPT", kind="EQUITY", source="vci",
        target_session=day, observed_at=datetime(2026, 10, 15, 16, 10, tzinfo=VN), minimum_bars=50, provenance="LIVE_UNVERIFIED")
    assert not eligible(h.payload)
    certified = certify_history(h)
    assert eligible(certified.payload)
    assert certified.payload["metadata"]["provenance"] == "LIVE_UNVERIFIED"
    assert certified.payload["metadata"]["adjustment"] != "VERIFIED"
    corrupt = deepcopy(certified.payload); corrupt["bars"][-1]["close"] += 1
    assert not eligible(corrupt)
    bad_units = deepcopy(h.payload); bad_units["metadata"]["source_price_unit"] = "VND"
    with pytest.raises(DataError):
        certify_history(DailyHistory("FPT", day, "COMPLETED_DAILY", bad_units))
    fixture = normalize_universe(pd.DataFrame({"symbol": ["FPT"], "exchange": ["HOSE"]}))
    with pytest.raises(DataError):
        certify_universe(fixture)
