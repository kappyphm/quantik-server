"""Consistent database bundles and restores into a separate, empty target.

Credentials stay in child environments, never command arguments or manifests.
PostgreSQL utilities may run natively or in an explicitly selected local database container.
"""
from datetime import datetime, timezone
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url

from service.db import make_engine


class BackupError(Exception):
    pass


def digest(path: Path) -> str:
    result = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b""):
            result.update(block)
    return result.hexdigest()


def sqlite_path(url: str) -> Path:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:" or parsed.query:
        raise BackupError("SQLITE_FILE_REQUIRED")
    return Path(parsed.database).resolve()


def sqlite_read(path: Path):
    return sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=15)


def sqlite_verify(db) -> str:
    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
        raise BackupError("SQLITE_INTEGRITY_FAILED")
    rows = db.execute("SELECT version_num FROM alembic_version").fetchall()
    if len(rows) != 1:
        raise BackupError("MIGRATION_REVISION_REQUIRED")
    return rows[0][0]


def copy_sqlite(source: Path, target: Path) -> str:
    deadline = time.monotonic()+120
    def progress(*_):
        if time.monotonic() > deadline:
            raise BackupError("SQLITE_BACKUP_TIMEOUT")
    with closing(sqlite_read(source)) as original, closing(sqlite3.connect(target, timeout=15)) as restored:
        original.backup(restored, pages=256, progress=progress, sleep=.05)
        return sqlite_verify(restored)


def private_file(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)


def sync_file(path: Path) -> None:
    # Windows _commit requires a writable descriptor, even after writers closed.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def pg_environment(url: str) -> dict:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not parsed.database or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", parsed.database):
        raise BackupError("POSTGRES_DATABASE_NAME_REQUIRED")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update(PGHOST=parsed.host or "localhost", PGPORT=str(parsed.port or 5432), PGDATABASE=parsed.database,
        PGCONNECT_TIMEOUT="10", PGAPPNAME="quantik-backup")
    if parsed.username:
        env["PGUSER"] = parsed.username
    if parsed.password is not None:
        env["PGPASSWORD"] = parsed.password
    supported = {"sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY", "options": "PGOPTIONS", "connect_timeout": "PGCONNECT_TIMEOUT"}
    for key, value in parsed.query.items():
        if key not in supported or not isinstance(value, str):
            raise BackupError("UNSUPPORTED_POSTGRES_URL_OPTION")
        env[supported[key]] = value
    return env


def pg_command(tool: str, url: str, container: str | None) -> tuple[list[str], dict]:
    env = pg_environment(url)
    if not container:
        executable = shutil.which(tool)
        if executable is None:
            raise BackupError("POSTGRES_TOOL_UNAVAILABLE")
        return [executable], env
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container) or env["PGHOST"] not in {"127.0.0.1", "localhost"}:
        raise BackupError("LOCAL_POSTGRES_CONTAINER_REQUIRED")
    inspected = subprocess.run(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container],
        capture_output=True, timeout=15, check=False)
    if inspected.returncode:
        raise BackupError("POSTGRES_CONTAINER_UNAVAILABLE")
    ports = json.loads(inspected.stdout).get("5432/tcp") or []
    if not any(row["HostPort"] == env["PGPORT"] and row["HostIp"] in {"127.0.0.1", "0.0.0.0"} for row in ports):
        raise BackupError("POSTGRES_CONTAINER_PORT_MISMATCH")
    # The published endpoint was verified; utilities connect within that same server container.
    env.update(PGHOST="127.0.0.1", PGPORT="5432")
    command = ["docker", "exec", "-i"]
    for key in sorted(key for key in env if key.startswith("PG")):
        command.extend(["--env", key])
    return [*command, container, tool], env


def pg_run(command: list[str], env: dict, *, source=None, destination=None) -> None:
    result = subprocess.run(command, env=env, stdin=source or subprocess.DEVNULL,
        stdout=destination or subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800, check=False)
    if result.returncode:
        # PostgreSQL error text can contain credentials, data, object names or source URLs.
        raise BackupError("POSTGRES_TOOL_FAILED")


def pg_backup(url: str, path: Path, container: str | None) -> str:
    command, env = pg_command("pg_dump", url, container)
    engine = make_engine(url)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db, db.begin():
            revision = db.scalar(text("SELECT version_num FROM alembic_version"))
            snapshot = db.scalar(text("SELECT pg_export_snapshot()"))
            with path.open("wb") as stream:
                pg_run([*command, "--format=custom", "--no-owner", "--no-acl", "--snapshot="+snapshot], env, destination=stream)
            return revision
    finally:
        engine.dispose()


