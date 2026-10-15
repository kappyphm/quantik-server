"""Bounded HTTP metrics, safe structured events and durable process heartbeats."""
from collections import defaultdict
from datetime import datetime, timezone
import json
import logging
import math
import threading
import time
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from service.models import ProcessHeartbeat
from service.operational_config import insert_for

logger = logging.getLogger("quantik.events")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def emit(event: str, **fields) -> None:
    # Callers supply named operational facts, never bodies/URLs/headers/exception text.
    logger.info(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields},
        ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def labels(values: dict) -> str:
    return "{"+",".join(f"{key}={json.dumps(str(value), ensure_ascii=False)}" for key, value in sorted(values.items()))+"}" if values else ""


class HttpMetrics:
    bounds = (.005, .025, .1, .25, .5, 1, 2, 5, 10, 30, math.inf)

    def __init__(self):
        self.lock = threading.Lock()
        self.values = defaultdict(lambda: [0, 0.0, [0]*len(self.bounds)])

    def observe(self, method: str, route: str, status: int, duration: float) -> None:
        if route == "/internal/metrics":
            return
        method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"
        with self.lock:
            row = self.values[(method, route, str(status//100)+"xx")]
            row[0] += 1
            row[1] += duration
            for i, bound in enumerate(self.bounds):
                row[2][i] += int(duration <= bound)

    def render(self) -> str:
        lines = ["# TYPE quantik_http_requests_total counter"]
        with self.lock:
            rows = [(key, (v[0], v[1], list(v[2]))) for key, v in sorted(self.values.items())]
        # Keep each metric family contiguous as required by text exposition.
        for (method, route, status), row in rows:
            lines.append("quantik_http_requests_total"+labels({"method": method, "route": route, "status": status})+" "+str(row[0]))
        lines.append("# TYPE quantik_http_request_duration_seconds histogram")
        for (method, route, status), row in rows:
            base = {"method": method, "route": route, "status": status}
            for bound, count in zip(self.bounds, row[2]):
                lines.append("quantik_http_request_duration_seconds_bucket"+labels(base | {"le": "+Inf" if math.isinf(bound) else str(bound)})+" "+str(count))
            lines.append("quantik_http_request_duration_seconds_count"+labels(base)+" "+str(row[0]))
            lines.append("quantik_http_request_duration_seconds_sum"+labels(base)+" "+str(row[1]))
        lines.extend(["# TYPE quantik_process_cpu_seconds_total counter", "quantik_process_cpu_seconds_total "+str(time.process_time())])
        return "\n".join(lines)+"\n"


class ProcessMonitor:
    def __init__(self, sessions: sessionmaker[Session], role: str):
        self.sessions, self.role, self.id = sessions, role, str(uuid4())
        self.started_at = self.progress_at = int(time.time())
        self.state, self.error_type = "RUNNING", None
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.thread = threading.Thread(target=self.run, name="process-heartbeat", daemon=True)

    def progress(self, error_type: str | None = None) -> None:
        with self.lock:
            self.progress_at = int(time.time())
            self.state, self.error_type = ("ERROR", error_type[:100]) if error_type else ("RUNNING", None)

    def pulse(self) -> None:
        now = int(time.time())
        with self.lock:
            values = {"role": self.role, "started_at": self.started_at, "last_seen": now,
                "progress_at": self.progress_at, "state": self.state, "error_type": self.error_type}
        with self.sessions() as db, db.begin():
            db.execute(insert_for(db)(ProcessHeartbeat).values(id=self.id, **values)
                .on_conflict_do_update(index_elements=["id"], set_=values))
            db.execute(delete(ProcessHeartbeat).where(ProcessHeartbeat.last_seen < now-86400))

    def run(self) -> None:
        while not self.stop.wait(20):
            try:
                self.pulse()
            except Exception as exc:
                emit("heartbeat.write_failed", role=self.role, error_type=type(exc).__name__)

    def __enter__(self):
        self.pulse()
        self.thread.start()
        emit("process.started", role=self.role, instance_id=self.id)
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=5)
        with self.lock:
            self.state = "STOPPED"
        try:
            self.pulse()
        except Exception as exc:
            emit("heartbeat.stop_failed", role=self.role, error_type=type(exc).__name__)
        emit("process.stopped", role=self.role, instance_id=self.id)
