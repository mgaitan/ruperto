"""FastAPI application factory for the Ruperto service."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

from ruperto import get_version
from ruperto.assistant import OrderingAssistantService
from ruperto.auth import normalize_email
from ruperto.config import Settings
from ruperto.db import DatabaseRuntime, create_database_runtime, init_database, ping_database
from ruperto.models import Channel, DeliveryType, OrderStatus, PaymentMethod, StaffRole
from ruperto.repository import BusinessRepository, OrderNotFoundError
from ruperto.schemas import (
    AssistantTurnResult,
    CustomerSnapshot,
    DevMessageRequest,
    DevNotificationPollRequest,
    MenuItemSnapshot,
    OrderSnapshot,
    OrderStatusUpdateRequest,
    OutboundNotificationSnapshot,
    StaffUserSnapshot,
    StoreBusinessHoursSnapshot,
    StoreBusinessHoursUpdateEntry,
    StoreBusinessHoursUpdateRequest,
    StoreMembershipSnapshot,
    StoreProfileSnapshot,
    StoreProfileUpdateRequest,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
SESSION_STAFF_USER_ID_KEY = "dashboard_staff_user_id"
SESSION_STORE_ID_KEY = "dashboard_store_id"
DASHBOARD_LOGIN_PATH = "/dashboard/login"
DASHBOARD_FLASH_MESSAGES = {
    "agent-updated": "Configuración del agente actualizada.",
    "hours-updated": "Horarios actualizados.",
    "login-required": "Iniciá sesión para acceder al panel.",
    "logged-out": "Sesión cerrada.",
    "order-updated": "Estado del pedido actualizado.",
    "profile-updated": "Perfil del local actualizado.",
    "role-updated": "Rol actualizado.",
    "store-switched": "Local activo actualizado.",
}
DASHBOARD_ORDER_STATUS_LABELS = {
    OrderStatus.DRAFT: "Borrador",
    OrderStatus.CONFIRMED: "Confirmado",
    OrderStatus.IN_PREPARATION: "En preparación",
    OrderStatus.ALMOST_READY: "Casi listo",
    OrderStatus.READY_FOR_PICKUP: "Listo para retirar",
    OrderStatus.OUT_FOR_DELIVERY: "En reparto",
    OrderStatus.DELIVERED: "Entregado",
    OrderStatus.CANCELLED: "Cancelado",
}
DASHBOARD_DELIVERY_TYPE_LABELS = {
    DeliveryType.DELIVERY: "Envío",
    DeliveryType.PICKUP: "Retiro",
}
DASHBOARD_PAYMENT_METHOD_LABELS = {
    PaymentMethod.CASH: "Efectivo",
    PaymentMethod.CARD_LINK: "Link de pago",
    PaymentMethod.TRANSFER: "Transferencia",
}
DASHBOARD_STAFF_ROLE_LABELS = {
    StaffRole.OWNER: "Propietario",
    StaffRole.MANAGER: "Encargado",
    StaffRole.STAFF: "Equipo",
}
DASHBOARD_ORDER_STATUS_STYLES = {
    OrderStatus.DRAFT: "bg-slate-200 text-slate-700",
    OrderStatus.CONFIRMED: "bg-amber-100 text-amber-800",
    OrderStatus.IN_PREPARATION: "bg-sky-100 text-sky-800",
    OrderStatus.ALMOST_READY: "bg-violet-100 text-violet-800",
    OrderStatus.READY_FOR_PICKUP: "bg-emerald-100 text-emerald-800",
    OrderStatus.OUT_FOR_DELIVERY: "bg-cyan-100 text-cyan-800",
    OrderStatus.DELIVERED: "bg-emerald-200 text-emerald-900",
    OrderStatus.CANCELLED: "bg-rose-100 text-rose-800",
}
DASHBOARD_WEEKDAYS = {
    0: "Lunes",
    1: "Martes",
    2: "Miércoles",
    3: "Jueves",
    4: "Viernes",
    5: "Sábado",
    6: "Domingo",
}


class DashboardNavItem(TypedDict):
    """One navigation item in the dashboard sidebar."""

    key: str
    label: str
    path: str


class DashboardNavItemState(DashboardNavItem):
    """One navigation item with active state for templates."""

    active: bool


class DashboardNavSection(TypedDict):
    """One navigation section in the dashboard sidebar."""

    section: str
    items: list[DashboardNavItem]


class DashboardNavSectionState(TypedDict):
    """One navigation section ready for template rendering."""

    section: str
    items: list[DashboardNavItemState]


DASHBOARD_NAVIGATION: list[DashboardNavSection] = [
    {
        "section": "Operación",
        "items": [
            {"key": "home", "label": "Inicio", "path": "/dashboard"},
            {"key": "customers", "label": "Clientes", "path": "/dashboard/customers"},
        ],
    },
    {
        "section": "Configuración",
        "items": [
            {"key": "menu", "label": "Carta de productos", "path": "/dashboard/settings/menu"},
            {"key": "profile", "label": "Perfil del local", "path": "/dashboard/settings/profile"},
            {"key": "agent", "label": "Configuración del agente", "path": "/dashboard/settings/agent"},
            {"key": "hours", "label": "Agenda semanal", "path": "/dashboard/settings/hours"},
            {"key": "users", "label": "Usuarios / roles", "path": "/dashboard/settings/users"},
        ],
    },
]


@dataclass(slots=True)
class ApplicationRuntime:
    """Long-lived objects attached to the FastAPI lifespan."""

    settings: Settings
    database: DatabaseRuntime


@dataclass(slots=True)
class DashboardIdentity:
    """Current dashboard identity resolved from the session."""

    staff_user: StaffUserSnapshot
    memberships: list[StoreMembershipSnapshot]
    active_store_id: int


@dataclass(slots=True)
class DashboardPageSpec:
    """Static metadata for one dashboard page."""

    active_page: str
    title: str
    description: str


def get_runtime(request: Request) -> ApplicationRuntime:
    """Return the initialized runtime from the current request."""
    return request.app.state.runtime


def parse_session_int(value: object) -> int | None:
    """Parse one integer-like value stored in the signed session cookie."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def dashboard_redirect(*, path: str = "/dashboard", flash: str | None = None) -> RedirectResponse:
    """Redirect back to one dashboard page using the PRG pattern."""
    query = urlencode({"flash": flash}) if flash is not None else ""
    url = f"{path}?{query}" if query else path
    return RedirectResponse(url=url, status_code=303)


