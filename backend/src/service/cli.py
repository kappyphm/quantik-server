import argparse
import os
from pathlib import Path

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
    parser.add_argument("command", choices=["migrate", "generate-keys", "seed-admin"])
    args = parser.parse_args()
    load_dotenv()
    settings = Settings()
    Path("runtime").mkdir(exist_ok=True)
    if args.command == "migrate":
        command.upgrade(Config("backend/alembic.ini"), "head")
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
