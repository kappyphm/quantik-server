from collections.abc import Callable
import hmac

from fastapi import APIRouter, Depends, Header, Response

from service.identity import AuthError
from service.jobs import JobError
from service.operations_health import Collector, prometheus
from service.settings import Settings
from service.telemetry import HttpMetrics


def router(settings: Settings, admin: Callable, collector: Collector, http: HttpMetrics) -> APIRouter:
    routes = APIRouter()

    def snapshot():
        try:
            return collector.snapshot()
        except Exception:
            raise JobError("OPERATIONS_NOT_READY", 503) from None

    @routes.get("/api/v1/admin/operations/health")
    def health(actor=Depends(admin)):
        return snapshot()

    @routes.get("/internal/metrics", include_in_schema=False)
    def metrics(authorization: str | None = Header(default=None)):
        expected = settings.metrics_token.get_secret_value()
        scheme, _, token = (authorization or "").partition(" ")
        if not expected:
            raise AuthError("METRICS_DISABLED", 503)
        if scheme.lower() != "bearer" or not hmac.compare_digest(expected.encode(), token.encode()):
            raise AuthError("METRICS_UNAUTHORIZED", 401)
        body = http.render()+prometheus(snapshot(), settings.release_id)
        return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")

    return routes
