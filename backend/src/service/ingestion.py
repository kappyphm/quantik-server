"""Resumable ingestion; a partial fetch never becomes a ready market manifest."""
import os
from datetime import date

from sqlalchemy import select

from service.db import make_engine, session_factory
from service.market_data import DataError, publish_manifest, save_history, save_universe
from service.models import HistoryRevision
from service.settings import Settings
from service.vnstock_provider import VnstockProvider


def ingest_market(settings: Settings, start: date, target_session: date,
                  selected_symbols: list[str] | None = None, input_mode: str = "COMPLETED_DAILY",
                  minimum_bars: int = 1) -> dict:
    if start > target_session:
        raise DataError("INVALID_DATE_WINDOW")
    key = os.environ.get("VNSTOCK_API_KEY", "")
    if not key:
        raise DataError("PROVIDER_API_KEY_MISSING")
    engine = make_engine(settings.database_url)
    sessions = session_factory(engine)
    try:
        provider = VnstockProvider(key, sessions)
        payload = provider.universe()
        with sessions() as db, db.begin():
            universe = save_universe(db, payload)
        members = payload["members"]
        if selected_symbols and not set(selected_symbols).issubset(members):
            raise DataError("UNKNOWN_INSTRUMENT")
        symbols = ["VNINDEX", *sorted(set(selected_symbols or members))]
        failures = {}
        for symbol in symbols:
            try:
                history = provider.history(symbol, start, target_session,
                    kind="INDEX" if symbol == "VNINDEX" else "EQUITY", input_mode=input_mode, minimum_bars=minimum_bars)
                with sessions() as db, db.begin():
                    record = save_history(db, history)
                print(f"Ingested {symbol}: {len(history.payload['bars'])} bars, revision {record.id}")
            except DataError as exc:
                failures[symbol] = exc.code
                print(f"Skipped {symbol}: {exc.code}")
                if exc.retryable or exc.code.startswith("VNSTOCK_AUTH") or exc.code.startswith("VNSTOCK_TIER"):
                    break  # Resume later; do not hammer the provider on a shared outage/budget.
        with sessions() as db, db.begin():
            histories = {}
            for row in db.scalars(select(HistoryRevision).where(HistoryRevision.target_session == target_session,
                HistoryRevision.input_mode == input_mode)):
                if row.symbol not in members and row.symbol != "VNINDEX":
                    continue
                previous = histories.get(row.symbol)
                if previous is None or row.payload["metadata"]["observed_at"] > previous.payload["metadata"]["observed_at"]:
                    histories[row.symbol] = row
            try:
                manifest = publish_manifest(db, universe, histories, target_session=target_session,
                    input_mode=input_mode)
                manifest_id = manifest.id
            except DataError as exc:
                manifest_id = None
                failures["manifest"] = exc.code
        return {"manifest_id": manifest_id, "histories_available": len(histories), "failures": failures}
    finally:
        engine.dispose()
