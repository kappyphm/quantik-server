"""Reviewed exchange reference; deliberately no extrapolation to unverified years."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session

from service.operational_config import OperationsPolicy, TradingCalendar, active_version, activate, create_version


def expected_screening(calendar: TradingCalendar, slot: str, now: datetime) -> tuple[date, str] | None:
    from service.screening import POLICY
    if now.tzinfo is None:
        raise ValueError("Calendar clock must include a timezone")
    slots = ("AM", "PM") if slot == "ALL" else (slot,)
    expected = []
    for session in calendar.sessions:
        for candidate in slots:
            deadline = datetime.combine(session, time.fromisoformat(POLICY[candidate]["deadline"]), ZoneInfo("Asia/Ho_Chi_Minh"))
            if deadline <= now:
                expected.append((session, candidate))
    return max(expected) if expected else None


def calendar_2026() -> dict:
    closed = {date.fromisoformat(value) for value in ["2026-01-01", "2026-01-02", "2026-02-16",
        "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-04-27",
        "2026-04-30", "2026-05-01", "2026-08-31", "2026-09-01", "2026-09-02"]}
    start, end = date(2026, 1, 1), date(2026, 12, 31)
    sessions = [start+timedelta(days=i) for i in range((end-start).days+1)
        if (start+timedelta(days=i)).weekday() < 5 and start+timedelta(days=i) not in closed]
    return {"start": start.isoformat(), "end": end.isoformat(), "sessions": [d.isoformat() for d in sessions],
        "source_urls": ["https://www.hsx.vn/vi/tin-tuc/hose-thong-bao-lich-nghi-giao-dich-nam-2026/2437411",
            "https://portal.hnx.vn/vi-vn/chi-tiet-lich-nghi-gd-60021971.html",
            "https://www.hnx.vn/vi-vn/chi-tiet-lich-nghi-gd-60022084.html"],
        "review_note": "Reviewed official 2026 exchange announcements on 2026-10-15, including updated Jan 2 closure. "
            "Jan 10 and Aug 22 office make-up Saturdays are not trading sessions. No 2027 inference.",
        "provenance": "EXCHANGE_REFERENCE"}


def seed_defaults(db: Session, actor_id: str) -> dict:
    result = {}
    for kind, payload in (("OPERATIONS", OperationsPolicy().model_dump(mode="json")), ("TRADING_CALENDAR", calendar_2026())):
        current = active_version(db, kind)
        if current is None:
            current = create_version(db, kind, payload, actor_id)
            activate(db, current.id, None, "Bootstrap reviewed operation defaults and exchange calendar", actor_id)
        result[kind] = current.id
    return result
