import argparse
import os
from pathlib import Path
from datetime import date

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import load_dotenv

from service.db import make_engine, session_factory
from service.identity import seed_admin
from service.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="quantik")
    parser.add_argument("command", choices=["migrate", "generate-keys", "generate-integration-key", "seed-admin", "ingest-market", "train-model", "publish-model", "build-snapshot", "worker", "dispatcher", "dependencies", "reconciler", "recover-delivery", "screen-market", "seed-operations", "scheduler", "schedule-worker", "backup", "restore-backup", "verify-backup"])
    parser.add_argument("--target-session", type=date.fromisoformat)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 1, 1))
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--manifest-id")
    parser.add_argument("--model-id")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--transport", choices=["amqp", "local"], default="amqp")
    parser.add_argument("--slot", choices=["AM", "PM"])
    parser.add_argument("--input-mode", choices=["COMPLETED_DAILY", "PROVISIONAL_DAILY"], default="COMPLETED_DAILY")
    parser.add_argument("--minimum-bars", type=int, default=1)
    parser.add_argument("--kind", choices=["INGEST", "SCREEN", "SNAPSHOT", "TRAIN"])
    parser.add_argument("--directory", type=Path, default=Path("runtime/backups"))
    parser.add_argument("--backup", type=Path, help="Completed backup bundle directory")
    parser.add_argument("--postgres-container", help="Explicit local PostgreSQL container used for pg_dump/pg_restore utilities")
    args = parser.parse_args()
    load_dotenv()
    settings = Settings()
    Path("runtime").mkdir(exist_ok=True)
    if args.command in {"backup", "restore-backup", "verify-backup"}:
        from service.backups import BackupError, create_backup, inspect_backup, restore_backup
        try:
            if args.command == "backup":
                result = {"backup_directory": str(create_backup(settings.database_url, args.directory, container=args.postgres_container))}
            elif not args.backup:
                parser.error("--backup is required")
            elif args.command == "verify-backup":
                manifest, _ = inspect_backup(args.backup)
                result = {"status": "CHECKSUM_VERIFIED", "dialect": manifest["dialect"], "schema_revision": manifest["schema_revision"]}
            else:
                target = os.environ.get("QUANTIK_RESTORE_DATABASE_URL")
                if not target:
                    parser.error("Set QUANTIK_RESTORE_DATABASE_URL to a separate empty restore target")
                result = restore_backup(args.backup, target, settings.database_url, container=args.postgres_container)
            print(result)
        except BackupError as exc:
            raise SystemExit(str(exc)) from None
        except Exception as exc:
            raise SystemExit("BACKUP_OPERATION_FAILED:"+type(exc).__name__) from None
    elif args.command == "generate-integration-key":
        from cryptography.fernet import Fernet
        path = settings.integration_encryption_key_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SystemExit("Integration encryption key already exists; refusing to overwrite") from None
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(Fernet.generate_key()+b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        print("Integration encryption key generated; back it up separately and restrict file access")
    elif args.command == "migrate":
        command.upgrade(Config("backend/alembic.ini"), "head")
    elif args.command == "seed-operations":
        from sqlalchemy import select
        from service.models import Account
        from service.calendar_reference import seed_defaults
        engine = make_engine(settings.database_url)
        try:
            with session_factory(engine)() as db, db.begin():
                actor = db.scalar(select(Account).where(Account.role == "admin", Account.enabled.is_(True)).limit(1))
                if actor is None:
                    parser.error("seed-operations requires an existing enabled seed-admin")
                result = seed_defaults(db, actor.id)
            print({"active_versions": result})
        finally:
            engine.dispose()
    elif args.command in {"worker", "dispatcher", "dependencies", "reconciler", "recover-delivery", "scheduler", "schedule-worker"}:
        if args.command == "schedule-worker" and not args.kind:
            parser.error("schedule-worker requires --kind INGEST|SCREEN|SNAPSHOT|TRAIN")
        from service.runtime import run_service
        run_service(args.command, settings, once=args.once, transport=args.transport, kind=args.kind)
    elif args.command == "generate-keys":
        private_path, public_path = settings.jwt_private_key_path, settings.jwt_public_key_path
        if private_path.exists() or public_path.exists():
            raise SystemExit("Key files already exist; refusing to overwrite")
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        private_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.parent.mkdir(parents=True, exist_ok=True)
        with private_path.open("xb") as file:
            file.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
        with public_path.open("xb") as file:
            file.write(key.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))
        print("JWT keys generated; restrict private-key file access before deployment")
    elif args.command == "ingest-market":
        if not args.target_session:
            parser.error("ingest-market requires --target-session YYYY-MM-DD")
        from service.ingestion import ingest_market
        from service.market_data import DataError
        try:
            result = ingest_market(settings, args.start, args.target_session, args.symbol, args.input_mode, args.minimum_bars)
        except DataError as exc:
            print(f"Ingestion stopped: {exc.code}")
            raise SystemExit(1) from None
        print(result)
        if result["manifest_id"] is None:
            raise SystemExit(1)
    elif args.command == "screen-market":
        if not args.target_session or not args.slot:
            parser.error("screen-market requires --target-session YYYY-MM-DD and --slot AM|PM")
        from service.screening import run_screening
        engine = make_engine(settings.database_url)
        try:
            run = run_screening(session_factory(engine), args.target_session, args.slot,
                manifest_id=args.manifest_id, allow_fixture=settings.allow_fixture_analysis)
            print({"run_id": run.id, "status": run.status, "reason": run.reason})
            if run.status == "FAILED":
                raise SystemExit(1)
        finally:
            engine.dispose()
    elif args.command in {"train-model", "publish-model", "build-snapshot"}:
        from dataclasses import replace
        from service.model_lifecycle import promote_model, train_manifest
        from service.market_data import DataError
        from service.quant.configuration import QuantConfig
        engine = make_engine(settings.database_url)
        sessions = session_factory(engine)
        try:
            if args.command in {"build-snapshot", "train-model"} and args.manifest_id:
                from service.analysis_readiness import validate_manifest, model_for_session
                from service.models import DataManifest
                config = replace(QuantConfig(), PRICE_INPUT_UNIT="VND")
                with sessions() as db:
                    manifest = db.get(DataManifest, args.manifest_id)
                    if not manifest:
                        raise DataError("MANIFEST_NOT_FOUND")
                    validate_manifest(db, manifest, settings)
                    if args.command == "build-snapshot" and not settings.allow_degraded_analysis and not model_for_session(db, manifest.target_session, config):
                        raise DataError("MODEL_NOT_READY", True)
            if args.command == "build-snapshot":
                if not args.manifest_id:
                    parser.error("build-snapshot requires --manifest-id")
                from service.snapshots import build_market_snapshot
                snapshot = build_market_snapshot(sessions, args.manifest_id, replace(QuantConfig(), PRICE_INPUT_UNIT="VND"))
                print({"snapshot_id": snapshot.id, "status": snapshot.status, "coverage_ratio": snapshot.payload["coverage_ratio"]})
            elif args.command == "train-model":
                if not args.manifest_id:
                    parser.error("train-model requires --manifest-id")
                model = train_manifest(sessions, args.manifest_id, replace(QuantConfig(), PRICE_INPUT_UNIT="VND"))
                print({"model_id": model.id, "status": model.status, "validation": model.artifact["validation"]})
            else:
                if not args.model_id:
                    parser.error("publish-model requires --model-id")
                with sessions() as db, db.begin():
                    model = promote_model(db, args.model_id)
                print({"model_id": model.id, "status": model.status})
        except DataError as exc:
            print(f"Model lifecycle stopped: {exc.code}")
            raise SystemExit(1) from None
        except ValueError:
            print("Training stopped: invalid or insufficient input history")
            raise SystemExit(1) from None
        finally:
            engine.dispose()
    else:
        username = os.environ.get("QUANTIK_ADMIN_USERNAME", "admin")
        password = os.environ.get("QUANTIK_ADMIN_PASSWORD", "")
        engine = make_engine(settings.database_url)
        try:
            with session_factory(engine)() as db, db.begin():
                created = seed_admin(db, username, password)
            print("Admin created" if created else "Existing admin preserved")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
