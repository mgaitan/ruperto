"""Tests for the FastAPI application bootstrap."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from base64 import b64encode
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from starlette.datastructures import FormData
from starlette.requests import Request as StarletteRequest

from ruperto.app import (
    app as package_app,
)
from ruperto.app import (
    build_scoped_dev_external_user_id,
    create_app,
    enrich_kapso_payload_from_headers,
    extract_kapso_phone_number_id,
    format_dashboard_datetime,
    load_dashboard_identity,
    parse_store_hours_form,
    resolve_dev_demo_identity,
    serialize_store_hours_for_dashboard,
)
from ruperto.channels.base import ChannelDeliveryError
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.mail import SignupEmailDeliveryError
from ruperto.models import (
    Channel,
    ChannelProvider,
    DeliveryType,
    MunicipalCaseStatus,
    MunicipalRequestKind,
    OrderStatus,
    PaymentMethod,
    StaffRole,
    StaffUser,
    StoreMembership,
    StoreProfile,
    StoreVertical,
)
from ruperto.repository import BusinessRepository
from ruperto.schemas import (
    AssistantTurnResult,
    MunicipalCaseCreateRequest,
    MunicipalCaseStatusUpdateRequest,
    OrderStatusUpdateRequest,
    StoreBusinessHoursSnapshot,
    StoreBusinessHoursUpdateEntry,
    StoreBusinessHoursUpdateRequest,
    StoreChannelConnectionUpdateRequest,
    StoreProfileUpdateRequest,
)

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_FOUND = 303
HTTP_FORBIDDEN = 403
HTTP_UNAUTHORIZED = 401
HTTP_UNPROCESSABLE_ENTITY = 422
HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409
HTTP_SERVICE_UNAVAILABLE = 503
MIN_MENU_ITEMS = 35
DEFAULT_WEEKLY_HOURS = 7
TENANT_STORE_ID = 7
SMTP_PORT = 587
HANDOFF_AUTO_REPLY_ERROR = "The bot should not send an automatic reply during human handoff."


def build_settings(
    tmp_path: Path,
    *,
    auto_init_db: bool = True,
    store_vertical: StoreVertical = StoreVertical.ORDERING,
    store_slug: str = "test-rotiseria",
) -> Settings:
    """Create isolated settings for application tests."""
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        auto_init_db=auto_init_db,
        store_name="Test Rotisería",
        bot_name="Test Bot",
        store_location="Córdoba",
        store_vertical=store_vertical,
        store_slug=store_slug,
        dashboard_session_secret="test-session-secret",
        dashboard_admin_email="staff@example.com",
        dashboard_admin_password=SecretStr("super-secret"),
        dashboard_admin_name="Staff User",
        kapso_api_key=None,
        kapso_phone_number_id=None,
        kapso_webhook_secret=None,
    )


def build_kapso_signature(*, payload: bytes, secret: str) -> str:
    """Build the Kapso HMAC signature for one raw payload."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


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


def build_request(
    app,
    *,
    path: str,
    method: str = "GET",
    session: dict[str, int] | None = None,
    headers: dict[str, str] | None = None,
) -> StarletteRequest:
    """Create a bare Starlette request with session data for direct endpoint calls."""
    normalized_headers = {
        "host": "testserver",
        **(headers or {}),
    }
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [(key.lower().encode("utf-8"), value.encode("utf-8")) for key, value in normalized_headers.items()],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
        "session": session or {},
    }
    return StarletteRequest(scope)


def get_route_endpoint(app, path: str, method: str):
    """Return the route endpoint registered for one path and method."""
    for route in app.router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    msg = f"Could not find route {method} {path!r}"
    raise AssertionError(msg)


async def form_data(values: dict[str, str]) -> FormData:
    """Return a form payload compatible with direct request overrides."""
    return FormData(values)


def with_request_form(request: StarletteRequest, values: dict[str, str]) -> StarletteRequest:
    """Override one request form reader in a type-checker-friendly way."""

    async def _form() -> FormData:
        return await form_data(values)

    cast(Any, request).form = _form
    return request


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


async def create_whatsapp_confirmed_order(settings: Settings) -> int:
    """Create one confirmed WhatsApp order ready for status transitions."""
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            phone_number="+5493513308454",
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(
            customer_id=customer.id,
            conversation_id=conversation.id,
            sku="hamburguesa-doble",
            quantity=1,
        )
        await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
        confirmed_order = await repository.set_order_payment_method(customer.id, conversation.id, PaymentMethod.CASH)
        confirmed_order = await repository.confirm_current_order(customer.id, conversation.id)
        await session.commit()
    await runtime.engine.dispose()
    return confirmed_order.id


async def create_whatsapp_municipal_case(settings: Settings) -> int:
    """Create one municipal WhatsApp case ready for status transitions."""
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile()
        customer = await repository.get_or_create_customer(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            phone_number="+5493513308454",
            store_id=store.id,
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            customer_id=customer.id,
            store_id=store.id,
        )
        water_area = next(
            area
            for area in await repository.list_municipal_areas(store_id=store.id)
            if area.name == "Solicitud de agua"
        )
        water_category = next(
            category
            for category in await repository.list_municipal_categories(store_id=store.id, area_id=water_area.id)
            if category.name == "Falta de agua"
        )
        created_case = await repository.create_municipal_case(
            store_id=store.id,
            payload=MunicipalCaseCreateRequest(
                area_id=water_area.id,
                category_id=water_category.id,
                customer_id=customer.id,
                conversation_id=conversation.id,
                title="Sin agua en barrio centro",
                description="Hace un día que no hay agua.",
            ),
        )
        await session.commit()
    await runtime.engine.dispose()
    return created_case.id


async def create_dev_municipal_case(settings: Settings, *, external_user_id: str) -> int:
    """Create one municipal dev-chat case ready for kanban notifications."""
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile()
        scoped_external_user_id = build_scoped_dev_external_user_id(
            store_slug=store.slug,
            external_user_id=external_user_id,
        )
        customer = await repository.get_or_create_customer(
            channel=Channel.DEV,
            external_id=scoped_external_user_id,
            store_id=store.id,
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id=scoped_external_user_id,
            customer_id=customer.id,
            store_id=store.id,
        )
        lighting_area = next(
            area
            for area in await repository.list_municipal_areas(store_id=store.id)
            if area.name == "Alumbrado público"
        )
        lighting_category = next(
            category
            for category in await repository.list_municipal_categories(store_id=store.id, area_id=lighting_area.id)
            if category.name == "Lámpara apagada"
        )
        created_case = await repository.create_municipal_case(
            store_id=store.id,
            payload=MunicipalCaseCreateRequest(
                area_id=lighting_area.id,
                category_id=lighting_category.id,
                customer_id=customer.id,
                conversation_id=conversation.id,
                title="Farola sin luz",
                description="La farola no prende desde anoche.",
            ),
        )
        await session.commit()
    await runtime.engine.dispose()
    return created_case.id


async def create_whatsapp_handoff_conversation(settings: Settings) -> int:
    """Create one WhatsApp conversation already waiting for a human reply."""
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            phone_number="+5493513308454",
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            customer_id=customer.id,
        )
        await repository.update_store_channel_connection(
            store_id=settings.default_store_id,
            channel=Channel.WHATSAPP,
            payload=StoreChannelConnectionUpdateRequest(
                phone_number_id="597907523413541",
                api_key="kapso-key",
                webhook_secret="kapso-secret",
                is_active=True,
            ),
        )
        await repository.activate_conversation_handoff(
            conversation_id=conversation.id,
            reason="Te paso con una persona del equipo.",
            latest_customer_message="Necesito hablar con alguien.",
        )
        await session.commit()
    await runtime.engine.dispose()
    return conversation.id


def test_root_endpoint(tmp_path: Path):
    """The root endpoint exposes basic service metadata."""
    app = create_app(build_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == HTTP_OK
    assert response.json()["store_name"] == "Test Rotisería"
    assert response.json()["bot_name"] == "Test Bot"
    assert response.json()["store_locale"] == "es-AR"


def test_root_asgi_entrypoint_exposes_the_main_app():
    """The deployment shim exposes the same FastAPI app from the repository root."""
    asgi_path = Path(__file__).resolve().parents[1] / "asgi.py"
    spec = importlib.util.spec_from_file_location("asgi", asgi_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.app is package_app


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


def test_root_endpoint_renders_homepage_for_browser(tmp_path: Path):
    """Browser requests to the root path render the public landing page."""
    app = create_app(build_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/", headers={"accept": "text/html"})

    assert response.status_code == HTTP_OK
    assert "Crear cuenta" in response.text
    assert 'value="ordering"' in response.text
    assert 'value="municipal"' in response.text
    assert "Katupyry" in response.text
    assert "Inteligencia Artificial para la vida Real" in response.text
    assert "Crear cuenta y entrar al panel" in response.text


def test_demo_chat_page_renders_the_browser_harness(tmp_path: Path):
    """The lightweight demo chat page is available from the main app."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        redirect_response = client.get("/demo/chat", follow_redirects=False)
        response = client.get(f"/demo/chat/{settings.store_slug}")

    assert redirect_response.status_code == HTTP_FOUND
    assert redirect_response.headers["location"] == f"/demo/chat/{settings.store_slug}"
    assert response.status_code == HTTP_OK
    assert "Demo online" in response.text
    assert "Clientes demo" in response.text
    assert f"/api/dev/messages/{settings.store_slug}" in response.text
    assert f"/api/dev/notifications/{settings.store_slug}" in response.text
    assert "Martín" in response.text
    assert "Crear cliente aleatorio" in response.text
    assert "Simular identidad por teléfono" in response.text


def test_home_signup_creates_ordering_tenant_and_logs_into_dashboard(tmp_path: Path):
    """The public signup form can create an ordering tenant and start a session."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/signup",
            data={
                "store_name": "Rotisería Centro",
                "full_name": "Dueña Centro",
                "email": "owner-centro@example.com",
                "password": "super-secret-123",
                "vertical": StoreVertical.ORDERING.value,
            },
            follow_redirects=False,
        )
        dashboard_response = client.get("/dashboard")

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"] == "/dashboard"
    assert dashboard_response.status_code == HTTP_OK
    assert "Rotisería Centro" in dashboard_response.text

    async def assert_signup_result():
        runtime = create_database_runtime(settings)
        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile_by_slug("rotiseria-centro")
            staff_user = await repository.get_staff_user_by_email("owner-centro@example.com")
            assert store is not None
            assert store.vertical == StoreVertical.ORDERING
            assert staff_user is not None
            memberships = await repository.list_store_memberships_for_staff_user(staff_user.id)
            hours = await repository.list_store_business_hours(store_id=store.id)
            assert any(membership.store_id == store.id for membership in memberships)
            assert len(hours) == DEFAULT_WEEKLY_HOURS
        await runtime.engine.dispose()

    anyio.run(assert_signup_result)


def test_home_signup_creates_municipal_tenant_with_seeded_catalog(tmp_path: Path):
    """The public signup form seeds the municipal vertical and logs the owner in."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/signup",
            data={
                "store_name": "Municipalidad Demo",
                "full_name": "Ana Vecina",
                "email": "ana@example.com",
                "password": "super-secret-123",
                "vertical": StoreVertical.MUNICIPAL.value,
            },
            follow_redirects=False,
        )
        dashboard_response = client.get("/dashboard")

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"] == "/dashboard"
    assert dashboard_response.status_code == HTTP_OK
    assert "Municipio" in dashboard_response.text
    assert "Personas" in dashboard_response.text

    async def assert_signup_result():
        runtime = create_database_runtime(settings)
        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile_by_slug("municipalidad-demo")
            assert store is not None
            assert store.vertical == StoreVertical.MUNICIPAL
            areas = await repository.list_municipal_areas(store_id=store.id)
            hours = await repository.list_store_business_hours(store_id=store.id)
            assert len(areas) > 0
            assert len(hours) == DEFAULT_WEEKLY_HOURS
        await runtime.engine.dispose()

    anyio.run(assert_signup_result)


def test_home_signup_rejects_existing_email(tmp_path: Path):
    """The public signup form surfaces duplicate owner emails without overwriting users."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/signup",
            data={
                "store_name": "Otro Tenant",
                "full_name": "Staff Duplicado",
                "email": "staff@example.com",
                "password": "super-secret-123",
                "vertical": StoreVertical.ORDERING.value,
            },
            headers={"accept": "text/html"},
        )

    assert response.status_code == HTTP_CONFLICT
    assert "Ya existe una cuenta con ese correo." in response.text


def test_home_signup_sends_welcome_email_when_smtp_is_configured(tmp_path: Path):
    """Configured SMTP sends a welcome email after signup succeeds."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "smtp_server": "smtp.example.com",
            "smtp_port": SMTP_PORT,
            "smtp_user": "mailer@example.com",
            "smtp_password": SecretStr("smtp-secret"),
        }
    )
    app = create_app(settings)

    with patch("ruperto.app.send_signup_welcome_email") as send_signup_email, TestClient(app) as client:
        response = client.post(
            "/signup",
            data={
                "store_name": "Rotiseria Norte",
                "full_name": "Nora Owner",
                "email": "nora@example.com",
                "password": "super-secret-123",
                "vertical": StoreVertical.ORDERING.value,
            },
            follow_redirects=False,
        )

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"] == "/dashboard"
    send_signup_email.assert_called_once()
    call = send_signup_email.call_args.kwargs
    assert call["smtp_server"] == "smtp.example.com"
    assert call["smtp_port"] == SMTP_PORT
    assert call["smtp_user"] == "mailer@example.com"
    assert call["recipient_email"] == "nora@example.com"
    assert call["store_name"] == "Rotiseria Norte"
    assert call["vertical"] == StoreVertical.ORDERING


def test_home_signup_direct_route_call_covers_smtp_success_branch(tmp_path: Path):
    """Direct signup handler calls cover the SMTP success branch under Python 3.13 coverage."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "smtp_server": "smtp.example.com",
            "smtp_port": SMTP_PORT,
            "smtp_user": "mailer@example.com",
            "smtp_password": SecretStr("smtp-secret"),
        }
    )
    app = create_app(settings)

    async def run_signup():
        request = with_request_form(
            build_request(app, path="/signup", method="POST"),
            {
                "store_name": "Rotiseria Centro Directa",
                "full_name": "Nora Directa",
                "email": "nora-directa@example.com",
                "password": "super-secret-123",
                "vertical": StoreVertical.ORDERING.value,
            },
        )
        return await get_route_endpoint(app, "/signup", "POST")(request=request)

    with patch("ruperto.app.send_signup_welcome_email") as send_signup_email, TestClient(app):
        response = anyio.run(run_signup)

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"] == "/dashboard"
    send_signup_email.assert_called_once()


