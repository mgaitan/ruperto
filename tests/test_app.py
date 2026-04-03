"""Tests for the FastAPI application bootstrap."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ruperto.app import create_app
from ruperto.config import Settings
from ruperto.models import OrderStatus

HTTP_OK = 200
HTTP_NOT_FOUND = 404
MIN_MENU_ITEMS = 35
DEFAULT_WEEKLY_HOURS = 7
UPDATED_WEEKLY_HOURS = 2


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


def collect_tool_returns(messages: list[ModelMessage]) -> dict[str, Any]:
    """Collect the latest tool returns emitted during a deterministic model run."""
    tool_returns: dict[str, Any] = {}
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, ToolReturnPart):
                tool_returns[part.tool_name] = part.content
    return tool_returns


def dev_message_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Deterministic model used to exercise the development chat endpoint."""
    tool_returns = collect_tool_returns(messages)
    sequence: list[tuple[str, dict[str, Any]]] = [
        ("update_customer_name", {"name": "Martina"}),
        ("add_item_to_current_order", {"sku": "hamburguesa-completa", "quantity": 1}),
        ("set_order_delivery_type", {"delivery_type": "pickup"}),
        ("set_order_payment_method", {"payment_method": "cash"}),
        ("confirm_current_order", {}),
    ]
    for tool_name, arguments in sequence:
        if tool_name not in tool_returns:
            return ModelResponse(
                parts=[ToolCallPart(tool_name, arguments)],
                model_name="function:test-api-dev-message",
            )

    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": "Hola Martina, tu pedido quedó confirmado.",
                    "next_step": "complete",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-api-dev-message",
    )


def test_mvp_api_surface_exposes_dev_chat_and_read_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The API exposes basic store, catalog, customer, and order surfaces."""
    monkeypatch.setattr("ruperto.assistant.build_google_model", lambda settings: FunctionModel(dev_message_model))
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        store_response = client.get("/api/store-profile")
        store_hours_response = client.get("/api/store-hours")
        menu_response = client.get("/api/menu-items", params={"only_available": "false"})
        first_chat_response = client.post(
            "/api/dev/messages",
            json={"external_user_id": "cli-user", "message_text": "Hola, quiero pedir"},
        )
        second_chat_response = client.post(
            "/api/dev/messages",
            json={"external_user_id": "cli-user", "message_text": "Martina"},
        )
        chat_response = client.post(
            "/api/dev/messages",
            json={"external_user_id": "cli-user", "message_text": "Quiero una hamburguesa"},
        )
        customers_response = client.get("/api/customers", params={"limit": 1})
        orders_response = client.get("/api/orders", params={"limit": 10})
        confirmed_orders_response = client.get("/api/orders", params={"status": "confirmed", "limit": 10})

    assert store_response.status_code == HTTP_OK
    assert store_response.json()["locale"] == "es-AR"
    assert store_hours_response.status_code == HTTP_OK
    assert len(store_hours_response.json()) == DEFAULT_WEEKLY_HOURS

    assert menu_response.status_code == HTTP_OK
    assert len(menu_response.json()) >= MIN_MENU_ITEMS

    assert first_chat_response.status_code == HTTP_OK
    assert first_chat_response.json()["reply"]["next_step"] == "ask_name"
    assert second_chat_response.status_code == HTTP_OK
    assert second_chat_response.json()["customer"]["name"] == "Martina"
    assert second_chat_response.json()["reply"]["next_step"] == "choose_items"

    assert chat_response.status_code == HTTP_OK
    chat_payload = chat_response.json()
    assert chat_payload["customer"]["name"] == "Martina"
    assert chat_payload["reply"]["next_step"] == "complete"
    assert chat_payload["current_order"]["status"] == "confirmed"

    assert customers_response.status_code == HTTP_OK
    assert customers_response.json()[0]["name"] == "Martina"

    assert orders_response.status_code == HTTP_OK
    assert len(orders_response.json()) == 1

    assert confirmed_orders_response.status_code == HTTP_OK
    assert confirmed_orders_response.json()[0]["status"] == "confirmed"


def test_staff_can_update_order_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Staff endpoints can move an order through the operational workflow."""
    monkeypatch.setattr("ruperto.assistant.build_google_model", lambda settings: FunctionModel(dev_message_model))
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Hola"})
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Martina"})
        order_response = client.post(
            "/api/dev/messages",
            json={"external_user_id": "cli-user", "message_text": "Quiero una hamburguesa"},
        )
        order_id = order_response.json()["current_order"]["id"]
        status_response = client.patch(
            f"/api/orders/{order_id}/status",
            json={"status": OrderStatus.ALMOST_READY.value},
        )

    assert status_response.status_code == HTTP_OK
    assert status_response.json()["status"] == OrderStatus.ALMOST_READY.value


def test_staff_update_order_status_returns_not_found_for_unknown_order(tmp_path: Path):
    """The staff status endpoint returns 404 when the order is missing."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.patch("/api/orders/999/status", json={"status": OrderStatus.ALMOST_READY.value})

    assert response.status_code == HTTP_NOT_FOUND
    assert response.json()["detail"] == "Order not found."


def test_staff_can_replace_store_hours(tmp_path: Path):
    """The staff API can replace the weekly opening-hours schedule."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.put(
            "/api/store-hours",
            json={
                "hours": [
                    {"weekday": 0, "opens_at": None, "closes_at": None, "closed": True},
                    {"weekday": 1, "opens_at": "18:00", "closes_at": "23:30", "closed": False},
                ]
            },
        )

    assert response.status_code == HTTP_OK
    assert len(response.json()) == UPDATED_WEEKLY_HOURS
    assert response.json()[0]["closed"] is True
    assert response.json()[1]["opens_at"] == "18:00"
