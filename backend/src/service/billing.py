"""Prepaid billing. Caller commits payment, entitlement and audit as one transaction."""
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from service.jobs import JobError
from service.models import Account, AuditEvent, BankPayment, PlanVersion, PurchaseOrder, QuotaBucket, Subscription
from service.settings import Settings


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100)
    price_vnd: int = Field(ge=1000, le=100_000_000)
    duration_days: int = Field(ge=1, le=366)
    analysis_quota: int = Field(ge=1, le=100_000)
    active_limit: int = Field(ge=1, le=10)
    features: dict[str, bool] = Field(default_factory=lambda: {"screening": True, "analysis": True})

    @field_validator("features")
    @classmethod
    def supported_features(cls, value: dict[str, bool]) -> dict[str, bool]:
        if set(value) != {"screening", "analysis"} or not all(value.values()):
            raise ValueError("Only implemented screening and analysis features can be sold")
        return value


class Webhook(BaseModel):
    # Ignore provider-added balance/description fields; never persist raw bank descriptions.
    model_config = ConfigDict(strict=True)
    id: int = Field(gt=0)
    gateway: str = Field(min_length=1, max_length=32)
    transactionDate: str = Field(max_length=19)
    accountNumber: str = Field(min_length=1, max_length=64)
    code: str | None = Field(default=None, max_length=128)
    content: str = Field(default="", max_length=4096)
    transferType: str = Field(pattern="^(in|out)$")
    transferAmount: int = Field(gt=0, le=2_000_000_000)
    referenceCode: str = Field(default="", max_length=128)

    @field_validator("transactionDate")
    @classmethod
    def valid_date(cls, value: str) -> str:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return value


def audit(db: Session, action: str, details: dict, actor_id: str | None = None) -> None:
    db.add(AuditEvent(actor_id=actor_id, action=action, occurred_at=int(time.time()), details=details))


def bank_name(value: str) -> str:
    value = value.strip().upper()
    return {"TPB": "TPBANK", "970423": "TPBANK"}.get(value, value)


def require_configuration(settings: Settings) -> None:
    if not settings.payments_enabled:
        raise JobError("PAYMENTS_DISABLED", 503)
    if not all([settings.sepay_bank_code, settings.sepay_account_number, settings.sepay_account_name,
                settings.sepay_webhook_api_key.get_secret_value()]):
        raise JobError("PAYMENT_CONFIGURATION_MISSING", 503)


def authenticate_webhook(settings: Settings, authorization: str | None) -> None:
    expected = settings.sepay_webhook_api_key.get_secret_value()
    scheme, _, token = (authorization or "").partition(" ")
    if not expected or scheme.lower() != "apikey" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise JobError("WEBHOOK_UNAUTHORIZED", 401)


def create_plan(db: Session, body: PlanInput, actor_id: str) -> PlanVersion:
    plan = PlanVersion(**body.model_dump(), available=False, created_at=int(time.time()))
    db.add(plan)
    db.flush()
    audit(db, "billing.plan_created", {"plan_id": plan.id}, actor_id)
    return plan


def plan_view(plan: PlanVersion) -> dict:
    return {key: getattr(plan, key) for key in ("id", "name", "price_vnd", "duration_days", "analysis_quota",
        "active_limit", "features", "available") } | {"currency": "VND", "renewal": "PREPAID_NEXT_PERIOD"}