def dashboard_login_redirect(*, next_url: str = "/dashboard", flash: str | None = None) -> RedirectResponse:
    """Redirect to the login screen while preserving the intended destination."""
    params = {"next": next_url}
    if flash is not None:
        params["flash"] = flash
    return RedirectResponse(url=f"{DASHBOARD_LOGIN_PATH}?{urlencode(params)}", status_code=303)


def format_dashboard_datetime(value: datetime | None, timezone_name: str) -> str | None:
    """Format a timestamp for the staff dashboard."""
    if value is None:
        return None
    local_value = value.astimezone(ZoneInfo(timezone_name))
    return local_value.strftime("%Y-%m-%d %H:%M")


def serialize_order_for_dashboard(order: OrderSnapshot, timezone_name: str) -> dict[str, Any]:
    """Return a dashboard-friendly order payload."""
    return {
        "id": order.id,
        "status": order.status,
        "status_label": DASHBOARD_ORDER_STATUS_LABELS[order.status],
        "status_style": DASHBOARD_ORDER_STATUS_STYLES[order.status],
        "delivery_type": DASHBOARD_DELIVERY_TYPE_LABELS[order.delivery_type]
        if order.delivery_type is not None
        else "—",
        "delivery_address": order.delivery_address or "—",
        "payment_method": DASHBOARD_PAYMENT_METHOD_LABELS[order.payment_method]
        if order.payment_method is not None
        else "—",
        "requested_ready_at": format_dashboard_datetime(order.requested_ready_at, timezone_name),
        "preparation_starts_at": format_dashboard_datetime(order.preparation_starts_at, timezone_name),
        "total_amount_display": order.total_amount_display,
        "item_summary": ", ".join(f"{item.quantity} x {item.name}" for item in order.items),
    }


