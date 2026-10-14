"""Candidate artifacts and atomic publication; fixtures cannot be published."""
import time
from dataclasses import asdict
from datetime import date

from sqlalchemy import select, update

from service.market_data import DataError, DailyHistory, checksum, persist_immutable
from service.models import DataManifest, HistoryRevision, ModelVersion, PublishedModel, UniverseVersion
from service.quant.configuration import QuantConfig
from service.quant.training import TrainingPolicy, train_artifact


def manifest_frames(db, manifest: DataManifest) -> tuple[dict, object, dict]:
    universe = db.get(UniverseVersion, manifest.universe_id)
    if not universe or manifest.input_mode != "COMPLETED_DAILY":
        raise DataError("MODEL_REQUIRES_COMPLETED_MANIFEST")
    data, sectors, index = {}, {}, None
    for symbol, history_id in manifest.histories.items():
        history = db.get(HistoryRevision, history_id)
        if not history or history.symbol != symbol or history.target_session != manifest.target_session:
            raise DataError("MANIFEST_HISTORY_MISMATCH")
        frame = DailyHistory(symbol, history.target_session, history.input_mode, history.payload).frame()
        if symbol == "VNINDEX":
            index = frame
        elif universe.payload["members"][symbol].get("sector"):
            data[symbol], sectors[symbol] = frame, universe.payload["members"][symbol]["sector"]
    if index is None:
        raise DataError("INDEX_NOT_READY")
    return data, index, sectors


def save_candidate(db, manifest: DataManifest, artifact: dict, config: QuantConfig) -> ModelVersion:
    if artifact["cutoff_session"] != manifest.target_session.isoformat():
        raise DataError("MODEL_CUTOFF_MISMATCH")
    histories = [db.get(HistoryRevision, history_id) for history_id in manifest.histories.values()]
    source_verified = all(h and h.payload["metadata"].get("provenance") == "LIVE_VALIDATED"
        and h.payload["metadata"].get("adjustment") == "VERIFIED" for h in histories)
    evidence = {"live_data_validated": source_verified, "validation_accepted": artifact["validation"]["accepted"],
        "exclusions": manifest.coverage["excluded"], "manifest_checksum": manifest.checksum}
    identity = {"manifest_id": manifest.id, "artifact": artifact, "config": asdict(config), "evidence": evidence}
    return persist_immutable(db, ModelVersion, identity, manifest_id=manifest.id,
        cutoff_session=manifest.target_session, created_at=int(time.time()), published_at=None,
        status="CANDIDATE" if artifact["validation"]["accepted"] else "REJECTED",
        config=asdict(config), artifact=artifact, evidence=evidence)


def promote_model(db, model_id: str) -> ModelVersion:
    model = db.scalar(select(ModelVersion).where(ModelVersion.id == model_id).with_for_update())
    if not model:
        raise DataError("MODEL_NOT_FOUND")
    identity = {"manifest_id": model.manifest_id, "artifact": model.artifact,
        "config": model.config, "evidence": model.evidence}
    if checksum(identity) != model.checksum:
        raise DataError("MODEL_CHECKSUM_MISMATCH")
    if not model.evidence["validation_accepted"] or not model.artifact["validation"]["accepted"]:
        raise DataError("MODEL_VALIDATION_REJECTED")
    if not model.evidence["live_data_validated"]:
        raise DataError("MODEL_LIVE_DATA_UNVERIFIED")
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    # Concurrent publication serializes on one named pointer; never replace a newer cutoff.
    db.execute(insert(PublishedModel).values(name="weekly-lightgbm", model_id=model.id)
        .on_conflict_do_nothing(index_elements=["name"]))
    pointer = db.scalar(select(PublishedModel).where(PublishedModel.name == "weekly-lightgbm").with_for_update())
    incumbent = db.get(ModelVersion, pointer.model_id)
    if incumbent and incumbent.cutoff_session > model.cutoff_session:
        raise DataError("MODEL_PROMOTION_STALE")
    pointer.model_id = model.id
    model.status = "PUBLISHED"
    if model.published_at is None:
        model.published_at = int(time.time())
    return model


def train_manifest(sessions, manifest_id: str, config: QuantConfig, policy: TrainingPolicy | None = None) -> ModelVersion:
    with sessions() as db:
        manifest = db.get(DataManifest, manifest_id)
        if not manifest:
            raise DataError("MANIFEST_NOT_FOUND")
        data, index, sectors = manifest_frames(db, manifest)
        pointer = db.get(PublishedModel, "weekly-lightgbm")
        incumbent = db.get(ModelVersion, pointer.model_id) if pointer else None
        incumbent_artifact = incumbent.artifact if incumbent else None
    artifact = train_artifact(data, index, sectors, config, policy, incumbent_artifact)
    with sessions() as db, db.begin():
        manifest = db.get(DataManifest, manifest_id)
        return save_candidate(db, manifest, artifact, config)