def test_home_signup_reports_smtp_delivery_errors_and_rolls_back(tmp_path: Path):
    """Configured SMTP failures abort the signup instead of hiding the error."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "smtp_server": "smtp.example.com",
            "smtp_port": SMTP_PORT,
            "smtp_user": "mailer@example.com",
            "smtp_password": SecretStr("smtp-secret"),
        }
    )
    app = create_app(settings)

    with (
        patch(
            "ruperto.app.send_signup_welcome_email",
            side_effect=SignupEmailDeliveryError(),
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            "/signup",
            data={
                "store_name": "Tenant Fallido",
                "full_name": "Falla Mail",
                "email": "falla@example.com",
                "password": "super-secret-123",
                "vertical": StoreVertical.ORDERING.value,
            },
            headers={"accept": "text/html"},
        )

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert "No pudimos enviar el correo de bienvenida." in response.text

    async def assert_signup_rolled_back():
        runtime = create_database_runtime(settings)
        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)
            assert await repository.get_staff_user_by_email("falla@example.com") is None
            assert await repository.get_store_profile_by_slug("tenant-fallido") is None
        await runtime.engine.dispose()

    anyio.run(assert_signup_rolled_back)


def test_home_signup_returns_html_validation_errors_for_incomplete_form(tmp_path: Path):
    """Browser signups keep the landing page and validation hint on bad input."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/signup",
            data={"store_name": "", "full_name": "", "email": "", "password": "", "vertical": "ordering"},
            headers={"accept": "text/html"},
        )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert "Completá nombre, responsable, correo, contraseña y el tipo de organización." in response.text


def test_home_signup_returns_json_validation_errors_for_incomplete_form(tmp_path: Path):
    """API signups return structured validation errors when fields are missing."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/signup",
            data={"store_name": "", "full_name": "", "email": "", "password": "", "vertical": "ordering"},
        )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert response.json()["detail"]


def test_home_signup_reports_smtp_delivery_errors_as_json(tmp_path: Path):
    """API signups surface SMTP delivery failures as JSON errors too."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "smtp_server": "smtp.example.com",
            "smtp_port": SMTP_PORT,
            "smtp_user": "mailer@example.com",
            "smtp_password": SecretStr("smtp-secret"),
        }
    )
    app = create_app(settings)

    with (
        patch(
            "ruperto.app.send_signup_welcome_email",
            side_effect=SignupEmailDeliveryError(),
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            "/signup",
            data={
                "store_name": "Tenant JSON Fallido",
                "full_name": "Falla JSON",
                "email": "falla-json@example.com",
                "password": "super-secret-123",
                "vertical": StoreVertical.ORDERING.value,
            },
            headers={"accept": "application/json"},
        )

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert response.json() == {"detail": "Could not deliver signup email."}


def test_home_signup_direct_route_raises_http_exception_for_json_email_failures(tmp_path: Path):
    """The signup endpoint raises the JSON HTTP exception branch outside HTML form flows."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "smtp_server": "smtp.example.com",
            "smtp_port": SMTP_PORT,
            "smtp_user": "mailer@example.com",
            "smtp_password": SecretStr("smtp-secret"),
        }
    )
    app = create_app(settings)

    async def run_signup():
        request = with_request_form(
            build_request(app, path="/signup", method="POST", headers={"accept": "application/json"}),
            {
                "store_name": "Tenant JSON Directo",
                "full_name": "Falla Directa",
                "email": "falla-directa@example.com",
                "password": "super-secret-123",
                "vertical": StoreVertical.ORDERING.value,
            },
        )
        return await get_route_endpoint(app, "/signup", "POST")(request=request)

    with (
        patch("ruperto.app.send_signup_welcome_email", side_effect=SignupEmailDeliveryError()),
        TestClient(app),
        pytest.raises(HTTPException) as error,
    ):
        anyio.run(run_signup)

    assert error.value.status_code == HTTP_SERVICE_UNAVAILABLE
    assert error.value.detail == "Could not deliver signup email."


def test_demo_chat_page_renders_municipal_prompts_per_slug(tmp_path: Path):
    """Municipal demo pages expose complaint-oriented quick prompts."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/demo/chat/mi-muni")

    assert response.status_code == HTTP_OK
    assert "Hola, quiero hacer un reclamo" in response.text
    assert "/api/dev/messages/mi-muni" in response.text
    assert "Personas demo" in response.text
    assert "Agregar persona demo" in response.text
    assert "Crear persona aleatoria" in response.text
    assert "Simular identidad por teléfono" in response.text
    assert "Cliente demo" not in response.text
    assert "cliente demo" not in response.text


def test_resolve_dev_demo_identity_prefers_phone_when_enabled():
    """The browser demo can switch to a phone-based identity that mimics WhatsApp."""
    assert (
        resolve_dev_demo_identity(
            external_user_id="demo-phone:abc",
            phone_number="+54 351 555 7788",
            use_phone_identity=True,
        )
        == "+543515557788"
    )
    assert (
        resolve_dev_demo_identity(
            external_user_id="demo-phone:abc",
            phone_number="",
            use_phone_identity=True,
        )
        == "demo-phone:abc"
    )
    assert (
        resolve_dev_demo_identity(
            external_user_id="demo-phone:abc",
            phone_number="+54 351 555 7788",
            use_phone_identity=False,
        )
        == "demo-phone:abc"
    )


def test_demo_chat_page_returns_not_found_for_unknown_slug(tmp_path: Path):
    """Unknown tenant slugs fail closed instead of rendering the default demo."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/demo/chat/slug-inexistente")

    assert response.status_code == HTTP_NOT_FOUND
    assert response.json()["detail"] == "Store not found."


def test_slug_scoped_demo_message_endpoint_can_use_phone_identity(tmp_path: Path, mocker):
    """The store-scoped demo endpoint can mimic WhatsApp-style identity by phone number."""
    settings = build_settings(tmp_path, store_slug="mi-muni")
    app = create_app(settings)
    fake_result = AssistantTurnResult.model_validate(
        {
            "conversation_id": 1,
            "customer": {"id": 1, "name": "Tomas", "phone_number": "+543515557788", "default_address": None},
            "reply": {"reply_text": "Hola", "next_step": "choose_items"},
            "current_order": None,
        }
    )
    handled_messages = []

    async def fake_handle_inbound_customer_message(*, session_factory, settings, inbound_message, store_id=None):
        handled_messages.append((inbound_message, store_id))
        return fake_result

    mocker.patch("ruperto.app.handle_inbound_customer_message", side_effect=fake_handle_inbound_customer_message)

    with TestClient(app) as client:
        response = client.post(
            "/api/dev/messages/mi-muni",
            json={
                "external_user_id": "browser-tab-1",
                "message_text": "Hola",
                "phone_number": "+54 351 555 7788",
                "use_phone_identity": True,
            },
        )

    assert response.status_code == HTTP_OK
    inbound_message, resolved_store_id = handled_messages[0]
    assert inbound_message.external_user_id == "mi-muni:+543515557788"
    assert inbound_message.metadata == {"phone_number": "+54 351 555 7788"}
    assert resolved_store_id == 1


def test_extract_kapso_phone_number_id_supports_direct_and_nested_payloads():
    """Kapso webhook routing can read the phone number id from either payload shape."""
    assert extract_kapso_phone_number_id({"phone_number_id": "direct-id"}) == "direct-id"
    assert extract_kapso_phone_number_id({"conversation": {"phone_number_id": "nested-id"}}) == "nested-id"
    assert extract_kapso_phone_number_id({"conversation": {}}) is None
    assert extract_kapso_phone_number_id(["not-a-dict"]) is None


def test_enrich_kapso_payload_from_headers_backfills_event_metadata():
    """Kapso event headers can restore the webhook type when the body omits it."""
    payload = {"message": {"text": {"body": "hola"}}}

    enriched = enrich_kapso_payload_from_headers(
        payload=payload,
        headers={"X-Webhook-Event": "whatsapp.message.received", "X-Webhook-Batch": "true"},
    )

    assert isinstance(enriched, Mapping)
    enriched_mapping = cast(dict[str, object], enriched)
    assert enriched_mapping["event"] == "whatsapp.message.received"
    assert enriched_mapping["batch"] is True


def test_enrich_kapso_payload_from_headers_keeps_non_mapping_payloads_unchanged():
    """Header enrichment leaves non-dict webhook payloads untouched."""
    payload = ["not-a-dict"]

    assert enrich_kapso_payload_from_headers(payload=payload, headers={"X-Webhook-Event": "ignored"}) == payload


def test_kapso_webhook_requires_valid_signature(tmp_path: Path):
    """Kapso webhooks are rejected when the signature does not match."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": SecretStr("kapso-key"),
            "kapso_phone_number_id": "597907523413541",
            "kapso_webhook_secret": SecretStr("kapso-secret"),
        }
    )
    app = create_app(settings)
    payload = {
        "event": "whatsapp.message.received",
        "message": {
            "id": "wamid.123",
            "from": "5493513308454",
            "timestamp": "1730092800",
            "type": "text",
            "text": {"body": "Hola"},
        },
        "conversation": {
            "id": "conv_123",
            "phone_number": "+5493513308454",
            "phone_number_id": "597907523413541",
            "kapso": {"contact_name": "Pedro"},
        },
        "phone_number_id": "597907523413541",
    }

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/kapso",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Webhook-Signature": "bad-signature"},
        )

    assert response.status_code == HTTP_UNAUTHORIZED


