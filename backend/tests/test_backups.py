import sqlite3
import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest

from service.backups import BackupError, create_backup, inspect_backup, restore_backup
from service.db import Base, make_engine, session_factory
from service.models import Account
from sqlalchemy import text
from sqlalchemy.engine import make_url


def test_consistent_backup_restore_and_refuse_overwrite(tmp_path, monkeypatch):
    source = tmp_path/"source.sqlite"
    url = "sqlite:///"+source.as_posix()
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    command.upgrade(Config("backend/alembic.ini"), "head")
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO accounts(id,role,enabled,username) VALUES ('staff','admin',1,'fixture')")
    bundle = create_backup(url, tmp_path/"backups")
    manifest, database = inspect_backup(bundle)
    assert manifest["schema_revision"] == "0012" and "source" not in manifest
    # Copy retains committed data even after source changes.
    with sqlite3.connect(source) as db:
        db.execute("UPDATE accounts SET username='new-source-value' WHERE id='staff'")
    destination = tmp_path/"restored.sqlite"
    restored_url = "sqlite:///"+destination.as_posix()
    assert restore_backup(bundle, restored_url, url)["status"] == "RESTORED"
    with sqlite3.connect(destination) as db:
        assert db.execute("SELECT username FROM accounts WHERE id='staff'").fetchone() == ("fixture",)
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
    with pytest.raises(FileExistsError):
        restore_backup(bundle, restored_url, url)
    with pytest.raises(BackupError, match="RESTORE_CURRENT_DATABASE_FORBIDDEN"):
        restore_backup(bundle, url, url)
    with database.open("ab") as stream:
        stream.write(b"tampered")
    rejected = tmp_path/"rejected.sqlite"
    with pytest.raises(BackupError, match="BACKUP_CHECKSUM_MISMATCH"):
        restore_backup(bundle, "sqlite:///"+rejected.as_posix(), url)
    assert not rejected.exists()
    assert not list((tmp_path/"backups").glob("*.partial"))


def test_postgres_bundle_restore_matches_every_table(tmp_path, monkeypatch):
    url, container = os.environ.get("QUANTIK_CHECK_DATABASE_URL"), os.environ.get("QUANTIK_CHECK_POSTGRES_CONTAINER")
    if not url or not container:
        pytest.skip("Use tmpfs infrastructure runner --backup-only")
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    with session_factory(engine)() as db, db.begin():
        db.add(Account(id="backup-fixture", role="admin", username="before-backup"))
    def content(db_engine):
        with db_engine.connect() as db:
            return {table.name: list(db.execute(table.select().order_by(*table.primary_key.columns)).mappings())
                for table in Base.metadata.sorted_tables}
    expected = content(engine)
    bundle = create_backup(url, tmp_path/"backups", container=container)
    manifest, _ = inspect_backup(bundle)
    assert manifest["dialect"] == "postgresql"
    assert make_url(url).password not in (bundle/"manifest.json").read_text()
    name = "quantik_restore_"+uuid4().hex[:10]
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
        db.execute(text('CREATE DATABASE "'+name+'"'))
        db.execute(text("UPDATE accounts SET username='after-backup' WHERE id='backup-fixture'"))
    target = make_url(url).set(database=name).render_as_string(hide_password=False)
    assert restore_backup(bundle, target, url, container=container)["status"] == "RESTORED"
    restored = make_engine(target)
    try:
        assert content(restored) == expected
        with pytest.raises(BackupError, match="RESTORE_TARGET_NOT_EMPTY"):
            restore_backup(bundle, target, url, container=container)
        with pytest.raises(BackupError, match="RESTORE_CURRENT_DATABASE_FORBIDDEN"):
            restore_backup(bundle, url, url, container=container)
    finally:
        restored.dispose()
        engine.dispose()
