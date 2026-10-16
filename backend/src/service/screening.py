"""Fenced screening runs, readiness/deadline policy and atomic publication."""
import time
from datetime import datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from service.market_data import DataError, DailyHistory, checksum
from service.screening_validation import eligible
from service.models import DataManifest, HistoryRevision, PublishedScreening, ScreeningResult, ScreeningRun, UniverseVersion, new_id

VN = ZoneInfo("Asia/Ho_Chi_Minh")
POLICY = {"version": "screening-tristate-1", "minimum_history_coverage": .95,
    "minimum_average_volume_shares": 100000, "top_n": 70,
    "AM": {"trigger": "11:30", "deadline": "12:55", "observation_end": "13:00"},
    "PM": {"trigger": "15:00", "deadline": "17:30", "completed_from": "16:00"}}


def insert_for(db):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def validate_history(history, target, mode, slot, now, allow_fixture):
    if not history or history.target_session != target or history.input_mode != mode:
        raise DataError("HISTORY_NOT_READY", True)
    if checksum(history.payload) != history.checksum:
        raise DataError("HISTORY_CHECKSUM_MISMATCH")
    metadata = history.payload["metadata"]
    observed = datetime.fromisoformat(metadata["observed_at"])
    if observed.tzinfo is None or observed > now + timedelta(seconds=5):
        raise DataError("INVALID_OBSERVATION_TIME")
    local = observed.astimezone(VN)
    if slot == "AM" and (local.date() != target or not day_time(11, 30) <= local.time() < day_time(13)):
        raise DataError("AM_CUTOFF_MISMATCH", True)
    if slot == "PM" and (local.date() < target or (local.date() == target and local.time() < day_time(16))):
        raise DataError("PM_DATA_NOT_COMPLETED", True)
    if not allow_fixture and not eligible(history.payload):
        raise DataError("LIVE_DATA_NOT_VALIDATED", True)
    return DailyHistory(history.symbol, target, mode, history.payload).frame()