def test_kapso_webhook_requires_runtime_configuration(tmp_path: Path):
    """Kapso webhook processing stays disabled until the runtime credentials exist."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": None,
            "kapso_phone_number_id": None,
            "kapso_webhook_secret": None,
        }
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/kapso",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE


def test_dashboard_agent_channel_form_requires_login(tmp_path: Path):
    """Channel settings are protected behind the same dashboard login as the rest of the panel."""
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/dashboard/settings/agent/channel",
            data={"kapso_phone_number_id": "597907523413541"},
            follow_redirects=False,
        )

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"].startswith("/dashboard/login")


def test_kapso_webhook_rejects_invalid_json(tmp_path: Path):
    """Kapso webhook processing rejects malformed JSON payloads."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": SecretStr("kapso-key"),
            "kapso_phone_number_id": "597907523413541",
            "kapso_webhook_secret": SecretStr("kapso-secret"),
        }
    )
    app = create_app(settings)
    raw_payload = b"{bad json"
    signature = build_kapso_signature(payload=raw_payload, secret="kapso-secret")

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/kapso",
            content=raw_payload,
            headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
        )

    assert response.status_code == HTTP_BAD_REQUEST


def test_kapso_webhook_rejects_non_object_json_payload(tmp_path: Path):
    """Kapso webhook payloads must decode to a JSON object."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": SecretStr("kapso-key"),
            "kapso_phone_number_id": "597907523413541",
            "kapso_webhook_secret": SecretStr("kapso-secret"),
        }
    )
    app = create_app(settings)
    raw_payload = json.dumps(["not", "an", "object"]).encode("utf-8")
    signature = build_kapso_signature(payload=raw_payload, secret="kapso-secret")

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/kapso",
            content=raw_payload,
            headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
        )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {"detail": "Invalid Kapso webhook payload."}


def test_kapso_webhook_processes_inbound_message_and_sends_reply(tmp_path: Path, mocker):
    """Kapso inbound webhooks are normalized, processed, and answered."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": SecretStr("kapso-key"),
            "kapso_phone_number_id": "597907523413541",
            "kapso_webhook_secret": SecretStr("kapso-secret"),
        }
    )
    app = create_app(settings)
    fake_result = AssistantTurnResult.model_validate(
        {
            "conversation_id": 1,
            "customer": {"id": 1, "name": "Pedro", "phone_number": "+5493513308454", "default_address": None},
            "reply": {"reply_text": "Hola Pedro, ¿qué te gustaría pedir?", "next_step": "choose_items"},
            "current_order": None,
        }
    )
    handled_messages = []
    sent_payloads: list[dict[str, Any]] = []

    async def fake_handle_inbound_customer_message(*, session_factory, settings, inbound_message, store_id=None):
        handled_messages.append(inbound_message)
        return fake_result

    async def fake_send_text(self, message):
        sent_payloads.append({"to": message.external_user_id, "text": message.message_text})

    mocker.patch("ruperto.app.handle_inbound_customer_message", side_effect=fake_handle_inbound_customer_message)
    mocker.patch("ruperto.channels.kapso_whatsapp.KapsoWhatsAppGateway.send_text", new=fake_send_text)

    payload = {
        "event": "whatsapp.message.received",
        "message": {
            "id": "wamid.123",
            "from": "5493513308454",
            "timestamp": "1730092800",
            "type": "text",
            "text": {"body": "Hola, ¿tenés menú?"},
        },
        "conversation": {
            "id": "conv_123",
            "phone_number": "+5493513308454",
            "phone_number_id": "597907523413541",
            "kapso": {"contact_name": "Pedro"},
        },
        "phone_number_id": "597907523413541",
    }
    raw_payload = json.dumps(payload).encode("utf-8")
    signature = build_kapso_signature(payload=raw_payload, secret="kapso-secret")

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/kapso",
            content=raw_payload,
            headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
        )

    assert response.status_code == HTTP_OK
    assert response.json() == {"status": "ok", "processed": 1}
    assert handled_messages[0].external_user_id == "+5493513308454"
    assert handled_messages[0].sender_name == "Pedro"
    assert handled_messages[0].message_text == "Hola, ¿tenés menú?"
    assert sent_payloads == [{"to": "+5493513308454", "text": "Hola Pedro, ¿qué te gustaría pedir?"}]


def test_kapso_webhook_processes_header_only_event_payloads(tmp_path: Path, mocker):
    """Kapso event webhooks can rely on headers for the event name."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": SecretStr("kapso-key"),
            "kapso_phone_number_id": "597907523413541",
            "kapso_webhook_secret": SecretStr("kapso-secret"),
        }
    )
    app = create_app(settings)
    fake_result = AssistantTurnResult.model_validate(
        {
            "conversation_id": 1,
            "customer": {"id": 1, "name": "Pedro", "phone_number": "+5493513308454", "default_address": None},
            "reply": {"reply_text": "Hola Pedro, ¿qué te gustaría pedir?", "next_step": "choose_items"},
            "current_order": None,
        }
    )
    handled_messages = []
    sent_payloads: list[dict[str, Any]] = []

    async def fake_handle_inbound_customer_message(*, session_factory, settings, inbound_message, store_id=None):
        handled_messages.append(inbound_message)
        return fake_result

    async def fake_send_text(self, message):
        sent_payloads.append({"to": message.external_user_id, "text": message.message_text})

    mocker.patch("ruperto.app.handle_inbound_customer_message", side_effect=fake_handle_inbound_customer_message)
    mocker.patch("ruperto.channels.kapso_whatsapp.KapsoWhatsAppGateway.send_text", new=fake_send_text)

    payload = {
        "message": {
            "id": "wamid.123",
            "from": "5493513308454",
            "timestamp": "1730092800",
            "type": "text",
            "text": {"body": "Hola, ¿tenés menú?"},
        },
        "conversation": {
            "id": "conv_123",
            "phone_number": "+5493513308454",
            "phone_number_id": "597907523413541",
            "kapso": {"contact_name": "Pedro"},
        },
        "phone_number_id": "597907523413541",
    }
    raw_payload = json.dumps(payload).encode("utf-8")
    signature = build_kapso_signature(payload=raw_payload, secret="kapso-secret")

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/kapso",
            content=raw_payload,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
                "X-Webhook-Event": "whatsapp.message.received",
            },
        )

    assert response.status_code == HTTP_OK
    assert response.json() == {"status": "ok", "processed": 1}
    assert handled_messages[0].external_user_id == "+5493513308454"
    assert sent_payloads == [{"to": "+5493513308454", "text": "Hola Pedro, ¿qué te gustaría pedir?"}]


def test_kapso_webhook_skips_bot_reply_while_waiting_for_human(tmp_path: Path, mocker):
    """WhatsApp turns stop auto-replying once the conversation was handed to a human."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": SecretStr("kapso-key"),
            "kapso_phone_number_id": "597907523413541",
            "kapso_webhook_secret": SecretStr("kapso-secret"),
        }
    )
    conversation_id = anyio.run(lambda: create_whatsapp_handoff_conversation(settings))
    app = create_app(settings)

    async def fail_if_called(self, message):
        raise AssertionError(HANDOFF_AUTO_REPLY_ERROR)

    mocker.patch("ruperto.channels.kapso_whatsapp.KapsoWhatsAppGateway.send_text", new=fail_if_called)

    payload = {
        "message": {
            "id": "wamid.123",
            "from": "5493513308454",
            "timestamp": "1730092800",
            "type": "text",
            "text": {"body": "¿Sigue alguien ahí?"},
        },
        "conversation": {
            "id": "conv_123",
            "phone_number": "+5493513308454",
            "phone_number_id": "597907523413541",
            "kapso": {"contact_name": "Pedro"},
        },
        "phone_number_id": "597907523413541",
    }
    raw_payload = json.dumps(payload).encode("utf-8")
    signature = build_kapso_signature(payload=raw_payload, secret="kapso-secret")

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/kapso",
            content=raw_payload,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
                "X-Webhook-Event": "whatsapp.message.received",
            },
        )

    assert response.status_code == HTTP_OK
    assert response.json() == {"status": "ok", "processed": 1}

    runtime = create_database_runtime(settings)

    async def assert_latest_message():
        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)
            handoffs = await repository.list_active_conversation_handoffs(store_id=settings.default_store_id)
        assert handoffs[0].conversation_id == conversation_id
        assert handoffs[0].latest_customer_message == "¿Sigue alguien ahí?"

    anyio.run(assert_latest_message)
    anyio.run(runtime.engine.dispose)


def test_kapso_webhook_rejects_non_object_json_payloads(tmp_path: Path):
    """Kapso webhooks require a JSON object after header enrichment."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": SecretStr("kapso-key"),
            "kapso_phone_number_id": "597907523413541",
            "kapso_webhook_secret": SecretStr("kapso-secret"),
        }
    )
    app = create_app(settings)
    raw_payload = json.dumps(["not-a-dict"]).encode("utf-8")
    signature = build_kapso_signature(payload=raw_payload, secret="kapso-secret")

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/kapso",
            content=raw_payload,
            headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
        )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["detail"] == "Invalid Kapso webhook payload."


def test_format_dashboard_datetime_formats_local_time():
    """Dashboard timestamps are shown in the configured local timezone."""
    value = datetime(2026, 4, 4, 15, 30, tzinfo=UTC)

    assert format_dashboard_datetime(value, "America/Argentina/Cordoba") == "2026-04-04 12:30"


def test_public_settings_marks_secret_configuration():
    """Secret-backed integrations are only exposed as configured flags."""
    settings = Settings(
        gemini_api_key=SecretStr("gemini-key"),
        kapso_api_key=SecretStr("kapso-key"),
        kapso_webhook_secret=None,
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
    assert public["kapso_webhook_secret_configured"] is False
    assert public["smtp_server"] == "smtp.example.com"
    assert public["smtp_port"] == SMTP_PORT
    assert public["smtp_user"] == "mailer@example.com"
    assert public["smtp_password_configured"] is True
    assert public["smtp_configured"] is True


def test_public_settings_marks_kapso_webhook_secret_configuration():
    """Kapso webhook secrets are exposed as safe configured flags only."""
    settings = Settings(
        kapso_api_key=SecretStr("kapso-key"),
        kapso_phone_number_id="597907523413541",
        kapso_webhook_secret=SecretStr("kapso-secret"),
    )

    public = settings.public_settings()

    assert public["kapso_api_key_configured"] is True
    assert public["kapso_phone_number_id"] == "597907523413541"
    assert public["kapso_webhook_secret_configured"] is True


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


def test_order_status_update_delivers_whatsapp_notifications(tmp_path: Path, mocker):
    """Status changes dispatch queued notifications through the WhatsApp gateway."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": SecretStr("kapso-key"),
            "kapso_phone_number_id": "597907523413541",
            "kapso_webhook_secret": SecretStr("kapso-secret"),
        }
    )
    order_id = anyio.run(create_whatsapp_confirmed_order, settings)
    app = create_app(settings)
    sent_payloads: list[dict[str, Any]] = []

    async def fake_send_text(self, message):
        sent_payloads.append({"to": message.external_user_id, "text": message.message_text})

    mocker.patch("ruperto.channels.kapso_whatsapp.KapsoWhatsAppGateway.send_text", new=fake_send_text)

    with TestClient(app) as client:
        response = client.patch(
            f"/api/orders/{order_id}/status",
            json={"status": OrderStatus.ALMOST_READY.value},
        )

    assert response.status_code == HTTP_OK
    assert sent_payloads == [{"to": "+5493513308454", "text": "Tu pedido ya casi está 👀"}]


