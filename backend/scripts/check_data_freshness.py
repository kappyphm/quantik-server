"""Data-freshness checker for the market-data pipeline.

Reads only the database (DATABASE_URL) and reports whether published market
data is keeping up. Designed to run from cron on the server every 30-60
minutes; it prints one JSON line and exits:

    0  fresh, no auth failures
    1  stale: latest manifest is older than the expected session
    2  provider auth failures (VNSTOCK_AUTH_*) among recent FAILED tasks

Wire the non-zero exits to whatever alert channel operations uses; silence
from this checker must itself be treated as a fault, so also schedule a
daily "still alive" run whose output is expected.

The expected-session rule is a weekday heuristic (Mon-Fri, Asia/Ho_Chi_Minh,
today counts once it is 17:00). Exchange holidays are not modelled here;
the trading calendar in operational config remains the source of truth and
a holiday false alarm is cleared by checking it.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

VN = ZoneInfo("Asia/Ho_Chi_Minh")


def expected_session(now: datetime):
    day = now.date()
    if now.hour >= 17 and day.weekday() < 5:
        return day
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print(json.dumps({"status": "ERROR", "reason": "DATABASE_URL missing"}))
        return 2
    now = datetime.now(VN)
    expected = expected_session(now)
    engine = create_engine(url)
    with engine.connect() as conn:
        latest_manifest = conn.execute(text(
            "SELECT max(target_session) FROM data_manifests")).scalar()
        auth_failures = conn.execute(text(
            "SELECT count(*) FROM scheduled_tasks WHERE status = 'FAILED' "
            "AND reason LIKE 'VNSTOCK_AUTH_%' AND created_at > :since"),
            {"since": int(now.timestamp()) - 86400}).scalar()
        latest_screening = conn.execute(text(
            "SELECT max(target_session) FROM screening_runs WHERE status = 'READY'")).scalar()
    stale = latest_manifest is None or latest_manifest < expected
    report = {"status": "STALE" if stale else "OK",
        "checked_at": now.isoformat(), "expected_session": expected.isoformat(),
        "latest_manifest_session": latest_manifest.isoformat() if latest_manifest else None,
        "latest_screening_session": latest_screening.isoformat() if latest_screening else None,
        "auth_failures_24h": int(auth_failures or 0)}
    print(json.dumps(report))
    if auth_failures:
        return 2
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
