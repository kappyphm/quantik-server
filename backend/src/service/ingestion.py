"""Resumable ingestion; a partial fetch never becomes a ready market manifest."""
from types import SimpleNamespace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from service.db import make_engine, session_factory
from service.market_data import DataError, checksum, publish_manifest, save_history, save_universe
from service.models import HistoryRevision
from service.settings import Settings
from service.vnstock_provider import VnstockProvider
from service.integrations import read as read_integration


def reusable_history(record, start: date, minimum_bars: int) -> bool:
    metadata = record.payload["metadata"]
    requested_start = metadata.get("requested_start")
    if not requested_start or requested_start > start.isoformat() or len(record.payload["bars"]) < minimum_bars:
        return False
    if metadata.get("provenance") not in {"LIVE_UNVERIFIED", "LIVE_VALIDATED"} or checksum(record.payload) != record.checksum:
        return False
    from service.screening_validation import eligible
    if not eligible(record.payload):
        return False
    if record.input_mode == "PROVISIONAL_DAILY":
        observed = datetime.fromisoformat(metadata["observed_at"]).astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
        return observed.date() == record.target_session and time(11, 30) <= observed.time() < time(13)
    return True


def ingest_market(settings: Settings, start: date, target_session: date,
                  selected_symbols: list[str] | None = None, input_mode: str = "COMPLETED_DAILY",
                  minimum_bars: int = 1, *, operation_guard=None, resume: bool = False) -> dict:
    if start > target_session:
        raise DataError("INVALID_DATE_WINDOW")
    engine = make_engine(settings.database_url)
    sessions = session_factory(engine)
    try:
        with sessions() as db:
            values, _ = read_integration(db, "vnstock", settings)
            key = values["api_key"]
        if not key:
            raise DataError("PROVIDER_API_KEY_MISSING")

        def guard():
            if operation_guard:
                with sessions() as db, db.begin():
                    operation_guard(db)

        guard()
        provider = VnstockProvider(key, sessions)
        guard()
        payload = provider.universe()
        with sessions() as db, db.begin():
            if operation_guard:
                operation_guard(db)
            universe = save_universe(db, payload)
        members = payload["members"]
        if selected_symbols and not set(selected_symbols).issubset(members):
            raise DataError("UNKNOWN_INSTRUMENT")
        symbols = ["VNINDEX", *sorted(set(selected_symbols or members))]
        failures, retry_after = {}, None
        for symbol in symbols:
            if resume:
                with sessions() as db:
                    row = db.scalar(select(HistoryRevision).where(HistoryRevision.symbol == symbol,
                        HistoryRevision.target_session == target_session, HistoryRevision.input_mode == input_mode)
                        .order_by(HistoryRevision.payload["metadata"]["observed_at"].as_string().desc()).limit(1))
                    if row and reusable_history(row, start, minimum_bars):
                        continue
            try:
                guard()
                history = provider.history(symbol, start, target_session,
                    kind="INDEX" if symbol == "VNINDEX" else "EQUITY", input_mode=input_mode, minimum_bars=minimum_bars)
                with sessions() as db, db.begin():
                    if operation_guard:
                        operation_guard(db)
                    record = save_history(db, history)
                print(f"Ingested {symbol}: {len(history.payload['bars'])} bars, revision {record.id}")
            except DataError as exc:
                if exc.code.startswith("TASK_"):
                    raise
                failures[symbol] = exc.code
                retry_after = max(retry_after or 0, exc.retry_after or 0) or None
                print(f"Skipped {symbol}: {exc.code}")
                shared_failure = exc.code.startswith(("PROVIDER_", "VNSTOCK_")) and (
                    exc.retryable or exc.code.startswith(("VNSTOCK_AUTH", "VNSTOCK_TIER")))
                if symbol == "VNINDEX" or shared_failure:
                    break  # Index is mandatory; outages/budgets stop the shared source.
                # A suspended/new symbol lacking today's bar must not starve the
                # rest of the market. Publication still requires >=95% coverage.
        with sessions() as db, db.begin():
            if operation_guard:
                operation_guard(db)
            histories = {}
            # Manifest publication needs only IDs/metadata, not every historical bar in RAM.
            headers = db.execute(select(HistoryRevision.id, HistoryRevision.symbol, HistoryRevision.target_session,
                HistoryRevision.input_mode, HistoryRevision.payload["metadata"].label("metadata"))
                .where(HistoryRevision.target_session == target_session, HistoryRevision.input_mode == input_mode))
            for header in headers.mappings():
                row = SimpleNamespace(id=header["id"], symbol=header["symbol"], target_session=header["target_session"],
                    input_mode=header["input_mode"], payload={"metadata": header["metadata"]})
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
        return {"manifest_id": manifest_id, "histories_available": len(histories), "failures": failures, "retry_after": retry_after}
    finally:
        engine.dispose()
