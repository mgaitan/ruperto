"""Application settings shared by the API, CLI, and future integrations."""

from __future__ import annotations

import json

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from the environment.

    The current settings focus on the service bootstrap and deployment surface.
    They intentionally reserve a small set of fields for the future agent and
    channel integrations so the public configuration contract starts stable.
    """

    model_config = SettingsConfigDict(
        env_prefix="RUPERTO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./ruperto.db"
    auto_init_db: bool = True

    store_name: str = "Demo Rotisería"
    bot_name: str = "Ruperto"
    store_location: str | None = None
    store_description: str = "Rotisería de barrio con pedidos asistidos por chat."
    assistant_personality: str = "Amable, ágil y confiable."
    store_locale: str = "es-AR"
    store_timezone: str = "America/Argentina/Cordoba"
    store_transfer_alias: str | None = "demo.rotiseria"
    default_store_id: int = 1
    dashboard_session_secret: str = "change-me-in-production"
    dashboard_admin_email: str | None = None
    dashboard_admin_password: SecretStr | None = None
    dashboard_admin_name: str = "Store Admin"
    smtp_server: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None

    gemini_model: str = "gemini-2.5-flash-lite"
    gemini_api_key: SecretStr | None = None
    assistant_model_timeout_seconds: float = 25.0
    assistant_model_retry_attempts: int = 1
    kapso_api_key: SecretStr | None = None
    kapso_phone_number_id: str | None = None
    kapso_webhook_secret: SecretStr | None = None

    @field_validator(
        "dashboard_admin_email",
        "smtp_server",
        "smtp_user",
        "kapso_phone_number_id",
        mode="before",
    )
    @classmethod
    def blank_strings_to_none(cls, value: str | None) -> str | None:
        """Treat blank optional strings from `.env` as missing values."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator(
        "dashboard_admin_password",
        "smtp_password",
        "gemini_api_key",
        "kapso_api_key",
        "kapso_webhook_secret",
        mode="before",
    )
    @classmethod
    def blank_secrets_to_none(cls, value: str | SecretStr | None) -> str | SecretStr | None:
        """Treat blank secret values from `.env` as missing values."""
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        stripped = value.strip()
        return stripped or None

    def public_settings(self) -> dict[str, str | bool | float | None]:
        """Return a safe snapshot suitable for logs, docs, and diagnostics."""
        return {
            "environment": self.environment,
            "database_url": self.database_url,
            "auto_init_db": self.auto_init_db,
            "store_name": self.store_name,
            "bot_name": self.bot_name,
            "store_location": self.store_location,
            "store_description": self.store_description,
            "assistant_personality": self.assistant_personality,
            "store_locale": self.store_locale,
            "store_timezone": self.store_timezone,
            "store_transfer_alias": self.store_transfer_alias,
            "default_store_id": self.default_store_id,
            "dashboard_session_secret_configured": self.dashboard_session_secret != "change-me-in-production",
            "dashboard_admin_email": self.dashboard_admin_email,
            "dashboard_admin_password_configured": self.dashboard_admin_password is not None,
            "dashboard_admin_name": self.dashboard_admin_name,
            "smtp_server": self.smtp_server,
            "smtp_port": self.smtp_port,
            "smtp_user": self.smtp_user,
            "smtp_password_configured": self.smtp_password is not None,
            "smtp_configured": all(
                (
                    self.smtp_server is not None,
                    self.smtp_port is not None,
                    self.smtp_user is not None,
                    self.smtp_password is not None,
                )
            ),
            "gemini_model": self.gemini_model,
            "gemini_api_key_configured": self.gemini_api_key is not None,
            "assistant_model_timeout_seconds": self.assistant_model_timeout_seconds,
            "assistant_model_retry_attempts": self.assistant_model_retry_attempts,
            "kapso_api_key_configured": self.kapso_api_key is not None,
            "kapso_phone_number_id": self.kapso_phone_number_id,
            "kapso_webhook_secret_configured": self.kapso_webhook_secret is not None,
        }

    def public_settings_json(self) -> str:
        """Return the public snapshot as formatted JSON."""
        return json.dumps(self.public_settings(), indent=2)
