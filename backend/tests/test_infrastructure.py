"""Opt-in local real-infrastructure checks; fixtures never claim scientific/live acceptance."""
from entitlement_fixtures import paid_bucket, fail_job, complete_job
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import secrets
import subprocess
import threading
import time
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import bindparam, func, select
from sqlalchemy.dialects.postgresql import JSONB

from service import admin, billing, jobs, scheduling
from service.analysis_worker import AnalysisWorker
from service.broker import RabbitTransport, dispatch_once
from service.db import make_engine, session_factory
from service.delivery import Envelope
from service.models import (Account, AnalysisReport, BankPayment, Inbox, Job, Outbox, QuotaBucket,
    QuotaRefund, Reservation, Subscription, UsageLedger, ActiveRuntimeConfig, ScheduledTask)
from service.operational_config import activate, create_version
from service.reports import SECTION_NAMES
from service.canonical_json import semantic_checksum
from service.market_data import checksum
from service.settings import Settings


@pytest.fixture
def infrastructure(monkeypatch):
    url = os.environ.get("QUANTIK_CHECK_DATABASE_URL")
    if not url:
        pytest.skip("Use the opt-in tmpfs infrastructure runner")
    monkeypatch.setenv("QUANTIK_DATABASE_URL", url)
    monkeypatch.setenv("QUANTIK_ENVIRONMENT", "test")
    command.upgrade(Config("backend/alembic.ini"), "head")
    engine = make_engine(url)
    yield session_factory(engine), url
    engine.dispose()


def parallel(operation, count=12):
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait(timeout=15)
        try:
            return operation(index)
        except jobs.JobError as exc:
            return {"code": exc.code, "status": exc.status}

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


def customer(sessions, total=20, active=3):
    with sessions() as db, db.begin():
        account = Account(role="user", google_sub=uuid4().hex)
        db.add(account)
        db.flush()
        bucket = paid_bucket(db, account.id, valid_from=int(time.time())-10, valid_until=int(time.time())+3600, quota_total=total, active_limit=active)
        db.add(bucket)
        db.flush()
        return account.id, bucket.id


VERSIONS = {"data_manifest": "fixture", "market_snapshot": "fixture", "model": "fixture", "config": "fixture", "engines": {}}


def test_postgres_jsonb_numeric_checksum_roundtrip(infrastructure):
    sessions, _ = infrastructure
    original = {"signed_zero": -0.0, "integer_exponent": 1e20, "small": 1.234567890123456e-17,
        "fraction": 0.30000000000000004, "boolean": False}
    with sessions() as db:
        restored = db.scalar(select(bindparam("payload", original, type_=JSONB)))
    assert checksum(original) != checksum(restored)  # Reproduces the text-hash failure on real JSONB.
    assert semantic_checksum(original) == semantic_checksum(restored)
    assert semantic_checksum(original) != semantic_checksum(restored | {"fraction": 0.3})


