from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEXA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "production"] = "development"
    database_path: str = "./data/nexa_ai.db"
    database_url: SecretStr | None = None
    batch_size_limit: int = Field(default=25, ge=1, le=100)
    processing_lease_seconds: float = Field(default=180.0, ge=1, le=86400)
    conversation_context_limit: int = Field(default=10, ge=0, le=10)
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=180)
    access_tokens_json: dict[str, str] = Field(default_factory=dict)
    rate_limit_requests: int = Field(default=60, ge=1, le=10000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=86400)
    retry_max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_delay_seconds: float = Field(default=5.0, gt=0, le=3600)
    retry_max_delay_seconds: float = Field(default=300.0, gt=0, le=86400)
    message_retention_days: int = Field(default=90, ge=1, le=3650)

    def __init__(self, **values: Any) -> None:
        try:
            super().__init__(**values)
        except Exception as exc:
            # Pydantic validation errors can include rejected input values (e.g. bearer tokens).
            if isinstance(exc, ValidationError):
                issues = "; ".join(
                    error["msg"]
                    for error in exc.errors(include_input=False, include_context=False)
                )
            else:
                issues = type(exc).__name__
            raise RuntimeError(f"Nexa configuration validation failed: {issues}") from None

    @model_validator(mode="after")
    def validate_provider_and_production_requirements(self) -> "Settings":
        parsed_url = urlsplit(self.llm_base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("NEXA_LLM_BASE_URL must be a valid HTTP(S) base URL")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", self.llm_model):
            raise ValueError("NEXA_LLM_MODEL contains invalid characters")
        if self.retry_base_delay_seconds > self.retry_max_delay_seconds:
            raise ValueError("Retry base delay cannot exceed the retry maximum delay")

        if self.environment == "production":
            api_key = self.llm_api_key.get_secret_value().strip()
            if len(api_key) < 20 or api_key.lower() in {
                "replace-with-provider-key",
                "your-api-key",
                "change-me",
            }:
                raise ValueError("A valid NEXA_LLM_API_KEY is required in production")
            if parsed_url.scheme != "https":
                raise ValueError("NEXA_LLM_BASE_URL must use HTTPS in production")
            database_url = self.database_url.get_secret_value().strip() if self.database_url else ""
            if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("NEXA_DATABASE_URL must configure PostgreSQL in production")
            database_parts = urlsplit(database_url)
            if not database_parts.hostname or not database_parts.path.strip("/"):
                raise ValueError("NEXA_DATABASE_URL must include a PostgreSQL host and database")
            query_options = dict(
                item.split("=", 1) for item in database_parts.query.split("&") if "=" in item
            )
            if query_options.get("sslmode", "").lower() not in {"require", "verify-ca", "verify-full"}:
                raise ValueError("NEXA_DATABASE_URL must require TLS with sslmode")
            if not self.access_tokens_json:
                raise ValueError("NEXA_ACCESS_TOKENS_JSON is required in production")
            if any(
                len(token) < 32 or not user_id.strip()
                for token, user_id in self.access_tokens_json.items()
            ):
                raise ValueError("Production bearer tokens must be high entropy and map to users")
        return self

    @property
    def database_file(self) -> Path:
        return Path(self.database_path).expanduser().resolve()

    @property
    def database_connection_url(self) -> str:
        value = self.database_url.get_secret_value().strip() if self.database_url else ""
        if value:
            if value.startswith("postgresql://"):
                return "postgresql+psycopg://" + value.removeprefix("postgresql://")
            return value
        return f"sqlite:///{self.database_file.as_posix()}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