def test_municipal_case_status_update_delivers_whatsapp_notifications(tmp_path: Path, mocker):
    """Municipal status changes dispatch queued notifications through the WhatsApp gateway."""
    settings = build_settings(
        tmp_path,
        store_vertical=StoreVertical.MUNICIPAL,
        store_slug="mi-muni",
    ).model_copy(
        update={
            "kapso_api_key": SecretStr("kapso-key"),
            "kapso_phone_number_id": "597907523413541",
            "kapso_webhook_secret": SecretStr("kapso-secret"),
        }
    )
    case_id = anyio.run(create_whatsapp_municipal_case, settings)
    app = create_app(settings)
    sent_payloads: list[dict[str, Any]] = []

    async def fake_send_text(self, message):
        sent_payloads.append({"to": message.external_user_id, "text": message.message_text})

    mocker.patch("ruperto.channels.kapso_whatsapp.KapsoWhatsAppGateway.send_text", new=fake_send_text)

    with TestClient(app) as client:
        login_dashboard(client)
        response = client.patch(
            f"/api/kanban/municipal/cases/{case_id}/status",
            json=MunicipalCaseStatusUpdateRequest(status=MunicipalCaseStatus.TRIAGED).model_dump(mode="json"),
        )

    assert response.status_code == HTTP_OK
    assert sent_payloads == [{"to": "+5493513308454", "text": f"Tu solicitud #{case_id} ya está en revisión 👀"}]


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


def test_dev_notifications_endpoint_supports_slug_scoped_demo_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Slug-scoped demo notifications stay bound to the selected tenant."""
    settings = build_settings(tmp_path)
    monkeypatch.setattr("ruperto.assistant.build_google_model", lambda settings: FunctionModel(dev_message_model))
    app = create_app(settings)

    with TestClient(app) as client:
        client.post(
            f"/api/dev/messages/{settings.store_slug}",
            json={"external_user_id": "cli-user", "message_text": "Hola, quiero pedir"},
        )
        client.post(
            f"/api/dev/messages/{settings.store_slug}",
            json={"external_user_id": "cli-user", "message_text": "Martina"},
        )
        order_response = client.post(
            f"/api/dev/messages/{settings.store_slug}",
            json={"external_user_id": "cli-user", "message_text": "Quiero una hamburguesa"},
        )
        client.post(
            f"/api/dev/messages/{settings.store_slug}",
            json={"external_user_id": "cli-user", "message_text": "Confirmá el pedido"},
        )
        order_id = order_response.json()["current_order"]["id"]
        client.patch(f"/api/orders/{order_id}/status", json={"status": OrderStatus.ALMOST_READY.value})
        poll_response = client.get(
            f"/api/dev/notifications/{settings.store_slug}",
            params={"external_user_id": "cli-user"},
        )

    assert poll_response.status_code == HTTP_OK
    assert [notification["event_type"] for notification in poll_response.json()] == ["order_almost_ready"]


def test_municipal_dev_notifications_endpoint_returns_pending_messages(tmp_path: Path):
    """Municipal kanban status changes can be polled from the slug-scoped demo chat."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    case_id = anyio.run(lambda: create_dev_municipal_case(settings, external_user_id="cli-user"))
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        status_response = client.patch(
            f"/api/kanban/municipal/cases/{case_id}/status",
            json=MunicipalCaseStatusUpdateRequest(status=MunicipalCaseStatus.RESOLVED).model_dump(mode="json"),
        )
        first_poll = client.get(
            f"/api/dev/notifications/{settings.store_slug}",
            params={"external_user_id": "cli-user"},
        )
        second_poll = client.get(
            f"/api/dev/notifications/{settings.store_slug}",
            params={"external_user_id": "cli-user"},
        )

    assert status_response.status_code == HTTP_OK
    assert first_poll.status_code == HTTP_OK
    assert [notification["event_type"] for notification in first_poll.json()] == ["municipal_case_resolved"]
    assert [notification["municipal_case_id"] for notification in first_poll.json()] == [case_id]
    assert second_poll.status_code == HTTP_OK
    assert second_poll.json() == []


def test_municipal_dev_notifications_endpoint_supports_phone_identity_mode(tmp_path: Path):
    """The browser demo can poll municipal notifications using a WhatsApp-like phone identity."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    case_id = anyio.run(lambda: create_dev_municipal_case(settings, external_user_id="+543515557788"))
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        status_response = client.patch(
            f"/api/kanban/municipal/cases/{case_id}/status",
            json=MunicipalCaseStatusUpdateRequest(status=MunicipalCaseStatus.RESOLVED).model_dump(mode="json"),
        )
        poll_response = client.get(
            f"/api/dev/notifications/{settings.store_slug}",
            params={
                "external_user_id": "browser-tab-1",
                "phone_number": "+54 351 555 7788",
                "use_phone_identity": "true",
            },
        )

    assert status_response.status_code == HTTP_OK
    assert poll_response.status_code == HTTP_OK
    assert [notification["event_type"] for notification in poll_response.json()] == ["municipal_case_resolved"]
    assert [notification["municipal_case_id"] for notification in poll_response.json()] == [case_id]


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
    assert "Local de comida" in response.text
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


def test_serialize_store_hours_for_dashboard_skips_closed_and_incomplete_rows():
    """Dashboard schedule serialization only exposes usable slots."""
    serialized = serialize_store_hours_for_dashboard(
        [
            StoreBusinessHoursSnapshot(
                id=1,
                store_id=1,
                weekday=0,
                slot_index=0,
                opens_at=None,
                closes_at=None,
                closed=True,
            ),
            StoreBusinessHoursSnapshot(
                id=2,
                store_id=1,
                weekday=1,
                slot_index=0,
                opens_at="11:30",
                closes_at="15:00",
                closed=False,
            ),
            StoreBusinessHoursSnapshot(
                id=3,
                store_id=1,
                weekday=1,
                slot_index=1,
                opens_at="19:00",
                closes_at=None,
                closed=False,
            ),
        ]
    )

    monday = next(day for day in serialized if day["weekday"] == 0)
    tuesday = next(day for day in serialized if day["weekday"] == 1)

    assert monday["closed"] is True
    assert monday["slots"] == []
    assert tuesday["closed"] is False
    assert tuesday["slots"] == [{"slot_index": 0, "opens_at": "11:30", "closes_at": "15:00"}]


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
        channel_response = client.post(
            "/dashboard/settings/agent/channel",
            data={
                "kapso_phone_number_id": "597907523413541",
                "kapso_api_key": "kapso-key",
                "kapso_webhook_secret": "kapso-secret",
                "kapso_is_active": "on",
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
    assert channel_response.status_code == HTTP_OK
    assert "597907523413541" in channel_response.text
    assert hours_response.status_code == HTTP_OK
    assert "Horarios actualizados." in hours_response.text
    assert store_response.json()["store_name"] == "Panel Rotisería"
    assert store_response.json()["bot_name"] == "Panel Bot"
    assert store_response.json()["transfer_alias"] == "panel.rotiseria"
    assert store_response.json()["vertical"] == "ordering"
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


def test_dashboard_login_rejects_staff_without_store_memberships(tmp_path: Path):
    """Staff users without memberships cannot open a dashboard session."""
    app = create_app(build_settings(tmp_path))
    authenticated_staff = StaffUser(
        id=999,
        email="staff@example.com",
        full_name="Staff Demo",
        password_hash="hashed",
    )

    with (
        patch.object(
            BusinessRepository,
            "authenticate_staff_user",
            new=AsyncMock(return_value=authenticated_staff),
        ),
        patch.object(
            BusinessRepository,
            "list_store_memberships_for_staff_user",
            new=AsyncMock(return_value=[]),
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            "/dashboard/login",
            data={"email": "staff@example.com", "password": "secret123", "next": "/dashboard"},
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
    assert 'href="/demo/chat/test-rotiseria"' in response.text


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
        handoff_reply_response = client.post("/dashboard/handoffs/1/reply", data={}, follow_redirects=False)
        handoff_release_response = client.post("/dashboard/handoffs/1/release", data={}, follow_redirects=False)

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
    assert handoff_reply_response.status_code == HTTP_FOUND
    assert handoff_reply_response.headers["location"].startswith("/dashboard/login")
    assert handoff_release_response.status_code == HTTP_FOUND
    assert handoff_release_response.headers["location"].startswith("/dashboard/login")


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


def test_dashboard_customers_page_supports_handoff_reply_and_release(tmp_path: Path, mocker):
    """Staff can reply manually and release a WhatsApp handoff from the dashboard."""
    settings = build_settings(tmp_path)
    conversation_id = anyio.run(lambda: create_whatsapp_handoff_conversation(settings))
    sent_payloads: list[dict[str, str]] = []

    async def fake_send_text(self, message):
        sent_payloads.append({"to": message.external_user_id, "text": message.message_text})

    mocker.patch("ruperto.channels.kapso_whatsapp.KapsoWhatsAppGateway.send_text", new=fake_send_text)
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        customers_response = client.get("/dashboard/customers")
        reply_response = client.post(
            f"/dashboard/handoffs/{conversation_id}/reply",
            data={"message_text": "Hola, ya lo está revisando una persona del equipo."},
            follow_redirects=True,
        )
        release_response = client.post(
            f"/dashboard/handoffs/{conversation_id}/release",
            follow_redirects=True,
        )

    assert customers_response.status_code == HTTP_OK
    assert "Conversaciones derivadas a atención humana" in customers_response.text
    assert "Necesito hablar con alguien." in customers_response.text
    assert reply_response.status_code == HTTP_OK
    assert "La respuesta humana ya salió por el canal oficial." in reply_response.text
    assert sent_payloads == [{"to": "+5493513308454", "text": "Hola, ya lo está revisando una persona del equipo."}]
    assert release_response.status_code == HTTP_OK
    assert "La conversación volvió al bot." in release_response.text

    runtime = create_database_runtime(settings)

    async def assert_handoff_released():
        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)
            assert await repository.list_active_conversation_handoffs(store_id=settings.default_store_id) == []

    anyio.run(assert_handoff_released)
    anyio.run(runtime.engine.dispose)


def test_dashboard_handoff_release_succeeds_for_known_conversations(tmp_path: Path):
    """Operators can release a handoff directly from the dashboard queue."""
    settings = build_settings(tmp_path)
    conversation_id = anyio.run(lambda: create_whatsapp_handoff_conversation(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        response = client.post(f"/dashboard/handoffs/{conversation_id}/release", follow_redirects=False)

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"] == "/dashboard/customers?flash=handoff-released"


def test_dashboard_handoff_reply_validates_missing_message_and_unknown_conversation(tmp_path: Path):
    """Handoff reply routes surface validation and missing conversation errors."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        blank_response = client.post("/dashboard/handoffs/1/reply", data={"message_text": "   "})
        missing_reply_response = client.post("/dashboard/handoffs/999/reply", data={"message_text": "Hola"})
        missing_release_response = client.post("/dashboard/handoffs/999/release")

    assert blank_response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert blank_response.json()["detail"] == "Reply text is required."
    assert missing_reply_response.status_code == HTTP_NOT_FOUND
    assert missing_reply_response.json()["detail"] == "Conversation not found."
    assert missing_release_response.status_code == HTTP_NOT_FOUND
    assert missing_release_response.json()["detail"] == "Conversation not found."


