import hashlib
import secrets
import time
from pathlib import Path

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from service.models import Account, AuditEvent, LoginThrottle, RefreshSession, UsedRefreshToken
from service.settings import Settings

passwords = PasswordHasher()
# A real hash also makes unknown-account login run the password verification path.
dummy_hash = passwords.hash(secrets.token_urlsafe(32))


class AuthError(Exception):
    def __init__(self, code: str = "UNAUTHENTICATED", status: int = 401, retry_after: int | None = None):
        self.code, self.status, self.retry_after = code, status, retry_after


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def audit(db: Session, action: str, actor: str | None = None) -> None:
    db.add(AuditEvent(actor_id=actor, action=action, occurred_at=int(time.time()), details={}))


def seed_admin(db: Session, username: str, password: str) -> bool:
    if len(password) < 14:
        raise ValueError("Admin password must contain at least 14 characters")
    existing = db.scalar(select(Account).where(Account.username == username))
    if existing:
        if existing.role != "admin":
            raise ValueError("Username belongs to a non-admin account")
        return False
    account = Account(username=username, password_hash=passwords.hash(password), role="admin")
    db.add(account)
    db.flush()
    audit(db, "admin.seed", account.id)
    return True


class Identity:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.private = Path(settings.jwt_private_key_path).read_bytes()
        self.public = Path(settings.jwt_public_key_path).read_bytes()
        # Fail fast on mismatched keys, without logging key contents.
        probe = jwt.encode({"sub": "key-check"}, self.private, algorithm="RS256")
        jwt.decode(probe, self.public, algorithms=["RS256"])

    def access(self, account: Account, session: RefreshSession) -> str:
        now = int(time.time())
        return jwt.encode({"sub": account.id, "role": account.role, "session_id": session.id,
            "iss": self.settings.jwt_issuer, "aud": f"quantik-{account.role}",
            "iat": now, "exp": now + self.settings.access_seconds,
            "jti": secrets.token_hex(16)}, self.private, algorithm="RS256")

    def new_session(self, db: Session, account: Account) -> tuple[str, str]:
        token = secrets.token_urlsafe(48)
        session = RefreshSession(account_id=account.id, token_hash=digest(token),
            expires_at=int(time.time()) + self.settings.refresh_seconds)
        db.add(session)
        db.flush()
        return self.access(account, session), token

    def admin_login(self, db: Session, username: str, password: str, ip: str) -> tuple[str, str]:
        now = int(time.time())
        # Durable, shared throttles: both account and IP are bounded across replicas.
        keys = [digest("account:" + username), digest("ip:" + ip)]
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        for key in sorted(keys):
            db.execute(insert(LoginThrottle).values(key=key, failures=0, blocked_until=0)
                .on_conflict_do_nothing(index_elements=["key"]))
        rows = list(db.scalars(select(LoginThrottle).where(LoginThrottle.key.in_(keys))
            .order_by(LoginThrottle.key).with_for_update()))
        if any(row.blocked_until > now for row in rows):
            db.commit()
            raise AuthError("LOGIN_RATE_LIMITED", 429, max(row.blocked_until-now for row in rows))
        account = db.scalar(select(Account).where(Account.username == username, Account.role == "admin"))
        try:
            passwords.verify(account.password_hash if account and account.password_hash else dummy_hash, password)
            valid = account is not None and account.enabled
        except (VerificationError, InvalidHashError):
            valid = False
        if not valid:
            for row in rows:
                row.failures = row.failures + 1 if row.blocked_until == 0 and row.last_failure_at > now-900 else 1
                row.last_failure_at = now
                row.blocked_until = now + 900 if row.failures >= 5 else 0
            audit(db, "admin.login_failed")
            db.commit()  # Preserve failed-attempt counters even though the request fails.
            raise AuthError()
        for row in rows:
            row.failures, row.blocked_until, row.last_failure_at = 0, 0, 0
        audit(db, "admin.login", account.id)
        result = self.new_session(db, account)
        db.commit()
        return result

    def authenticate(self, db: Session, token: str, role: str) -> Account:
        try:
            claims = jwt.decode(token, self.public, algorithms=["RS256"],
                audience=f"quantik-{role}", issuer=self.settings.jwt_issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss", "jti", "session_id", "role"]})
        except jwt.PyJWTError:
            raise AuthError() from None
        account = db.get(Account, claims["sub"])
        session = db.get(RefreshSession, claims["session_id"])
        if not account or not account.enabled or account.role != role or claims["role"] != role:
            raise AuthError()
        if not session or session.account_id != account.id or session.revoked or session.expires_at <= int(time.time()):
            raise AuthError()
        return account

    def refresh(self, db: Session, token: str, role: str) -> tuple[str, str]:
        hashed = digest(token)
        session = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == hashed).with_for_update())
        if not session:
            used = db.get(UsedRefreshToken, hashed)
            if used:
                db.execute(update(RefreshSession).where(RefreshSession.id == used.session_id).values(revoked=True))
                audit(db, "auth.refresh_reuse")
                db.commit()
            raise AuthError()
        account = db.get(Account, session.account_id)
        if session.revoked or session.expires_at <= int(time.time()) or not account or not account.enabled or account.role != role:
            raise AuthError()
        replacement = secrets.token_urlsafe(48)
        changed = db.execute(update(RefreshSession).where(RefreshSession.id == session.id,
            RefreshSession.token_hash == hashed, RefreshSession.revoked.is_(False))
            .values(token_hash=digest(replacement), previous_hash=hashed,
                generation=RefreshSession.generation + 1))
        if changed.rowcount != 1:
            db.rollback()
            db.execute(update(RefreshSession).where(RefreshSession.id == session.id).values(revoked=True))
            db.commit()
            raise AuthError()
        db.add(UsedRefreshToken(token_hash=hashed, session_id=session.id))
        audit(db, "auth.refresh", account.id)
        access = self.access(account, session)
        db.commit()
        return access, replacement

    def logout(self, db: Session, token: str) -> None:
        hashed = digest(token)
        session = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == hashed))
        used = db.get(UsedRefreshToken, hashed) if not session else None
        session_id = session.id if session else used.session_id if used else None
        if session_id:
            db.execute(update(RefreshSession).where(RefreshSession.id == session_id).values(revoked=True))
            audit(db, "auth.logout")
        db.commit()
