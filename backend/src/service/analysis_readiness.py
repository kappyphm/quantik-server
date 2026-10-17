"""Shared Quant input readiness; provider ingestion remains a separate pipeline."""
import time
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta

from sqlalchemy import select

from service.market_data import DataError, checksum
from service.models import DataManifest, HistoryRevision, MarketSnapshot, ModelVersion, PublishedSnapshot, UniverseVersion
from service.quant.configuration import QuantConfig
from service.scheduling import VN

DEFAULT_CONFIG = replace(QuantConfig(), PRICE_INPUT_UNIT="VND")


def require_snapshot_service(db, target: date) -> None:
    name = "completed:" + target.isoformat() + ":" + checksum(asdict(DEFAULT_CONFIG))
    ready = db.scalar(select(MarketSnapshot.id).join(PublishedSnapshot, PublishedSnapshot.snapshot_id == MarketSnapshot.id)
        .where(PublishedSnapshot.name == name, MarketSnapshot.status == "READY"))
    if ready:
        return
    from service.operational_config import active_version, OperationsPolicy
    active = active_version(db, "OPERATIONS")
    if not active:
        raise DataError("DEPENDENCY_OPERATIONS_NOT_READY", True)
    policy = OperationsPolicy.model_validate(active.payload)
    if not policy.enabled or not policy.snapshots_enabled:
        raise DataError("DEPENDENCY_OPERATOR_PAUSED", True)


def completed_sessions(db, now: int | None = None) -> list[date]:
    from service.operational_config import TradingCalendar, active_version
    from service.scheduling import stamp
    current = int(time.time()) if now is None else now
    reference = active_version(db, "TRADING_CALENDAR")
    if not reference:
        raise DataError("CALENDAR_NOT_READY", True)
    calendar = TradingCalendar.model_validate(reference.payload)
    today = datetime.fromtimestamp(current, VN).date()
    if not calendar.start <= today <= calendar.end:
        raise DataError("CALENDAR_OUT_OF_RANGE", True)
    return [day for day in calendar.sessions if stamp(day, "16:00") <= current]


def validate_request(db, settings, target: date, symbol: str) -> None:
    if target not in completed_sessions(db):
        raise DataError("ANALYSIS_SESSION_NOT_COMPLETED")
    manifest, _ = ready_inputs(db, target, DEFAULT_CONFIG, settings)
    require_snapshot_service(db, target)
    universe = db.get(UniverseVersion, manifest.universe_id)
    if not universe or symbol not in universe.payload["members"]:
        raise DataError("UNKNOWN_INSTRUMENT")
    if symbol not in manifest.histories:
        raise DataError("SYMBOL_DATA_NOT_READY", True)


def options(db, settings, target: date | None = None) -> dict:
    reason, sessions, selected, symbols = None, [], target, []
    try:
        sessions = completed_sessions(db)
        selected = target or (sessions[-1] if sessions else None)
        if selected not in sessions:
            raise DataError("ANALYSIS_SESSION_NOT_COMPLETED")
        manifest, _ = ready_inputs(db, selected, DEFAULT_CONFIG, settings)
        require_snapshot_service(db, selected)
        universe = db.get(UniverseVersion, manifest.universe_id)
        symbols = sorted(set(universe.payload["members"]) & set(manifest.histories))
    except DataError as exc:
        reason = exc.code
    return {"sessions": list(reversed(sessions[-60:])), "selected_session": selected,
        "symbols": symbols, "ready": reason is None, "reason": reason}


def model_for_session(db, target: date, config: QuantConfig, *, now: int | None = None) -> str | None:
    today = datetime.fromtimestamp(int(time.time()) if now is None else now, VN).date()
    rows = db.execute(select(ModelVersion.id, ModelVersion.evidence, ModelVersion.config).where(
        ModelVersion.status == "PUBLISHED", ModelVersion.cutoff_session <= target,
        ModelVersion.cutoff_session <= today, ModelVersion.cutoff_session > today-timedelta(days=28))
        .order_by(ModelVersion.cutoff_session.desc(), ModelVersion.published_at.desc(), ModelVersion.id))
    for row in rows:
        if (row.evidence.get("live_data_validated") and row.evidence.get("validation_accepted")
                and checksum(row.config) == checksum(asdict(config))):
            return row.id
    return None


def validate_manifest(db, manifest: DataManifest, settings) -> None:
    if manifest.input_mode != "COMPLETED_DAILY":
        raise DataError("QUANT_REQUIRES_COMPLETED_DATA")
    if settings.allow_fixture_analysis:
        return
    universe = db.get(UniverseVersion, manifest.universe_id)
    if not universe or universe.payload.get("provenance") != "LIVE_VALIDATED":
        raise DataError("UNIVERSE_NOT_VALIDATED", True)
    rows = list(db.execute(select(HistoryRevision.id, HistoryRevision.payload["metadata"])
        .where(HistoryRevision.id.in_(list(manifest.histories.values())))))
    if len(rows) != len(manifest.histories) or any(meta.get("provenance") != "LIVE_VALIDATED"
            or meta.get("adjustment") != "VERIFIED" for _, meta in rows):
        raise DataError("QUANT_DATA_NOT_VALIDATED", True)


def ready_inputs(db, target: date, config: QuantConfig, settings) -> tuple[DataManifest, str | None]:
    manifest = db.scalar(select(DataManifest).where(DataManifest.target_session == target,
        DataManifest.input_mode == "COMPLETED_DAILY").order_by(DataManifest.revision_order.desc()).limit(1))
    if not manifest:
        raise DataError("DATA_NOT_READY", True)
    validate_manifest(db, manifest, settings)
    model_id = model_for_session(db, target, config)
    if not model_id and not settings.allow_degraded_analysis:
        raise DataError("MODEL_NOT_READY", True)
    return manifest, model_id
