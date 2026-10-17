import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from service.identity import AuthError, audit, digest
from service.models import Account, OAuthFlow
from service.settings import Settings


class GoogleProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.Client(timeout=15)
        self.jwks = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", timeout=15)

    def close(self) -> None:
        self.http.close()

    def verified_identity(self, code: str, verifier: str, nonce: str) -> dict:
        try:
            response = self.http.post("https://oauth2.googleapis.com/token", data={
                "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret.get_secret_value(),
                "redirect_uri": self.settings.public_origin + "/api/v1/auth/google/callback"})
            response.raise_for_status()
            token = response.json()["id_token"]
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.settings.google_client_id,
                issuer=["accounts.google.com", "https://accounts.google.com"],
                options={"require": ["sub", "exp", "iat", "aud", "iss", "nonce"]})
            if not secrets.compare_digest(claims["nonce"], nonce):
                raise AuthError("INVALID_GOOGLE_TOKEN")
            if claims.get("azp", self.settings.google_client_id) != self.settings.google_client_id:
                raise AuthError("INVALID_GOOGLE_TOKEN")
            if not isinstance(claims["sub"], str) or not 0 < len(claims["sub"]) <= 255:
                raise AuthError("INVALID_GOOGLE_TOKEN")
            # Profile comes only from the same signature/nonce-verified ID token.
            name = claims.get("name")
            email = claims.get("email")
            name = name.strip() if isinstance(name, str) else None
            email = email.strip() if isinstance(email, str) and claims.get("email_verified") is True else None
            if name and (len(name) > 255 or not name.isprintable()):
                name = None
            if email and (len(email) > 320 or not email.isprintable() or "@" not in email):
                email = None
            return {"sub": claims["sub"], "display_name": name or None, "email": email or None}
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError, TypeError):
            # Never expose Google's raw response, authorization code or tokens.
            raise AuthError("GOOGLE_AUTH_FAILED") from None


def start_flow(db: Session, settings: Settings) -> tuple[str, str]:
    if not settings.google_client_id or not settings.google_client_secret.get_secret_value():
        raise AuthError("GOOGLE_NOT_CONFIGURED", 503)
    state, browser, nonce, verifier = [secrets.token_urlsafe(48) for _ in range(4)]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    db.add(OAuthFlow(state_hash=digest(state), browser_hash=digest(browser), nonce=nonce,
        verifier=verifier, expires_at=int(time.time()) + 600))
    db.commit()
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": settings.google_client_id, "response_type": "code", "scope": "openid email profile",
        "redirect_uri": settings.public_origin + "/api/v1/auth/google/callback",
        "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"})
    return url, browser


def callback(db: Session, provider: GoogleProvider, state: str, browser: str, code: str) -> str:
    flow = db.get(OAuthFlow, digest(state))
    now = int(time.time())
    if not flow or flow.stage != "STARTED" or flow.expires_at <= now or not secrets.compare_digest(flow.browser_hash, digest(browser)):
        raise AuthError("INVALID_OAUTH_STATE")
    changed = db.execute(update(OAuthFlow).where(OAuthFlow.state_hash == flow.state_hash,
        OAuthFlow.stage == "STARTED").values(stage="VERIFYING"))
    if changed.rowcount != 1:
        db.rollback()
        raise AuthError("INVALID_OAUTH_STATE")
    db.commit()  # Consume state before external I/O; callbacks are never replayed.
    profile = provider.verified_identity(code, flow.verifier, flow.nonce)
    sub = profile["sub"]
    # Unique Google sub plus upsert handles simultaneous first logins across browsers.
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    from service.models import new_id
    created_id = db.scalar(insert(Account).values(id=new_id(), google_sub=sub, role="user", enabled=True, profile_required=True)
        .on_conflict_do_nothing(index_elements=["google_sub"]).returning(Account.id))
    account = db.scalar(select(Account).where(Account.google_sub == sub).with_for_update())
    if not account or not account.enabled or account.role != "user":
        raise AuthError()
    if not account.date_of_birth:
        account.display_name = profile["display_name"]
    account.email = profile["email"]
    if created_id:
        from service.onboarding import grant_signup
        grant_signup(db, account)
    exchange = secrets.token_urlsafe(48)
    flow.account_id, flow.exchange_hash = account.id, digest(exchange)
    flow.expires_at, flow.stage = int(time.time()) + 60, "VERIFIED"
    audit(db, "user.google_verified", account.id)
    db.commit()
    return exchange


def exchange_identity(db: Session, exchange: str, browser: str) -> Account:
    flow = db.scalar(select(OAuthFlow).where(OAuthFlow.exchange_hash == digest(exchange)).with_for_update())
    if not flow or flow.stage != "VERIFIED" or flow.expires_at <= int(time.time()) or not secrets.compare_digest(flow.browser_hash, digest(browser)):
        raise AuthError("INVALID_AUTH_EXCHANGE")
    changed = db.execute(update(OAuthFlow).where(OAuthFlow.state_hash == flow.state_hash,
        OAuthFlow.stage == "VERIFIED").values(stage="EXCHANGED"))
    if changed.rowcount != 1:
        raise AuthError("INVALID_AUTH_EXCHANGE")
    account = db.get(Account, flow.account_id)
    if not account or not account.enabled or account.role != "user":
        raise AuthError()
    return account