def serialize_store_hours_for_dashboard(hours: list[StoreBusinessHoursSnapshot]) -> list[dict[str, Any]]:
    """Return the weekly schedule grouped by day for form rendering."""
    grouped_hours: dict[int, list[dict[str, Any]]] = {weekday: [] for weekday in DASHBOARD_WEEKDAYS}
    for row in sorted(hours, key=lambda item: (item.weekday, item.slot_index)):
        if row.closed or row.opens_at is None or row.closes_at is None:
            continue
        grouped_hours[row.weekday].append(
            {
                "slot_index": row.slot_index,
                "opens_at": row.opens_at,
                "closes_at": row.closes_at,
            }
        )

    return [
        {
            "weekday": weekday,
            "label": DASHBOARD_WEEKDAYS[weekday],
            "slots": grouped_hours[weekday],
            "closed": len(grouped_hours[weekday]) == 0,
        }
        for weekday in DASHBOARD_WEEKDAYS
    ]


def parse_store_hours_form(form: Mapping[str, object]) -> list[StoreBusinessHoursUpdateEntry]:
    """Parse a dynamic weekly schedule form with zero or more slots per day."""
    slot_pattern = re.compile(r"^(opens_at|closes_at)_(\d)_(\d+)$")
    slot_values: dict[tuple[int, int], dict[str, str | None]] = {}

    for key in form:
        match = slot_pattern.match(str(key))
        if match is None:
            continue
        field_name, weekday_text, slot_index_text = match.groups()
        weekday = int(weekday_text)
        slot_index = int(slot_index_text)
        slot_entry = slot_values.setdefault((weekday, slot_index), {"opens_at": None, "closes_at": None})
        value = str(form.get(key, "")).strip() or None
        slot_entry[field_name] = value

    parsed_entries: list[StoreBusinessHoursUpdateEntry] = []
    for weekday in DASHBOARD_WEEKDAYS:
        day_slots = sorted(
            (
                (slot_index, values)
                for (slot_weekday, slot_index), values in slot_values.items()
                if slot_weekday == weekday
            ),
            key=lambda item: item[0],
        )
        if not day_slots:
            parsed_entries.append(
                StoreBusinessHoursUpdateEntry(
                    weekday=weekday,
                    slot_index=0,
                    opens_at=None,
                    closes_at=None,
                    closed=True,
                )
            )
            continue

        for slot_index, values in day_slots:
            parsed_entries.append(
                StoreBusinessHoursUpdateEntry(
                    weekday=weekday,
                    slot_index=slot_index,
                    opens_at=values["opens_at"],
                    closes_at=values["closes_at"],
                    closed=False,
                )
            )

    return parsed_entries


def dashboard_login_context(
    *,
    request: Request,
    next_url: str,
    flash: str | None = None,
    error_message: str | None = None,
    email: str = "",
) -> dict[str, Any]:
    """Build the template context shared by login GET and failed POST responses."""
    return {
        "request": request,
        "next_url": next_url,
        "error_message": error_message,
        "flash_message": DASHBOARD_FLASH_MESSAGES.get(flash or ""),
        "email": email,
    }


def demo_chat_page_context(*, request: Request, settings: Settings) -> dict[str, Any]:
    """Build the template context for the lightweight demo chat page."""
    return {
        "request": request,
        "store_name": settings.store_name,
        "bot_name": settings.bot_name,
        "store_location": settings.store_location or "Local sin ubicación configurada",
        "api_path": "/api/dev/messages",
        "notifications_api_path": "/api/dev/notifications",
        "demo_profiles": [
            {"label": "Martín", "phone": "+54 351 555 7788"},
            {"label": "Ana", "phone": "+54 9 11 3344 5566"},
        ],
        "demo_prompts": [
            "Hola, quiero ver la carta",
            "Soy Martín Gaitán",
            "Una hamburguesa doble cheddar para retirar",
            "¿Me lo podés preparar para las 12?",
        ],
    }


def dashboard_store_scope_note(memberships: list[StoreMembershipSnapshot]) -> str | None:
    """Explain the current tenancy boundary of the MVP dashboard."""
    if len(memberships) <= 1:
        return None
    return (
        "El cambio de local ya separa el perfil y los horarios. "
        "Pedidos, clientes y catálogo siguen compartiendo el modelo MVP mientras avanzamos con el multi-tenant."
    )


