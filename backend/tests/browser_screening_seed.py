"""Seed an explicitly isolated browser-test database; stdout is private test state.

Run only with a new directory supplied by the browser harness. Never targets .env DB.
"""
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from service import billing
from service.db import make_engine, session_factory
from service.identity import Identity
from service.market_data import normalize_history, normalize_universe, publish_manifest, save_history, save_universe
from service.models import Account, PlanVersion, PurchaseOrder, BankPayment
from service.screening import run_screening
from service.settings import Settings


def main():
    root = Path(sys.argv[1]).resolve()
    if not root.name.startswith("quantik-screening-e2e-") or not root.is_dir() or any(root.iterdir()):
        raise RuntimeError("Use an empty temporary screening test directory")
    url = "sqlite:///" + (root / "test.sqlite").as_posix()
    os.environ["QUANTIK_DATABASE_URL"] = url
    os.environ["QUANTIK_ENVIRONMENT"] = "test"
    command.upgrade(Config("backend/alembic.ini"), "head")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private, public = root/"private.pem", root/"public.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(_env_file=None, database_url=url, environment="test", jwt_private_key_path=private, jwt_public_key_path=public)
    engine = make_engine(url); sessions = session_factory(engine)
    observed = datetime(2026, 10, 15, 16, 10, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    with sessions() as db, db.begin():
        universe = save_universe(db, normalize_universe(pd.DataFrame({"symbol": ["FPT", "HPG"], "exchange": ["HOSE", "HOSE"]})))
        refs = {}
        for symbol in ["VNINDEX", "FPT", "HPG"]:
            x = np.arange(80); close = 58 + .03*x + 2*np.sin(x/2)
            frame = pd.DataFrame({"time": pd.bdate_range(end=observed.date(), periods=80), "open": close*.999, "high": close*1.015, "low": close*.985, "close": close, "volume": np.round(200000+10000*np.sin(x/3))})
            refs[symbol] = save_history(db, normalize_history(frame, symbol=symbol, kind="INDEX" if symbol=="VNINDEX" else "EQUITY", source="vci", target_session=observed.date(), observed_at=observed, minimum_bars=50))
        manifest = publish_manifest(db, universe, refs, target_session=observed.date(), input_mode="COMPLETED_DAILY")
        manifest_id = manifest.id
        owner = Account(google_sub="browser-screening-fixture", role="user", display_name="Browser fixture", email="fixture@example.test")
        db.add(owner); db.flush()
        now = int(time.time())
        plan = PlanVersion(name="Browser fixture plan", price_vnd=10000, duration_days=30, analysis_quota=20, active_limit=1, features={"screening": True, "analysis": True}, available=True, created_at=now)
        db.add(plan); db.flush()
        order = PurchaseOrder(account_id=owner.id, plan_id=plan.id, idempotency_key="fixture", reference="QTKBROWSER", amount_vnd=10000, bank_code="TPBANK", bank_account="111", account_name="Fixture", transfer_content="QTKBROWSER", created_at=now, expires_at=now+600)
        db.add(order); db.flush()
        payment = BankPayment(id="browser-fixture", fingerprint="f"*64, order_id=order.id, bank_code="TPBANK", bank_account="111", direction="in", amount_vnd=10000, transaction_at=now, received_at=now, references=[order.reference], status="RECONCILIATION")
        db.add(payment); db.flush(); billing.grant(db, payment, order, now)
        _, refresh = Identity(settings).new_session(db, owner)
    result = run_screening(sessions, observed.date(), "PM", manifest_id=manifest_id, now=observed, allow_fixture=True)
    assert result.status == "READY", result.reason
    engine.dispose()
    print(json.dumps({"database_url": url, "private": str(private), "public": str(public), "refresh_cookie": refresh, "run_id": result.id}))


if __name__ == "__main__":
    main()
