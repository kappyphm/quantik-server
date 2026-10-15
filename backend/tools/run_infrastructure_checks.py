"""Opt-in PostgreSQL/RabbitMQ checks using existing images, tmpfs and owned-resource cleanup.

No image pull/build, persistent volumes, real credentials or provider calls.
Run from repository root with the shared venv activated.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres-image", required=True, help="Existing image tag or sha256 ID")
    parser.add_argument("--broker-image", help="Existing image tag or sha256 ID")
    parser.add_argument("--backup-only", action="store_true", help="Only one PostgreSQL container and focused backup/restore checks")
    args = parser.parse_args()
    if not args.backup_only and not args.broker_image:
        parser.error("--broker-image is required unless --backup-only")
    root = Path(__file__).resolve().parents[2]
    run_id = uuid4().hex[:10]
    directory = root / "runtime" / "infrastructure-check"
    directory.mkdir(parents=True, exist_ok=True)
    env_path = directory / (run_id+".env")
    password = secrets.token_urlsafe(32)
    env_path.write_text(f"POSTGRES_USER=quantik_check\nPOSTGRES_DB=quantik_check\nPOSTGRES_PASSWORD={password}\n"
        f"RABBITMQ_DEFAULT_USER=quantik_check\nRABBITMQ_DEFAULT_PASS={password}\n"
        "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS=+S 2:2 +sbwt none +sbwtdcpu none +sbwtdio none\n", encoding="utf-8")
    with env_path.open("a", encoding="utf-8") as file:
        file.write("ERL_FLAGS=+S 2:2\n")
    containers = []

    def command(arguments, *, check=True, env=None, timeout=60):
        result = subprocess.run(arguments, cwd=root, env=env, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if check and result.returncode:
            # Do not include commands, connection URLs or inherited environment in failures.
            detail = (result.stdout+result.stderr).replace(password, "<redacted>")[-7000:]
            raise RuntimeError(f"Command failed ({result.returncode}): {detail}")
        return result

    def launch(suffix, image, port, mount, memory, extra):
        command(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
        name = f"quantik-server-check-{run_id}-{suffix}"
        containers.append(name)
        command(["docker", "run", "-d", "--pull=never", "--name", name,
            "--label", "quantik.check="+run_id, "--memory", memory, "--cpus", "1",
            "--env-file", str(env_path), "--tmpfs", mount, "-p", f"127.0.0.1::{port}", image, *extra])
        mapped = command(["docker", "port", name, str(port)+"/tcp"]).stdout.strip()
        print("Temporary container started: "+suffix, flush=True)
        return name, int(mapped.rsplit(":", 1)[1])

    def ready(name, probe, *, rabbit=False):
        deadline = time.monotonic()+90
        while True:
            # Root probes can create a root-only Erlang cookie during Rabbit initialization.
            prefix = ["docker", "exec"] + (["--user", "rabbitmq"] if rabbit else [])
            if command([*prefix, name, *probe], check=False, timeout=15).returncode == 0:
                return
            state = command(["docker", "inspect", name, "--format", "{{json .State}}"], check=False)
            terminal = state.returncode or not json.loads(state.stdout)["Running"]
            if terminal or time.monotonic() >= deadline:
                logs = command(["docker", "logs", "--tail", "25", name], check=False)
                raise RuntimeError("Temporary infrastructure readiness failed: "+
                    (logs.stdout+logs.stderr).replace(password, "<redacted>"))
            time.sleep(1)

    started = time.monotonic()
    try:
        db, db_port = launch("db", args.postgres_image, 5432,
            "/var/lib/postgresql/data:rw,nosuid,size=256m", "512m",
            ["postgres", "-c", "shared_buffers=32MB", "-c", "max_connections=40", "-c", "work_mem=2MB",
                "-c", "min_wal_size=32MB", "-c", "max_wal_size=96MB"])
        ready(db, ["pg_isready", "-U", "quantik_check", "-d", "quantik_check"])
        if args.backup_only:
            from sqlalchemy.engine import URL
            url = URL.create("postgresql+psycopg", username="quantik_check", password=password,
                host="127.0.0.1", port=db_port, database="quantik_check")
            env = os.environ.copy()
            env.update(QUANTIK_ENVIRONMENT="test", VNSTOCK_API_KEY="", QUANTIK_SCHEDULED_PROVIDER_ENABLED="false",
                QUANTIK_CHECK_DATABASE_URL=url.render_as_string(hide_password=False),
                QUANTIK_CHECK_POSTGRES_CONTAINER=db)
            result = command([sys.executable, "-m", "pytest", "backend/tests/test_backups.py", "-q", "--tb=short"],
                env=env, check=False, timeout=120)
            print((result.stdout+result.stderr).replace(password, "<redacted>"), flush=True)
            if result.returncode:
                raise RuntimeError("Backup infrastructure checks failed")
            (directory/"latest-backup-result.json").write_text(json.dumps({"status": "PASSED",
                "duration_seconds": round(time.monotonic()-started, 2), "postgres_image": args.postgres_image,
                "scope": "SQLite and real PostgreSQL backup bundle restore; all model tables compared; no provider calls"}, indent=2), encoding="utf-8")
            return
        broker, broker_port = launch("broker", args.broker_image, 5672,
            "/var/lib/rabbitmq:rw,nosuid,size=128m", "512m", [])
        ready(broker, ["rabbitmq-diagnostics", "-q", "ping"], rabbit=True)
        print("Temporary PostgreSQL and RabbitMQ ready; no persistent volumes", flush=True)
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import URL
        url = URL.create("postgresql+psycopg", username="quantik_check", password=password,
            host="127.0.0.1", port=db_port, database="quantik_check")
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA checks"))
            connection.execute(text("CREATE SCHEMA snapshot_checks"))
        engine.dispose()
        env = os.environ.copy()
        env.update(QUANTIK_ENVIRONMENT="test", VNSTOCK_API_KEY="", QUANTIK_SCHEDULED_PROVIDER_ENABLED="false",
            QUANTIK_CHECK_DATABASE_URL=url.update_query_dict({"options": "-csearch_path=checks"}).render_as_string(hide_password=False),
            QUANTIK_CHECK_SNAPSHOT_URL=url.update_query_dict({"options": "-csearch_path=snapshot_checks"}).render_as_string(hide_password=False),
            QUANTIK_CHECK_AMQP_URL=f"amqp://quantik_check:{password}@127.0.0.1:{broker_port}/%2F",
            QUANTIK_CHECK_BROKER_CONTAINER=broker, QUANTIK_CHECK_OWNER=run_id)
        result = command([sys.executable, "-m", "pytest", "backend/tests/test_infrastructure.py",
            "backend/tests/test_snapshots.py", "-q", "--tb=short"], env=env, check=False, timeout=240)
        print((result.stdout+result.stderr).replace(password, "<redacted>"), flush=True)
        if result.returncode:
            raise RuntimeError("Real infrastructure checks failed")
        # Custom dump/restore into a separate DB, followed by exact content fingerprints.
        command(["docker", "exec", db, "pg_dump", "-U", "quantik_check", "-d", "quantik_check", "-Fc", "-f", "/tmp/check.dump"])
        command(["docker", "exec", db, "createdb", "-U", "quantik_check", "quantik_restore"])
        command(["docker", "exec", db, "pg_restore", "--exit-on-error", "--no-owner", "-U", "quantik_check",
            "-d", "quantik_restore", "/tmp/check.dump"])
        from hashlib import sha256
        from service.db import Base
        from service import models  # noqa: F401

        def fingerprint(database):
            engine = create_engine(url.set(database=database))
            result = {}
            try:
                with engine.connect() as connection:
                    for schema in ("checks", "snapshot_checks"):
                        connection.execute(text("SET search_path TO "+schema))
                        for table in Base.metadata.sorted_tables:
                            rows = list(connection.execute(table.select().order_by(*table.primary_key.columns)).mappings())
                            result[schema+"."+table.name] = {"rows": len(rows), "sha256": sha256(json.dumps(
                                [dict(row) for row in rows], sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()}
                        result[schema+".schema_revision"] = connection.scalar(text("SELECT version_num FROM alembic_version"))
            finally:
                engine.dispose()
            return result

        original = fingerprint("quantik_check")
        restored = fingerprint("quantik_restore")
        if original != restored:
            raise RuntimeError("Restored database does not match the original content")
        report = {"status": "PASSED", "duration_seconds": round(time.monotonic()-started, 2),
            "postgres_image": args.postgres_image, "broker_image": args.broker_image,
            "restore_fingerprints": original, "scope": "Local real infrastructure with explicit synthetic fixtures; not live scientific/VPS evidence"}
        (directory/"latest-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("pg_dump/pg_restore matched all table contents in both isolated schemas", flush=True)
    finally:
        cleanup_errors = []
        for name in reversed(containers):
            try:
                inspect = command(["docker", "inspect", name, "--format", '{{index .Config.Labels "quantik.check"}}'], check=False)
                if inspect.returncode == 0 and inspect.stdout.strip() == run_id:
                    command(["docker", "rm", "-f", "-v", name])
            except Exception:
                cleanup_errors.append(name)
        inventory = command(["docker", "ps", "-a", "--format", "{{.Names}}"], check=False)
        if inventory.returncode:
            cleanup_errors.append("docker-inventory-unavailable")
        else:
            remaining = set(inventory.stdout.splitlines()).intersection(containers)
            cleanup_errors.extend(sorted(remaining))
        env_path.unlink(missing_ok=True)
        if cleanup_errors:
            raise RuntimeError("Cleanup could not confirm removal: "+", ".join(cleanup_errors))
        print("Owned temporary containers removed; temporary credentials deleted", flush=True)


if __name__ == "__main__":
    main()
