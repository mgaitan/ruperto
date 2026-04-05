"""Tests for the FastAPI application bootstrap."""

from __future__ import annotations

import json
from base64 import b64encode
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from ruperto.app import create_app, format_dashboard_datetime, parse_store_hours_form
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import OrderStatus, StaffRole, StoreMembership
from ruperto.repository import BusinessRepository

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_FOUND = 303
HTTP_FORBIDDEN = 403
HTTP_UNAUTHORIZED = 401
HTTP_UNPROCESSABLE_ENTITY = 422
MIN_MENU_ITEMS = 35
DEFAULT_WEEKLY_HOURS = 7
TENANT_STORE_ID = 7
SMTP_PORT = 587


def build_settings(tmp_path: Path, *, auto_init_db: bool = True) -> Settings:
    """Create isolated settings for application tests."""
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        auto_init_db=auto_init_db,
        store_name="Test Rotisería",
        bot_name="Test Bot",
        store_location="Córdoba",
        dashboard_session_secret="test-session-secret",
        dashboard_admin_email="staff@example.com",
        dashboard_admin_password=SecretStr("super-secret"),
        dashboard_admin_name="Staff User",
    )


def login_dashboard(client: TestClient, *, email: str = "staff@example.com", password: str = "super-secret"):
    """Sign in to the dashboard using the bootstrapped staff credentials."""
    return client.post(
        "/dashboard/login",
        data={"email": email, "password": password, "next": "/dashboard"},
        follow_redirects=True,
    )


def set_dashboard_session(client: TestClient, settings: Settings, session_data: dict[str, int]):
    """Write a valid signed dashboard session cookie into the test client."""
    signer = TimestampSigner(settings.dashboard_session_secret)
    payload = b64encode(json.dumps(session_data).encode("utf-8"))
    client.cookies.set("session", signer.sign(payload).decode("utf-8"))


async def add_second_store_membership(settings: Settings):
    """Create a second store and assign the bootstrap admin to it."""
    runtime = create_database_runtime(settings)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        staff_user = await repository.get_staff_user_by_email(settings.dashboard_admin_email or "")
        assert staff_user is not None
        second_store = await repository.create_store_profile(
            store_name="Sucursal Centro",
            bot_name="Ruperto Centro",
            store_description="Segundo local para pruebas.",
            assistant_personality="Steady and concise.",
        )
        await repository.ensure_staff_user(
            email=settings.dashboard_admin_email or "",
            full_name=settings.dashboard_admin_name,
            password=(settings.dashboard_admin_password or SecretStr("")).get_secret_value(),
            store_id=second_store.id,
            role=StaffRole.MANAGER,
        )
        await session.commit()
    await runtime.engine.dispose()


async def bootstrap_staff_tenant_fixture(settings: Settings):
    """Initialize the database and seed an additional store membership."""
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    await runtime.engine.dispose()
    await add_second_store_membership(settings)


async def remove_staff_memberships(settings: Settings):
    """Delete all memberships for the bootstrap staff user."""
    runtime = create_database_runtime(settings)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        staff_user = await repository.get_staff_user_by_email(settings.dashboard_admin_email or "")
        assert staff_user is not None
        memberships = (
            await session.scalars(select(StoreMembership).where(StoreMembership.staff_user_id == staff_user.id))
        ).all()
        for membership in memberships:
            await session.delete(membership)
        await session.commit()
    await runtime.engine.dispose()


async def add_staff_user_to_default_store(settings: Settings):
    """Create one extra staff membership in the default store for dashboard tests."""
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        await repository.ensure_staff_user(
            email="team@example.com",
            full_name="Equipo Local",
            password="team-secret",
            store_id=1,
            role=StaffRole.STAFF,
        )
        await session.commit()
    await runtime.engine.dispose()


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


