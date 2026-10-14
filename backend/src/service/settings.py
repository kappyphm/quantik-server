from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUANTIK_", env_file=".env", extra="ignore")
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

    @model_validator(mode="after")
    def production_checks(self) -> "Settings":
        if self.environment == "production":
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Production requires PostgreSQL")
            if not self.public_origin.startswith("https://"):
                raise ValueError("Production requires HTTPS origin")
        if self.access_seconds <= 0 or self.refresh_seconds <= self.access_seconds:
            raise ValueError("Invalid token lifetimes")
        return self
