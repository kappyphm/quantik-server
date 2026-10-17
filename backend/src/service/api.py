from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from typing import Literal
import time
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from datetime import date, datetime, timezone

from service.db import SCHEMA_REVISION, make_engine, session_factory
from service.identity import AuthError, Identity
from service.google_auth import GoogleProvider, callback, exchange_identity, start_flow
from service import jobs
from service.models import AnalysisReport, Job, QuotaBucket, Reservation
from service.reports import ReportResult
from service.models import PublishedScreening, ScreeningResult, ScreeningRun, UniverseVersion, QuotaRefund
from service.screening_contracts import ScreeningPage
from service.settings import Settings
from service.telemetry import HttpMetrics, ProcessMonitor, emit
from service.operations_health import Collector
from service.onboarding import ProfileInput, profile_view


class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class AuthExchange(BaseModel):
    code: str = Field(min_length=20, max_length=128)


class Sizing(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    account_size_vnd: float = Field(gt=0)
    max_position_risk_ratio: float = Field(gt=0, le=0.1, default=0.02)


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,16}$")
    target_session: date
    sizing: Sizing | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    sessions = session_factory(engine)
    http_metrics = HttpMetrics()
    collector = Collector(sessions, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.identity = Identity(settings)
        app.state.google = GoogleProvider(settings)
        try:
            with ProcessMonitor(sessions, "api") as monitor:
                app.state.monitor = monitor
                yield
        finally:
            app.state.google.close()
            engine.dispose()

    app = FastAPI(title="Quantik API", version="1.0.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.operations_collector = collector
    bearer = HTTPBearer(auto_error=False)

    def db_session():
        with sessions() as db:
            yield db

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = str(uuid4())
        started, status = time.perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            duration = time.perf_counter()-started
            http_metrics.observe(request.method, route, status, duration)
            monitor = getattr(request.app.state, "monitor", None)
            if monitor:
                monitor.progress()
            if route not in {"/health/live", "/health/ready", "/internal/metrics"}:
                emit("http.request", request_id=request.state.request_id, method=request.method if request.method in
                    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER",
                    route=route, status=status, duration_seconds=round(duration, 6))

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        return JSONResponse(status_code=exc.status, content={"code": exc.code,
            "message": "Authentication failed", "request_id": request.state.request_id})

    @app.exception_handler(jobs.JobError)
    async def job_error(request: Request, exc: jobs.JobError):
        return JSONResponse(status_code=exc.status, content={"code": exc.code,
            "message": "Operation failed", "request_id": request.state.request_id})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic errors include raw input values; do not echo passwords/tokens.
        return JSONResponse(status_code=422, content={"code": "INVALID_REQUEST",
            "message": "Request validation failed", "request_id": request.state.request_id,
            "details": [{"location": list(err["loc"]), "type": err["type"]} for err in exc.errors()]})

    def cookie_name(role: str) -> str:
        return f"quantik_{role}_refresh"

    def cookie_path(role: str) -> str:
        return "/api/v1/admin/auth" if role == "admin" else "/api/v1/auth"

    def tokens(response: Response, result: tuple[str, str], role: str):
        access, refresh = result
        response.set_cookie(cookie_name(role), refresh, httponly=True,
            secure=settings.environment == "production", samesite="strict", path=cookie_path(role),
            max_age=settings.refresh_seconds)
        return {"access_token": access, "token_type": "bearer", "expires_in": settings.access_seconds}

    def require_origin(request: Request):
        if request.headers.get("origin") != settings.public_origin:
            raise AuthError("INVALID_ORIGIN", 403)

    def admin(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db=Depends(db_session)):
        if not credentials:
            raise AuthError()
        return request.app.state.identity.authenticate(db, credentials.credentials, "admin")

    @app.get("/health/live")
    def live():
        return {"status": "live"}

    @app.get("/health/ready")
    def ready(db=Depends(db_session)):
        try:
            revision = db.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != SCHEMA_REVISION:
                raise ValueError("Migration required")
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "DATABASE_SCHEMA"})
        return {"status": "ready", "schema_revision": revision}

    @app.post("/api/v1/admin/auth/login")
    def login(body: AdminLogin, request: Request, response: Response, db=Depends(db_session)):
        # Trust only direct peer address until a reverse proxy allowlist is configured.
        result = request.app.state.identity.admin_login(db, body.username, body.password,
            request.client.host if request.client else "unknown")
        return tokens(response, result, "admin")

    @app.post("/api/v1/admin/auth/refresh")
    def refresh(request: Request, response: Response, db=Depends(db_session)):
        require_origin(request)
        token = request.cookies.get(cookie_name("admin"))
        if not token:
            raise AuthError()
        return tokens(response, request.app.state.identity.refresh(db, token, "admin"), "admin")

    @app.post("/api/v1/admin/auth/logout", status_code=204)
    def logout(request: Request, db=Depends(db_session)):
        require_origin(request)
        request.app.state.identity.logout(db, request.cookies.get(cookie_name("admin"), ""))
        response = Response(status_code=204)
        response.delete_cookie(cookie_name("admin"), path=cookie_path("admin"))
        return response

    @app.get("/api/v1/admin/me")
    def me(account=Depends(admin)):
        return {"id": account.id, "username": account.username, "role": account.role}

    def user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db=Depends(db_session)):
        if not credentials:
            raise AuthError()
        return request.app.state.identity.authenticate(db, credentials.credentials, "user")

    @app.get("/api/v1/auth/google/start")
    def google_start(response: Response, db=Depends(db_session)):
        url, browser = start_flow(db, settings)
        response.set_cookie("quantik_oauth_browser", browser, httponly=True,
            secure=settings.environment == "production", samesite="lax", path="/api/v1/auth", max_age=600)
        return {"authorization_url": url}

    @app.get("/api/v1/auth/google/callback")
    def google_callback(request: Request, state: str, code: str, db=Depends(db_session)):
        exchange = callback(db, request.app.state.google, state,
            request.cookies.get("quantik_oauth_browser", ""), code)
        # The browser receives a short-lived exchange code in the response body.
        # FE performs a same-origin POST; JWT/refresh tokens never appear in URLs.
        return {"exchange_code": exchange, "expires_in": 60}

    @app.post("/api/v1/auth/exchange")
    def google_exchange(body: AuthExchange, request: Request, response: Response, db=Depends(db_session)):
        require_origin(request)
        account = exchange_identity(db, body.code, request.cookies.get("quantik_oauth_browser", ""))
        result = request.app.state.identity.new_session(db, account)
        audit_result = tokens(response, result, "user")
        db.commit()
        response.delete_cookie("quantik_oauth_browser", path="/api/v1/auth")
        return audit_result

    @app.post("/api/v1/auth/refresh")
    def user_refresh(request: Request, response: Response, db=Depends(db_session)):
        require_origin(request)
        return tokens(response, request.app.state.identity.refresh(db,
            request.cookies.get(cookie_name("user"), ""), "user"), "user")

    @app.post("/api/v1/auth/logout", status_code=204)
    def user_logout(request: Request, db=Depends(db_session)):
        require_origin(request)
        request.app.state.identity.logout(db, request.cookies.get(cookie_name("user"), ""))
        response = Response(status_code=204)
        response.delete_cookie(cookie_name("user"), path=cookie_path("user"))
        return response

    @app.get("/api/v1/me")
    def profile(account=Depends(user)):
        return profile_view(account)

    @app.patch("/api/v1/me/profile")
    def update_profile(body: ProfileInput, account=Depends(user), db=Depends(db_session)):
        from service.models import Account
        from service.identity import audit
        account = db.scalar(select(Account).where(Account.id == account.id).with_for_update())
        for key, value in body.model_dump().items():
            setattr(account, key, value)
        account.profile_required = False
        audit(db, "user.profile_updated", account.id)
        db.commit()
        return profile_view(account)

    def job_response(db, job):
        reservation = db.scalar(select(Reservation).where(Reservation.job_id == job.id))
        report = db.scalar(select(AnalysisReport).where(AnalysisReport.job_id == job.id))
        refund = db.scalar(select(QuotaRefund).join(Reservation, Reservation.id == QuotaRefund.reservation_id)
            .where(Reservation.job_id == job.id))
        return {"id": job.id, "symbol": job.symbol, "target_session": job.target_session,
            "created_at": job.created_at, "deadline": job.deadline, "attempt": job.attempt,
            "status": job.status, "reason": job.reason, "quota_effect": reservation.state,
            "report_id": report.id if report else None, "compute_started_at": job.compute_started_at,
            "quota_refund_id": refund.id if refund else None}

    @app.post("/api/v1/analyses", status_code=202)
    def create_analysis(body: AnalysisRequest, account=Depends(user), db=Depends(db_session),
                        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128)):
        from service.models import Account
        from service.daily_limits import current_bucket
        from service.analysis_readiness import validate_request
        from service.market_data import DataError
        db.scalar(select(Account).where(Account.id == account.id).with_for_update())
        existing = db.scalar(select(Job).where(Job.account_id == account.id, Job.idempotency_key == idempotency_key))
        if not existing:
            if account.profile_required:
                raise jobs.JobError("PROFILE_REQUIRED", 403)
            if not current_bucket(db, account.id, int(time.time())):
                raise jobs.JobError("NO_ENTITLEMENT", 403)
            try:
                validate_request(db, settings, body.target_session, body.symbol)
            except DataError as exc:
                raise jobs.JobError(exc.code, 503 if exc.retryable else 422) from None
        job = jobs.admit(db, account.id, body.symbol, body.target_session, idempotency_key,
            body.sizing.model_dump() if body.sizing else None)
        db.commit()
        return job_response(db, job)

    @app.get("/api/v1/analyses/options")
    def analysis_options(target_session: date | None = None, account=Depends(user), db=Depends(db_session)):
        from service.analysis_readiness import options
        return options(db, settings, target_session)

    @app.get("/api/v1/analyses")
    def analyses(account=Depends(user), db=Depends(db_session), cursor: str | None = None,
                 symbol: str | None = Query(None, pattern=r"^[A-Z0-9]{2,16}$"), limit: int = Query(20, ge=1, le=50)):
        from service.pagination import newest, encode
        stmt = newest(select(Job).where(Job.account_id == account.id), Job, "created_at", cursor)
        if symbol:
            stmt = stmt.where(Job.symbol == symbol)
        result = list(db.scalars(stmt.limit(limit+1)))
        return {"items": [job_response(db, job) for job in result[:limit]],
            "next_cursor": encode(result[limit-1].created_at, result[limit-1].id) if len(result) > limit else None}

    @app.get("/api/v1/analyses/{job_id}")
    def analysis(job_id: str, account=Depends(user), db=Depends(db_session)):
        return job_response(db, jobs.owned_job(db, account.id, job_id))

    @app.get("/api/v1/reports/{report_id}", response_model=ReportResult)
    def report(report_id: str, account=Depends(user), db=Depends(db_session)):
        report = db.scalar(select(AnalysisReport).where(AnalysisReport.id == report_id,
            AnalysisReport.account_id == account.id))
        if not report:
            raise jobs.JobError("NOT_FOUND", 404)
        return {"report_id": report.id, **report.payload}

    @app.get("/api/v1/me/usage")
    def usage(account=Depends(user), db=Depends(db_session)):
        from service.daily_limits import current_bucket
        current_bucket(db, account.id, int(time.time()))
        db.commit()
        return {"buckets": [{"id": b.id, "total": b.quota_total, "reserved": b.reserved,
            "consumed": b.consumed, "created": b.daily_created, "quota_period": "DAY" if b.subscription_id else "LEGACY", "active_jobs": b.active_jobs, "active_limit": b.active_limit,
            "valid_from": b.valid_from, "valid_until": b.valid_until}
            for b in db.scalars(select(QuotaBucket).where(QuotaBucket.account_id == account.id)
                .order_by(QuotaBucket.valid_from.desc()).limit(50))]}

    def screening_header(run):
        return {"id": run.id, "target_session": run.target_session.isoformat(), "slot": run.slot,
            "status": run.status, "reason": run.reason, "manifest_id": run.manifest_id,
            "published_at": run.published_at}

    def require_screening_source(run):
        if not settings.allow_fixture_analysis and (run.summary or {}).get("source_mode") not in {"LIVE_VALIDATED", "LIVE_SCREENING_VALIDATED"}:
            raise jobs.JobError("SCREENING_LIVE_DATA_REQUIRED", 503)

    @app.get("/api/v1/screenings/latest")
    def screening_latest(slot: Literal["ALL", "AM", "PM"] = "ALL", account=Depends(user), db=Depends(db_session)):
        from service.screening_access import require_access
        require_access(db, account.id)
        query = select(ScreeningRun)
        if slot != "ALL":
            query = query.where(ScreeningRun.slot == slot)
        attempt = db.scalar(query.order_by(ScreeningRun.target_session.desc(), ScreeningRun.slot.desc()).limit(1))
        pointer = db.get(PublishedScreening, slot)
        if not pointer:
            return JSONResponse(status_code=503, content={"code": "SCREENING_NOT_READY",
                "latest_attempt": screening_header(attempt) if attempt else None,
                "retry_after_seconds": 60}, headers={"Retry-After": "60"})
        run = db.get(ScreeningRun, pointer.run_id)
        require_screening_source(run)
        reasons = []
        if attempt and (attempt.target_session, attempt.slot) >= (run.target_session, run.slot) and attempt.status != "READY":
            reasons.append("LATEST_RUN_UNAVAILABLE")
        from service.operational_config import active_version, TradingCalendar
        from service.calendar_reference import expected_screening
        calendar_version = active_version(db, "TRADING_CALENDAR")
        calendar_status = "NOT_CONFIGURED"
        if calendar_version:
            calendar = TradingCalendar.model_validate(calendar_version.payload)
            from zoneinfo import ZoneInfo
            now = datetime.now(timezone.utc)
            if calendar.start <= now.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date() <= calendar.end:
                calendar_status = "READY"
                expected = expected_screening(calendar, slot, now)
                if expected and (run.target_session, run.slot) < expected:
                    reasons.append("EXPECTED_SLOT_UNPUBLISHED")
            else:
                calendar_status = "OUT_OF_RANGE"
                reasons.append("CALENDAR_OUT_OF_RANGE")
        if calendar_status != "READY" and int(time.time()) - run.published_at > 36*3600:
            reasons.append("PUBLICATION_AGE")
        return {**screening_header(run), "summary": run.summary, "stale": bool(reasons), "stale_reasons": reasons,
            "server_now": int(time.time()), "refresh_after_seconds": 60,
            "schedule": {"timezone": "Asia/Ho_Chi_Minh", "AM": "11:30–12:55", "PM": "16:00–17:30"},
            "latest_attempt": screening_header(attempt) if attempt else None, "calendar_status": calendar_status}

    @app.get("/api/v1/screenings/{run_id}/results", response_model=ScreeningPage)
    def screening_results(run_id: str, group: Literal["ALL", "OVERALL", "STRONG", "ACCUMULATION", "DISTRIBUTION"] = "ALL",
                          cursor: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                          account=Depends(user), db=Depends(db_session)):
        from service.screening_access import require_access
        require_access(db, account.id)
        run = db.get(ScreeningRun, run_id)
        if not run:
            raise jobs.JobError("NOT_FOUND", 404)
        if run.status != "READY":
            raise jobs.JobError("SCREENING_RUN_NOT_READY", 409)
        require_screening_source(run)
        columns = {"OVERALL": ScreeningResult.overall_rank, "STRONG": ScreeningResult.strong_rank,
            "ACCUMULATION": ScreeningResult.accumulation_rank, "DISTRIBUTION": ScreeningResult.distribution_rank}
        query = select(ScreeningResult).where(ScreeningResult.run_id == run_id)
        if group == "ALL":
            if cursor:
                query = query.where(ScreeningResult.symbol > cursor)
            query = query.order_by(ScreeningResult.symbol)
        else:
            rank = columns[group]
            query = query.where(rank.is_not(None)).order_by(rank)
            if cursor:
                if not cursor.isdigit():
                    raise jobs.JobError("INVALID_CURSOR", 422)
                query = query.where(rank > int(cursor))
        rows = list(db.scalars(query.limit(limit+1)))
        items = [{**row.payload, "ranks": {"OVERALL": row.overall_rank, "STRONG": row.strong_rank,
            "ACCUMULATION": row.accumulation_rank, "DISTRIBUTION": row.distribution_rank}} for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = rows[limit-1].symbol if group == "ALL" else str(getattr(rows[limit-1], group.lower()+"_rank"))
        return {"run_id": run_id, "group": group, "items": items, "next_cursor": next_cursor}

    @app.get("/api/v1/instruments")
    def instruments(cursor: str | None = None, exchange: Literal["HOSE", "HNX", "UPCOM"] | None = None,
                    limit: int = Query(default=100, ge=1, le=500), account=Depends(user), db=Depends(db_session)):
        query = select(UniverseVersion)
        if not settings.allow_fixture_analysis:
            query = query.where(UniverseVersion.payload["provenance"].as_string() == "LIVE_VALIDATED")
        reference = db.scalar(query.order_by(UniverseVersion.created_at.desc(), UniverseVersion.id).limit(1))
        if not reference:
            raise jobs.JobError("INSTRUMENTS_NOT_READY", 503)
        selected = [(symbol, meta) for symbol, meta in sorted(reference.payload["members"].items())
            if (not cursor or symbol > cursor) and (not exchange or meta["exchange"] == exchange)]
        return {"universe_version": reference.id, "items": [{"symbol": s, **meta} for s, meta in selected[:limit]],
            "next_cursor": selected[limit-1][0] if len(selected) > limit else None}

    from service.billing_api import router as billing_router
    app.include_router(billing_router(settings, user, admin, db_session))
    from service.admin_api import router as admin_router
    app.include_router(admin_router(admin, db_session))
    from service.config_api import router as config_router
    app.include_router(config_router(settings, admin, db_session))
    from service.observability_api import router as observability_router
    app.include_router(observability_router(settings, admin, collector, http_metrics))
    from service.integration_api import router as integration_router
    app.include_router(integration_router(settings, admin, db_session))
    return app
