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
    parser.add_argument("command", choices=["migrate", "generate-keys", "seed-admin", "ingest-market", "train-model", "publish-model", "build-snapshot", "worker", "dispatcher", "dependencies", "reconciler", "recover-delivery"])
    parser.add_argument("--target-session", type=date.fromisoformat)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 1, 1))
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--manifest-id")
    parser.add_argument("--model-id")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--transport", choices=["amqp", "local"], default="amqp")
    args = parser.parse_args()
    load_dotenv()
    settings = Settings()
    Path("runtime").mkdir(exist_ok=True)
    if args.command == "migrate":
        command.upgrade(Config("backend/alembic.ini"), "head")
    elif args.command in {"worker", "dispatcher", "dependencies", "reconciler", "recover-delivery"}:
        from service.runtime import run_service
        run_service(args.command, settings, once=args.once, transport=args.transport)
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
            result = ingest_market(settings, args.start, args.target_session, args.symbol)
        except DataError as exc:
            print(f"Ingestion stopped: {exc.code}")
            raise SystemExit(1) from None
        print(result)
        if result["manifest_id"] is None:
            raise SystemExit(1)
    elif args.command in {"train-model", "publish-model", "build-snapshot"}:
        from dataclasses import replace
        from service.model_lifecycle import promote_model, train_manifest
        from service.market_data import DataError
        from service.quant.configuration import QuantConfig
        engine = make_engine(settings.database_url)
        sessions = session_factory(engine)
        try:
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
