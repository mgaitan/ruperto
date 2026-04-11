"""Tests for application settings normalization."""

from __future__ import annotations

from pydantic import SecretStr

from ruperto.config import Settings


def test_settings_treat_blank_secret_strings_as_missing_values():
    """Blank secret strings should normalize to `None` without depending on a local `.env` file."""
    settings = Settings.model_validate(
        {
            "kapso_api_key": "   ",
            "kapso_webhook_secret": "",
            "dashboard_admin_password": " ",
            "smtp_password": SecretStr("   "),
        },
    )

    assert settings.kapso_api_key is None
    assert settings.kapso_webhook_secret is None
    assert settings.dashboard_admin_password is None
    assert settings.smtp_password is None