def test_demo_chat_page_renders_the_browser_harness(tmp_path: Path):
    """The lightweight demo chat page is available from the main app."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/demo/chat")

    assert response.status_code == HTTP_OK
    assert "Demo online" in response.text
    assert "Clientes demo" in response.text
    assert "/api/dev/messages" in response.text
    assert "/api/dev/notifications" in response.text
    assert "Martín" in response.text
    assert "Crear cliente aleatorio" in response.text
    assert "marked.min.js" in response.text
    assert "purify.min.js" in response.text


def test_format_dashboard_datetime_formats_local_time():
    """Dashboard timestamps are shown in the configured local timezone."""
    value = datetime(2026, 4, 4, 15, 30, tzinfo=UTC)

    assert format_dashboard_datetime(value, "America/Argentina/Cordoba") == "2026-04-04 12:30"


def test_public_settings_marks_secret_configuration():
    """Secret-backed integrations are only exposed as configured flags."""
    settings = Settings(
        gemini_api_key=SecretStr("gemini-key"),
        kapso_api_key=SecretStr("kapso-key"),
        default_store_id=TENANT_STORE_ID,
        smtp_server="smtp.example.com",
        smtp_port=SMTP_PORT,
        smtp_user="mailer@example.com",
        smtp_password=SecretStr("smtp-secret"),
    )
    public = settings.public_settings()

    assert public["gemini_api_key_configured"] is True
    assert public["kapso_api_key_configured"] is True
    assert public["default_store_id"] == TENANT_STORE_ID
    assert public["smtp_server"] == "smtp.example.com"
    assert public["smtp_port"] == SMTP_PORT
    assert public["smtp_user"] == "mailer@example.com"
    assert public["smtp_password_configured"] is True
    assert public["smtp_configured"] is True


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


def extract_latest_user_text(messages: list[ModelMessage]) -> str:
    """Return the latest user prompt text from the deterministic model history."""
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                return content
    return ""


def dev_message_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Deterministic model used to exercise the development chat endpoint."""
    tool_returns = collect_tool_returns(messages)
    latest_user_text = extract_latest_user_text(messages).casefold().strip()
    if latest_user_text == "martina":
        if "update_customer_name" not in tool_returns:
            return ModelResponse(
                parts=[ToolCallPart("update_customer_name", {"name": "Martina"})],
                model_name="function:test-api-dev-message",
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "reply_text": "Hola Martina, decime qué te gustaría pedir.",
                        "next_step": "choose_items",
                        "handoff": False,
                    },
                )
            ],
            model_name="function:test-api-dev-message",
        )

    if "confirm" in latest_user_text:
        if "confirm_current_order" not in tool_returns:
            return ModelResponse(
                parts=[ToolCallPart("confirm_current_order", {})],
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

    sequence: list[tuple[str, dict[str, Any]]] = [
        ("add_item_to_current_order", {"sku": "hamburguesa-completa", "quantity": 1}),
        ("set_order_delivery_type", {"delivery_type": "pickup"}),
        ("set_order_payment_method", {"payment_method": "cash"}),
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
                    "reply_text": "Así queda el pedido. Si está bien, confirmámelo.",
                    "next_step": "confirm_order",
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
        confirm_response = client.post(
            "/api/dev/messages",
            json={"external_user_id": "cli-user", "message_text": "Confirmá el pedido"},
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
    assert second_chat_response.json()["current_order"] is None

    assert chat_response.status_code == HTTP_OK
    chat_payload = chat_response.json()
    assert chat_payload["customer"]["name"] == "Martina"
    assert chat_payload["reply"]["next_step"] == "choose_items"
    assert chat_payload["current_order"]["status"] == "draft"
    assert "bebida o un postre" in chat_payload["reply"]["reply_text"].lower()

    assert confirm_response.status_code == HTTP_OK
    assert confirm_response.json()["reply"]["next_step"] == "complete"
    assert confirm_response.json()["current_order"]["status"] == "confirmed"

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
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Hola, quiero pedir"})
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Martina"})
        order_response = client.post(
            "/api/dev/messages",
            json={"external_user_id": "cli-user", "message_text": "Quiero una hamburguesa"},
        )
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Confirmá el pedido"})
        order_id = order_response.json()["current_order"]["id"]
        status_response = client.patch(
            f"/api/orders/{order_id}/status",
            json={"status": OrderStatus.ALMOST_READY.value},
        )

    assert status_response.status_code == HTTP_OK
    assert status_response.json()["status"] == OrderStatus.ALMOST_READY.value


def test_dev_notifications_endpoint_returns_pending_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The browser harness can poll queued status notifications for one demo client."""
    monkeypatch.setattr("ruperto.assistant.build_google_model", lambda settings: FunctionModel(dev_message_model))
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Hola, quiero pedir"})
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Martina"})
        order_response = client.post(
            "/api/dev/messages",
            json={"external_user_id": "cli-user", "message_text": "Quiero una hamburguesa"},
        )
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Confirmá el pedido"})
        order_id = order_response.json()["current_order"]["id"]
        client.patch(f"/api/orders/{order_id}/status", json={"status": OrderStatus.ALMOST_READY.value})
        client.patch(f"/api/orders/{order_id}/status", json={"status": OrderStatus.READY_FOR_PICKUP.value})
        first_poll = client.get("/api/dev/notifications", params={"external_user_id": "cli-user"})
        second_poll = client.get("/api/dev/notifications", params={"external_user_id": "cli-user"})

    assert first_poll.status_code == HTTP_OK
    assert [notification["event_type"] for notification in first_poll.json()] == ["order_almost_ready", "order_ready"]
    assert second_poll.status_code == HTTP_OK
    assert second_poll.json() == []


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
                    {"weekday": 0, "slot_index": 0, "opens_at": None, "closes_at": None, "closed": True},
                    {"weekday": 1, "slot_index": 0, "opens_at": "11:30", "closes_at": "15:00", "closed": False},
                    {"weekday": 1, "slot_index": 1, "opens_at": "19:00", "closes_at": "23:30", "closed": False},
                ]
            },
        )

    assert response.status_code == HTTP_OK
    assert len(response.json()) == DEFAULT_WEEKLY_HOURS + 1
    assert response.json()[0]["closed"] is True
    assert response.json()[1]["opens_at"] == "11:30"
    assert response.json()[2]["opens_at"] == "19:00"


def test_staff_replace_store_hours_rejects_invalid_slots(tmp_path: Path):
    """The staff API rejects invalid daily slot combinations."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.put(
            "/api/store-hours",
            json={
                "hours": [
                    {"weekday": 0, "slot_index": 0, "opens_at": "11:00", "closes_at": "15:00", "closed": False},
                    {"weekday": 0, "slot_index": 1, "opens_at": "14:00", "closes_at": "18:00", "closed": False},
                ]
            },
        )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert response.json()["detail"] == "Business-hours slots cannot overlap on the same day."


def test_staff_can_update_store_profile_via_api(tmp_path: Path):
    """The store profile API persists bot customization fields."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.put(
            "/api/store-profile",
            json={
                "store_name": "Nueva Rotisería",
                "bot_name": "Ruperto Plus",
                "store_location": "Alta Gracia",
                "store_description": "Pedidos con atención propia.",
                "assistant_personality": "Calm, warm, and concise.",
                "transfer_alias": "nueva.rotiseria",
            },
        )
        refreshed = client.get("/api/store-profile")

    assert response.status_code == HTTP_OK
    assert response.json()["store_name"] == "Nueva Rotisería"
    assert response.json()["bot_name"] == "Ruperto Plus"
    assert refreshed.json()["transfer_alias"] == "nueva.rotiseria"


def test_dashboard_renders_sections_with_tailwind(tmp_path: Path):
    """The dashboard home and navigation render the new split IA/staff layout."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        redirect_response = client.get("/dashboard", follow_redirects=False)
        login_response = login_dashboard(client)
        response = client.get("/dashboard")
        customers_response = client.get("/dashboard/customers")
        menu_response = client.get("/dashboard/settings/menu")

    assert redirect_response.status_code == HTTP_FOUND
    assert redirect_response.headers["location"].startswith("/dashboard/login")
    assert login_response.status_code == HTTP_OK
    assert "Cerrar sesión" in login_response.text
    assert response.status_code == HTTP_OK
    assert "Ruperto dashboard" in response.text
    assert "tailwindcss.com" in response.text
    assert "Inicio" in response.text
    assert "Clientes" in response.text
    assert "Carta de productos" in response.text
    assert "Pedidos recientes" in response.text
    assert customers_response.status_code == HTTP_OK
    assert "Buscar cliente" in customers_response.text
    assert menu_response.status_code == HTTP_OK
    assert "Esta primera versión deja la carta en modo consulta." in menu_response.text


def test_parse_store_hours_form_supports_multiple_slots_and_ignores_unrelated_keys():
    """The dashboard hours parser keeps zero-to-many slots per day."""
    parsed = parse_store_hours_form(
        {
            "csrf_token": "ignored",
            "opens_at_1_0": "11:30",
            "closes_at_1_0": "15:00",
            "opens_at_1_2": "19:00",
            "closes_at_1_2": "23:00",
            "store_name": "ignored too",
        }
    )

    monday_slots = [row for row in parsed if row.weekday == 1]
    sunday_row = next(row for row in parsed if row.weekday == 0)

    assert len(parsed) == DEFAULT_WEEKLY_HOURS + 1
    assert [row.slot_index for row in monday_slots] == [0, 2]
    assert monday_slots[0].opens_at == "11:30"
    assert monday_slots[1].closes_at == "23:00"
    assert sunday_row.closed is True


def test_dashboard_forms_can_update_profile_agent_and_hours(tmp_path: Path):
    """The split dashboard forms persist profile, agent, and schedule settings."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        profile_response = client.post(
            "/dashboard/settings/profile",
            data={
                "store_name": "Panel Rotisería",
                "store_location": "Anisacate",
                "store_description": "Simple dashboard edits.",
                "transfer_alias": "panel.rotiseria",
            },
            follow_redirects=True,
        )
        agent_response = client.post(
            "/dashboard/settings/agent",
            data={
                "bot_name": "Panel Bot",
                "assistant_personality": "Helpful and steady.",
            },
            follow_redirects=True,
        )
        hours_response = client.post(
            "/dashboard/settings/hours",
            data={
                "opens_at_1_0": "11:30",
                "closes_at_1_0": "15:00",
                "opens_at_1_1": "19:00",
                "closes_at_1_1": "23:00",
                "opens_at_2_0": "19:00",
                "closes_at_2_0": "23:00",
            },
            follow_redirects=True,
        )
        store_response = client.get("/api/store-profile")
        store_hours_response = client.get("/api/store-hours")

    assert profile_response.status_code == HTTP_OK
    assert "Perfil del local actualizado." in profile_response.text
    assert agent_response.status_code == HTTP_OK
    assert "Configuración del agente actualizada." in agent_response.text
    assert hours_response.status_code == HTTP_OK
    assert "Horarios actualizados." in hours_response.text
    assert store_response.json()["store_name"] == "Panel Rotisería"
    assert store_response.json()["bot_name"] == "Panel Bot"
    assert store_response.json()["transfer_alias"] == "panel.rotiseria"
    assert store_hours_response.json()[0]["closed"] is True
    assert store_hours_response.json()[1]["opens_at"] == "11:30"
    assert store_hours_response.json()[2]["opens_at"] == "19:00"


def test_dashboard_hours_form_rejects_invalid_slots(tmp_path: Path):
    """The dashboard schedule form surfaces invalid slot errors."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        response = client.post(
            "/dashboard/settings/hours",
            data={
                "opens_at_1_0": "11:00",
                "closes_at_1_0": "15:00",
                "opens_at_1_1": "14:00",
                "closes_at_1_1": "18:00",
            },
            follow_redirects=False,
        )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert response.json()["detail"] == "Business-hours slots cannot overlap on the same day."


def test_dashboard_rejects_invalid_form_payloads(tmp_path: Path):
    """The dashboard returns validation errors for malformed form submissions."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        profile_response = client.post(
            "/dashboard/settings/profile",
            data={
                "store_name": "",
                "store_description": "",
            },
        )
        agent_response = client.post(
            "/dashboard/settings/agent",
            data={"bot_name": "", "assistant_personality": ""},
        )
        order_response = client.post("/dashboard/orders/999/status", data={"status": "not-a-real-status"})

    assert profile_response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert agent_response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert order_response.status_code == HTTP_UNPROCESSABLE_ENTITY


def test_dashboard_can_update_order_status_and_handles_missing_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The dashboard can move orders and still reports missing records explicitly."""
    monkeypatch.setattr("ruperto.assistant.build_google_model", lambda settings: FunctionModel(dev_message_model))
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Hola, quiero pedir"})
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Martina"})
        order_response = client.post(
            "/api/dev/messages",
            json={"external_user_id": "cli-user", "message_text": "Quiero una hamburguesa"},
        )
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Confirmá el pedido"})
        order_id = order_response.json()["current_order"]["id"]
        dashboard_response = client.post(
            f"/dashboard/orders/{order_id}/status",
            data={"status": OrderStatus.ALMOST_READY.value},
            follow_redirects=True,
        )
        refreshed = client.get("/api/orders", params={"limit": 10})
        missing_response = client.post(
            "/dashboard/orders/999/status",
            data={"status": OrderStatus.ALMOST_READY.value},
        )

    assert dashboard_response.status_code == HTTP_OK
    assert "Estado del pedido actualizado." in dashboard_response.text
    assert refreshed.json()[0]["status"] == OrderStatus.ALMOST_READY.value
    assert missing_response.status_code == HTTP_NOT_FOUND
    assert missing_response.json()["detail"] == "Order not found."


def test_dashboard_login_rejects_invalid_credentials(tmp_path: Path):
    """The login screen returns a friendly error when the password is wrong."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/dashboard/login",
            data={"email": "staff@example.com", "password": "wrong", "next": "/dashboard"},
        )

    assert response.status_code == HTTP_UNAUTHORIZED
    assert "Credenciales inválidas." in response.text


def test_dashboard_login_page_redirects_when_already_authenticated(tmp_path: Path):
    """The login page redirects authenticated staff back to the dashboard."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        login_page = client.get("/dashboard/login")
        login_dashboard(client)
        redirect_response = client.get("/dashboard/login", follow_redirects=False)

    assert login_page.status_code == HTTP_OK
    assert "Ingresá al panel" in login_page.text
    assert redirect_response.status_code == HTTP_FOUND
    assert redirect_response.headers["location"] == "/dashboard"


def test_dashboard_shell_links_to_demo_chat(tmp_path: Path):
    """The staff dashboard exposes a direct link to the browser demo chat."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        response = client.get("/dashboard")

    assert response.status_code == HTTP_OK
    assert "Abrir demo del chat" in response.text
    assert 'href="/demo/chat"' in response.text


def test_dashboard_logout_clears_the_session(tmp_path: Path):
    """Signing out redirects back to the login page and removes dashboard access."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        logout_response = client.post("/dashboard/logout", follow_redirects=False)
        dashboard_response = client.get("/dashboard", follow_redirects=False)

    assert logout_response.status_code == HTTP_FOUND
    assert logout_response.headers["location"].startswith("/dashboard/login")
    assert dashboard_response.status_code == HTTP_FOUND
    assert dashboard_response.headers["location"].startswith("/dashboard/login")


def test_dashboard_protected_posts_require_login(tmp_path: Path):
    """Protected dashboard forms redirect anonymous requests to the login screen."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        profile_response = client.post("/dashboard/settings/profile", data={}, follow_redirects=False)
        agent_response = client.post("/dashboard/settings/agent", data={}, follow_redirects=False)
        hours_response = client.post("/dashboard/settings/hours", data={}, follow_redirects=False)
        users_response = client.post("/dashboard/settings/users/1/role", data={}, follow_redirects=False)
        order_response = client.post("/dashboard/orders/1/status", data={}, follow_redirects=False)
        switch_response = client.post("/dashboard/active-store", data={}, follow_redirects=False)

    assert profile_response.status_code == HTTP_FOUND
    assert profile_response.headers["location"].startswith("/dashboard/login")
    assert agent_response.status_code == HTTP_FOUND
    assert agent_response.headers["location"].startswith("/dashboard/login")
    assert hours_response.status_code == HTTP_FOUND
    assert hours_response.headers["location"].startswith("/dashboard/login")
    assert users_response.status_code == HTTP_FOUND
    assert users_response.headers["location"].startswith("/dashboard/login")
    assert order_response.status_code == HTTP_FOUND
    assert order_response.headers["location"].startswith("/dashboard/login")
    assert switch_response.status_code == HTTP_FOUND
    assert switch_response.headers["location"].startswith("/dashboard/login")


def test_dashboard_store_switch_rejects_invalid_requests(tmp_path: Path):
    """Store switching validates both the input and the accessible memberships."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        invalid_id_response = client.post("/dashboard/active-store", data={"store_id": "oops"})
        forbidden_response = client.post("/dashboard/active-store", data={"store_id": "999"})

    assert invalid_id_response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert invalid_id_response.json()["detail"] == "Invalid store id."
    assert forbidden_response.status_code == HTTP_FORBIDDEN
    assert forbidden_response.json()["detail"] == "Store not accessible."


def test_dashboard_split_pages_require_login(tmp_path: Path):
    """Anonymous requests to split dashboard pages redirect to the login screen."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        customers = client.get("/dashboard/customers", follow_redirects=False)
        menu = client.get("/dashboard/settings/menu", follow_redirects=False)
        profile = client.get("/dashboard/settings/profile", follow_redirects=False)
        agent = client.get("/dashboard/settings/agent", follow_redirects=False)
        hours = client.get("/dashboard/settings/hours", follow_redirects=False)
        users = client.get("/dashboard/settings/users", follow_redirects=False)

    for response in (customers, menu, profile, agent, hours, users):
        assert response.status_code == HTTP_FOUND
        assert response.headers["location"].startswith("/dashboard/login")


def test_dashboard_can_switch_the_active_store(tmp_path: Path):
    """A signed-in user can switch between the stores they belong to."""
    settings = build_settings(tmp_path)
    anyio.run(bootstrap_staff_tenant_fixture, settings)
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        before = client.get("/dashboard")
        switch_response = client.post(
            "/dashboard/active-store",
            data={"store_id": "2"},
            follow_redirects=True,
        )

    assert "Test Rotisería" in before.text
    assert switch_response.status_code == HTTP_OK
    assert "Sucursal Centro" in switch_response.text
    assert "Local activo actualizado." in switch_response.text


def test_dashboard_customers_search_filters_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The customers page filters the list with the search box."""
    monkeypatch.setattr("ruperto.assistant.build_google_model", lambda settings: FunctionModel(dev_message_model))
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Hola, quiero pedir"})
        client.post("/api/dev/messages", json={"external_user_id": "cli-user", "message_text": "Martina"})
        response = client.get("/dashboard/customers", params={"q": "martina"})
        empty_response = client.get("/dashboard/customers", params={"q": "nadie"})

    assert response.status_code == HTTP_OK
    assert "Martina" in response.text
    assert "1 clientes encontrados" in response.text
    assert empty_response.status_code == HTTP_OK
    assert "No hay clientes que coincidan con esa búsqueda." in empty_response.text


def test_dashboard_settings_pages_render_menu_filters_and_profile_data(tmp_path: Path):
    """Settings pages render their dedicated content and menu filters work."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        menu_response = client.get("/dashboard/settings/menu", params={"q": "doble", "category": "Comidas"})
        profile_response = client.get("/dashboard/settings/profile")
        agent_response = client.get("/dashboard/settings/agent")
        hours_response = client.get("/dashboard/settings/hours")
        users_response = client.get("/dashboard/settings/users")

    assert menu_response.status_code == HTTP_OK
    assert "Hamburguesa doble cheddar" in menu_response.text
    assert "Pizza muzzarella" not in menu_response.text
    assert profile_response.status_code == HTTP_OK
    assert "Alias de transferencia" in profile_response.text
    assert agent_response.status_code == HTTP_OK
    assert "Modelo" in agent_response.text
    assert hours_response.status_code == HTTP_OK
    assert "Guardar agenda semanal" in hours_response.text
    assert users_response.status_code == HTTP_OK
    assert "Usuarios del local" in users_response.text


def test_dashboard_can_update_store_membership_role(tmp_path: Path):
    """The users page lets staff update the role for one membership in the active store."""
    settings = build_settings(tmp_path)
    anyio.run(add_staff_user_to_default_store, settings)
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        users_page = client.get("/dashboard/settings/users")
        runtime = app.state.runtime

        async def load_team_membership_id():
            async with runtime.database.session_factory() as session:
                repository = BusinessRepository(session)
                memberships = await repository.list_staff_memberships_for_store(store_id=1)
                for membership in memberships:
                    if membership.email == "team@example.com":
                        return membership.membership_id
                raise AssertionError

        membership_id = anyio.run(load_team_membership_id)
        update_response = client.post(
            f"/dashboard/settings/users/{membership_id}/role",
            data={"role": StaffRole.MANAGER.value},
            follow_redirects=True,
        )

        async def load_role():
            async with runtime.database.session_factory() as session:
                repository = BusinessRepository(session)
                memberships = await repository.list_staff_memberships_for_store(store_id=1)
                for membership in memberships:
                    if membership.email == "team@example.com":
                        return membership.role
                raise AssertionError

        updated_role = anyio.run(load_role)

    assert users_page.status_code == HTTP_OK
    assert "Usuarios del local" in users_page.text
    assert "Equipo Local" in users_page.text
    assert update_response.status_code == HTTP_OK
    assert "Rol actualizado." in update_response.text
    assert updated_role == StaffRole.MANAGER


def test_dashboard_user_role_endpoint_validates_input_and_missing_memberships(tmp_path: Path):
    """Role updates fail cleanly for invalid payloads or unknown memberships."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        login_dashboard(client)
        invalid_role = client.post("/dashboard/settings/users/1/role", data={"role": "boss"})
        missing_membership = client.post(
            "/dashboard/settings/users/999/role",
            data={"role": StaffRole.MANAGER.value},
        )

    assert invalid_role.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert invalid_role.json()["detail"] == "Invalid role."
    assert missing_membership.status_code == HTTP_NOT_FOUND
    assert missing_membership.json()["detail"] == "Store membership not found."


def test_dashboard_redirects_stale_or_invalid_sessions_to_login(tmp_path: Path):
    """Stale session state fails closed and can recover the default store scope."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        set_dashboard_session(
            client,
            settings,
            {"dashboard_staff_user_id": 9999, "dashboard_store_id": 9999},
        )
        stale_user_response = client.get("/dashboard", follow_redirects=False)

    assert stale_user_response.status_code == HTTP_FOUND
    assert stale_user_response.headers["location"].startswith("/dashboard/login")


def test_dashboard_falls_back_to_the_default_store_when_session_store_is_invalid(tmp_path: Path):
    """The dashboard resets the active store when the session points to an unknown store."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        runtime = app.state.runtime

        async def load_staff_id():
            async with runtime.database.session_factory() as session:
                repository = BusinessRepository(session)
                staff_user = await repository.get_staff_user_by_email("staff@example.com")
                assert staff_user is not None
                return staff_user.id

        staff_user_id = anyio.run(load_staff_id)
        set_dashboard_session(
            client,
            settings,
            {"dashboard_staff_user_id": staff_user_id, "dashboard_store_id": 9999},
        )
        response = client.get("/dashboard")

    assert response.status_code == HTTP_OK
    assert "Test Rotisería" in response.text


def test_dashboard_rejects_sessions_without_memberships(tmp_path: Path):
    """Signed sessions without store memberships are cleared and redirected."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        runtime = app.state.runtime

        async def load_staff_id():
            async with runtime.database.session_factory() as session:
                repository = BusinessRepository(session)
                staff_user = await repository.get_staff_user_by_email("staff@example.com")
                assert staff_user is not None
                return staff_user.id

        staff_user_id = anyio.run(load_staff_id)
        anyio.run(remove_staff_memberships, settings)
        set_dashboard_session(
            client,
            settings,
            {"dashboard_staff_user_id": staff_user_id, "dashboard_store_id": 1},
        )
        response = client.get("/dashboard", follow_redirects=False)

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"].startswith("/dashboard/login")
