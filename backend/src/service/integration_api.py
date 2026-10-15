from collections.abc import Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from service import integrations
from service.settings import Settings


class IntegrationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if len(value.strip()) < 10:
            raise ValueError("Reason required")
        return value.strip()

    def changes(self) -> dict:
        return {name: getattr(self, name) for name in self.model_fields_set if name not in {"reason", "expected_revision"}}


class VnstockPatch(IntegrationPatch):
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def valid_secret(cls, value):
        if value is not None:
            secret = value.get_secret_value()
            if secret != secret.strip() or not secret.isprintable():
                raise ValueError("Invalid key")
        return value


class SepayPatch(IntegrationPatch):
    webhook_api_key: SecretStr | None = Field(default=None, min_length=16, max_length=4096)
    bank_code: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9]*$")
    account_number: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9]*$")
    account_name: str | None = Field(default=None, max_length=200)
    transfer_prefix: str | None = Field(default=None, max_length=50)
    requires_transfer_prefix: bool | None = None
    payments_enabled: bool | None = None

    @field_validator("webhook_api_key")
    @classmethod
    def valid_secret(cls, value):
        return VnstockPatch.valid_secret(value)

    @model_validator(mode="after")
    def validate_changes(self):
        for field, value in self.changes().items():
            if field != "webhook_api_key" and value is None:
                raise ValueError("Only secrets accept null to clear")
            if isinstance(value, str) and (value != value.strip() or (value and not value.isprintable())):
                raise ValueError("Invalid configuration text")
        return self


def router(settings: Settings, admin: Callable, db_session: Callable) -> APIRouter:
    routes = APIRouter(prefix="/api/v1/admin/integrations")

    @routes.get("/vnstock")
    def vnstock(actor=Depends(admin), db=Depends(db_session)):
        return integrations.view(db, "vnstock", settings)

    @routes.patch("/vnstock")
    def update_vnstock(body: VnstockPatch, actor=Depends(admin), db=Depends(db_session)):
        result = integrations.patch(db, "vnstock", settings, body.changes(), body.expected_revision, body.reason, actor.id)
        db.commit()
        return result

    @routes.get("/sepay")
    def sepay(actor=Depends(admin), db=Depends(db_session)):
        return integrations.view(db, "sepay", settings)

    @routes.patch("/sepay")
    def update_sepay(body: SepayPatch, actor=Depends(admin), db=Depends(db_session)):
        result = integrations.patch(db, "sepay", settings, body.changes(), body.expected_revision, body.reason, actor.id)
        db.commit()
        return result

    return routes