def order_view(order: PurchaseOrder, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    status = "EXPIRED" if order.status == "PENDING" and now >= order.expires_at else order.status
    result = {key: getattr(order, key) for key in ("id", "plan_id", "amount_vnd", "reference", "created_at", "expires_at", "paid_at")}
    result.update(status=status, currency="VND", bank=order.bank_code, account_number=order.bank_account,
        account_name=order.account_name, transfer_content=order.transfer_content,
        qr_url="https://vietqr.app/img?" + urlencode({"acc": order.bank_account, "bank": order.bank_code,
            "amount": order.amount_vnd, "des": order.transfer_content, "holder": order.account_name, "template": "compact"})
            if status == "PENDING" else None)
    return result


def create_order(db: Session, account_id: str, plan_id: str, key: str, settings: Settings, now: int | None = None) -> PurchaseOrder:
    now = int(time.time()) if now is None else now
    if not 1 <= len(key) <= 128:
        raise JobError("INVALID_IDEMPOTENCY_KEY", 422)
    account = db.scalar(select(Account).where(Account.id == account_id).with_for_update().execution_options(populate_existing=True))
    if not account or not account.enabled or account.role != "user":
        raise JobError("ACCOUNT_UNAVAILABLE", 403)
    previous = db.scalar(select(PurchaseOrder).where(PurchaseOrder.account_id == account_id, PurchaseOrder.idempotency_key == key))
    if previous:
        if previous.plan_id != plan_id:
            raise JobError("IDEMPOTENCY_CONFLICT", 409)
        return previous
    require_configuration(settings)
    plan = db.scalar(select(PlanVersion).where(PlanVersion.id == plan_id).with_for_update())
    if not plan or not plan.available:
        raise JobError("PLAN_UNAVAILABLE", 409)
    if db.scalar(select(func.count()).select_from(PurchaseOrder).where(PurchaseOrder.account_id == account_id,
            PurchaseOrder.status == "PENDING", PurchaseOrder.expires_at > now)) >= 5:
        raise JobError("PENDING_ORDER_LIMIT", 429)
    reference = "QTK" + secrets.token_hex(8).upper()
    order = PurchaseOrder(account_id=account_id, plan_id=plan.id, idempotency_key=key,
        reference=reference, amount_vnd=plan.price_vnd, bank_code=settings.sepay_bank_code,
        bank_account=settings.sepay_account_number, account_name=settings.sepay_account_name,
        transfer_content=" ".join(filter(None, [settings.sepay_transfer_prefix.strip(), reference])),
        created_at=now, expires_at=now+1800, status="PENDING")
    db.add(order)
    db.flush()
    audit(db, "billing.order_created", {"order_id": order.id, "plan_id": plan.id}, account_id)
    return order


def owned_order(db: Session, account_id: str, order_id: str) -> PurchaseOrder:
    order = db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == order_id, PurchaseOrder.account_id == account_id))
    if not order:
        raise JobError("NOT_FOUND", 404)
    return order


def grant(db: Session, payment: BankPayment, order: PurchaseOrder, now: int, actor_id: str | None = None, *, manual: bool = False) -> None:
    # Account lock serializes renewals, followed by order lock (same order as create_order).
    with db.no_autoflush:
        account = db.scalar(select(Account).where(Account.id == order.account_id).with_for_update().execution_options(populate_existing=True))
        order = db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == order.id).with_for_update().execution_options(populate_existing=True))
    payment.order_id = order.id
    if not account.enabled:
        payment.reason = "ACCOUNT_DISABLED"
        return
    if order.status == "PAID":
        payment.reason = "ORDER_ALREADY_PAID"
        return
    # Recheck after locks: cancellation/expiry can win while this webhook waits.
    payment.reason = mismatch(payment, order, now, manual=manual)
    if payment.reason:
        return
    plan = db.get(PlanVersion, order.plan_id)
    # Immutable version: future plan withdrawal cannot change an already purchased contract.
    last_end = db.scalar(select(func.max(Subscription.valid_until)).where(Subscription.account_id == account.id))
    start = max(now, last_end or now)
    bucket = QuotaBucket(account_id=account.id, valid_from=start, valid_until=start+plan.duration_days*86400,
        quota_total=plan.analysis_quota, active_limit=plan.active_limit, entitlement_version=plan.id)
    db.add(bucket)
    db.flush()
    db.add(Subscription(account_id=account.id, order_id=order.id, payment_id=payment.id, plan_id=plan.id,
        bucket_id=bucket.id, valid_from=bucket.valid_from, valid_until=bucket.valid_until))
    payment.status, payment.reason = "APPLIED", None
    order.status, order.paid_at = "PAID", now
    audit(db, "billing.payment_applied", {"order_id": order.id, "payment_id": payment.id, "bucket_id": bucket.id}, actor_id)


def mismatch(payment: BankPayment, order: PurchaseOrder, now: int, *, manual: bool = False) -> str | None:
    if payment.direction != "in":
        return "OUTBOUND_TRANSFER"
    if bank_name(payment.bank_code) != bank_name(order.bank_code) or payment.bank_account != order.bank_account:
        return "WRONG_BANK_ACCOUNT"
    if payment.amount_vnd != order.amount_vnd:
        return "AMOUNT_MISMATCH"
    if payment.transaction_at > now + 60:
        return "FUTURE_TRANSACTION"
    if order.status == "CANCELLED":
        return "ORDER_CANCELLED"
    if not manual and (order.status != "PENDING" or payment.transaction_at < order.created_at or
                       payment.transaction_at >= order.expires_at or now >= order.expires_at):
        return "ORDER_CLOSED_OR_LATE"
    return None