def run_screening(sessions, target_session, slot: str, *, manifest_id: str | None = None,
                  now: datetime | None = None, allow_fixture: bool = False, operation_guard=None,
                  backfill: bool = False) -> ScreeningRun:
    if slot not in {"AM", "PM"}:
        raise DataError("INVALID_SCREENING_SLOT")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise DataError("SCREENING_CLOCK_REQUIRES_TIMEZONE")
    started = time.monotonic()
    trigger = datetime.combine(target_session, day_time.fromisoformat(POLICY[slot]["trigger"]), VN)
    deadline = datetime.combine(target_session, day_time.fromisoformat(POLICY[slot]["deadline"]), VN)
    mode = "PROVISIONAL_DAILY" if slot == "AM" else "COMPLETED_DAILY"
    token = new_id()
    with sessions() as db, db.begin():
        task = operation_guard(db) if operation_guard else None
        if backfill:
            if (task is None or task.kind != "BACKFILL" or task.target_session != target_session
                    or slot != "PM" or now < datetime.combine(target_session, day_time(16), VN)):
                raise DataError("INVALID_BACKFILL_OPERATION")
            deadline = datetime.fromtimestamp(task.deadline, timezone.utc)
        db.execute(insert_for(db)(ScreeningRun).values(id=new_id(), target_session=target_session, slot=slot,
            status="WAITING_DATA", policy=POLICY, created_at=int(now.timestamp()), lease_until=0)
            .on_conflict_do_nothing(index_elements=["target_session", "slot"]))
        run = db.scalar(select(ScreeningRun).where(ScreeningRun.target_session == target_session,
            ScreeningRun.slot == slot).with_for_update())
        if run.status == "READY":
            return run
        if run.execution_token and run.lease_until > int(time.time()):
            raise DataError("SCREENING_IN_PROGRESS", True)
        run_id = run.id
        if now < trigger:
            run.status, run.reason = "WAITING_DATA", "SLOT_NOT_STARTED"
            return run
        if now >= deadline:
            run.status, run.reason = "FAILED", "SCREENING_DEADLINE"
            return run
        run.status, run.reason = "RUNNING", None
        run.execution_token, run.lease_until = token, int(time.time()) + 600
    try:
        with sessions() as db:
            manifest = db.get(DataManifest, manifest_id) if manifest_id else db.scalar(select(DataManifest).where(
                DataManifest.target_session == target_session, DataManifest.input_mode == mode)
                .order_by(DataManifest.revision_order.desc()).limit(1))
            if not manifest:
                raise DataError("MANIFEST_NOT_READY", True)
            if manifest.target_session != target_session or manifest.input_mode != mode:
                raise DataError("SCREENING_MANIFEST_MISMATCH")
            universe = db.get(UniverseVersion, manifest.universe_id)
            if not universe:
                raise DataError("UNIVERSE_NOT_READY", True)
            if not allow_fixture and not eligible(universe.payload, universe=True):
                raise DataError("UNIVERSE_NOT_VALIDATED", True)
            members = universe.payload["members"]
            index_record = db.get(HistoryRevision, manifest.histories.get("VNINDEX", ""))
            index = validate_history(index_record, target_session, mode, slot, now, allow_fixture)
            if len(index) < 50 or index_record.payload["metadata"]["kind"] != "INDEX":
                raise DataError("INDEX_HISTORY_NOT_READY", True)
            selected_manifest_id = manifest.id
            history_ids = manifest.histories.copy()
            source_is_live = universe.payload.get("provenance") == "LIVE_VALIDATED" and index_record.payload["metadata"].get("provenance") == "LIVE_VALIDATED"
            screening_live = eligible(universe.payload, universe=True) and eligible(index_record.payload)
        from service.quant.screening import evaluate_symbol, market_context, rank_results
        from service.quant.screening_legacy import RegimeAdjustment
        context = market_context(index, mode)
        results, unavailable, ready_count = [], {}, 0
        for symbol, instrument in sorted(members.items()):
            frame, observed_at = None, None
            with sessions() as db, db.begin():
                renewed = db.execute(update(ScreeningRun).where(ScreeningRun.id == run_id,
                    ScreeningRun.execution_token == token, ScreeningRun.lease_until > int(time.time()))
                    .values(lease_until=int(time.time()) + 600, manifest_id=selected_manifest_id))
                if renewed.rowcount != 1:
                    raise DataError("SCREENING_LEASE_LOST", True)
                history = db.get(HistoryRevision, history_ids.get(symbol, ""))
                try:
                    frame = validate_history(history, target_session, mode, slot, now, allow_fixture)
                    if history.symbol != symbol or history.payload["metadata"]["kind"] != "EQUITY":
                        raise DataError("INSTRUMENT_KIND_MISMATCH")
                    ready_count += 1
                    observed_at = history.payload["metadata"]["observed_at"]
                    source_is_live = source_is_live and history.payload["metadata"].get("provenance") == "LIVE_VALIDATED"
                    screening_live = screening_live and eligible(history.payload)
                except DataError as exc:
                    unavailable[symbol] = exc.code
                    frame = None
            result = evaluate_symbol(symbol, frame, index, input_mode=mode,
                minimum_average_volume=POLICY["minimum_average_volume_shares"])
            result["exchange"] = instrument["exchange"]
            result["warnings"] = sorted(set(result["warnings"] + (history.payload["metadata"].get("warnings", []) if frame is not None else [])))
            result["volume_context"]["observed_at"] = observed_at
            if symbol in unavailable:
                result["rejection_reasons"].append(unavailable[symbol])
            if result["assessment"].get("status") == "AVAILABLE":
                scores = [round(result["groups"][g]["score"], 1) for g in ["STRONG", "ACCUMULATION", "DISTRIBUTION"]]
                result["assessment"]["adjusted_signal"] = RegimeAdjustment()._apply_regime_adjustment(
                    *scores, context["screening_adjustments"])
            results.append(result)
        coverage = ready_count / len(members) if members else 0
        if coverage < POLICY["minimum_history_coverage"]:
            raise DataError("SCREENING_COVERAGE_NOT_READY", True)
        rankings = rank_results(results, POLICY["top_n"])
        if not any(rankings[group] for group in ["STRONG", "ACCUMULATION", "DISTRIBUTION"]):
            raise DataError("SCREENING_ZERO_ELIGIBLE")
        if now + timedelta(seconds=time.monotonic()-started) >= deadline:
            raise DataError("SCREENING_DEADLINE")
        summary = {"manifest_id": selected_manifest_id, "input_mode": mode, "market_context": context,
            "execution_mode": "BACKFILL" if backfill else "SCHEDULED",
            "history_coverage_ratio": coverage, "expected_symbols": len(members), "ready_symbols": ready_count,
            "unavailable": unavailable, "top_symbols": rankings["TOP"],
            "ranking_counts": {group: len(values) for group, values in rankings.items()},
            "source_mode": "LIVE_VALIDATED" if source_is_live else "LIVE_SCREENING_VALIDATED" if screening_live else "FIXTURE",
            "validation_scope": "SCREENING_ONLY" if screening_live and not source_is_live else "FULL" if source_is_live else "FIXTURE"}
        maps = {group: {symbol: i+1 for i, symbol in enumerate(order)} for group, order in rankings.items() if group != "TOP"}
        with sessions() as db, db.begin():
            if operation_guard:
                operation_guard(db)
            run = db.scalar(select(ScreeningRun).where(ScreeningRun.id == run_id).with_for_update())
            if run.execution_token != token or run.lease_until <= int(time.time()):
                raise DataError("SCREENING_LEASE_LOST", True)
            for result in results:
                symbol = result["symbol"]
                db.add(ScreeningResult(run_id=run_id, symbol=symbol, payload=result,
                    overall_rank=maps["OVERALL"].get(symbol), strong_rank=maps["STRONG"].get(symbol),
                    accumulation_rank=maps["ACCUMULATION"].get(symbol), distribution_rank=maps["DISTRIBUTION"].get(symbol)))
            run.status, run.summary, run.reason = "READY", summary, None
            run.published_at, run.execution_token, run.lease_until = int(now.timestamp()), None, 0
            for name in ["ALL", slot]:
                db.execute(insert_for(db)(PublishedScreening).values(name=name, run_id=run_id)
                    .on_conflict_do_nothing(index_elements=["name"]))
                pointer = db.scalar(select(PublishedScreening).where(PublishedScreening.name == name).with_for_update())
                incumbent = db.get(ScreeningRun, pointer.run_id)
                if (incumbent.target_session, incumbent.slot == "PM") <= (target_session, slot == "PM"):
                    pointer.run_id = run_id
            db.flush()
            return run
    except Exception as exc:
        reason = exc.code if isinstance(exc, DataError) else "SCREENING_COMPUTE_ERROR"
        retryable = isinstance(exc, DataError) and exc.retryable
        with sessions() as db, db.begin():
            run = db.scalar(select(ScreeningRun).where(ScreeningRun.id == run_id).with_for_update())
            if run.execution_token == token:
                run.status = "WAITING_DATA" if retryable and now + timedelta(seconds=time.monotonic()-started) < deadline else "FAILED"
                run.reason, run.execution_token, run.lease_until = reason, None, 0
            return run