def dashboard_navigation(active_page: str) -> list[DashboardNavSectionState]:
    """Return the dashboard navigation with the current page marked as active."""
    return [
        {
            "section": section["section"],
            "items": [
                {
                    "key": item["key"],
                    "label": item["label"],
                    "path": item["path"],
                    "active": item["key"] == active_page,
                }
                for item in section["items"]
            ],
        }
        for section in DASHBOARD_NAVIGATION
    ]


def matches_customer_query(customer: CustomerSnapshot, query: str) -> bool:
    """Return whether one customer matches the dashboard search query."""
    haystack = " ".join(
        part.lower()
        for part in (
            customer.name or "",
            customer.phone_number or "",
            customer.default_address or "",
        )
    )
    return query in haystack


def matches_menu_query(item: MenuItemSnapshot, query: str, *, category: str | None) -> bool:
    """Return whether one menu item matches the dashboard filters."""
    if category and item.category != category:
        return False
    if not query:
        return True
    haystack = " ".join((item.name, item.description, item.sku, item.category)).lower()
    return query in haystack


def dashboard_page_context(
    *,
    request: Request,
    identity: DashboardIdentity,
    flash: str | None,
    page: DashboardPageSpec,
    store: StoreProfileSnapshot,
) -> dict[str, Any]:
    """Build the shared shell context used by all dashboard pages."""
    return {
        "request": request,
        "active_page": page.active_page,
        "active_store_id": identity.active_store_id,
        "current_user": identity.staff_user,
        "flash_message": DASHBOARD_FLASH_MESSAGES.get(flash or ""),
        "memberships": identity.memberships,
        "nav_sections": dashboard_navigation(page.active_page),
        "page_description": page.description,
        "page_title": page.title,
        "store": store,
        "tenant_scope_note": dashboard_store_scope_note(identity.memberships),
    }


async def load_dashboard_identity(request: Request) -> DashboardIdentity | None:
    """Resolve the current signed-in dashboard user from the session."""
    runtime = get_runtime(request)
    staff_user_id = parse_session_int(request.session.get(SESSION_STAFF_USER_ID_KEY))
    if staff_user_id is None:
        return None

    async with runtime.database.session_factory() as session:
        repository = BusinessRepository(session)
        staff_user = await repository.get_staff_user_by_id(staff_user_id)
        if staff_user is None or not staff_user.is_active:
            request.session.clear()
            return None
        memberships = await repository.list_store_memberships_for_staff_user(staff_user_id)
        if not memberships:
            request.session.clear()
            return None

    requested_store_id = parse_session_int(request.session.get(SESSION_STORE_ID_KEY))
    available_store_ids = {membership.store_id for membership in memberships}
    if requested_store_id not in available_store_ids:
        default_store_id = runtime.settings.default_store_id
        requested_store_id = default_store_id if default_store_id in available_store_ids else memberships[0].store_id
        request.session[SESSION_STORE_ID_KEY] = requested_store_id

    return DashboardIdentity(
        staff_user=staff_user,
        memberships=memberships,
        active_store_id=requested_store_id,
    )


