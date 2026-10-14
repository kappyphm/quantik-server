from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import text

from service.db import make_engine, session_factory
from service.identity import AuthError, Identity
from service.google_auth import GoogleProvider, callback, exchange_identity, start_flow
from service.settings import Settings


class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class AuthExchange(BaseModel):
    code: str = Field(min_length=20, max_length=128)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    sessions = session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.identity = Identity(settings)
        app.state.google = GoogleProvider(settings)
        yield
        app.state.google.close()
        engine.dispose()

    app = FastAPI(title="Quantik API", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    bearer = HTTPBearer(auto_error=False)

    def db_session():
        with sessions() as db:
            yield db

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        return JSONResponse(status_code=exc.status, content={"code": exc.code,
            "message": "Authentication failed", "request_id": request.state.request_id})

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
            if revision != "0002":
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
        return {"id": account.id, "role": account.role}

    return app
