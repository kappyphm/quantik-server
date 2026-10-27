"""Encrypted integration credentials with optimistic updates and redacted audit."""
import json
import time

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.orm import Session

from service.billing import audit, bank_name, require_configuration
from service.jobs import JobError
from service.models import IntegrationConfig
from service.operational_config import insert_for
from service.settings import Settings

SEPAY_FIELDS = ("webhook_api_key", "bank_code", "account_number", "account_name", "transfer_prefix", "requires_transfer_prefix", "payments_enabled")


def cipher(settings: Settings) -> Fernet:
    try:
        path = settings.integration_encryption_key_path
        if path.stat().st_size > 128:
            raise ValueError()
        return Fernet(path.read_bytes().strip())
    except (OSError, ValueError):
        raise JobError("INTEGRATION_ENCRYPTION_KEY_UNAVAILABLE", 503) from None


def defaults(name: str, settings: Settings) -> dict:
    if name == "vnstock":
        return {"api_key": settings.vnstock_api_key.get_secret_value()}
    return {field: settings.sepay_webhook_api_key.get_secret_value() if field == "webhook_api_key" else
        getattr(settings, "payments_enabled" if field == "payments_enabled" else "sepay_"+field) for field in SEPAY_FIELDS}


def read(db: Session, name: str, settings: Settings) -> tuple[dict, IntegrationConfig | None]:
    row = db.get(IntegrationConfig, name)
    if row is None or row.encrypted_payload is None:
        return defaults(name, settings), row
    try:
        envelope = json.loads(cipher(settings).decrypt(row.encrypted_payload.encode()))
        if envelope["name"] != name or envelope["revision"] != row.revision:
            raise ValueError()
        values = envelope["values"]
        expected = {"api_key"} if name == "vnstock" else set(SEPAY_FIELDS)
        if set(values) != expected:
            raise ValueError()
        return values, row
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise JobError("INTEGRATION_CONFIG_UNREADABLE", 503) from None


def sepay_settings(db: Session, settings: Settings) -> Settings:
    values, _ = read(db, "sepay", settings)
    return with_sepay(settings, values)


def with_sepay(settings: Settings, values: dict) -> Settings:
    return settings.model_copy(update={"payments_enabled" if field == "payments_enabled" else "sepay_"+field:
        SecretStr(value) if field == "webhook_api_key" else value for field, value in values.items()})


def view(db: Session, name: str, settings: Settings) -> dict:
    values, row = read(db, name, settings)
    result = {"provider": name, "revision": row.revision if row else 0,
        "source": "ADMIN_DATABASE" if row and row.encrypted_payload else "ENVIRONMENT",
        "updated_at": row.updated_at if row else None}
    try:
        cipher(settings)
        result["encryption_ready"] = True
    except JobError:
        result["encryption_ready"] = False
    if name == "vnstock":
        result["api_key_configured"] = bool(values["api_key"])
        result["scheduled_provider_enabled"] = settings.scheduled_provider_enabled
    else:
        result.update({field: value for field, value in values.items() if field != "webhook_api_key"})
        result["webhook_api_key_configured"] = bool(values["webhook_api_key"])
        missing = [field for field in ("webhook_api_key", "bank_code", "account_number", "account_name") if not values[field]]
        if values["requires_transfer_prefix"] and not values["transfer_prefix"]:
            missing.append("transfer_prefix")
        result["missing_fields"] = missing
        result["configuration_complete"] = not missing
        from sqlalchemy import func, select
        from service.models import BankPayment, PurchaseOrder
        now = int(time.time())
        result["pending_orders"] = db.scalar(select(func.count()).select_from(PurchaseOrder).where(
            PurchaseOrder.status == "PENDING", PurchaseOrder.expires_at > now)) or 0
        result["reconciliation_count"] = db.scalar(select(func.count()).select_from(BankPayment).where(
            BankPayment.status == "RECONCILIATION")) or 0
        result["last_payment_received_at"] = db.scalar(select(func.max(BankPayment.received_at)))
    return result


def patch(db: Session, name: str, settings: Settings, changes: dict, expected_revision: int, reason: str, actor_id: str) -> dict:
    encryption = cipher(settings)
    # Atomic insert avoids races when two admins configure a provider for the first time.
    db.execute(insert_for(db)(IntegrationConfig).values(name=name, revision=0)
        .on_conflict_do_nothing(index_elements=["name"]))
    values, row = read(db, name, settings)
    if row.revision != expected_revision:
        raise JobError("INTEGRATION_REVISION_CONFLICT", 409)
    if not changes:
        raise JobError("INTEGRATION_CHANGES_REQUIRED", 422)
    if name == "sepay" and "webhook_api_key" in changes and not changes["webhook_api_key"] and values["webhook_api_key"]:
        from sqlalchemy import func, select
        from service.models import BankPayment, PurchaseOrder
        pending = db.scalar(select(func.count()).select_from(PurchaseOrder).where(
            PurchaseOrder.status == "PENDING", PurchaseOrder.expires_at > int(time.time())))
        unresolved = db.scalar(select(func.count()).select_from(BankPayment).where(BankPayment.status == "RECONCILIATION"))
        if pending or unresolved:
            raise JobError("WEBHOOK_KEY_IN_USE", 409)
    values.update({field: (value.get_secret_value() if isinstance(value, SecretStr) else value) if value is not None else ""
        for field, value in changes.items()})
    if name == "sepay":
        values["bank_code"] = bank_name(values["bank_code"])
        if values["payments_enabled"]:
            try:
                require_configuration(with_sepay(settings, values))
            except JobError as exc:
                raise JobError(exc.code, 422) from None
    revision = expected_revision+1
    payload = encryption.encrypt(json.dumps({"name": name, "revision": revision, "values": values},
        ensure_ascii=False, separators=(",", ":")).encode()).decode()
    changed = db.execute(update(IntegrationConfig).where(IntegrationConfig.name == name, IntegrationConfig.revision == expected_revision)
        .values(revision=revision, encrypted_payload=payload, updated_at=int(time.time()), actor_id=actor_id))
    if changed.rowcount != 1:
        raise JobError("INTEGRATION_REVISION_CONFLICT", 409)
    audit(db, "integration.updated", {"provider": name, "revision": revision, "previous_revision": expected_revision,
        "changed_fields": sorted(changes), "reason": reason}, actor_id)
    db.flush()
    db.expire(row)
    return view(db, name, settings)