def test_dashboard_handoff_reply_surfaces_unknown_conversations_from_repository(tmp_path: Path):
    """The reply form returns 404 when the repository cannot resolve the conversation target."""
    app = create_app(build_settings(tmp_path))

    with (
        patch.object(BusinessRepository, "get_conversation_target", new=AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        login_dashboard(client)
        response = client.post("/dashboard/handoffs/123/reply", data={"message_text": "Hola"})

    assert response.status_code == HTTP_NOT_FOUND
    assert response.json()["detail"] == "Conversation not found."


def test_dashboard_handoff_reply_rejects_conversations_not_waiting_for_human(tmp_path: Path):
    """Operators cannot reply through the handoff form before the bot escalates."""
    settings = build_settings(tmp_path)
    app = create_app(settings)
    runtime = create_database_runtime(settings)

    async def prepare_conversation():
        await init_database(settings=settings, runtime=runtime)
        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)
            customer = await repository.get_or_create_customer(
                channel=Channel.WHATSAPP,
                external_id="+5493513308454",
                phone_number="+5493513308454",
            )
            conversation = await repository.get_or_create_conversation(
                channel=Channel.WHATSAPP,
                external_id="+5493513308454",
                customer_id=customer.id,
            )
            await session.commit()
            return conversation.id

    conversation_id = anyio.run(prepare_conversation)

    with TestClient(app) as client:
        login_dashboard(client)
        response = client.post(
            f"/dashboard/handoffs/{conversation_id}/reply",
            data={"message_text": "Hola"},
        )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["detail"] == "Conversation is not waiting for a human."
    anyio.run(runtime.engine.dispose)


def test_dashboard_handoff_reply_checks_awaiting_human_after_resolving_the_target(tmp_path: Path):
    """The reply route still blocks operator messages until the handoff is active."""
    app = create_app(build_settings(tmp_path))
    target = type("Target", (), {"channel": Channel.WHATSAPP, "external_id": "+5493513308454"})()

    with (
        patch.object(BusinessRepository, "get_conversation_target", new=AsyncMock(return_value=target)),
        patch.object(BusinessRepository, "conversation_is_awaiting_human", new=AsyncMock(return_value=False)),
        TestClient(app) as client,
    ):
        login_dashboard(client)
        response = client.post("/dashboard/handoffs/123/reply", data={"message_text": "Hola"})

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["detail"] == "Conversation is not waiting for a human."


def test_dashboard_handoff_release_surfaces_unknown_conversations_from_repository(tmp_path: Path):
    """The release form returns 404 when the conversation target is no longer available."""
    app = create_app(build_settings(tmp_path))

    with (
        patch.object(BusinessRepository, "get_conversation_target", new=AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        login_dashboard(client)
        response = client.post("/dashboard/handoffs/123/release")

    assert response.status_code == HTTP_NOT_FOUND
    assert response.json()["detail"] == "Conversation not found."


def test_dashboard_handoff_release_commits_successful_releases(tmp_path: Path):
    """Known conversations can leave the handoff queue cleanly."""
    app = create_app(build_settings(tmp_path))
    target = type("Target", (), {"channel": Channel.WHATSAPP, "external_id": "+5493513308454"})()

    with (
        patch.object(BusinessRepository, "get_conversation_target", new=AsyncMock(return_value=target)),
        patch.object(BusinessRepository, "release_conversation_handoff", new=AsyncMock(return_value=True)),
        TestClient(app) as client,
    ):
        login_dashboard(client)
        response = client.post("/dashboard/handoffs/123/release", follow_redirects=False)

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"] == "/dashboard/customers?flash=handoff-released"


def test_dashboard_handoff_reply_requires_channel_delivery_configuration(tmp_path: Path):
    """Operator replies fail fast when the active store lacks a configured gateway."""
    settings = build_settings(tmp_path)
    conversation_id = anyio.run(lambda: create_whatsapp_handoff_conversation(settings))
    app = create_app(settings.model_copy(update={"kapso_api_key": None, "kapso_phone_number_id": None}))

    runtime = create_database_runtime(settings)

    async def clear_connection():
        await init_database(settings=settings, runtime=runtime)
        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)
            await repository.update_store_channel_connection(
                store_id=settings.default_store_id,
                channel=Channel.WHATSAPP,
                payload=StoreChannelConnectionUpdateRequest(
                    phone_number_id="sandbox-phone",
                    api_key="",
                    webhook_secret=None,
                    is_active=False,
                ),
            )
            await session.commit()

    anyio.run(clear_connection)

    with TestClient(app) as client:
        login_dashboard(client)
        response = client.post(
            f"/dashboard/handoffs/{conversation_id}/reply",
            data={"message_text": "Hola"},
        )

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "Channel delivery is not configured."
    anyio.run(runtime.engine.dispose)


def test_dashboard_handoff_reply_surfaces_channel_delivery_failures(tmp_path: Path, mocker):
    """Operator replies return a controlled error when the provider rejects delivery."""
    settings = build_settings(tmp_path)
    conversation_id = anyio.run(lambda: create_whatsapp_handoff_conversation(settings))

    async def fail_send_text(self, message):
        raise ChannelDeliveryError()

    mocker.patch("ruperto.channels.kapso_whatsapp.KapsoWhatsAppGateway.send_text", new=fail_send_text)
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        response = client.post(
            f"/dashboard/handoffs/{conversation_id}/reply",
            data={"message_text": "Hola"},
        )

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "Could not deliver the reply through the channel."


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
    assert 'name="vertical"' not in profile_response.text
    assert 'name="slug"' in profile_response.text
    assert "/demo/chat/test-rotiseria" in profile_response.text
    assert agent_response.status_code == HTTP_OK
    assert "Modelo" in agent_response.text
    assert "WhatsApp vía Kapso" in agent_response.text
    assert hours_response.status_code == HTTP_OK
    assert "Guardar agenda semanal" in hours_response.text
    assert users_response.status_code == HTTP_OK
    assert "Usuarios del local" in users_response.text


def test_dashboard_switches_navigation_and_placeholders_for_municipal_vertical(tmp_path: Path):
    """Municipal tenants reuse the shell but show municipal navigation and placeholder content."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        home_response = client.get("/dashboard")
        customers_response = client.get("/dashboard/customers")
        menu_response = client.get("/dashboard/settings/menu")
        hours_response = client.get("/dashboard/settings/hours")
        profile_response = client.get("/dashboard/settings/profile")

    assert home_response.status_code == HTTP_OK
    assert "Municipio" in home_response.text
    assert "Personas" in home_response.text
    assert "Áreas y categorías" in home_response.text
    assert "Este municipio ya puede recibir conversaciones en su propio espacio." in home_response.text
    assert customers_response.status_code == HTTP_OK
    assert "Personas que escribieron" in customers_response.text
    assert "Buscar persona" in customers_response.text
    assert menu_response.status_code == HTTP_OK
    assert "Crear área" in menu_response.text
    assert "Crear categoría" in menu_response.text
    assert hours_response.status_code == HTTP_OK
    assert "La agenda comercial queda desactivada para municipios." in hours_response.text
    assert profile_response.status_code == HTTP_OK
    assert "Nombre del municipio" in profile_response.text
    assert "/demo/chat/mi-muni" in profile_response.text


def test_dashboard_kanban_page_renders_for_municipal_staff(tmp_path: Path):
    """Municipal staff can open the kanban board from the dashboard."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)

    with TestClient(app) as client:
        login_dashboard(client)
        response = client.get("/dashboard/kanban")

    assert response.status_code == HTTP_OK
    assert "Tablero Kanban" in response.text
    assert "Todas las áreas" in response.text


def test_dashboard_kanban_page_redirects_anonymous_staff_to_login(tmp_path: Path):
    """The municipal Kanban page keeps the usual dashboard auth guard."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/dashboard/kanban", follow_redirects=False)

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"] == "/dashboard/login?next=%2Fdashboard%2Fkanban&flash=login-required"


def test_dashboard_kanban_page_loads_municipal_areas_for_the_active_store(tmp_path: Path):
    """The Kanban page always fetches active municipal areas before rendering."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)
    municipal_store = StoreProfile(
        id=1,
        slug="mi-muni",
        store_name="Mi muni",
        bot_name="Ruperto",
        store_description="Municipio de prueba",
        assistant_personality="Amable",
        vertical=StoreVertical.MUNICIPAL,
        locale="es-AR",
        currency_code="ARS",
    )
    list_areas = AsyncMock(return_value=[])

    with (
        patch.object(BusinessRepository, "get_store_profile", new=AsyncMock(return_value=municipal_store)),
        patch.object(BusinessRepository, "list_municipal_areas", new=list_areas),
        TestClient(app) as client,
    ):
        login_dashboard(client)
        response = client.get("/dashboard/kanban")

    assert response.status_code == HTTP_OK
    list_areas.assert_awaited_once_with(store_id=settings.default_store_id, only_active=True)


def test_dashboard_municipal_catalog_forms_can_create_areas_and_categories(tmp_path: Path):
    """Municipal dashboard settings can create areas and categories for the active store."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)

    async def list_area_ids() -> list[int]:
        runtime = create_database_runtime(settings)
        await init_database(settings=settings, runtime=runtime)
        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)
            areas = await repository.list_municipal_areas(store_id=settings.default_store_id)
        await runtime.engine.dispose()
        return [area.id for area in areas]

    initial_area_ids = set(anyio.run(list_area_ids))

    with TestClient(app) as client:
        login_dashboard(client)
        create_area_response = client.post(
            "/dashboard/settings/municipal/areas",
            data={
                "name": "Arbolado",
                "description": "Consultas sobre poda y arboles",
                "display_order": "12",
            },
            follow_redirects=False,
        )

    assert create_area_response.status_code == HTTP_FOUND
    assert create_area_response.headers["location"] == "/dashboard/settings/menu?flash=municipal-area-created"

    updated_area_ids = set(anyio.run(list_area_ids))
    created_area_ids = updated_area_ids - initial_area_ids
    assert len(created_area_ids) == 1
    created_area_id = next(iter(created_area_ids))

    with TestClient(app) as client:
        login_dashboard(client)
        create_category_response = client.post(
            "/dashboard/settings/municipal/categories",
            data={
                "area_id": str(created_area_id),
                "name": "Ramas caidas",
                "description": "Ramas o restos sobre la via publica",
                "request_kind": "request",
                "display_order": "3",
                "requires_precise_location": "on",
            },
            follow_redirects=False,
        )
        menu_response = client.get("/dashboard/settings/menu")

    assert create_category_response.status_code == HTTP_FOUND
    assert create_category_response.headers["location"] == "/dashboard/settings/menu?flash=municipal-category-created"
    assert "Arbolado" in menu_response.text
    assert "Ramas caidas" in menu_response.text
    assert "Solicitud" in menu_response.text
    assert "Ubicación precisa" in menu_response.text


def test_dashboard_municipal_area_form_requires_authentication(tmp_path: Path):
    """Municipal area creation redirects anonymous staff to login."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post("/dashboard/settings/municipal/areas", data={"name": "Arbolado"}, follow_redirects=False)

    assert response.status_code == HTTP_FOUND
    assert "/dashboard/login" in response.headers["location"]


def test_dashboard_municipal_area_form_rejects_invalid_payloads_and_non_municipal_stores(tmp_path: Path):
    """Municipal area creation validates input and is disabled for ordering stores."""
    municipal_tmp_path = tmp_path / "municipal"
    municipal_tmp_path.mkdir()
    municipal_app = create_app(
        build_settings(municipal_tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="municipal-test")
    )
    with TestClient(municipal_app) as client:
        login_dashboard(client)
        invalid_response = client.post("/dashboard/settings/municipal/areas", data={"name": ""}, follow_redirects=False)

    assert invalid_response.status_code == HTTP_UNPROCESSABLE_ENTITY

    ordering_tmp_path = tmp_path / "ordering"
    ordering_tmp_path.mkdir()
    ordering_app = create_app(
        build_settings(ordering_tmp_path, store_vertical=StoreVertical.ORDERING, store_slug="ordering-test")
    )
    with TestClient(ordering_app) as client:
        login_dashboard(client)
        disabled_response = client.post(
            "/dashboard/settings/municipal/areas", data={"name": "Arbolado"}, follow_redirects=False
        )

    assert disabled_response.status_code == HTTP_NOT_FOUND


def test_dashboard_municipal_area_form_uses_store_vertical_guard(tmp_path: Path):
    """Area creation checks the active store vertical before writing municipal data."""
    app = create_app(build_settings(tmp_path))
    ordering_store = StoreProfile(
        id=1,
        slug="test-rotiseria",
        store_name="Test Rotisería",
        bot_name="Ruperto",
        store_description="Rotisería de prueba",
        assistant_personality="Amable",
        vertical=StoreVertical.ORDERING,
        locale="es-AR",
        currency_code="ARS",
    )

    with (
        patch.object(BusinessRepository, "get_store_profile", new=AsyncMock(return_value=ordering_store)),
        TestClient(app) as client,
    ):
        login_dashboard(client)
        response = client.post("/dashboard/settings/municipal/areas", data={"name": "Arbolado"})

    assert response.status_code == HTTP_NOT_FOUND
    assert response.json()["detail"] == "Municipal catalog not enabled."


def test_dashboard_municipal_category_form_handles_auth_validation_and_missing_area(tmp_path: Path):
    """Municipal category creation covers auth, validation and missing-area errors."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)

    with TestClient(app) as client:
        anonymous_response = client.post(
            "/dashboard/settings/municipal/categories", data={"area_id": "1"}, follow_redirects=False
        )
    assert anonymous_response.status_code == HTTP_FOUND
    assert "/dashboard/login" in anonymous_response.headers["location"]

    with TestClient(app) as client:
        login_dashboard(client)
        invalid_area_response = client.post(
            "/dashboard/settings/municipal/categories",
            data={"area_id": "x", "name": "Ramas caídas"},
            follow_redirects=False,
        )
        invalid_payload_response = client.post(
            "/dashboard/settings/municipal/categories",
            data={"area_id": "1", "name": "", "request_kind": "complaint"},
            follow_redirects=False,
        )
        missing_area_response = client.post(
            "/dashboard/settings/municipal/categories",
            data={"area_id": "999", "name": "Ramas caídas", "request_kind": "complaint"},
            follow_redirects=False,
        )

    assert invalid_area_response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert invalid_payload_response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert missing_area_response.status_code == HTTP_NOT_FOUND


def test_dashboard_municipal_category_form_is_disabled_for_ordering_stores(tmp_path: Path):
    """Ordering stores do not expose the municipal category creator."""
    app = create_app(build_settings(tmp_path, store_vertical=StoreVertical.ORDERING))

    with TestClient(app) as client:
        login_dashboard(client)
        response = client.post(
            "/dashboard/settings/municipal/categories",
            data={"area_id": "1", "name": "Ramas caídas", "request_kind": "complaint"},
            follow_redirects=False,
        )

    assert response.status_code == HTTP_NOT_FOUND


def test_dev_messages_route_switches_to_municipal_vertical_without_creating_orders(tmp_path: Path):
    """The shared chat API routes municipal tenants away from the ordering flow."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)

    def municipal_dev_message_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart(content='{"asks_for_catalog": true}')], model_name="municipal-test")

    with (
        TestClient(app) as client,
        patch(
            "ruperto.municipal.build_google_model",
            lambda settings: FunctionModel(municipal_dev_message_model),
        ),
    ):
        response = client.post(
            "/api/dev/messages/mi-muni",
            json={"external_user_id": "municipal-user", "message_text": "Hola, quiero hacer un reclamo"},
        )
        orders_response = client.get("/api/orders", params={"limit": 10})

    payload = response.json()
    assert response.status_code == HTTP_OK
    assert payload["reply"]["next_step"] == "choose_area"
    assert "Puedo ayudarte a cargar un reclamo o una solicitud" in payload["reply"]["reply_text"]
    assert payload["current_order"] is None
    assert orders_response.json() == []


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


