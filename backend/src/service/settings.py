from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUANTIK_", env_file=".env", extra="ignore", hide_input_in_errors=True)
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///runtime/quantik.sqlite"
    public_origin: str = "http://localhost:8000"
    jwt_private_key_path: Path = Path("runtime/jwt-private.pem")
    jwt_public_key_path: Path = Path("runtime/jwt-public.pem")
    jwt_issuer: str = "quantik"
    access_seconds: int = 900
    refresh_seconds: int = 604800
    google_client_id: str = Field(default="", validation_alias="GOOGLE_CLIENT_ID")
    google_client_secret: SecretStr = Field(default=SecretStr(""), validation_alias="GOOGLE_CLIENT_SECRET")
    amqp_url: SecretStr = SecretStr("amqp://guest:guest@127.0.0.1:5672/%2F")
    allow_fixture_analysis: bool = False
    allow_degraded_analysis: bool = False
    sepay_webhook_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="SEPAY_WEBHOOK_API_KEY")
    sepay_bank_code: str = Field(default="", validation_alias="SEPAY_BANK_CODE")
    sepay_account_number: str = Field(default="", validation_alias="SEPAY_ACCOUNT_NUMBER")
    sepay_account_name: str = Field(default="", validation_alias="SEPAY_ACCOUNT_NAME")
    sepay_transfer_prefix: str = Field(default="", validation_alias="SEPAY_TRANSFER_PREFIX", max_length=50)
    sepay_requires_transfer_prefix: bool = Field(default=False, validation_alias="SEPAY_REQUIRES_TRANSFER_PREFIX")
    payments_enabled: bool = False
    scheduled_provider_enabled: bool = False

    @model_validator(mode="after")
    def production_checks(self) -> "Settings":
        if self.environment == "production":
            from urllib.parse import urlsplit
            broker = urlsplit(self.amqp_url.get_secret_value())
            if broker.username == "guest" or not broker.password:
                raise ValueError("Production requires private broker credentials")
            if self.allow_fixture_analysis or self.allow_degraded_analysis:
                raise ValueError("Production requires validated data and a published model")
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Production requires PostgreSQL")
            if not self.public_origin.startswith("https://"):
                raise ValueError("Production requires HTTPS origin")
        if self.access_seconds <= 0 or self.refresh_seconds <= self.access_seconds:
            raise ValueError("Invalid token lifetimes")
        return self