def create_backup(url: str, directory: Path, *, container: str | None = None) -> Path:
    dialect = make_url(url).get_backend_name()
    if dialect not in {"sqlite", "postgresql"}:
        raise BackupError("UNSUPPORTED_DATABASE")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = "backup-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:10]
    partial, completed = directory/(name+".partial"), directory/name
    partial.mkdir(mode=0o700)
    database = partial/("database.sqlite" if dialect == "sqlite" else "database.dump")
    manifest = partial/"manifest.json"
    try:
        private_file(database)
        revision = copy_sqlite(sqlite_path(url), database) if dialect == "sqlite" else pg_backup(url, database, container)
        if not revision or not re.fullmatch(r"[0-9]{4}", revision):
            raise BackupError("MIGRATION_REVISION_REQUIRED")
        private_file(manifest)
        manifest.write_text(json.dumps({"format": 1, "dialect": dialect, "schema_revision": revision,
            "created_at": datetime.now(timezone.utc).isoformat(), "database_file": database.name,
            "size_bytes": database.stat().st_size, "sha256": digest(database)}, indent=2)+"\n", encoding="utf-8")
        sync_file(database)
        sync_file(manifest)
        partial.rename(completed)
        return completed
    except BaseException:
        # Only files created by this invocation; no recursive cleanup or retention deletion.
        for path in (database, manifest):
            path.unlink(missing_ok=True)
        partial.rmdir()
        raise


def inspect_backup(directory: Path) -> tuple[dict, Path]:
    directory = directory.resolve()
    manifest = directory/"manifest.json"
    if manifest.is_symlink() or manifest.stat().st_size > 65536:
        raise BackupError("INVALID_BACKUP_MANIFEST")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    expected = {"sqlite": "database.sqlite", "postgresql": "database.dump"}.get(data.get("dialect"))
    if data.get("format") != 1 or not expected or data.get("database_file") != expected:
        raise BackupError("INVALID_BACKUP_MANIFEST")
    database = directory/expected
    if database.is_symlink() or database.stat().st_size != data.get("size_bytes") or digest(database) != data.get("sha256"):
        raise BackupError("BACKUP_CHECKSUM_MISMATCH")
    return data, database


def restore_backup(directory: Path, target_url: str, current_url: str, *, container: str | None = None) -> dict:
    manifest, source = inspect_backup(directory)
    target = make_url(target_url)
    if target.get_backend_name() != manifest["dialect"]:
        raise BackupError("BACKUP_DIALECT_MISMATCH")
    if manifest["dialect"] == "sqlite":
        destination = sqlite_path(target_url)
        if make_url(current_url).get_backend_name() == "sqlite" and destination == sqlite_path(current_url):
            raise BackupError("RESTORE_CURRENT_DATABASE_FORBIDDEN")
        destination.parent.mkdir(parents=True, exist_ok=True)
        private_file(destination)  # O_EXCL: never overwrite even an empty existing database.
        try:
            revision = copy_sqlite(source, destination)
            if revision != manifest["schema_revision"]:
                raise BackupError("RESTORE_REVISION_MISMATCH")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    else:
        # Fail closed across aliases/hosts: disaster-recovery target must have a different DB name.
        if target.database == make_url(current_url).database:
            raise BackupError("RESTORE_CURRENT_DATABASE_FORBIDDEN")
        command, env = pg_command("pg_restore", target_url, container)
        engine = make_engine(target_url)
        try:
            with engine.connect() as db:
                objects = db.scalar(text("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'"))
                if objects:
                    raise BackupError("RESTORE_TARGET_NOT_EMPTY")
                functions = db.scalar(text("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'"))
                if functions:
                    raise BackupError("RESTORE_TARGET_NOT_EMPTY")
            with source.open("rb") as stream:
                pg_run([*command, "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl", "--dbname="+env["PGDATABASE"]],
                    env, source=stream)
            with engine.connect() as db:
                if db.scalar(text("SELECT version_num FROM alembic_version")) != manifest["schema_revision"]:
                    raise BackupError("RESTORE_REVISION_MISMATCH")
        finally:
            engine.dispose()
    return {"status": "RESTORED", "schema_revision": manifest["schema_revision"], "dialect": manifest["dialect"]}