def test_dashboard_invalid_session_is_cleared_when_memberships_disappear(tmp_path: Path):
    """App-level dashboard identity clears the browser session without memberships too."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    async def strip_memberships():
        runtime = app.state.runtime
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            staff_user = await repository.get_staff_user_by_email("staff@example.com")
            assert staff_user is not None
            memberships = (
                await session.scalars(select(StoreMembership).where(StoreMembership.staff_user_id == staff_user.id))
            ).all()
            for membership in memberships:
                await session.delete(membership)
            await session.commit()

    with TestClient(app) as client:
        login_dashboard(client)
        anyio.run(strip_memberships)
        response = client.get("/dashboard", follow_redirects=False)

    assert response.status_code == HTTP_FOUND
    assert response.headers["location"].startswith("/dashboard/login")


def test_load_dashboard_identity_clears_inactive_staff_sessions(tmp_path: Path):
    """Inactive staff users are logged out before the dashboard resolves identity."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    async def resolve_identity_with_inactive_staff():
        runtime = app.state.runtime
        async with runtime.database.session_factory() as session:
            staff_user = await session.scalar(select(StaffUser).where(StaffUser.email == "staff@example.com"))
            assert staff_user is not None
            staff_user.is_active = False
            staff_user_id = staff_user.id
            await session.commit()
        request = build_request(
            app,
            path="/dashboard",
            session={"dashboard_staff_user_id": staff_user_id, "dashboard_store_id": 1},
        )
        return await load_dashboard_identity(request), request.session

    with TestClient(app):
        identity, session_data = anyio.run(resolve_identity_with_inactive_staff)

    assert identity is None
    assert session_data == {}


