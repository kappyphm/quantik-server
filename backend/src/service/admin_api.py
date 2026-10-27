from service.pagination import encode as history_cursor, newest
"""Bounded operational views and role-specific, audited actions."""
from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from service import admin as operations
from service.models import (Account, AuditEvent, BankPayment, DataManifest, DependencyRequest, Job,
    ModelVersion, PublishedModel, QuotaBucket, QuotaRefund, Reservation, ScreeningRun, Subscription)


class CustomerState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    reason: str = Field(min_length=10, max_length=500)


class DemoCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


def router(admin: Callable, db_session: Callable) -> APIRouter:
    routes = APIRouter(prefix="/api/v1/admin")

    @routes.get("/users")
    def users(cursor: str | None = None, limit: int = Query(50, ge=1, le=100),
              q: str | None = Query(None, max_length=120), enabled: bool | None = None,
              actor=Depends(admin), db=Depends(db_session)):
        query = select(Account).where(Account.role == "user").order_by(Account.id)
        if q and q.strip():
            term = q.strip().lower()
            query = query.where(or_(func.lower(Account.display_name).contains(term, autoescape=True),
                func.lower(Account.email).contains(term, autoescape=True), Account.id == q.strip()))
        if enabled is not None:
            query = query.where(Account.enabled == enabled)
        if cursor:
            query = query.where(Account.id > cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        from service.onboarding import profile_view
        return {"items": [profile_view(a) | {"enabled": a.enabled} for a in rows[:limit]],
            "next_cursor": rows[limit-1].id if len(rows)>limit else None}

    @routes.post("/users/{account_id}/state")
    def state(account_id: str, body: CustomerState, actor=Depends(admin), db=Depends(db_session)):
        account = operations.set_enabled(db, account_id, body.enabled, body.reason, actor.id)
        db.commit()
        return {"id": account.id, "enabled": account.enabled, "sessions_revoked": not account.enabled,
            "pending_jobs": "RECONCILER_RELEASE" if not account.enabled else "UNCHANGED"}

    @routes.get("/jobs")
    def jobs(cursor: str | None = None, account_id: str | None = None, status: str | None = None,
             limit: int = Query(50, ge=1, le=100), actor=Depends(admin), db=Depends(db_session)):
        query = select(Job).order_by(Job.id)
        query = newest(query, Job, "created_at", cursor)
        if account_id:
            query = query.where(Job.account_id == account_id)
        if status:
            query = query.where(Job.status == status)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [{k: getattr(j, k) for k in ("id", "account_id", "symbol", "target_session", "status", "reason",
            "created_at", "deadline", "ready_at", "attempt", "compute_started_at")} for j in rows[:limit]],
            "next_cursor": history_cursor(rows[limit-1].created_at, rows[limit-1].id) if len(rows)>limit else None}

    @routes.get("/payments")
    def payments(cursor: str | None = None, status: str | None = None, limit: int = Query(50, ge=1, le=100),
                 actor=Depends(admin), db=Depends(db_session)):
        query = select(BankPayment).order_by(BankPayment.id)
        query = newest(query, BankPayment, "received_at", cursor)
        if status:
            query = query.where(BankPayment.status == status)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [{k: getattr(p,k) for k in ("id", "order_id", "amount_vnd", "status", "reason", "received_at")}
            for p in rows[:limit]], "next_cursor": history_cursor(rows[limit-1].received_at, rows[limit-1].id) if len(rows)>limit else None}

    @routes.get("/subscriptions")
    def subscriptions(cursor: str | None = None, account_id: str | None = None,
                      limit: int = Query(50, ge=1, le=100), actor=Depends(admin), db=Depends(db_session)):
        query = select(Subscription).order_by(Subscription.id)
        query = newest(query, Subscription, "valid_from", cursor)
        if account_id:
            query = query.where(Subscription.account_id == account_id)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [{k: getattr(s,k) for k in ("id", "account_id", "order_id", "payment_id", "plan_id",
            "bucket_id", "valid_from", "valid_until")} for s in rows[:limit]],
            "next_cursor": history_cursor(rows[limit-1].valid_from, rows[limit-1].id) if len(rows)>limit else None}

    @routes.get("/audit")
    def audit(cursor: str | None = None, action: str | None = None, limit: int = Query(50, ge=1, le=100),
              actor=Depends(admin), db=Depends(db_session)):
        query = select(AuditEvent).order_by(AuditEvent.id)
        query = newest(query, AuditEvent, "occurred_at", cursor)
        if action:
            query = query.where(AuditEvent.action == action)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [{k: getattr(e,k) for k in ("id", "actor_id", "action", "occurred_at", "details")} for e in rows[:limit]],
            "next_cursor": history_cursor(rows[limit-1].occurred_at, rows[limit-1].id) if len(rows)>limit else None}

    @routes.get("/models")
    def models(cursor: str | None = None, limit: int = Query(20, ge=1, le=100), actor=Depends(admin), db=Depends(db_session)):
        # Do not select model artifacts for an operational listing.
        query = select(ModelVersion.id, ModelVersion.status, ModelVersion.cutoff_session,
            ModelVersion.created_at, ModelVersion.published_at, ModelVersion.checksum).order_by(ModelVersion.id)
        if cursor:
            query = query.where(ModelVersion.id > cursor)
        rows = list(db.execute(query.limit(limit+1)).mappings())
        return {"items": [dict(r) for r in rows[:limit]], "next_cursor": rows[limit-1]["id"] if len(rows)>limit else None,
            "published": [{"name": p.name, "model_id": p.model_id} for p in db.scalars(select(PublishedModel))]}

    @routes.post("/demo/codes")
    def demo_create_code(body: DemoCodeInput, actor=Depends(admin), db=Depends(db_session)):
        from service.demo import code_view, create_code
        from service.billing import audit as billing_audit
        row, code = create_code(db, actor.id, body.expires_in_days)
        billing_audit(db, "demo.code_created", {"demo_code_id": row.id}, actor.id)
        db.commit()
        # Plaintext code is returned exactly once; it is never stored.
        return {**code_view(row), "code": code}

    @routes.get("/demo/codes")
    def demo_list_codes(actor=Depends(admin), db=Depends(db_session)):
        from service.demo import code_view, ensure_demo_account
        ensure_demo_account(db)
        from service.models import DemoCode
        rows = list(db.scalars(select(DemoCode).order_by(DemoCode.created_at.desc()).limit(100)))
        return {"items": [code_view(r) for r in rows]}

    @routes.post("/demo/codes/{code_id}/revoke")
    def demo_revoke_code(code_id: str, actor=Depends(admin), db=Depends(db_session)):
        from service.demo import code_view
        from service.billing import audit as billing_audit
        from service.models import DemoCode
        row = db.get(DemoCode, code_id)
        if not row:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        row.revoked = True
        billing_audit(db, "demo.code_revoked", {"demo_code_id": row.id}, actor.id)
        db.commit()
        return code_view(row)

    @routes.post("/demo/reset")
    def demo_reset(actor=Depends(admin), db=Depends(db_session)):
        from service.billing import audit as billing_audit
        from service.demo import reset_demo
        result = reset_demo(db)
        billing_audit(db, "demo.reset", result, actor.id)
        db.commit()
        return result

    @routes.get("/data-health")
    def data_health(actor=Depends(admin), db=Depends(db_session)):
        latest = db.execute(select(DataManifest.id, DataManifest.target_session, DataManifest.input_mode,
            DataManifest.coverage, DataManifest.created_at).order_by(DataManifest.revision_order.desc()).limit(1)).mappings().first()
        screening = db.scalar(select(ScreeningRun).order_by(ScreeningRun.created_at.desc()).limit(1))
        return {"latest_manifest": dict(latest) if latest else None,
            "latest_screening": {"id": screening.id, "status": screening.status, "reason": screening.reason} if screening else None,
            "dependencies": {status: count for status, count in db.execute(select(DependencyRequest.status, func.count())
                .group_by(DependencyRequest.status))},
            "note": "Manifest presence alone does not certify live data or adjustment validation"}

    return routes