AvailableMenuQuery = Annotated[bool, Query()]
LimitQuery = Annotated[int, Query(ge=1, le=200)]
OrderStatusQuery = Annotated[OrderStatus | None, Query()]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize and dispose service resources around the FastAPI lifespan."""
    settings: Settings = app.state.settings
    database = create_database_runtime(settings)

    if settings.auto_init_db:
        await init_database(settings=settings, runtime=database)

    app.state.runtime = ApplicationRuntime(settings=settings, database=database)
    yield
    await database.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:  # noqa: C901, PLR0915
    """Build the FastAPI application with a fully configured lifespan."""
    active_settings = settings or Settings()
    app = FastAPI(
        title="Ruperto API",
        version=get_version(),
        lifespan=lifespan,
        summary="Conversational ordering backend for food businesses.",
    )
    app.state.settings = active_settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=active_settings.dashboard_session_secret,
        same_site="lax",
        https_only=active_settings.environment == "production",
    )

    @app.get("/")
    async def read_root(request: Request) -> dict[str, Any]:
        runtime = get_runtime(request)
        return {
            "app": "ruperto",
            "version": get_version(),
            "environment": runtime.settings.environment,
            "store_name": runtime.settings.store_name,
            "bot_name": runtime.settings.bot_name,
            "store_locale": runtime.settings.store_locale,
        }

    @app.get("/healthz")
    async def healthcheck(request: Request) -> dict[str, str]:
        runtime = get_runtime(request)
        await ping_database(runtime.database)
        return {"status": "ok", "database": "ok"}

    @app.get("/demo/chat", response_class=HTMLResponse)
    async def read_demo_chat(request: Request) -> Response:
        runtime = get_runtime(request)
        context = demo_chat_page_context(request=request, settings=runtime.settings)
        return TEMPLATES.TemplateResponse(request=request, name="demo_chat.html", context=context)

    @app.get(DASHBOARD_LOGIN_PATH, response_class=HTMLResponse)
    async def get_dashboard_login(
        request: Request,
        next: str = "/dashboard",  # noqa: A002
        flash: str | None = None,
    ) -> Response:
        identity = await load_dashboard_identity(request)
        if identity is not None:
            return dashboard_redirect()
        context = dashboard_login_context(request=request, next_url=next, flash=flash)
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_login.html", context=context)

    @app.post(DASHBOARD_LOGIN_PATH, response_class=HTMLResponse)
    async def post_dashboard_login(request: Request) -> Response:
        runtime = get_runtime(request)
        form = await request.form()
        next_url = str(form.get("next", "/dashboard")) or "/dashboard"
        email = normalize_email(str(form.get("email", "")))
        password = str(form.get("password", ""))

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            staff_user = await repository.authenticate_staff_user(email=email, password=password)
            memberships = (
                await repository.list_store_memberships_for_staff_user(staff_user.id) if staff_user is not None else []
            )

        if staff_user is None or not memberships:
            context = dashboard_login_context(
                request=request,
                next_url=next_url,
                error_message="Credenciales inválidas.",
                email=email,
            )
            return TEMPLATES.TemplateResponse(
                request=request,
                name="dashboard_login.html",
                context=context,
                status_code=401,
            )

        request.session.clear()
        request.session[SESSION_STAFF_USER_ID_KEY] = staff_user.id
        request.session[SESSION_STORE_ID_KEY] = memberships[0].store_id
        return RedirectResponse(url=next_url, status_code=303)

    @app.post("/dashboard/logout")
    async def post_dashboard_logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return dashboard_login_redirect(flash="logged-out")

    @app.post("/dashboard/active-store")
    async def post_dashboard_active_store(request: Request) -> RedirectResponse:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(flash="login-required")

        form = await request.form()
        requested_store_id = parse_session_int(form.get("store_id"))
        if requested_store_id is None:
            raise HTTPException(status_code=422, detail="Invalid store id.")
        available_store_ids = {membership.store_id for membership in identity.memberships}
        if requested_store_id not in available_store_ids:
            raise HTTPException(status_code=403, detail="Store not accessible.")
        request.session[SESSION_STORE_ID_KEY] = requested_store_id
        return dashboard_redirect(path="/dashboard", flash="store-switched")

    @app.get("/dashboard", response_class=HTMLResponse)
    async def read_dashboard_home(
        request: Request,
        limit: LimitQuery = 20,
        flash: str | None = None,
    ) -> Response:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url=request.url.path, flash="login-required")

        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)
            orders = await repository.list_orders(limit=limit)
            customers = await repository.list_customers(limit=50)
            menu_items = await repository.list_menu_items(only_available=False)

        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=DashboardPageSpec(
                active_page="home",
                title="Inicio",
                description="Resumen rápido del local, con métricas y los pedidos más recientes.",
            ),
            store=store,
        )
        context.update(
            {
                "order_statuses": [
                    {"value": status.value, "label": DASHBOARD_ORDER_STATUS_LABELS[status]} for status in OrderStatus
                ],
                "orders": [serialize_order_for_dashboard(order, runtime.settings.store_timezone) for order in orders],
                "stats": {
                    "orders": len(orders),
                    "customers": len(customers),
                    "available_menu_items": sum(1 for item in menu_items if item.available),
                    "catalog_items": len(menu_items),
                },
            }
        )
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_home.html", context=context)

    @app.get("/dashboard/customers", response_class=HTMLResponse)
    async def read_dashboard_customers(
        request: Request,
        q: str = "",
        flash: str | None = None,
    ) -> Response:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url=request.url.path, flash="login-required")

        runtime = get_runtime(request)
        query = q.strip().lower()
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)
            customers = await repository.list_customers(limit=200)

        filtered_customers = [
            customer for customer in customers if not query or matches_customer_query(customer, query)
        ]
        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=DashboardPageSpec(
                active_page="customers",
                title="Clientes",
                description="Listado de clientes recientes con búsqueda por nombre, teléfono o dirección.",
            ),
            store=store,
        )
        context.update(
            {
                "customers": filtered_customers,
                "customers_query": q,
                "results_count": len(filtered_customers),
            }
        )
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_customers.html", context=context)

    @app.get("/dashboard/settings/menu", response_class=HTMLResponse)
    async def read_dashboard_menu_settings(
        request: Request,
        q: str = "",
        category: str | None = None,
        flash: str | None = None,
    ) -> Response:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url=request.url.path, flash="login-required")

        runtime = get_runtime(request)
        query = q.strip().lower()
        selected_category = category or None
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)
            menu_items = await repository.list_menu_items(only_available=False)

        categories = sorted({item.category for item in menu_items})
        filtered_items = [item for item in menu_items if matches_menu_query(item, query, category=selected_category)]
        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=DashboardPageSpec(
                active_page="menu",
                title="Carta de productos",
                description="Vista general del catálogo actual. Por ahora es una página de consulta y revisión.",
            ),
            store=store,
        )
        context.update(
            {
                "menu_categories": categories,
                "menu_category": selected_category or "",
                "menu_items": filtered_items,
                "menu_query": q,
            }
        )
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_settings_menu.html", context=context)

    @app.get("/dashboard/settings/profile", response_class=HTMLResponse)
    async def read_dashboard_profile_settings(request: Request, flash: str | None = None) -> Response:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url=request.url.path, flash="login-required")

        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)

        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=DashboardPageSpec(
                active_page="profile",
                title="Perfil del local",
                description="Datos públicos y operativos del local visibles para el equipo y el checkout.",
            ),
            store=store,
        )
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_settings_profile.html", context=context)

    @app.post("/dashboard/settings/profile")
    async def post_dashboard_store_profile(request: Request) -> RedirectResponse:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url="/dashboard/settings/profile", flash="login-required")

        runtime = get_runtime(request)
        form = await request.form()
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            current_store = await repository.get_store_profile(store_id=identity.active_store_id)
            try:
                payload = StoreProfileUpdateRequest(
                    store_name=str(form.get("store_name", "")),
                    bot_name=current_store.bot_name,
                    store_location=str(form.get("store_location", "")) or None,
                    store_description=str(form.get("store_description", "")),
                    assistant_personality=current_store.assistant_personality,
                    transfer_alias=str(form.get("transfer_alias", "")) or None,
                )
            except ValidationError as error:
                raise HTTPException(status_code=422, detail=error.errors()) from error
            await repository.update_store_profile(payload, store_id=identity.active_store_id)
            await session.commit()
        return dashboard_redirect(path="/dashboard/settings/profile", flash="profile-updated")

    @app.get("/dashboard/settings/agent", response_class=HTMLResponse)
    async def read_dashboard_agent_settings(request: Request, flash: str | None = None) -> Response:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url=request.url.path, flash="login-required")

        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)

        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=DashboardPageSpec(
                active_page="agent",
                title="Configuración del agente",
                description="Definí cómo se presenta y cómo responde el asistente del local.",
            ),
            store=store,
        )
        context["settings_snapshot"] = runtime.settings.public_settings()
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_settings_agent.html", context=context)

    @app.post("/dashboard/settings/agent")
    async def post_dashboard_agent_settings(request: Request) -> RedirectResponse:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url="/dashboard/settings/agent", flash="login-required")

        runtime = get_runtime(request)
        form = await request.form()
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            current_store = await repository.get_store_profile(store_id=identity.active_store_id)
            try:
                payload = StoreProfileUpdateRequest(
                    store_name=current_store.store_name,
                    bot_name=str(form.get("bot_name", "")),
                    store_location=current_store.store_location,
                    store_description=current_store.store_description,
                    assistant_personality=str(form.get("assistant_personality", "")),
                    transfer_alias=current_store.transfer_alias,
                )
            except ValidationError as error:
                raise HTTPException(status_code=422, detail=error.errors()) from error
            await repository.update_store_profile(payload, store_id=identity.active_store_id)
            await session.commit()
        return dashboard_redirect(path="/dashboard/settings/agent", flash="agent-updated")

    @app.get("/dashboard/settings/hours", response_class=HTMLResponse)
    async def read_dashboard_hours_settings(request: Request, flash: str | None = None) -> Response:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url=request.url.path, flash="login-required")

        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)
            hours = await repository.list_store_business_hours(store_id=identity.active_store_id)

        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=DashboardPageSpec(
                active_page="hours",
                title="Agenda semanal",
                description="Configurá los horarios de apertura del local por día de la semana.",
            ),
            store=store,
        )
        context["hours"] = serialize_store_hours_for_dashboard(hours)
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_settings_hours.html", context=context)

    @app.post("/dashboard/settings/hours")
    async def post_dashboard_store_hours(request: Request) -> RedirectResponse:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url="/dashboard/settings/hours", flash="login-required")

        runtime = get_runtime(request)
        form = await request.form()
        payload = StoreBusinessHoursUpdateRequest(hours=parse_store_hours_form(form))

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            try:
                await repository.replace_store_business_hours(
                    hours=[
                        StoreBusinessHoursSnapshot(
                            id=0,
                            store_id=identity.active_store_id,
                            weekday=row.weekday,
                            slot_index=row.slot_index,
                            opens_at=row.opens_at,
                            closes_at=row.closes_at,
                            closed=row.closed,
                        )
                        for row in payload.hours
                    ],
                    store_id=identity.active_store_id,
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            await session.commit()
        return dashboard_redirect(path="/dashboard/settings/hours", flash="hours-updated")

    @app.get("/dashboard/settings/users", response_class=HTMLResponse)
    async def read_dashboard_user_settings(request: Request, flash: str | None = None) -> Response:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url=request.url.path, flash="login-required")

        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)
            staff_memberships = await repository.list_staff_memberships_for_store(store_id=identity.active_store_id)

        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=DashboardPageSpec(
                active_page="users",
                title="Usuarios / roles",
                description=(
                    "Listado de accesos al panel para este local. Por ahora podés ajustar el rol de cada membresía."
                ),
            ),
            store=store,
        )
        context.update(
            {
                "staff_memberships": staff_memberships,
                "staff_roles": [
                    {"value": role.value, "label": DASHBOARD_STAFF_ROLE_LABELS[role]} for role in StaffRole
                ],
            }
        )
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_settings_users.html", context=context)

    @app.post("/dashboard/settings/users/{membership_id}/role")
    async def post_dashboard_user_role(request: Request, membership_id: int) -> RedirectResponse:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url="/dashboard/settings/users", flash="login-required")

        role_text = str((await request.form()).get("role", ""))
        try:
            role = StaffRole(role_text)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid role.") from error

        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            try:
                await repository.update_store_membership_role(
                    membership_id=membership_id,
                    store_id=identity.active_store_id,
                    role=role,
                )
            except ValueError as error:
                raise HTTPException(status_code=404, detail="Store membership not found.") from error
            await session.commit()
        return dashboard_redirect(path="/dashboard/settings/users", flash="role-updated")

    @app.post("/dashboard/orders/{order_id}/status")
    async def post_dashboard_order_status(request: Request, order_id: int) -> RedirectResponse:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url="/dashboard", flash="login-required")

        runtime = get_runtime(request)
        form = await request.form()
        try:
            payload = OrderStatusUpdateRequest.model_validate({"status": str(form.get("status", ""))})
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors()) from error

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            try:
                await repository.update_order_status(order_id, payload.status)
            except OrderNotFoundError as error:
                raise HTTPException(status_code=404, detail="Order not found.") from error
            await session.commit()
        return dashboard_redirect(path="/dashboard", flash="order-updated")

    @app.get("/api/store-profile", response_model=StoreProfileSnapshot)
    async def read_store_profile(request: Request) -> StoreProfileSnapshot:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            return await repository.get_store_profile(store_id=runtime.settings.default_store_id)

    @app.put("/api/store-profile", response_model=StoreProfileSnapshot)
    async def put_store_profile(
        request: Request,
        payload: StoreProfileUpdateRequest,
    ) -> StoreProfileSnapshot:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            updated_store = await repository.update_store_profile(
                payload,
                store_id=runtime.settings.default_store_id,
            )
            await session.commit()
            return updated_store

    @app.get("/api/menu-items", response_model=list[MenuItemSnapshot])
    async def read_menu_items(
        request: Request,
        only_available: AvailableMenuQuery = True,
    ) -> list[MenuItemSnapshot]:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            return await repository.list_menu_items(only_available=only_available)

    @app.get("/api/store-hours", response_model=list[StoreBusinessHoursSnapshot])
    async def read_store_hours(request: Request) -> list[StoreBusinessHoursSnapshot]:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            return await repository.list_store_business_hours(store_id=runtime.settings.default_store_id)

    @app.put("/api/store-hours", response_model=list[StoreBusinessHoursSnapshot])
    async def put_store_hours(
        request: Request,
        payload: StoreBusinessHoursUpdateRequest,
    ) -> list[StoreBusinessHoursSnapshot]:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            try:
                updated_hours = await repository.replace_store_business_hours(
                    hours=[
                        StoreBusinessHoursSnapshot(
                            id=0,
                            store_id=runtime.settings.default_store_id,
                            weekday=row.weekday,
                            slot_index=row.slot_index,
                            opens_at=row.opens_at,
                            closes_at=row.closes_at,
                            closed=row.closed,
                        )
                        for row in payload.hours
                    ],
                    store_id=runtime.settings.default_store_id,
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            await session.commit()
            return updated_hours

    @app.get("/api/customers", response_model=list[CustomerSnapshot])
    async def read_customers(
        request: Request,
        limit: LimitQuery = 50,
    ) -> list[CustomerSnapshot]:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            return await repository.list_customers(limit=limit)

    @app.get("/api/orders", response_model=list[OrderSnapshot])
    async def read_orders(
        request: Request,
        limit: LimitQuery = 50,
        status: OrderStatusQuery = None,
    ) -> list[OrderSnapshot]:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            return await repository.list_orders(limit=limit, status=status)

    @app.patch("/api/orders/{order_id}/status", response_model=OrderSnapshot)
    async def patch_order_status(
        request: Request,
        order_id: int,
        payload: OrderStatusUpdateRequest,
    ) -> OrderSnapshot:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            try:
                updated_order = await repository.update_order_status(order_id, payload.status)
            except OrderNotFoundError as error:
                raise HTTPException(status_code=404, detail="Order not found.") from error
            await session.commit()
            return updated_order

    @app.post("/api/dev/messages", response_model=AssistantTurnResult)
    async def post_dev_message(request: Request, payload: DevMessageRequest) -> AssistantTurnResult:
        runtime = get_runtime(request)
        service = OrderingAssistantService(
            session_factory=runtime.database.session_factory,
            settings=runtime.settings,
        )
        return await service.handle_customer_message(
            channel=Channel.DEV,
            external_user_id=payload.external_user_id,
            message_text=payload.message_text,
        )

    @app.get("/api/dev/notifications", response_model=list[OutboundNotificationSnapshot])
    async def get_dev_notifications(
        request: Request,
        external_user_id: str,
    ) -> list[OutboundNotificationSnapshot]:
        runtime = get_runtime(request)
        payload = DevNotificationPollRequest.model_validate({"external_user_id": external_user_id})
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            notifications = await repository.list_pending_notifications(
                channel=Channel.DEV,
                external_id=payload.external_user_id,
            )
            await session.commit()
            return notifications

    return app


app = create_app()