def test_load_dashboard_identity_clears_sessions_without_memberships_directly(tmp_path: Path):
    """Identity resolution clears sessions when the staff user lost every membership."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    async def resolve_identity_without_memberships():
        runtime = app.state.runtime
        async with runtime.database.session_factory() as session:
            staff_user = await session.scalar(select(StaffUser).where(StaffUser.email == "staff@example.com"))
            assert staff_user is not None
            memberships = (
                await session.scalars(select(StoreMembership).where(StoreMembership.staff_user_id == staff_user.id))
            ).all()
            for membership in memberships:
                await session.delete(membership)
            await session.commit()
            staff_user_id = staff_user.id
        request = build_request(
            app,
            path="/dashboard",
            session={"dashboard_staff_user_id": staff_user_id, "dashboard_store_id": 1},
        )
        return await load_dashboard_identity(request), request.session

    with TestClient(app):
        identity, session_data = anyio.run(resolve_identity_without_memberships)

    assert identity is None
    assert session_data == {}


async def exercise_dashboard_success_routes(ordering_app, municipal_app, municipal_slug: str):
    """Drive direct dashboard route calls that coverage misses in Python 3.13."""
    ordering_runtime = ordering_app.state.runtime
    municipal_runtime = municipal_app.state.runtime
    async with ordering_runtime.database.session_factory() as session:
        repository = BusinessRepository(session)
        ordering_staff = await repository.get_staff_user_by_email("staff@example.com")
        assert ordering_staff is not None
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="coverage-user")
        await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="coverage-user",
            customer_id=customer.id,
        )
        await session.commit()
    async with municipal_runtime.database.session_factory() as session:
        repository = BusinessRepository(session)
        municipal_staff = await repository.get_staff_user_by_email("staff@example.com")
        assert municipal_staff is not None
        water_area = next(
            area for area in await repository.list_municipal_areas(store_id=1) if area.name == "Solicitud de agua"
        )
        await repository.update_store_channel_connection(
            store_id=1,
            channel=Channel.WHATSAPP,
            provider=ChannelProvider.KAPSO,
            payload=StoreChannelConnectionUpdateRequest(
                phone_number_id="597907523413541",
                api_key="kapso-key",
                webhook_secret="kapso-secret",
                is_active=True,
            ),
        )
        await session.commit()

    ordering_session = {"dashboard_staff_user_id": ordering_staff.id, "dashboard_store_id": 1}
    municipal_session = {"dashboard_staff_user_id": municipal_staff.id, "dashboard_store_id": 1}

    dashboard_home_response = await get_route_endpoint(ordering_app, "/dashboard", "GET")(
        request=build_request(ordering_app, path="/dashboard", session=ordering_session),
    )
    dashboard_customers_response = await get_route_endpoint(ordering_app, "/dashboard/customers", "GET")(
        request=build_request(ordering_app, path="/dashboard/customers", session=ordering_session),
    )
    ordering_menu_response = await get_route_endpoint(ordering_app, "/dashboard/settings/menu", "GET")(
        request=build_request(ordering_app, path="/dashboard/settings/menu", session=ordering_session),
    )
    municipal_menu_response = await get_route_endpoint(municipal_app, "/dashboard/settings/menu", "GET")(
        request=build_request(municipal_app, path="/dashboard/settings/menu", session=municipal_session),
    )

    area_request = build_request(
        municipal_app,
        path="/dashboard/settings/municipal/areas",
        method="POST",
        session=municipal_session,
    )
    area_request = with_request_form(
        area_request,
        {"name": "Defensa civil", "description": "", "display_order": "5"},
    )
    create_area_response = await get_route_endpoint(municipal_app, "/dashboard/settings/municipal/areas", "POST")(
        request=area_request,
    )

    category_request = build_request(
        municipal_app,
        path="/dashboard/settings/municipal/categories",
        method="POST",
        session=municipal_session,
    )
    category_request = with_request_form(
        category_request,
        {
            "area_id": str(water_area.id),
            "name": "Refuerzo",
            "description": "",
            "request_kind": MunicipalRequestKind.REQUEST.value,
            "display_order": "3",
        },
    )
    create_category_response = await get_route_endpoint(
        municipal_app,
        "/dashboard/settings/municipal/categories",
        "POST",
    )(request=category_request)

    profile_request = build_request(
        ordering_app,
        path="/dashboard/settings/profile",
        method="POST",
        session=ordering_session,
    )
    profile_request = with_request_form(
        profile_request,
        {
            "store_name": "Cobertura Rotisería",
            "store_location": "Alta Gracia",
            "store_description": "Cobertura de dashboard.",
            "transfer_alias": "cobertura.rotiseria",
        },
    )
    profile_response = await get_route_endpoint(ordering_app, "/dashboard/settings/profile", "POST")(
        request=profile_request,
    )

    agent_get_response = await get_route_endpoint(ordering_app, "/dashboard/settings/agent", "GET")(
        request=build_request(ordering_app, path="/dashboard/settings/agent", session=ordering_session),
    )

    agent_request = build_request(
        ordering_app,
        path="/dashboard/settings/agent",
        method="POST",
        session=ordering_session,
    )
    agent_request = with_request_form(
        agent_request,
        {"bot_name": "Cobertura Bot", "assistant_personality": "Helpful and brief."},
    )
    agent_post_response = await get_route_endpoint(ordering_app, "/dashboard/settings/agent", "POST")(
        request=agent_request,
    )

    channel_request = build_request(
        ordering_app,
        path="/dashboard/settings/agent/channel",
        method="POST",
        session=ordering_session,
    )
    channel_request = with_request_form(
        channel_request,
        {
            "kapso_phone_number_id": "597907523413541",
            "kapso_api_key": "kapso-key",
            "kapso_webhook_secret": "kapso-secret",
            "kapso_is_active": "on",
        },
    )
    channel_response = await get_route_endpoint(ordering_app, "/dashboard/settings/agent/channel", "POST")(
        request=channel_request,
    )

    hours_get_response = await get_route_endpoint(ordering_app, "/dashboard/settings/hours", "GET")(
        request=build_request(ordering_app, path="/dashboard/settings/hours", session=ordering_session),
    )
    users_get_response = await get_route_endpoint(ordering_app, "/dashboard/settings/users", "GET")(
        request=build_request(ordering_app, path="/dashboard/settings/users", session=ordering_session),
    )

    hours_request = build_request(
        ordering_app,
        path="/dashboard/settings/hours",
        method="POST",
        session=ordering_session,
    )
    hours_request = with_request_form(
        hours_request,
        {"opens_at_1_0": "11:30", "closes_at_1_0": "15:00"},
    )
    hours_post_response = await get_route_endpoint(ordering_app, "/dashboard/settings/hours", "POST")(
        request=hours_request,
    )

    store_profile_response = await get_route_endpoint(ordering_app, "/api/store-profile", "PUT")(
        request=build_request(ordering_app, path="/api/store-profile", method="PUT"),
        payload=StoreProfileUpdateRequest(
            store_name="Cobertura Rotisería",
            bot_name="Cobertura Bot",
            store_location="Alta Gracia",
            store_description="Cobertura de dashboard.",
            assistant_personality="Helpful and brief.",
            transfer_alias="cobertura.rotiseria",
        ),
    )

    store_hours_response = await get_route_endpoint(ordering_app, "/api/store-hours", "PUT")(
        request=build_request(ordering_app, path="/api/store-hours", method="PUT"),
        payload=StoreBusinessHoursUpdateRequest(
            hours=[
                StoreBusinessHoursUpdateEntry(
                    weekday=1,
                    slot_index=0,
                    opens_at="11:30",
                    closes_at="15:00",
                    closed=False,
                )
            ]
        ),
    )

    notifications_response = await get_route_endpoint(ordering_app, "/api/dev/notifications", "GET")(
        request=build_request(ordering_app, path="/api/dev/notifications"),
        external_user_id="coverage-user",
        phone_number=None,
        use_phone_identity=False,
    )
    scoped_notifications_response = await get_route_endpoint(
        municipal_app,
        "/api/dev/notifications/{store_slug}",
        "GET",
    )(
        request=build_request(municipal_app, path=f"/api/dev/notifications/{municipal_slug}"),
        store_slug=municipal_slug,
        external_user_id="coverage-user",
        phone_number=None,
        use_phone_identity=False,
    )

    return {
        "dashboard_home_response": dashboard_home_response,
        "dashboard_customers_response": dashboard_customers_response,
        "ordering_menu_response": ordering_menu_response,
        "municipal_menu_response": municipal_menu_response,
        "create_area_response": create_area_response,
        "create_category_response": create_category_response,
        "profile_response": profile_response,
        "agent_get_response": agent_get_response,
        "agent_post_response": agent_post_response,
        "channel_response": channel_response,
        "hours_get_response": hours_get_response,
        "users_get_response": users_get_response,
        "hours_post_response": hours_post_response,
        "store_profile_response": store_profile_response,
        "store_hours_response": store_hours_response,
        "notifications_response": notifications_response,
        "scoped_notifications_response": scoped_notifications_response,
    }


def test_dashboard_direct_route_calls_cover_shared_controller_success_paths(tmp_path: Path):
    """Direct endpoint calls cover shared dashboard success paths under Python 3.13 coverage."""
    (tmp_path / "ordering").mkdir()
    (tmp_path / "municipal").mkdir()
    ordering_settings = build_settings(tmp_path / "ordering")
    municipal_settings = build_settings(
        tmp_path / "municipal",
        store_vertical=StoreVertical.MUNICIPAL,
        store_slug="mi-muni",
    )
    ordering_app = create_app(ordering_settings)
    municipal_app = create_app(municipal_settings)

    with TestClient(ordering_app), TestClient(municipal_app):
        responses = anyio.run(
            exercise_dashboard_success_routes,
            ordering_app,
            municipal_app,
            municipal_settings.store_slug,
        )

    assert responses["dashboard_home_response"].status_code == HTTP_OK
    assert responses["dashboard_customers_response"].status_code == HTTP_OK
    assert responses["ordering_menu_response"].status_code == HTTP_OK
    assert responses["municipal_menu_response"].status_code == HTTP_OK
    assert (
        responses["create_area_response"].headers["location"] == "/dashboard/settings/menu?flash=municipal-area-created"
    )
    assert (
        responses["create_category_response"].headers["location"]
        == "/dashboard/settings/menu?flash=municipal-category-created"
    )
    assert responses["profile_response"].headers["location"] == "/dashboard/settings/profile?flash=profile-updated"
    assert responses["agent_get_response"].status_code == HTTP_OK
    assert responses["agent_post_response"].headers["location"] == "/dashboard/settings/agent?flash=agent-updated"
    assert responses["channel_response"].headers["location"] == "/dashboard/settings/agent?flash=agent-updated"
    assert responses["hours_get_response"].status_code == HTTP_OK
    assert responses["users_get_response"].status_code == HTTP_OK
    assert responses["hours_post_response"].headers["location"] == "/dashboard/settings/hours?flash=hours-updated"
    assert responses["store_profile_response"].store_name == "Cobertura Rotisería"
    assert len(responses["store_hours_response"]) == DEFAULT_WEEKLY_HOURS
    assert responses["notifications_response"] == []
    assert responses["scoped_notifications_response"] == []


async def exercise_dashboard_error_routes(app):
    """Drive direct dashboard error branches that coverage misses in Python 3.13."""
    runtime = app.state.runtime
    async with runtime.database.session_factory() as session:
        repository = BusinessRepository(session)
        staff_user = await repository.get_staff_user_by_email("staff@example.com")
        assert staff_user is not None
        session_data = {"dashboard_staff_user_id": staff_user.id, "dashboard_store_id": 1}

    duplicate_request = build_request(
        app,
        path="/signup",
        method="POST",
        headers={"accept": "text/html"},
    )
    duplicate_request = with_request_form(
        duplicate_request,
        {
            "store_name": "Otro Tenant",
            "full_name": "Staff Duplicado",
            "email": "staff@example.com",
            "password": "super-secret-123",
            "vertical": StoreVertical.ORDERING.value,
        },
    )
    duplicate_response = await get_route_endpoint(app, "/signup", "POST")(request=duplicate_request)

    missing_membership_response = None
    try:
        role_request = build_request(
            app,
            path="/dashboard/settings/users/999/role",
            method="POST",
            session=session_data,
        )
        role_request = with_request_form(role_request, {"role": StaffRole.MANAGER.value})
        await get_route_endpoint(app, "/dashboard/settings/users/{membership_id}/role", "POST")(
            request=role_request,
            membership_id=999,
        )
    except HTTPException as error:
        missing_membership_response = error

    missing_order_response = None
    try:
        order_request = build_request(
            app,
            path="/dashboard/orders/999/status",
            method="POST",
            session=session_data,
        )
        order_request = with_request_form(order_request, {"status": OrderStatus.CONFIRMED.value})
        await get_route_endpoint(app, "/dashboard/orders/{order_id}/status", "POST")(
            request=order_request,
            order_id=999,
        )
    except HTTPException as error:
        missing_order_response = error

    missing_api_order_response = None
    try:
        await get_route_endpoint(app, "/api/orders/{order_id}/status", "PATCH")(
            request=build_request(app, path="/api/orders/999/status", method="PATCH"),
            order_id=999,
            payload=OrderStatusUpdateRequest(status=OrderStatus.CONFIRMED),
        )
    except HTTPException as error:
        missing_api_order_response = error

    return duplicate_response, missing_membership_response, missing_order_response, missing_api_order_response


def test_dashboard_direct_route_calls_cover_shared_controller_error_paths(tmp_path: Path):
    """Direct endpoint calls cover shared dashboard error handling branches too."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app):
        duplicate_response, missing_membership_response, missing_order_response, missing_api_order_response = anyio.run(
            exercise_dashboard_error_routes,
            app,
        )

    assert duplicate_response.status_code == HTTP_CONFLICT
    assert missing_membership_response is not None
    assert missing_membership_response.status_code == HTTP_NOT_FOUND
    assert missing_order_response is not None
    assert missing_order_response.status_code == HTTP_NOT_FOUND
    assert missing_api_order_response is not None
    assert missing_api_order_response.status_code == HTTP_NOT_FOUND


def test_dashboard_category_route_covers_non_municipal_and_missing_area_errors(tmp_path: Path):
    """Municipal category creation fails cleanly for wrong verticals and unknown areas."""
    (tmp_path / "ordering").mkdir()
    (tmp_path / "municipal").mkdir()
    ordering_app = create_app(build_settings(tmp_path / "ordering"))
    municipal_app = create_app(build_settings(tmp_path / "municipal", store_vertical=StoreVertical.MUNICIPAL))

    async def exercise_category_errors():
        ordering_runtime = ordering_app.state.runtime
        municipal_runtime = municipal_app.state.runtime
        async with ordering_runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            staff_user = await repository.get_staff_user_by_email("staff@example.com")
            assert staff_user is not None
            ordering_session = {"dashboard_staff_user_id": staff_user.id, "dashboard_store_id": 1}
        async with municipal_runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            staff_user = await repository.get_staff_user_by_email("staff@example.com")
            assert staff_user is not None
            municipal_session = {"dashboard_staff_user_id": staff_user.id, "dashboard_store_id": 1}

        ordering_request = build_request(
            ordering_app,
            path="/dashboard/settings/municipal/categories",
            method="POST",
            session=ordering_session,
        )
        ordering_request = with_request_form(
            ordering_request,
            {
                "area_id": "1",
                "name": "Consulta",
                "description": "",
                "request_kind": MunicipalRequestKind.REQUEST.value,
                "display_order": "1",
            },
        )
        municipal_request = build_request(
            municipal_app,
            path="/dashboard/settings/municipal/categories",
            method="POST",
            session=municipal_session,
        )
        municipal_request = with_request_form(
            municipal_request,
            {
                "area_id": "999",
                "name": "Consulta",
                "description": "",
                "request_kind": MunicipalRequestKind.REQUEST.value,
                "display_order": "1",
            },
        )
        category_endpoint = get_route_endpoint(municipal_app, "/dashboard/settings/municipal/categories", "POST")
        with pytest.raises(HTTPException) as non_municipal_error:
            await get_route_endpoint(ordering_app, "/dashboard/settings/municipal/categories", "POST")(
                request=ordering_request,
            )
        with pytest.raises(HTTPException) as missing_area_error:
            await category_endpoint(request=municipal_request)
        return non_municipal_error.value, missing_area_error.value

    with TestClient(ordering_app), TestClient(municipal_app):
        non_municipal_error, missing_area_error = anyio.run(exercise_category_errors)

    assert non_municipal_error.status_code == HTTP_NOT_FOUND
    assert missing_area_error.status_code == HTTP_NOT_FOUND