def cancel_order(db: Session, account_id: str, order_id: str) -> PurchaseOrder:
    db.scalar(select(Account).where(Account.id == account_id).with_for_update())
    order = db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == order_id,
        PurchaseOrder.account_id == account_id).with_for_update())
    if not order:
        raise JobError("NOT_FOUND", 404)
    if order.status == "PAID":
        raise JobError("PAID_ORDER_CANNOT_CANCEL", 409)
    if order.status != "CANCELLED":
        order.status = "CANCELLED"
        audit(db, "billing.order_cancelled", {"order_id": order.id}, account_id)
    return order


def receive(db: Session, body: Webhook, now: int | None = None) -> BankPayment:
    now = int(time.time()) if now is None else now
    # Word boundaries prevent accidental partial matching. Multiple references require reconciliation.
    refs = sorted(set(re.findall(r"(?<![A-Z0-9])QTK[0-9A-F]{16}(?![A-Z0-9])", (body.content + " " + (body.code or "")).upper())))
    transaction_at = int(datetime.strptime(body.transactionDate, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")).timestamp())
    canonical = body.model_dump()
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    bank_key = hashlib.sha256(json.dumps([bank_name(body.gateway), body.accountNumber, body.referenceCode.strip()]).encode()).hexdigest() if body.referenceCode.strip() else None
    values = dict(id=str(body.id), bank_transaction_key=bank_key, fingerprint=fingerprint,
        bank_code=body.gateway, bank_account=body.accountNumber, direction=body.transferType,
        amount_vnd=body.transferAmount, transaction_at=transaction_at, received_at=now,
        references=refs, status="RECONCILIATION", reason="REFERENCE_MISSING_OR_AMBIGUOUS")
    # Native INSERT is the first write, including SQLite's legacy transaction mode.
    # A SAVEPOINT before BEGIN can commit independently and would break atomic rollback.
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    inserted = db.scalar(insert(BankPayment).values(**values).on_conflict_do_nothing().returning(BankPayment.id))
    if inserted is None:
        previous = db.get(BankPayment, str(body.id))
        if previous is None and bank_key:
            previous = db.scalar(select(BankPayment).where(BankPayment.bank_transaction_key == bank_key))
        if previous is None:
            raise JobError("PAYMENT_DEDUPLICATION_FAILED", 500)
        if previous.fingerprint != fingerprint:
            audit(db, "billing.duplicate_payload_conflict", {"payment_id": previous.id, "received_id": str(body.id)})
        return previous
    payment = db.get(BankPayment, inserted)
    if len(refs) == 1:
        order = db.scalar(select(PurchaseOrder).where(PurchaseOrder.reference == refs[0]))
        if order:
            payment.order_id = order.id
            payment.reason = mismatch(payment, order, now)
            if payment.reason is None:
                grant(db, payment, order, now)
        else:
            payment.reason = "ORDER_NOT_FOUND"
    audit(db, "billing.webhook_received", {"payment_id": payment.id, "status": payment.status, "reason": payment.reason})
    return payment


def reconcile(db: Session, payment_id: str, order_id: str, actor_id: str, reason: str) -> BankPayment:
    if not 10 <= len(reason.strip()) <= 500:
        raise JobError("RECONCILIATION_REASON_REQUIRED", 422)
    # Lock account before payment/order to serialize both automatic and manual grants.
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise JobError("NOT_FOUND", 404)
    db.scalar(select(Account).where(Account.id == order.account_id).with_for_update())
    payment = db.scalar(select(BankPayment).where(BankPayment.id == payment_id).with_for_update().execution_options(populate_existing=True))
    if not payment:
        raise JobError("NOT_FOUND", 404)
    if payment.status == "APPLIED":
        if payment.order_id != order_id:
            raise JobError("PAYMENT_ALREADY_APPLIED", 409)
        return payment
    if mismatch(payment, order, int(time.time()), manual=True):
        raise JobError("PAYMENT_DOES_NOT_MATCH", 409)
    if payment.order_id and payment.order_id != order_id:
        raise JobError("PAYMENT_ORDER_CONFLICT", 409)
    grant(db, payment, order, int(time.time()), actor_id, manual=True)
    audit(db, "billing.manual_reconciliation", {"payment_id": payment.id, "order_id": order.id, "reason": reason}, actor_id)
    return payment