def test_postgres_races_and_atomic_financial_effects(infrastructure):
    sessions, url = infrastructure
    today = datetime.now(timezone.utc).date()
    owner, bucket_id = customer(sessions)

    def admit(index, key=None):
        with sessions() as db, db.begin():
            return jobs.admit(db, owner, "FPT", today, key or "race-"+str(index)).id

    admitted = parallel(admit)
    successful = [result for result in admitted if isinstance(result, str)]
    assert len(successful) == 3
    assert all(item["code"] == "ACTIVE_JOB_LIMIT" for item in admitted if isinstance(item, dict))
    with sessions() as db:
        bucket = db.get(QuotaBucket, bucket_id)
        assert (bucket.reserved, bucket.active_jobs, bucket.consumed) == (3, 3, 0)
    for job_id in successful:
        with sessions() as db, db.begin():
            fail_job(db, job_id)
    repeated = parallel(lambda index: admit(index, "one-idempotent-job"))
    assert all(isinstance(result, str) for result in repeated) and len(set(repeated)) == 1
    with sessions() as db, db.begin():
        fail_job(db, repeated[0])

    race_id = admit(100)
    with sessions() as db, db.begin():
        _, token = jobs.claim(db, race_id)
        jobs.begin_compute(db, race_id, token, VERSIONS)
    stamp = datetime.now(timezone.utc).isoformat()
    report = {"schema_version": "1.0", "job_id": race_id, "symbol": "FPT", "evaluated_at": stamp,
        "data_as_of": stamp, "input_mode": "COMPLETED_DAILY", "versions": VERSIONS,
        "sections": {name: {"status": "AVAILABLE", "metrics": {"explicit_fixture": True}} for name in SECTION_NAMES},
        "decision": {"action": "WATCH"}, "commentary": {"explicit_fixture": "Transaction check only"}}

    def terminate(index):
        with sessions() as db, db.begin():
            if index % 2:
                return fail_job(db, race_id).status
            return jobs.complete(db, race_id, token, report).id

    parallel(terminate)
    with sessions() as db:
        job = db.get(Job, race_id)
        assert job.status in {"SUCCEEDED", "FAILED"}
        reservation = db.scalar(select(Reservation).where(Reservation.job_id == race_id))
        assert reservation.state == ("CONSUMED" if job.status == "SUCCEEDED" else "RELEASED")
        assert db.scalar(select(func.count()).select_from(UsageLedger).where(UsageLedger.reservation_id == reservation.id,
            UsageLedger.effect.in_(["CONSUMED", "RELEASED"]))) == 1
        assert db.scalar(select(func.count()).select_from(AnalysisReport).where(AnalysisReport.job_id == race_id)) == int(job.status == "SUCCEEDED")
        assert (db.get(QuotaBucket, bucket_id).reserved, db.get(QuotaBucket, bucket_id).active_jobs) == (0, 0)
    with sessions() as db, db.begin():
        staff = [Account(role="admin", username=uuid4().hex) for _ in range(4)]
        buyer = Account(role="user", google_sub=uuid4().hex)
        db.add_all([*staff, buyer])
        db.flush()
        staff_ids, buyer_id = [a.id for a in staff], buyer.id
        plan = billing.create_plan(db, billing.PlanInput(name="Infrastructure fixture", price_vnd=99000,
            duration_days=30, analysis_quota=30, active_limit=2), staff_ids[0])
        plan.available = True
        plan_id = plan.id
    settings = Settings(_env_file=None, database_url=url, environment="test", payments_enabled=True,
        SEPAY_WEBHOOK_API_KEY="explicit-test-key", SEPAY_BANK_CODE="TPBank", SEPAY_ACCOUNT_NUMBER="00001111",
        SEPAY_ACCOUNT_NAME="Fixture", SEPAY_REQUIRES_TRANSFER_PREFIX=False)
    with sessions() as db, db.begin():
        order = billing.create_order(db, buyer_id, plan_id, "fixture-order", settings)
        order_id, reference = order.id, order.reference
    base_id = secrets.randbelow(1_000_000_000)+1

    def payment(index, duplicate=False):
        number = base_id if duplicate else base_id+index
        payload = billing.Webhook(id=number, gateway="TPBank", accountNumber="00001111", transferType="in",
            transferAmount=99000, content=reference, referenceCode="FIXTURE"+str(number),
            transactionDate=datetime.now(billing.ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d %H:%M:%S"))
        with sessions() as db, db.begin():
            return billing.receive(db, payload).id

    assert len(set(parallel(lambda index: payment(index, duplicate=True)))) == 1
    parallel(lambda index: payment(index+1))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Subscription).where(Subscription.order_id == order_id)) == 1
        assert db.scalar(select(func.count()).select_from(QuotaBucket).where(QuotaBucket.account_id == buyer_id)) == 1
        assert db.scalar(select(func.count()).select_from(BankPayment).where(BankPayment.order_id == order_id,
            BankPayment.status == "APPLIED")) == 1

    # Financial correction requires a successful execution regardless of which
    # terminal operation won the worker race above.
    refund_job_id = admit(101)
    with sessions() as db, db.begin():
        complete_job(db, refund_job_id)

    def refund(index):
        with sessions() as db, db.begin():
            return admin.refund_quota(db, refund_job_id, staff_ids[index % 4], "refund-"+str(index),
                "Verified fixture correction", "Original execution and settlement inspected").id

    refunded = parallel(refund)
    assert len([value for value in refunded if isinstance(value, str)]) == 1
    with sessions() as db:
        assert db.get(QuotaBucket, bucket_id).consumed == int(job.status == "SUCCEEDED")
        assert db.scalar(select(func.count()).select_from(QuotaRefund).join(Reservation).where(Reservation.job_id == refund_job_id)) == 1


