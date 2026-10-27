"""Explicit audited customer controls."""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from service.billing import audit
from service.jobs import JobError
from service.models import Account, RefreshSession


def set_enabled(db: Session, account_id: str, enabled: bool, reason: str, actor_id: str) -> Account:
    if not 10 <= len(reason.strip()) <= 500:
        raise JobError("ADMIN_REASON_REQUIRED", 422)
    account = db.scalar(select(Account).where(Account.id == account_id, Account.role == "user")
        .with_for_update().execution_options(populate_existing=True))
    if not account:
        raise JobError("NOT_FOUND", 404)
    before = account.enabled
    account.enabled = enabled
    if not enabled:
        db.execute(update(RefreshSession).where(RefreshSession.account_id == account_id).values(revoked=True))
    if before != enabled:
        audit(db, "admin.customer_state", {"account_id": account_id, "before": before, "after": enabled,
            "reason": reason}, actor_id)
    return account
