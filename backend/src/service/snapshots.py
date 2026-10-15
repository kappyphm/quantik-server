"""Shared full-universe reference snapshots with builder leases and atomic publication."""
import time
from dataclasses import asdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update

from service.market_data import DataError, checksum
from service.model_lifecycle import manifest_frames
from service.models import ComponentResult, DataManifest, HistoryRevision, MarketSnapshot, ModelVersion, PublishedModel, PublishedSnapshot, UniverseVersion, new_id
from service.quant import engines as e
from service.quant.assembly import directional_context
from service.quant.baseline import compute_baseline
from service.quant.configuration import QuantConfig, engine_context
from service.quant.scoring import build_reference
from service.quant.serialization import json_metrics
from service.quant.training import predict_snapshot
from service.quant.versions import ENGINE_VERSION, SNAPSHOT_POLICY


def _insert(db):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def cached_baseline(sessions, key: str, compute) -> dict:
    now, token = int(time.time()), new_id()
    with sessions() as db, db.begin():
        db.execute(_insert(db)(ComponentResult).values(key=key, status="BUILDING", lease_until=0,
            execution_token=None, payload=None).on_conflict_do_nothing(index_elements=["key"]))
        row = db.get(ComponentResult, key)
        if row.status == "READY":
            return row.payload
        claimed = db.execute(update(ComponentResult).where(ComponentResult.key == key,
            ComponentResult.status != "READY", ComponentResult.lease_until <= now)
            .values(status="BUILDING", execution_token=token, lease_until=now + 600))
        if claimed.rowcount != 1:
            raise DataError("COMPONENT_BUILD_IN_PROGRESS", True, 30)
    try:
        warnings = []
        payload = json_metrics(compute(), warnings)
        payload["serialization_warnings"] = warnings
        with sessions() as db, db.begin():
            changed = db.execute(update(ComponentResult).where(ComponentResult.key == key,
                ComponentResult.execution_token == token, ComponentResult.lease_until > int(time.time()))
                .values(status="READY", payload=payload, execution_token=None, lease_until=0))
            if changed.rowcount != 1:
                raise DataError("COMPONENT_LEASE_LOST", True)
        return payload
    except Exception:
        with sessions() as db, db.begin():
            db.execute(update(ComponentResult).where(ComponentResult.key == key, ComponentResult.execution_token == token)
                .values(status="FAILED", execution_token=None, lease_until=0))
        raise


