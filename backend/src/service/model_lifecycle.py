"""Candidate artifacts and atomic publication; fixtures cannot be published."""
import time
from dataclasses import asdict
from datetime import date

from sqlalchemy import select, update

from service.market_data import DataError, DailyHistory, checksum, persist_immutable, signal_dependency_change
from service.models import DataManifest, HistoryRevision, ModelVersion, PublishedModel, UniverseVersion
from service.quant.configuration import QuantConfig
from service.quant.training import TrainingPolicy, train_artifact


def manifest_frames(db, manifest: DataManifest, minimum_bars: int = 120) -> tuple[dict, object, dict]:
    universe = db.get(UniverseVersion, manifest.universe_id)
    if not universe or manifest.input_mode != "COMPLETED_DAILY":
        raise DataError("MODEL_REQUIRES_COMPLETED_MANIFEST")
    data, sectors, index = {}, {}, None
    for symbol, history_id in manifest.histories.items():
        history = db.get(HistoryRevision, history_id)
        if not history or history.symbol != symbol or history.target_session != manifest.target_session or history.input_mode != "COMPLETED_DAILY":
            raise DataError("MANIFEST_HISTORY_MISMATCH")
        if checksum(history.payload) != history.checksum:
            raise DataError("HISTORY_CHECKSUM_MISMATCH")
        if history.payload["metadata"].get("provenance") == "LIVE_UNVERIFIED":
            from service.quant_data_validation import history_accepted
            if not history_accepted(history.payload):
                raise DataError("QUANT_DATA_NOT_VALIDATED")
        frame = DailyHistory(symbol, history.target_session, history.input_mode, history.payload).frame()
        if symbol == "VNINDEX":
            index = frame
        elif universe.payload["members"][symbol].get("sector") and len(frame) >= minimum_bars:
            data[symbol], sectors[symbol] = frame, universe.payload["members"][symbol]["sector"]
    if index is None or len(index) < minimum_bars:
        raise DataError("INDEX_NOT_READY", True)
    return data, index, sectors


def save_candidate(db, manifest: DataManifest, artifact: dict, config: QuantConfig) -> ModelVersion:
    if artifact["cutoff_session"] != manifest.target_session.isoformat():
        raise DataError("MODEL_CUTOFF_MISMATCH")
    histories = [db.get(HistoryRevision, history_id) for history_id in manifest.histories.values()]
    reference = db.get(UniverseVersion, manifest.universe_id)
    from service.quant_data_validation import POLICY, SCOPE, universe_accepted, history_accepted
    source_verified = bool(reference and universe_accepted(reference.payload)) and all(
        h and history_accepted(h.payload) for h in histories)
    evidence = {"live_data_validated": source_verified, "validation_accepted": artifact["validation"]["accepted"],
        "exclusions": manifest.coverage["excluded"], "manifest_checksum": manifest.checksum,
        "source_validation_policy": POLICY, "source_validation_scope": SCOPE,
        "independent_corporate_action_audit": False}
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
    signal_dependency_change(db)
    return model


def train_manifest(sessions, manifest_id: str, config: QuantConfig, policy: TrainingPolicy | None = None, *, operation_guard=None) -> ModelVersion:
    with sessions() as db:
        if operation_guard:
            operation_guard(db)
        manifest = db.get(DataManifest, manifest_id)
        if not manifest:
            raise DataError("MANIFEST_NOT_FOUND")
        data, index, sectors = manifest_frames(db, manifest, config.MIN_HISTORY)
        pointer = db.get(PublishedModel, "weekly-lightgbm")
        incumbent = db.get(ModelVersion, pointer.model_id) if pointer else None
        incumbent_artifact = incumbent.artifact if incumbent else None
    artifact = train_artifact(data, index, sectors, config, policy, incumbent_artifact)
    with sessions() as db, db.begin():
        if operation_guard:
            operation_guard(db)
        manifest = db.get(DataManifest, manifest_id)
        return save_candidate(db, manifest, artifact, config)
