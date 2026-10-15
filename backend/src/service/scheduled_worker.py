"""One fenced operation per global lane; heavy engines are imported only after readiness."""
import threading
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from service import scheduling
from service.market_data import DataError
from service.jobs import JobError
from service.models import DataManifest, HistoryRevision, ModelVersion, PublishedModel, UniverseVersion, ScheduledTask
from service.settings import Settings


class Heartbeat:
    def __init__(self, sessions, settings, task_id: str, token: str):
        self.sessions, self.settings, self.task_id, self.token = sessions, settings, task_id, token
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.wait(20):
            try:
                scheduling.heartbeat(self.sessions, self.settings, self.task_id, self.token)
            except Exception:
                return  # Effects still require assert_current in their commit transaction.

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=5)


def completed_manifest(db: Session, target: date, settings: Settings) -> DataManifest:
    manifest = db.scalar(select(DataManifest).where(DataManifest.target_session == target,
        DataManifest.input_mode == "COMPLETED_DAILY").order_by(DataManifest.revision_order.desc()).limit(1))
    if manifest is None:
        raise DataError("COMPLETED_MANIFEST_NOT_READY", True)
    if settings.allow_fixture_analysis and settings.environment != "production":
        return manifest
    reference = db.get(UniverseVersion, manifest.universe_id)
    if not reference or reference.payload.get("provenance") != "LIVE_VALIDATED":
        raise DataError("UNIVERSE_NOT_VALIDATED", True)
    rows = list(db.execute(select(HistoryRevision.id, HistoryRevision.payload["metadata"])
        .where(HistoryRevision.id.in_(list(manifest.histories.values())))))
    if len(rows) != len(manifest.histories) or any(metadata.get("provenance") != "LIVE_VALIDATED" or
            metadata.get("adjustment") != "VERIFIED" for _, metadata in rows):
        raise DataError("LIVE_COMPLETED_DATA_NOT_VALIDATED", True)
    return manifest


def execute(sessions: sessionmaker[Session], settings: Settings, task: ScheduledTask, token: str) -> dict:
    def guard(db):
        return scheduling.assert_current(db, task.id, token, settings)

    with sessions() as db, db.begin():
        guard(db)
        policy = scheduling.policy_for(db, task)
    if task.kind == "INGEST":
        # Explicit environment switch is independent of admin config, respecting provider pauses.
        if not settings.scheduled_provider_enabled:
            raise DataError("SCHEDULED_PROVIDER_DISABLED", True)
        from service.ingestion import ingest_market
        result = ingest_market(settings, task.target_session-timedelta(days=policy.history_calendar_days),
            task.target_session, input_mode="PROVISIONAL_DAILY" if task.slot == "AM" else "COMPLETED_DAILY",
            operation_guard=guard, resume=True)
        if not result["manifest_id"]:
            raise DataError("INGESTION_MANIFEST_NOT_READY", True, result.get("retry_after"))
        return {"manifest_id": result["manifest_id"], "histories_available": result["histories_available"]}
    if task.kind == "SCREEN":
        from service.screening import run_screening
        run = run_screening(sessions, task.target_session, task.slot, allow_fixture=settings.allow_fixture_analysis,
            operation_guard=guard)
        if run.status != "READY":
            raise DataError(run.reason or "SCREENING_NOT_READY", run.status == "WAITING_DATA")
        return {"screening_run_id": run.id, "status": run.status}
    with sessions() as db:
        manifest_id = completed_manifest(db, task.target_session, settings).id
        if task.kind == "SNAPSHOT":
            pointer = db.get(PublishedModel, "weekly-lightgbm")
            model = db.get(ModelVersion, pointer.model_id) if pointer else None
            if not model or model.status != "PUBLISHED" or not 0 <= (task.target_session-model.cutoff_session).days < 28:
                raise DataError("PUBLISHED_MODEL_NOT_READY", True)
    from service.quant.configuration import QuantConfig
    from dataclasses import replace
    config = replace(QuantConfig(), PRICE_INPUT_UNIT="VND")
    if task.kind == "SNAPSHOT":
        from service.snapshots import build_market_snapshot
        snapshot = build_market_snapshot(sessions, manifest_id, config, operation_guard=guard)
        return {"snapshot_id": snapshot.id, "status": snapshot.status}
    if task.kind == "TRAIN":
        from service.model_lifecycle import promote_model, train_manifest
        model = train_manifest(sessions, manifest_id, config, operation_guard=guard)
        if model.status == "CANDIDATE" and policy.auto_promote:
            with sessions() as db, db.begin():
                guard(db)
                model = promote_model(db, model.id)
        return {"model_id": model.id, "status": model.status}
    raise DataError("UNKNOWN_SCHEDULED_OPERATION")


def run_once(sessions: sessionmaker[Session], settings: Settings, kind: str) -> int:
    claimed = scheduling.claim(sessions, settings, kind)
    if not claimed:
        return 0
    task, token = claimed
    result, error = None, None
    try:
        with Heartbeat(sessions, settings, task.id, token):
            result = execute(sessions, settings, task, token)
            with sessions() as db, db.begin():
                scheduling.assert_current(db, task.id, token, settings)
    except DataError as exc:
        error = exc
    except JobError as exc:
        error = DataError(exc.code)
    except Exception:
        error = DataError("SCHEDULED_OPERATION_ERROR", True)
    scheduling.finish(sessions, task.id, token, result=result, error=error)
    return 1
