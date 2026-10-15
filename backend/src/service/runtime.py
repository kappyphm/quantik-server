"""Explicit worker/control processes with bounded polling and graceful shutdown."""
import signal
import threading
import time
from contextlib import ExitStack

from service.broker import RabbitTransport, dispatch_once, recover_delivery
from service.db import make_engine, session_factory
from service.dependencies import reconcile_control_once, run_dependency_once
from service.telemetry import ProcessMonitor, emit


def run_service(command, settings, *, once=False, transport="amqp", kind=None) -> None:
    stop = threading.Event()
    previous = {}
    for name in (signal.SIGINT, signal.SIGTERM):
        previous[name] = signal.signal(name, lambda *_: stop.set())
    engine = make_engine(settings.database_url)
    sessions = session_factory(engine)
    broker = None
    lifecycle = ExitStack()
    last_error, last_error_at = None, 0.0
    try:
        monitor = lifecycle.enter_context(ProcessMonitor(sessions, command+(":"+kind if kind else "")))
        if command == "recover-delivery":
            emit("delivery.rearmed", count=recover_delivery(sessions))
            return
        if settings.environment == "production" and command == "worker" and transport != "amqp":
            raise ValueError("Production requires RabbitMQ transport")
        worker = None
        if command == "worker":
            from service.analysis_worker import AnalysisWorker, local_worker_once
            worker = AnalysisWorker(sessions, settings)
        while not stop.is_set():
            try:
                if command in {"dispatcher", "worker"} and (command == "dispatcher" or transport == "amqp"):
                    if broker is None:
                        broker = RabbitTransport(settings)
                    if command == "dispatcher":
                        count = dispatch_once(sessions, broker)
                        if count:
                            emit("delivery.dispatched", count=count)
                        broker.connection.process_data_events(time_limit=.1)
                    else:
                        broker.consume(worker, once=once, stop=stop)
                elif command == "worker":
                    count = local_worker_once(sessions, worker)
                    if count:
                        emit("delivery.local_handled", count=count)
                elif command == "dependencies":
                    run_dependency_once(sessions)
                elif command == "reconciler":
                    result = reconcile_control_once(sessions, settings)
                    if any(result.values()):
                        emit("control.reconciled", **result)
                elif command == "scheduler":
                    from service.scheduling import tick
                    result = tick(sessions, settings)
                    if once or result.get("created") or result.get("recovered") or result["state"] != "READY":
                        emit("scheduler.tick", **result)
                elif command == "schedule-worker":
                    from service.scheduled_worker import run_once
                    if run_once(sessions, settings, kind):
                        emit("scheduler.handled", kind=kind)
                else:
                    raise ValueError("Unknown runtime command")
                monitor.progress()
                last_error = None
            except Exception as exc:
                error = type(exc).__name__
                monitor.progress(error_type=error)
                instant = time.monotonic()
                if error != last_error or instant-last_error_at >= 30:
                    emit("process.iteration_failed", role=command, error_type=error)
                    last_error, last_error_at = error, instant
                if broker:
                    try:
                        broker.close()
                    except Exception:
                        pass
                    broker = None
                if once:
                    raise SystemExit(1) from None
            if once:
                break
            stop.wait(30 if command == "scheduler" else (5 if command in {"dependencies", "schedule-worker"} else 2))
    finally:
        if broker:
            try:
                broker.close()
            except Exception as exc:
                emit("broker.close_failed", error_type=type(exc).__name__)
        lifecycle.close()
        engine.dispose()
        for name, handler in previous.items():
            signal.signal(name, handler)
