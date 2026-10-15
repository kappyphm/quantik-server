"""RabbitMQ delivery transport. PostgreSQL remains the source of job state."""
from __future__ import annotations
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, or_, select

from service.delivery import Envelope, PoisonMessage
from service.models import Job, Outbox


class BrokerUnavailable(Exception):
    pass


class RabbitTransport:
    def __init__(self, settings, namespace: str = "quantik"):
        import pika
        logging.getLogger("pika").setLevel(logging.CRITICAL)
        self.pika, self.namespace = pika, namespace
        try:
            parameters = pika.URLParameters(settings.amqp_url.get_secret_value())
            parameters.heartbeat = 60
            parameters.blocked_connection_timeout = 10
            parameters.socket_timeout = 5
            parameters.stack_timeout = 10
            parameters.connection_attempts = 1
            self.connection = pika.BlockingConnection(parameters)
            self.channel = self.connection.channel()
            self.channel.exchange_declare(exchange=namespace, exchange_type="direct", durable=True)
            self.channel.exchange_declare(exchange=namespace + ".dead", exchange_type="direct", durable=True)
            self.queue = namespace + ".analysis"
            self.channel.queue_declare(queue=self.queue, durable=True, arguments={
                "x-dead-letter-exchange": namespace + ".dead", "x-dead-letter-routing-key": "analysis"})
            self.channel.queue_declare(queue=self.queue + ".dead", durable=True)
            self.channel.queue_bind(queue=self.queue, exchange=namespace, routing_key="analysis")
            self.channel.queue_bind(queue=self.queue + ".dead", exchange=namespace + ".dead", routing_key="analysis")
            self.channel.confirm_delivery()
            self.channel.basic_qos(prefetch_count=1)
        except Exception:
            if getattr(self, "connection", None) and self.connection.is_open:
                self.connection.close()
            raise BrokerUnavailable("BROKER_UNAVAILABLE") from None

    def publish(self, envelope: Envelope) -> None:
        try:
            self.channel.basic_publish(exchange=self.namespace, routing_key="analysis", body=envelope.encode(),
                mandatory=True, properties=self.pika.BasicProperties(content_type="application/json",
                    delivery_mode=2, message_id=envelope.event_id, type="analysis.ready"))
        except Exception:
            raise BrokerUnavailable("PUBLISH_NOT_CONFIRMED") from None

    def close(self) -> None:
        if self.connection.is_open:
            self.connection.close()

    def consume(self, worker: AnalysisWorker, once: bool = False, stop=None) -> None:
        # One compute slot per process. AMQP heartbeats continue on the connection
        # thread while the bounded executor performs the customer analysis.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis") as executor:
            try:
                for method, properties, body in self.channel.consume(self.queue, auto_ack=False, inactivity_timeout=1):
                    if method is None:
                        if once or (stop and stop.is_set()):
                            break
                        continue
                    try:
                        envelope = Envelope.decode(body)
                        future = executor.submit(worker.process, envelope)
                        while not future.done():
                            self.connection.process_data_events(time_limit=.2)
                        future.result()
                    except PoisonMessage:
                        self.channel.basic_nack(method.delivery_tag, requeue=False)
                    except Exception:
                        self.channel.basic_nack(method.delivery_tag, requeue=True)
                        self.connection.sleep(2)
                    else:
                        self.channel.basic_ack(method.delivery_tag)
                    if once or (stop and stop.is_set()):
                        break
            finally:
                if self.channel.is_open:
                    self.channel.cancel()


def dispatch_once(sessions, transport, limit: int = 100) -> int:
    now = int(time.time())
    # Round-robin within this batch: first pending event from each account precedes
    # a second event from any account. Skip-locked permits multiple dispatchers.
    eligible = or_(Job.status.in_(["SUCCEEDED", "FAILED", "CANCELLED", "WAITING_DATA"]),
        Job.status.in_(["QUEUED", "RETRY_WAIT"]) & (Job.ready_at <= now))
    with sessions() as db, db.begin():
        ranked = select(Outbox.id, func.row_number().over(partition_by=Job.account_id,
            order_by=[Outbox.created_at, Outbox.id]).label("account_rank"))
        ranked = ranked.join(Job, Job.id == Outbox.job_id).where(Outbox.published_at.is_(None), eligible).subquery()
        events = list(db.scalars(select(Outbox).join(ranked, ranked.c.id == Outbox.id)
            .order_by(ranked.c.account_rank, Outbox.created_at, Outbox.id).limit(limit)
            .with_for_update(of=Outbox, skip_locked=True)))
        for event in events:
            job = db.get(Job, event.job_id)
            if job.status in {"QUEUED", "RETRY_WAIT"}:
                transport.publish(Envelope(event.id, job.id))
            event.published_at = now  # Only after publisher confirm; rollback keeps it pending on failure.
    return len(events)


def recover_delivery(sessions) -> int:
    """Explicit post-restore operation; never re-publish live backlog on a timer."""
    count = 0
    with sessions() as db, db.begin():
        queued = list(db.scalars(select(Job).where(Job.status.in_(["QUEUED", "RETRY_WAIT"]))
            .with_for_update(skip_locked=True)))
        for job in queued:
            pending = db.scalar(select(Outbox.id).where(Outbox.job_id == job.id, Outbox.published_at.is_(None)).limit(1))
            if not pending:
                db.add(Outbox(job_id=job.id, event="analysis.ready", created_at=int(time.time())))
                count += 1
    return count
