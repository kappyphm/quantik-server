from service.pagination import encode as history_cursor, newest
"""Customer billing and explicit admin actions; dependencies supplied by the app."""
import json
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, or_, select

from service import billing
from service.jobs import JobError
from service.models import Account, BankPayment, PlanVersion, PurchaseOrder, Subscription, QuotaBucket
from service.settings import Settings
from service.integrations import sepay_settings
import time


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=36, max_length=36)
    new_order: bool = False


class AvailabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    available: bool
    replace_existing: bool = False


class ReconciliationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=36, max_length=36)
    reason: str = Field(min_length=10, max_length=500)


def payment_view(payment: BankPayment) -> dict:
    return {key: getattr(payment, key) for key in ("id", "order_id", "amount_vnd", "status", "reason", "received_at", "transaction_at")}


def router(settings: Settings, user: Callable, admin: Callable, db_session: Callable) -> APIRouter:
    routes = APIRouter(prefix="/api/v1")

    def effective_settings(db=Depends(db_session)):
        return sepay_settings(db, settings)

    @routes.get("/plans")
    def plans(db=Depends(db_session), current=Depends(effective_settings)):
        return {"payments_enabled": current.payments_enabled, "items": [billing.plan_view(p) for p in
            db.scalars(select(PlanVersion).where(PlanVersion.available.is_(True)).order_by(PlanVersion.price_vnd, PlanVersion.id))]}

    @routes.post("/admin/plans", status_code=201)
    def create_plan(body: billing.PlanInput,
                    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
                    account=Depends(admin), db=Depends(db_session)):
        plan = billing.create_plan(db, body, account.id, idempotency_key)
        db.commit()
        return billing.plan_view(plan)

    @routes.get("/admin/plans")
    def admin_plans(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), account=Depends(admin), db=Depends(db_session)):
        query = select(PlanVersion).order_by(PlanVersion.id)
        query = newest(query, PlanVersion, "created_at", cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [billing.plan_view(p) for p in rows[:limit]], "next_cursor": history_cursor(rows[limit-1].created_at, rows[limit-1].id) if len(rows) > limit else None}

    @routes.post("/admin/plans/{plan_id}/availability")
    def availability(plan_id: str, body: AvailabilityInput, account=Depends(admin), db=Depends(db_session)):
        plan = db.scalar(select(PlanVersion).where(PlanVersion.id == plan_id).with_for_update())
        if not plan:
            raise JobError("NOT_FOUND", 404)
        # Plan content is immutable. Price/quota changes always create a new version.
        before = plan.available
        if body.available and plan.replaces_plan_id and not before:
            if not body.replace_existing:
                raise JobError("PLAN_REPLACEMENT_CONFIRMATION_REQUIRED", 409)
            previous = db.scalar(select(PlanVersion).where(PlanVersion.id == plan.replaces_plan_id).with_for_update())
            competing = db.scalar(select(PlanVersion.id).where(PlanVersion.replaces_plan_id == previous.id,
                PlanVersion.available.is_(True), PlanVersion.id != plan.id).limit(1))
            if competing:
                raise JobError("PLAN_REPLACEMENT_CONFLICT", 409)
            previous.available = False
            billing.audit(db, "billing.plan_replaced", {"previous_plan_id": previous.id, "plan_id": plan.id}, account.id)
        plan.available = body.available
        billing.audit(db, "billing.plan_availability", {"plan_id": plan_id, "before": before, "after": body.available}, account.id)
        db.commit()
        return billing.plan_view(plan)

    @routes.post("/orders", status_code=201)
    def create_order(body: OrderInput, idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
                     account=Depends(user), db=Depends(db_session), current=Depends(effective_settings)):
        order = billing.create_order(db, account.id, body.plan_id, idempotency_key, current, new_order=body.new_order)
        db.commit()
        return billing.order_detail(db, order)

    @routes.get("/orders")
    def orders(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), account=Depends(user), db=Depends(db_session)):
        query = select(PurchaseOrder).where(PurchaseOrder.account_id == account.id).order_by(PurchaseOrder.id)
        query = newest(query, PurchaseOrder, "created_at", cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [billing.order_detail(db, o) for o in rows[:limit]], "next_cursor": history_cursor(rows[limit-1].created_at, rows[limit-1].id) if len(rows) > limit else None}

    @routes.get("/orders/{order_id}")
    def order(order_id: str, account=Depends(user), db=Depends(db_session)):
        return billing.order_detail(db, billing.owned_order(db, account.id, order_id))

    @routes.get("/admin/orders")
    def admin_orders(cursor: str | None = None, limit: int = Query(20, ge=1, le=100),
                     status: Literal["PENDING", "PAID", "EXPIRED", "CANCELLED"] | None = None,
                     account_id: str | None = None, plan_id: str | None = None,
                     reference: str | None = Query(None, min_length=1, max_length=32),
                     actor=Depends(admin), db=Depends(db_session)):
        now = int(time.time())
        query = select(PurchaseOrder, Account, PlanVersion).join(Account, Account.id == PurchaseOrder.account_id).join(
            PlanVersion, PlanVersion.id == PurchaseOrder.plan_id).order_by(PurchaseOrder.id)
        query = newest(query, PurchaseOrder, "created_at", cursor)
        if account_id:
            query = query.where(PurchaseOrder.account_id == account_id)
        if plan_id:
            query = query.where(PurchaseOrder.plan_id == plan_id)
        if reference:
            query = query.where(PurchaseOrder.reference == reference.strip().upper())
        if status == "EXPIRED":
            query = query.where(or_(PurchaseOrder.status == "EXPIRED", and_(
                PurchaseOrder.status == "PENDING", PurchaseOrder.expires_at <= now)))
        elif status == "PENDING":
            query = query.where(PurchaseOrder.status == "PENDING", PurchaseOrder.expires_at > now)
        elif status:
            query = query.where(PurchaseOrder.status == status)
        rows = list(db.execute(query.limit(limit + 1)))
        return {"items": [billing.order_view(order, now) | {"account_id": customer.id,
            "display_name": customer.display_name, "email": customer.email, "plan_name": plan.name}
            for order, customer, plan in rows[:limit]],
            "next_cursor": history_cursor(rows[limit - 1][0].created_at, rows[limit - 1][0].id) if len(rows) > limit else None}

    @routes.get("/admin/orders/{order_id}")
    def admin_order(order_id: str, actor=Depends(admin), db=Depends(db_session)):
        order = db.get(PurchaseOrder, order_id)
        if not order:
            raise JobError("NOT_FOUND", 404)
        customer = db.get(Account, order.account_id)
        plan = db.get(PlanVersion, order.plan_id)
        subscription = db.scalar(select(Subscription).where(Subscription.order_id == order.id))
        return billing.order_detail(db, order) | {
            "customer": {"id": customer.id, "display_name": customer.display_name, "email": customer.email},
            "plan": billing.plan_view(plan),
            "payments": [payment_view(p) for p in db.scalars(select(BankPayment).where(
                BankPayment.order_id == order.id).order_by(BankPayment.received_at, BankPayment.id))],
            "subscription": {key: getattr(subscription, key) for key in
                ("id", "bucket_id", "valid_from", "valid_until")} if subscription else None,
        }

    @routes.get("/payments")
    def payments(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), account=Depends(user), db=Depends(db_session)):
        query = select(BankPayment).join(PurchaseOrder, BankPayment.order_id == PurchaseOrder.id).where(
            PurchaseOrder.account_id == account.id).order_by(BankPayment.id)
        query = newest(query, BankPayment, "received_at", cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [payment_view(p) for p in rows[:limit]], "next_cursor": history_cursor(rows[limit-1].received_at, rows[limit-1].id) if len(rows) > limit else None}

    @routes.post("/orders/{order_id}/cancel")
    def cancel_order(order_id: str, account=Depends(user), db=Depends(db_session)):
        order = billing.cancel_order(db, account.id, order_id)
        db.commit()
        return billing.order_detail(db, order)

    @routes.get("/me/entitlements")
    def entitlements(account=Depends(user), db=Depends(db_session)):
        now = int(time.time())
        from service.daily_limits import current_bucket, day_bounds
        bucket = current_bucket(db, account.id, now)
        plan = db.get(PlanVersion, bucket.entitlement_version) if bucket else None
        period = db.get(Subscription, bucket.subscription_id) if bucket and bucket.subscription_id else None
        upcoming = db.scalar(select(Subscription).where(Subscription.account_id == account.id,
            Subscription.valid_from > now).order_by(Subscription.valid_from, Subscription.id).limit(1))
        db.commit()
        return {"active": bucket is not None, "bucket_id": bucket.id if bucket else None,
            "version": bucket.entitlement_version if bucket else None,
            "remaining": max(0, bucket.quota_total-bucket.daily_created) if bucket else 0,
            "active_limit": bucket.active_limit if bucket else 0,
            "valid_until": period.valid_until if period else bucket.valid_until if bucket else None,
            "daily_limit": bucket.quota_total if bucket else 0,
            "created_today": bucket.daily_created if bucket else 0,
            "daily_reset_at": day_bounds(now)[1] if bucket else None,
            "quota_period": "DAY", "quota_timezone": "Asia/Ho_Chi_Minh",
            "plan": billing.plan_view(plan) if plan else None,
            "next_subscription": billing.period_view(db, upcoming) if upcoming else None}

    @routes.get("/subscription")
    def subscription(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), account=Depends(user), db=Depends(db_session)):
        query = select(Subscription).where(Subscription.account_id == account.id).order_by(Subscription.id)
        query = newest(query, Subscription, "valid_from", cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"renewal": "PREPAID_NEXT_PERIOD", "items": [billing.period_view(db, s) for s in rows[:limit]],
            "next_cursor": history_cursor(rows[limit-1].valid_from, rows[limit-1].id) if len(rows) > limit else None}

    @routes.post("/webhooks/sepay")
    async def webhook(request: Request, authorization: str | None = Header(default=None), db=Depends(db_session), current=Depends(effective_settings)):
        billing.authenticate_webhook(current, authorization)
        # Stopping new sales must not stop receipts for orders already issued.
        # Authentication above remains mandatory, even while sales are disabled.
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
        query = newest(query, BankPayment, "received_at", cursor)
        rows = list(db.scalars(query.limit(limit+1)))
        return {"items": [payment_view(p) | {"references": p.references} for p in rows[:limit]],
            "next_cursor": history_cursor(rows[limit-1].received_at, rows[limit-1].id) if len(rows) > limit else None}

    @routes.post("/admin/reconciliation/{payment_id}/apply")
    def apply(payment_id: str, body: ReconciliationInput, account=Depends(admin), db=Depends(db_session)):
        payment = billing.reconcile(db, payment_id, body.order_id, account.id, body.reason)
        db.commit()
        return payment_view(payment)

    return routes
