"""Tests for the FastAPI application bootstrap."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from ruperto.app import create_app
from ruperto.config import Settings

HTTP_OK = 200


def build_settings(tmp_path: Path, *, auto_init_db: bool = True) -> Settings:
    """Create isolated settings for application tests."""
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        auto_init_db=auto_init_db,
        store_name="Test Rotisería",
        bot_name="Test Bot",
        store_location="Córdoba",
    )


def test_root_endpoint(tmp_path: Path):
    """The root endpoint exposes basic service metadata."""
    app = create_app(build_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == HTTP_OK
    assert response.json()["store_name"] == "Test Rotisería"
    assert response.json()["bot_name"] == "Test Bot"
    assert response.json()["store_locale"] == "es-AR"


def test_healthcheck_initializes_database(tmp_path: Path):
    """The healthcheck succeeds when the database runtime is ready."""
    app = create_app(build_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == HTTP_OK
    assert response.json() == {"status": "ok", "database": "ok"}
    assert (tmp_path / "app.db").exists()


def test_root_endpoint_without_auto_init(tmp_path: Path):
    """The app can start without schema bootstrap when auto init is disabled."""
    app = create_app(build_settings(tmp_path, auto_init_db=False))
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == HTTP_OK
    assert response.json()["environment"] == "test"


def test_public_settings_marks_secret_configuration():
    """Secret-backed integrations are only exposed as configured flags."""
    settings = Settings(
        gemini_api_key=SecretStr("gemini-key"),
        kapso_api_key=SecretStr("kapso-key"),
    )
    public = settings.public_settings()

    assert public["gemini_api_key_configured"] is True
    assert public["kapso_api_key_configured"] is True