def build_market_snapshot(sessions, manifest_id: str, config: QuantConfig, *, minimum_coverage: float = .8) -> MarketSnapshot:
    if not 0 < minimum_coverage <= 1:
        raise DataError("INVALID_SNAPSHOT_COVERAGE")
    now = int(time.time())
    evaluation = datetime.fromtimestamp(now, timezone.utc)
    evaluation_day = evaluation.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    with sessions() as db:
        manifest = db.get(DataManifest, manifest_id)
        if not manifest:
            raise DataError("MANIFEST_NOT_FOUND")
        data, index, sectors = manifest_frames(db, manifest)
        universe = db.get(UniverseVersion, manifest.universe_id)
        members = universe.payload["members"]
        pointer = db.get(PublishedModel, "weekly-lightgbm")
        model = db.get(ModelVersion, pointer.model_id) if pointer else None
        model_age = (evaluation_day - model.cutoff_session).days if model else None
        if model and (model_age < 0 or model_age >= 28 or model.cutoff_session > manifest.target_session):
            model = None
        model_id = model.id if model else None
        artifact = model.artifact if model else None
        dependency = {"manifest": manifest.id, "model": model_id, "config": asdict(config),
            "engine": ENGINE_VERSION, "policy": SNAPSHOT_POLICY, "evaluation_day": evaluation_day.isoformat(),
            "minimum_coverage": minimum_coverage}
    key, token = checksum(dependency), new_id()
    with sessions() as db, db.begin():
        db.execute(_insert(db)(MarketSnapshot).values(id=new_id(), dependency_key=key, manifest_id=manifest_id,
            model_id=model_id, target_session=manifest.target_session, created_at=now, status="BUILDING",
            lease_until=0, execution_token=None, config=asdict(config), payload=None, payload_checksum=None, reason=None)
            .on_conflict_do_nothing(index_elements=["dependency_key"]))
        snapshot = db.scalar(select(MarketSnapshot).where(MarketSnapshot.dependency_key == key))
        snapshot_id = snapshot.id
        if snapshot.status == "READY":
            if checksum(snapshot.payload) != snapshot.payload_checksum:
                raise DataError("SNAPSHOT_CHECKSUM_MISMATCH")
            return snapshot
        claimed = db.execute(update(MarketSnapshot).where(MarketSnapshot.id == snapshot_id,
            MarketSnapshot.status != "READY", MarketSnapshot.lease_until <= now)
            .values(status="BUILDING", execution_token=token, lease_until=now+600, reason=None))
        if claimed.rowcount != 1:
            raise DataError("SNAPSHOT_BUILD_IN_PROGRESS", True, 30)
    try:
        baselines, excluded = {}, {s: "HISTORY_OR_SECTOR_UNAVAILABLE" for s in members if s not in data}
        for symbol, frame in sorted(data.items()):
            with sessions() as db, db.begin():
                renewed = db.execute(update(MarketSnapshot).where(MarketSnapshot.id == snapshot_id,
                    MarketSnapshot.execution_token == token, MarketSnapshot.lease_until > int(time.time()))
                    .values(lease_until=int(time.time()) + 600))
                if renewed.rowcount != 1:
                    raise DataError("SNAPSHOT_LEASE_LOST", True)
            baseline_key = checksum({"stock": manifest.histories[symbol], "index": manifest.histories["VNINDEX"],
                "engine": ENGINE_VERSION, "config": asdict(config), "evaluation_day": evaluation_day.isoformat()})
            baseline = cached_baseline(sessions, baseline_key, lambda: compute_baseline(symbol, frame,
                exchange=members[symbol]["exchange"], evaluated_at=evaluation, index_frame=index, config=config))
            if baseline.get("error") or not baseline.get("stats") or not baseline.get("simulation_risk"):
                excluded[symbol] = "BASELINE_UNAVAILABLE"
                continue
            baselines[symbol] = baseline
            print(f"Snapshot baseline ready: {symbol}")
        eligible = {symbol: data[symbol] for symbol in baselines}
        eligible_sectors = {symbol: sectors[symbol] for symbol in baselines}
        if len(baselines) < 5 or len(baselines) / len(members) < minimum_coverage:
            raise DataError("SNAPSHOT_COVERAGE_NOT_READY", True)
        with engine_context(config):
            predictions = predict_snapshot(artifact, eligible, index, eligible_sectors, config) if artifact else {}
            sector_returns = e.CrossSectionalLightGBMEngine.sector_return_series(eligible, eligible_sectors)
            sector_trends = e.SectorEngine.sector_trend(eligible, eligible_sectors)
            grouped = {sector: [s for s in eligible if sectors[s] == sector] for sector in set(eligible_sectors.values())}
            correlations = {sector: e.CrossCorrelationEngine.analyze_sector(eligible, symbols)
                if len(symbols) >= 2 else {"error": "INSUFFICIENT_SECTOR_MEMBERS"}
                for sector, symbols in grouped.items()}
        reports = {}
        for symbol, baseline in baselines.items():
            context = {"sector": {"name": sectors[symbol], "trend": sector_trends.get(sectors[symbol], {})},
                "cross_corr": correlations[sectors[symbol]]}
            with engine_context(config):
                report = directional_context(baseline, data[symbol], index, sector_returns[sectors[symbol]],
                    predictions.get(symbol), context, config)
            report["range"] = f"{data[symbol].index[0].date().isoformat()} -> {data[symbol].index[-1].date().isoformat()}"
            report["date"] = evaluation.isoformat()
            report["cross_sectional_model"] = {"available": model_id is not None, "model_version": model_id,
                "model_age_days": model_age, "feature_count": len(artifact["feature_columns"]) if artifact else None,
                "n_symbols": len(predictions), "validation": artifact["validation"] if artifact else None}
            reports[symbol] = report
        warnings = ["MODEL_UNAVAILABLE"] if model_id is None else (["MODEL_AGE_WARNING"] if model_age >= 14 else [])
        with engine_context(config):
            reference = build_reference(reports)
        payload = json_metrics({"versions": dependency, "members": reports, "reference": reference,
            "excluded": excluded, "coverage_ratio": len(reports) / len(members), "warnings": warnings,
            "model_age_days": model_age, "data_as_of": index.index[-1].date().isoformat()})
        payload["reference_checksum"] = checksum(payload["reference"])
        payload["member_checksums"] = {symbol: checksum(report) for symbol, report in payload["members"].items()}
        hashed = checksum(payload)
        with sessions() as db, db.begin():
            changed = db.execute(update(MarketSnapshot).where(MarketSnapshot.id == snapshot_id,
                MarketSnapshot.execution_token == token, MarketSnapshot.lease_until > int(time.time()))
                .values(status="READY", execution_token=None, lease_until=0, payload=payload, payload_checksum=hashed))
            if changed.rowcount != 1:
                raise DataError("SNAPSHOT_LEASE_LOST", True)
            name = "completed:" + manifest.target_session.isoformat() + ":" + checksum(asdict(config))
            db.execute(_insert(db)(PublishedSnapshot).values(name=name, snapshot_id=snapshot_id)
                .on_conflict_do_nothing(index_elements=["name"]))
            pointer = db.scalar(select(PublishedSnapshot).where(PublishedSnapshot.name == name).with_for_update())
            incumbent = db.get(MarketSnapshot, pointer.snapshot_id)
            incumbent_manifest = db.get(DataManifest, incumbent.manifest_id)
            if incumbent_manifest.revision_order < manifest.revision_order or (
                incumbent_manifest.id == manifest.id and incumbent.created_at <= now):
                pointer.snapshot_id = snapshot_id
            return db.get(MarketSnapshot, snapshot_id)
    except Exception as exc:
        with sessions() as db, db.begin():
            db.execute(update(MarketSnapshot).where(MarketSnapshot.id == snapshot_id,
                MarketSnapshot.execution_token == token).values(status="FAILED", execution_token=None,
                    lease_until=0, reason=exc.code if isinstance(exc, DataError) else "SNAPSHOT_COMPUTE_ERROR"))
        raise