def test_dashboard_profile_and_agent_routes_cover_validation_errors(tmp_path: Path):
    """Direct profile handlers raise 422 responses when required form fields are missing."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    async def exercise_validation_errors():
        runtime = app.state.runtime
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            staff_user = await repository.get_staff_user_by_email("staff@example.com")
            assert staff_user is not None
            session_data = {"dashboard_staff_user_id": staff_user.id, "dashboard_store_id": 1}

        profile_request = build_request(
            app,
            path="/dashboard/settings/profile",
            method="POST",
            session=session_data,
        )
        profile_request = with_request_form(
            profile_request,
            {
                "store_name": "",
                "store_location": "Alta Gracia",
                "store_description": "Cobertura de dashboard.",
                "transfer_alias": "cobertura.rotiseria",
            },
        )
        agent_request = build_request(
            app,
            path="/dashboard/settings/agent",
            method="POST",
            session=session_data,
        )
        agent_request = with_request_form(agent_request, {"bot_name": "", "assistant_personality": ""})
        with pytest.raises(HTTPException) as profile_error:
            await get_route_endpoint(app, "/dashboard/settings/profile", "POST")(request=profile_request)
        with pytest.raises(HTTPException) as agent_error:
            await get_route_endpoint(app, "/dashboard/settings/agent", "POST")(request=agent_request)
        return profile_error.value, agent_error.value

    with TestClient(app):
        profile_error, agent_error = anyio.run(exercise_validation_errors)

    assert profile_error.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert agent_error.status_code == HTTP_UNPROCESSABLE_ENTITY


def test_dashboard_role_and_order_status_routes_cover_success_commits(tmp_path: Path):
    """Direct role/order handlers cover their successful commit paths."""
    settings = build_settings(tmp_path)
    app = create_app(settings)

    async def exercise_success_commits():
        runtime = app.state.runtime
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            owner = await repository.get_staff_user_by_email("staff@example.com")
            assert owner is not None
            extra_user = await repository.ensure_staff_user(
                email="team@example.com",
                full_name="Equipo Local",
                password="team-secret",
                store_id=1,
                role=StaffRole.STAFF,
            )
            memberships = await repository.list_staff_memberships_for_store(store_id=1)
            extra_membership = next(m for m in memberships if m.staff_user_id == extra_user.id)
            customer = await repository.get_or_create_customer(
                channel=Channel.DEV,
                external_id="order-coverage",
            )
            conversation = await repository.get_or_create_conversation(
                channel=Channel.DEV,
                external_id="order-coverage",
                customer_id=customer.id,
            )
            await repository.add_item_to_current_order(
                customer_id=customer.id,
                conversation_id=conversation.id,
                sku="hamburguesa-doble",
                quantity=1,
            )
            await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
            await repository.set_order_payment_method(customer.id, conversation.id, PaymentMethod.CASH)
            confirmed_order = await repository.confirm_current_order(customer.id, conversation.id)
            await session.commit()
            session_data = {"dashboard_staff_user_id": owner.id, "dashboard_store_id": 1}

        role_request = build_request(
            app,
            path=f"/dashboard/settings/users/{extra_membership.membership_id}/role",
            method="POST",
            session=session_data,
        )
        role_request = with_request_form(role_request, {"role": StaffRole.MANAGER.value})
        order_request = build_request(
            app,
            path=f"/dashboard/orders/{confirmed_order.id}/status",
            method="POST",
            session=session_data,
        )
        order_request = with_request_form(order_request, {"status": OrderStatus.ALMOST_READY.value})
        api_response = await get_route_endpoint(app, "/api/orders/{order_id}/status", "PATCH")(
            request=build_request(app, path=f"/api/orders/{confirmed_order.id}/status", method="PATCH"),
            order_id=confirmed_order.id,
            payload=OrderStatusUpdateRequest(status=OrderStatus.READY_FOR_PICKUP),
        )
        role_response = await get_route_endpoint(app, "/dashboard/settings/users/{membership_id}/role", "POST")(
            request=role_request,
            membership_id=extra_membership.membership_id,
        )
        order_response = await get_route_endpoint(app, "/dashboard/orders/{order_id}/status", "POST")(
            request=order_request,
            order_id=confirmed_order.id,
        )

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            memberships = await repository.list_staff_memberships_for_store(store_id=1)
            updated_membership = next(m for m in memberships if m.staff_user_id == extra_user.id)
            stored_order = await repository.get_order(confirmed_order.id)
        return role_response, order_response, api_response, updated_membership, stored_order

    with TestClient(app):
        role_response, order_response, api_response, updated_membership, stored_order = anyio.run(
            exercise_success_commits
        )

    assert role_response.headers["location"] == "/dashboard/settings/users?flash=role-updated"
    assert order_response.headers["location"] == "/dashboard?flash=order-updated"
    assert api_response.status == OrderStatus.READY_FOR_PICKUP
    assert updated_membership.role == StaffRole.MANAGER
    assert stored_order.status == OrderStatus.ALMOST_READY


def test_dashboard_route_smoke_covers_shared_ordering_controller_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A signed-in ordering tenant can exercise the shared dashboard controller glue."""
    monkeypatch.setattr("ruperto.assistant.build_google_model", lambda settings: FunctionModel(dev_message_model))
    app = create_app(build_settings(tmp_path))

    with TestClient(app) as client:
        root_json = client.get("/")
        root_html = client.get("/", headers={"accept": "text/html"})
        login_dashboard(client)
        home = client.get("/dashboard")
        customers = client.get("/dashboard/customers")
        menu = client.get("/dashboard/settings/menu")
        profile = client.post(
            "/dashboard/settings/profile",
            data={
                "store_name": "Cobertura Rotisería",
                "store_location": "Alta Gracia",
                "store_description": "Cobertura de dashboard.",
                "transfer_alias": "cobertura.rotiseria",
            },
            follow_redirects=False,
        )
        agent = client.post(
            "/dashboard/settings/agent",
            data={"bot_name": "Cobertura Bot", "assistant_personality": "Helpful and brief."},
            follow_redirects=False,
        )
        channel = client.post(
            "/dashboard/settings/agent/channel",
            data={
                "kapso_phone_number_id": "597907523413541",
                "kapso_api_key": "kapso-key",
                "kapso_webhook_secret": "kapso-secret",
                "kapso_is_active": "on",
            },
            follow_redirects=False,
        )
        hours = client.post(
            "/dashboard/settings/hours",
            data={"opens_at_1_0": "11:30", "closes_at_1_0": "15:00"},
            follow_redirects=False,
        )
        users = client.get("/dashboard/settings/users")
        client.post(
            "/api/dev/messages",
            json={"external_user_id": "coverage-user", "message_text": "Hola, quiero pedir"},
        )
        client.post("/api/dev/messages", json={"external_user_id": "coverage-user", "message_text": "Martina"})
        created = client.post(
            "/api/dev/messages",
            json={"external_user_id": "coverage-user", "message_text": "Quiero una hamburguesa"},
        )
        client.post(
            "/api/dev/messages",
            json={"external_user_id": "coverage-user", "message_text": "Confirmá el pedido"},
        )
        order_id = created.json()["current_order"]["id"]
        api_status = client.patch(f"/api/orders/{order_id}/status", json={"status": OrderStatus.ALMOST_READY.value})
        store_profile = client.put(
            "/api/store-profile",
            json={
                "store_name": "Cobertura Rotisería",
                "bot_name": "Cobertura Bot",
                "store_location": "Alta Gracia",
                "store_description": "Cobertura de dashboard.",
                "assistant_personality": "Helpful and brief.",
                "transfer_alias": "cobertura.rotiseria",
            },
        )
        store_hours = client.put(
            "/api/store-hours",
            json={
                "hours": [
                    {
                        "weekday": 1,
                        "slot_index": 0,
                        "opens_at": "11:30",
                        "closes_at": "15:00",
                        "closed": False,
                    }
                ]
            },
        )
        dashboard_status = client.post(
            f"/dashboard/orders/{order_id}/status",
            data={"status": OrderStatus.READY_FOR_PICKUP.value},
            follow_redirects=False,
        )
        notifications = client.get("/api/dev/notifications", params={"external_user_id": "coverage-user"})

    assert root_json.status_code == HTTP_OK
    assert root_html.status_code == HTTP_OK
    assert home.status_code == HTTP_OK
    assert customers.status_code == HTTP_OK
    assert menu.status_code == HTTP_OK
    assert profile.headers["location"] == "/dashboard/settings/profile?flash=profile-updated"
    assert agent.headers["location"] == "/dashboard/settings/agent?flash=agent-updated"
    assert channel.headers["location"] == "/dashboard/settings/agent?flash=agent-updated"
    assert hours.headers["location"] == "/dashboard/settings/hours?flash=hours-updated"
    assert users.status_code == HTTP_OK
    assert api_status.status_code == HTTP_OK
    assert store_profile.status_code == HTTP_OK
    assert store_hours.status_code == HTTP_OK
    assert dashboard_status.headers["location"] == "/dashboard?flash=order-updated"
    assert notifications.status_code == HTTP_OK


def test_dashboard_route_smoke_covers_shared_municipal_controller_paths(tmp_path: Path):
    """A signed-in municipal tenant can exercise the municipal dashboard controller glue."""
    settings = build_settings(tmp_path, store_vertical=StoreVertical.MUNICIPAL, store_slug="mi-muni")
    app = create_app(settings)
    case_id = anyio.run(lambda: create_dev_municipal_case(settings, external_user_id="coverage-muni"))

    with TestClient(app) as client:
        login_dashboard(client)
        home = client.get("/dashboard")
        customers = client.get("/dashboard/customers")
        menu = client.get("/dashboard/settings/menu")
        create_area = client.post(
            "/dashboard/settings/municipal/areas",
            data={"name": "Arbolado", "description": "Árboles", "display_order": "4"},
            follow_redirects=False,
        )
        create_category = client.post(
            "/dashboard/settings/municipal/categories",
            data={
                "area_id": "1",
                "name": "Consulta",
                "description": "Consulta general",
                "request_kind": "request",
                "display_order": "4",
            },
            follow_redirects=False,
        )
        profile = client.post(
            "/dashboard/settings/profile",
            data={
                "store_name": "Mi muni",
                "store_location": "Córdoba",
                "store_description": "Municipio de prueba",
                "transfer_alias": "",
            },
            follow_redirects=False,
        )
        agent = client.get("/dashboard/settings/agent")
        hours = client.get("/dashboard/settings/hours")
        users = client.get("/dashboard/settings/users")
        status = client.patch(
            f"/api/kanban/municipal/cases/{case_id}/status",
            json=MunicipalCaseStatusUpdateRequest(status=MunicipalCaseStatus.TRIAGED).model_dump(mode="json"),
        )
        notifications = client.get(
            f"/api/dev/notifications/{settings.store_slug}",
            params={"external_user_id": "coverage-muni"},
        )

    assert home.status_code == HTTP_OK
    assert customers.status_code == HTTP_OK
    assert menu.status_code == HTTP_OK
    assert create_area.headers["location"] == "/dashboard/settings/menu?flash=municipal-area-created"
    assert create_category.headers["location"] == "/dashboard/settings/menu?flash=municipal-category-created"
    assert profile.headers["location"] == "/dashboard/settings/profile?flash=profile-updated"
    assert agent.status_code == HTTP_OK
    assert hours.status_code == HTTP_OK
    assert users.status_code == HTTP_OK
    assert status.status_code == HTTP_OK
    assert notifications.status_code == HTTP_OK


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