def test_real_broker_confirm_duplicate_restart_and_dead_letter(infrastructure):
    sessions, url = infrastructure
    amqp = os.environ.get("QUANTIK_CHECK_AMQP_URL")
    if not amqp:
        pytest.skip("Opt-in RabbitMQ URL missing")
    settings = Settings(_env_file=None, database_url=url, environment="test", amqp_url=amqp,
        allow_fixture_analysis=False, allow_degraded_analysis=False)
    owner, bucket_id = customer(sessions)
    with sessions() as db, db.begin():
        job = jobs.admit(db, owner, "FPT", datetime.now(timezone.utc).date(), "real-broker")
        job_id = job.id
    namespace = "quantik_check_"+uuid4().hex
    broker = RabbitTransport(settings, namespace)
    try:
        assert dispatch_once(sessions, broker) >= 1
        with sessions() as db:
            event = db.scalar(select(Outbox).where(Outbox.job_id == job_id))
            assert event.published_at is not None
        broker.publish(Envelope(event.id, job_id))
        worker = AnalysisWorker(sessions, settings)
        broker.consume(worker, once=True)
        broker.consume(worker, once=True)
        with sessions() as db:
            assert db.get(Job, job_id).status == "WAITING_DATA"
            assert db.get(Job, job_id).compute_started_at is None
            assert db.scalar(select(func.count()).select_from(Inbox).where(Inbox.job_id == job_id)) == 1
            assert db.get(QuotaBucket, bucket_id).reserved == 1
        # Persistent envelope and durable queue survive RabbitMQ application restart.
        broker.publish(Envelope(str(uuid4()), str(uuid4())))
        name, owner_token = os.environ.get("QUANTIK_CHECK_BROKER_CONTAINER"), os.environ.get("QUANTIK_CHECK_OWNER")
        if name:
            assert name.startswith("quantik-server-check-") and owner_token
            verified = subprocess.run(["docker", "inspect", name, "--format", '{{index .Config.Labels "quantik.check"}}'],
                capture_output=True, text=True, check=True)
            assert verified.stdout.strip() == owner_token
            broker.close()
            for action in ("stop_app", "start_app"):
                subprocess.run(["docker", "exec", "--user", "rabbitmq", name, "rabbitmqctl", action], capture_output=True, check=True, timeout=45)
            broker = RabbitTransport(settings, namespace)
        assert broker.channel.queue_declare(queue=broker.queue, passive=True).method.message_count == 1
        broker.consume(worker, once=True)  # Unknown durable event is poison, never an authorized job.
        broker.channel.basic_publish(exchange=namespace, routing_key="analysis", body=b"invalid-envelope",
            properties=broker.pika.BasicProperties(delivery_mode=2))
        broker.consume(worker, once=True)
        assert broker.channel.queue_declare(queue=broker.queue+".dead", passive=True).method.message_count == 2
    finally:
        if broker.connection.is_open:
            broker.channel.queue_delete(queue=broker.queue)
            broker.channel.queue_delete(queue=broker.queue+".dead")
            broker.channel.exchange_delete(exchange=namespace)
            broker.channel.exchange_delete(exchange=namespace+".dead")
            broker.close()


def test_postgres_config_scheduler_and_global_lane_races(infrastructure):
    sessions, url = infrastructure
    settings = Settings(_env_file=None, database_url=url, environment="test")
    now = datetime.now(scheduling.VN)
    today = now.date()
    from datetime import timedelta
    with sessions() as db, db.begin():
        original = create_version(db, "OPERATIONS", {}, None)
        activate(db, original.id, None, "Activate isolated operation fixture", None)
        original_id = original.id
        versions = [create_version(db, "OPERATIONS", {"retry_seconds": 30+i}, None).id for i in range(12)]
        calendar = create_version(db, "TRADING_CALENDAR", {"start": (today-timedelta(days=1)).isoformat(),
            "end": (today+timedelta(days=7)).isoformat(), "sessions": [(today+timedelta(days=i)).isoformat() for i in (-1, 0, 1)],
            "source_urls": ["https://example.org/fixture"], "review_note": "Isolated explicit sessions for PostgreSQL lock tests", "provenance": "FIXTURE"}, None)
        activate(db, calendar.id, None, "Activate isolated calendar fixture", None, allow_fixture=True)

    def activation(index):
        with sessions() as db, db.begin():
            return activate(db, versions[index], original_id, "Concurrent operator config activation", None).id

    results = parallel(activation)
    assert len([value for value in results if isinstance(value, str)]) == 1
    assert all(value["code"] == "CONFIG_ACTIVATION_CONFLICT" for value in results if isinstance(value, dict))
    reports = parallel(lambda _: scheduling.tick(sessions, settings, now=now))
    assert all(report["state"] == "READY" for report in reports)
    with sessions() as db, db.begin():
        tasks = list(db.scalars(select(ScheduledTask).where(ScheduledTask.kind == "SCREEN", ScheduledTask.target_session == today)))
        assert len(tasks) == 2
        for task in tasks:
            task.status, task.scheduled_at, task.next_attempt_at, task.deadline = "PENDING", int(time.time())-1, int(time.time())-1, int(time.time())+3600
    claims = parallel(lambda _: scheduling.claim(sessions, settings, "SCREEN"))
    assert len([claim for claim in claims if claim]) == 1  # Two runnable tasks, one global lane.
    task, token = next(claim for claim in claims if claim)
    scheduling.heartbeat(sessions, settings, task.id, token)
    scheduling.finish(sessions, task.id, token, result={"explicit_fixture": True})
    second = scheduling.claim(sessions, settings, "SCREEN")
    assert second and second[0].id != task.id
    scheduling.finish(sessions, second[0].id, second[1], result={"explicit_fixture": True})
