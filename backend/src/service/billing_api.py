"""Customer billing and explicit admin actions; dependencies supplied by the app."""
import json
from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from service import billing
from service.jobs import JobError
from service.models import BankPayment, PlanVersion, PurchaseOrder, Subscription, QuotaBucket
from service.settings import Settings
import time


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=36, max_length=36)


class AvailabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    available: bool


class ReconciliationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=36, max_length=36)
    reason: str = Field(min_length=10, max_length=500)


def payment_view(payment: BankPayment) -> dict:
    return {key: getattr(payment, key) for key in ("id", "order_id", "amount_vnd", "status", "reason", "received_at", "transaction_at")}


def router(settings: Settings, user: Callable, admin: Callable, db_session: Callable) -> APIRouter:
    routes = APIRouter(prefix="/api/v1")

    @routes.get("/plans")
    def plans(db=Depends(db_session)):
        return {"payments_enabled": settings.payments_enabled, "items": [billing.plan_view(p) for p in
            db.scalars(select(PlanVersion).where(PlanVersion.available.is_(True)).order_by(PlanVersion.price_vnd, PlanVersion.id))]}

    @routes.post("/admin/plans", status_code=201)
    def create_plan(body: billing.PlanInput, account=Depends(admin), db=Depends(db_session)):
        plan = billing.create_plan(db, body, account.id)
        db.commit()
        return billing.plan_view(plan)

    @routes.get("/admin/plans")
    def admin_plans(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), account=Depends(admin), db=Depends(db_session)):
        query = select(PlanVersion).order_by(PlanVersion.id)
        if cursor:
            query = query.where(PlanVersion.id > cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [billing.plan_view(p) for p in rows[:limit]], "next_cursor": rows[limit-1].id if len(rows) > limit else None}

    @routes.post("/admin/plans/{plan_id}/availability")
    def availability(plan_id: str, body: AvailabilityInput, account=Depends(admin), db=Depends(db_session)):
        plan = db.scalar(select(PlanVersion).where(PlanVersion.id == plan_id).with_for_update())
        if not plan:
            raise JobError("NOT_FOUND", 404)
        # Plan content is immutable. Price/quota changes always create a new version.
        before = plan.available
        plan.available = body.available
        billing.audit(db, "billing.plan_availability", {"plan_id": plan_id, "before": before, "after": body.available}, account.id)
        db.commit()
        return billing.plan_view(plan)

    @routes.post("/orders", status_code=201)
    def create_order(body: OrderInput, idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
                     account=Depends(user), db=Depends(db_session)):
        order = billing.create_order(db, account.id, body.plan_id, idempotency_key, settings)
        db.commit()
        return billing.order_view(order)

    @routes.get("/orders")
    def orders(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), account=Depends(user), db=Depends(db_session)):
        query = select(PurchaseOrder).where(PurchaseOrder.account_id == account.id).order_by(PurchaseOrder.id)
        if cursor:
            query = query.where(PurchaseOrder.id > cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [billing.order_view(o) for o in rows[:limit]], "next_cursor": rows[limit-1].id if len(rows) > limit else None}

    @routes.get("/orders/{order_id}")
    def order(order_id: str, account=Depends(user), db=Depends(db_session)):
        return billing.order_view(billing.owned_order(db, account.id, order_id))

    @routes.get("/payments")
    def payments(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), account=Depends(user), db=Depends(db_session)):
        query = select(BankPayment).join(PurchaseOrder, BankPayment.order_id == PurchaseOrder.id).where(
            PurchaseOrder.account_id == account.id).order_by(BankPayment.id)
        if cursor:
            query = query.where(BankPayment.id > cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [payment_view(p) for p in rows[:limit]], "next_cursor": rows[limit-1].id if len(rows) > limit else None}

    @routes.post("/orders/{order_id}/cancel")
    def cancel_order(order_id: str, account=Depends(user), db=Depends(db_session)):
        order = billing.cancel_order(db, account.id, order_id)
        db.commit()
        return billing.order_view(order)

    @routes.get("/me/entitlements")
    def entitlements(account=Depends(user), db=Depends(db_session)):
        now = int(time.time())
        bucket = db.scalar(select(QuotaBucket).where(QuotaBucket.account_id == account.id,
            QuotaBucket.valid_from <= now, QuotaBucket.valid_until > now)
            .order_by(QuotaBucket.valid_from.desc(), QuotaBucket.id).limit(1))
        return {"active": bucket is not None, "bucket_id": bucket.id if bucket else None,
            "version": bucket.entitlement_version if bucket else None,
            "remaining": bucket.quota_total-bucket.consumed-bucket.reserved if bucket else 0,
            "active_limit": bucket.active_limit if bucket else 0,
            "valid_until": bucket.valid_until if bucket else None,
            "cancel_after_compute_consumes": True}

    @routes.get("/subscription")
    def subscription(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), account=Depends(user), db=Depends(db_session)):
        query = select(Subscription).where(Subscription.account_id == account.id).order_by(Subscription.id)
        if cursor:
            query = query.where(Subscription.id > cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"renewal": "PREPAID_NEXT_PERIOD", "items": [{key: getattr(s, key) for key in
            ("id", "order_id", "plan_id", "bucket_id", "valid_from", "valid_until")} for s in rows[:limit]],
            "next_cursor": rows[limit-1].id if len(rows) > limit else None}

    @routes.post("/webhooks/sepay")
    async def webhook(request: Request, authorization: str | None = Header(default=None), db=Depends(db_session)):
        billing.authenticate_webhook(settings, authorization)
        billing.require_configuration(settings)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                raise JobError("WEBHOOK_TOO_LARGE", 413)
        try:
            payload = billing.Webhook.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            raise JobError("INVALID_WEBHOOK", 422) from None
        billing.receive(db, payload)
        db.commit()
        return {"success": True}

    @routes.get("/admin/reconciliation")
    def reconciliation(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), account=Depends(admin), db=Depends(db_session)):
        query = select(BankPayment).where(BankPayment.status == "RECONCILIATION").order_by(BankPayment.id)
        if cursor:
            query = query.where(BankPayment.id > cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [payment_view(p) | {"references": p.references} for p in rows[:limit]],
            "next_cursor": rows[limit-1].id if len(rows) > limit else None}

    @routes.post("/admin/reconciliation/{payment_id}/apply")
    def apply(payment_id: str, body: ReconciliationInput, account=Depends(admin), db=Depends(db_session)):
        payment = billing.reconcile(db, payment_id, body.order_id, account.id, body.reason)
        db.commit()
        return payment_view(payment)

    return routes
