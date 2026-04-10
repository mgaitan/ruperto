"""FastAPI application factory for the Ruperto service."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict, cast
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

from ruperto import get_version
from ruperto.api import kanban
from ruperto.auth import normalize_email
from ruperto.channels.base import ChannelDeliveryError, InboundCustomerMessage, OutboundCustomerMessage
from ruperto.channels.service import (
    build_whatsapp_gateway_for_phone_number,
    deliver_order_notifications,
    handle_inbound_customer_message,
)
from ruperto.config import Settings
from ruperto.db import DatabaseRuntime, create_database_runtime, init_database, ping_database, seed_store_bootstrap_data
from ruperto.mail import SignupEmailDeliveryError, send_signup_welcome_email
from ruperto.models import (
    Channel,
    ChannelProvider,
    DeliveryType,
    MunicipalRequestKind,
    OrderStatus,
    PaymentMethod,
    StaffRole,
    StoreVertical,
)
from ruperto.repository import (
    BusinessRepository,
    MunicipalAreaNotFoundError,
    OrderNotFoundError,
    normalize_phone_number,
)
from ruperto.schemas import (
    AssistantTurnResult,
    CustomerSnapshot,
    DevMessageRequest,
    DevNotificationPollRequest,
    HomeSignupRequest,
    MenuItemSnapshot,
    MunicipalAreaCreateRequest,
    MunicipalCategoryCreateRequest,
    OrderSnapshot,
    OrderStatusUpdateRequest,
    OutboundNotificationSnapshot,
    StaffUserSnapshot,
    StoreBusinessHoursSnapshot,
    StoreBusinessHoursUpdateEntry,
    StoreBusinessHoursUpdateRequest,
    StoreChannelConnectionUpdateRequest,
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
    "municipal-area-created": "Area creada.",
    "municipal-category-created": "Categoria creada.",
    "role-updated": "Rol actualizado.",
    "store-switched": "Local activo actualizado.",
}
HOME_SIGNUP_VERTICAL_OPTIONS = [
    {
        "value": StoreVertical.ORDERING.value,
        "label": "Local de comida",
        "description": "Recibe pedidos, muestra la carta y organiza al equipo en un solo lugar.",
    },
    {
        "value": StoreVertical.MUNICIPAL.value,
        "label": "Municipio",
        "description": "Atiende consultas, reclamos y solicitudes de la comunidad por canales digitales.",
    },
]
DASHBOARD_VERTICAL_LABELS = {
    StoreVertical.ORDERING: "Local de comida",
    StoreVertical.MUNICIPAL: "Municipio",
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


DASHBOARD_NAVIGATION_ORDERING: list[DashboardNavSection] = [
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

DASHBOARD_NAVIGATION_MUNICIPAL: list[DashboardNavSection] = [
    {
        "section": "Operación",
        "items": [
            {"key": "home", "label": "Inicio", "path": "/dashboard"},
            {"key": "kanban", "label": "Tablero Kanban", "path": "/dashboard/kanban"},
            {"key": "customers", "label": "Personas", "path": "/dashboard/customers"},
            {"key": "menu", "label": "Áreas y categorías", "path": "/dashboard/settings/menu"},
        ],
    },
    {
        "section": "Configuración",
        "items": [
            {"key": "profile", "label": "Perfil del municipio", "path": "/dashboard/settings/profile"},
            {"key": "agent", "label": "Configuración del agente", "path": "/dashboard/settings/agent"},
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


def request_prefers_html(request: Request) -> bool:
    """Return whether the client is explicitly asking for an HTML page."""
    return "text/html" in request.headers.get("accept", "").lower()


def home_signup_context(
    *,
    request: Request,
    error_message: str | None = None,
    status_code: int = 200,
    form_values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the public landing-page context shared by the home routes."""
    values = {
        "store_name": "",
        "full_name": "",
        "email": "",
        "vertical": StoreVertical.ORDERING.value,
    }
    if form_values is not None:
        values.update({key: value for key, value in form_values.items() if key in values})
    return {
        "request": request,
        "error_message": error_message,
        "status_code": status_code,
        "form_values": values,
        "vertical_options": HOME_SIGNUP_VERTICAL_OPTIONS,
        "dashboard_login_path": DASHBOARD_LOGIN_PATH,
        "demo_chat_path": "/demo/chat",
    }


def build_signup_store_defaults(*, store_name: str, vertical: StoreVertical) -> dict[str, str | None]:
    """Return the initial store profile copy used by the public signup."""
    resolved_name = store_name.strip()
    if vertical == StoreVertical.MUNICIPAL:
        return {
            "bot_name": "Ruperto",
            "store_description": f"{resolved_name} atiende consultas, reclamos y solicitudes por chat.",
            "assistant_personality": "Claro, cercano y resolutivo.",
            "transfer_alias": None,
        }
    return {
        "bot_name": "Ruperto",
        "store_description": f"{resolved_name} recibe pedidos asistidos por chat.",
        "assistant_personality": "Amable, agil y confiable.",
        "transfer_alias": None,
    }


def demo_chat_page_context(*, request: Request, store: StoreProfileSnapshot) -> dict[str, Any]:
    """Build the template context for the lightweight demo chat page."""
    if store.vertical == StoreVertical.MUNICIPAL:
        demo_prompts = [
            "Hola, quiero hacer un reclamo",
            "Es por alumbrado público",
            "La luz no funciona en la esquina",
            "San Martín 123 esquina Belgrano",
        ]
        demo_chat_copy = {
            "profile_collection_title": "Personas demo",
            "profile_collection_description": "Elegí un teléfono o cargá uno nuevo.",
            "intro_subject_plural": "personas nuevas o personas que ya tienen memoria",
            "add_profile_button": "Agregar persona demo",
            "random_profile_button": "Crear persona aleatoria",
            "active_profile_fallback": "Persona demo",
            "active_phone_fallback": "Elegí o creá un número para empezar.",
            "memory_hint": "Si reutilizás el mismo teléfono, el backend recuerda el contexto de esa persona.",
            "message_placeholder": "Escribí como si fueras la persona...",
            "user_bubble_label": "Persona",
            "notification_text": "Llegó una notificación del caso.",
            "random_profile_ready_text": "Persona demo aleatoria lista para usar.",
            "profile_active_text": "Persona demo activa.",
            "profile_removed_text": "Persona demo eliminada.",
            "phone_identity_label": "Simular identidad por teléfono",
            "phone_identity_hint": (
                "Cuando está activo, este demo usa el teléfono como identidad estable, "
                "como si el mensaje llegara por WhatsApp."
            ),
            "phone_identity_on_text": "Identidad por teléfono activa.",
            "phone_identity_off_text": "Identidad por perfil activa.",
        }
    else:
        demo_prompts = [
            "Hola, quiero ver la carta",
            "Soy Martín Gaitán",
            "Una hamburguesa doble cheddar para retirar",
            "¿Me lo podés preparar para las 12?",
        ]
        demo_chat_copy = {
            "profile_collection_title": "Clientes demo",
            "profile_collection_description": "Elegí un teléfono o cargá uno nuevo.",
            "intro_subject_plural": "clientes nuevos o clientes que ya tienen memoria",
            "add_profile_button": "Agregar cliente demo",
            "random_profile_button": "Crear cliente aleatorio",
            "active_profile_fallback": "Cliente demo",
            "active_phone_fallback": "Elegí o creá un número para empezar.",
            "memory_hint": "Si reutilizás el mismo teléfono, el backend recuerda el contexto de ese cliente.",
            "message_placeholder": "Escribí como si fueras el cliente...",
            "user_bubble_label": "Cliente",
            "notification_text": "Llegó una notificación del pedido.",
            "random_profile_ready_text": "Cliente demo aleatorio listo para usar.",
            "profile_active_text": "Cliente demo activo.",
            "profile_removed_text": "Cliente demo eliminado.",
            "phone_identity_label": "Simular identidad por teléfono",
            "phone_identity_hint": (
                "Cuando está activo, este demo usa el teléfono como identidad estable, "
                "como si el mensaje llegara por WhatsApp."
            ),
            "phone_identity_on_text": "Identidad por teléfono activa.",
            "phone_identity_off_text": "Identidad por perfil activa.",
        }
    return {
        "request": request,
        "store_name": store.store_name,
        "bot_name": store.bot_name,
        "store_location": store.store_location or "Local sin ubicación configurada",
        "api_path": f"/api/dev/messages/{store.slug}",
        "notifications_api_path": f"/api/dev/notifications/{store.slug}",
        "demo_profiles": [
            {"label": "Martín", "phone": "+54 351 555 7788"},
            {"label": "Ana", "phone": "+54 9 11 3344 5566"},
        ],
        "demo_chat_copy": demo_chat_copy,
        "demo_prompts": demo_prompts,
    }


def extract_kapso_phone_number_id(payload: object) -> str | None:
    """Extract the WhatsApp phone-number identifier from a Kapso webhook payload."""
    if not isinstance(payload, dict):
        return None
    payload_dict = cast(dict[str, object], payload)
    direct_phone_number_id = payload_dict.get("phone_number_id")
    if isinstance(direct_phone_number_id, str) and direct_phone_number_id.strip():
        return direct_phone_number_id.strip()
    conversation = payload_dict.get("conversation")
    if not isinstance(conversation, dict):
        return None
    conversation_dict = cast(dict[str, object], conversation)
    conversation_phone_number_id = conversation_dict.get("phone_number_id")
    if isinstance(conversation_phone_number_id, str) and conversation_phone_number_id.strip():
        return conversation_phone_number_id.strip()
    return None


def enrich_kapso_payload_from_headers(*, payload: object, headers: Mapping[str, str]) -> object:
    """Backfill Kapso event metadata from headers when the JSON body omits it."""
    if not isinstance(payload, dict):
        return payload
    payload_dict = cast(dict[str, object], payload).copy()
    header_event = headers.get("X-Webhook-Event")
    if header_event and "event" not in payload_dict and "type" not in payload_dict:
        payload_dict["event"] = header_event
    header_batch = headers.get("X-Webhook-Batch")
    if header_batch and "batch" not in payload_dict:
        payload_dict["batch"] = header_batch.strip().lower() == "true"
    return payload_dict


def dashboard_store_scope_note(memberships: list[StoreMembershipSnapshot]) -> str | None:
    """Explain the current tenancy boundary of the MVP dashboard."""
    if len(memberships) <= 1:
        return None
    return (
        "El cambio de local ya separa el perfil y los horarios. "
        "Pedidos, contactos y catálogo siguen compartiendo el modelo MVP mientras avanzamos con el multi-local."
    )


def build_scoped_dev_external_user_id(*, store_slug: str, external_user_id: str) -> str:
    """Namespace one dev-chat identity under the public tenant slug."""
    return f"{store_slug}:{external_user_id.strip()}"


def resolve_dev_demo_identity(
    *,
    external_user_id: str,
    phone_number: str | None,
    use_phone_identity: bool,
) -> str:
    """Resolve the dev-chat identity, optionally using the normalized phone number."""
    if not use_phone_identity:
        return external_user_id.strip()
    return normalize_phone_number(phone_number) or external_user_id.strip()


async def get_store_profile_by_slug_or_404(*, request: Request, store_slug: str) -> StoreProfileSnapshot:
    """Load one tenant by its public slug or raise a 404."""
    runtime = get_runtime(request)
    async with runtime.database.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile_by_slug(store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found.")
    return store


def dashboard_navigation(active_page: str, *, vertical: StoreVertical) -> list[DashboardNavSectionState]:
    """Return the dashboard navigation with the current page marked as active."""
    sections = DASHBOARD_NAVIGATION_MUNICIPAL if vertical == StoreVertical.MUNICIPAL else DASHBOARD_NAVIGATION_ORDERING
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
        for section in sections
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
        "nav_sections": dashboard_navigation(page.active_page, vertical=store.vertical),
        "page_description": page.description,
        "page_title": page.title,
        "store": store,
        "store_vertical_label": DASHBOARD_VERTICAL_LABELS[store.vertical],
        "tenant_scope_note": dashboard_store_scope_note(identity.memberships),
    }


def profile_page_copy(store: StoreProfileSnapshot) -> DashboardPageSpec:
    """Return the dashboard copy for the store profile page."""
    if store.vertical == StoreVertical.MUNICIPAL:
        return DashboardPageSpec(
            active_page="profile",
            title="Perfil del municipio",
            description="Datos públicos y operativos del municipio que usa este asistente.",
        )
    return DashboardPageSpec(
        active_page="profile",
        title="Perfil del local",
        description="Datos públicos y operativos del local visibles para el equipo y el checkout.",
    )


def customers_page_copy(store: StoreProfileSnapshot) -> DashboardPageSpec:
    """Return the dashboard copy for the contacts page."""
    if store.vertical == StoreVertical.MUNICIPAL:
        return DashboardPageSpec(
            active_page="customers",
            title="Personas que escribieron",
            description="Listado propio de este municipio con búsqueda por nombre, teléfono o referencia.",
        )
    return DashboardPageSpec(
        active_page="customers",
        title="Clientes",
        description="Listado de clientes recientes con búsqueda por nombre, teléfono o dirección.",
    )


def menu_page_copy(store: StoreProfileSnapshot) -> DashboardPageSpec:
    """Return the dashboard copy for the catalog-like settings page."""
    if store.vertical == StoreVertical.MUNICIPAL:
        return DashboardPageSpec(
            active_page="menu",
            title="Áreas y categorías",
            description="Configurá las áreas municipales y las categorías que el asistente va a ofrecer.",
        )
    return DashboardPageSpec(
        active_page="menu",
        title="Carta de productos",
        description="Vista general del catálogo actual. Por ahora es una página de consulta y revisión.",
    )


def home_page_copy(store: StoreProfileSnapshot) -> DashboardPageSpec:
    """Return the dashboard copy for the tenant home page."""
    if store.vertical == StoreVertical.MUNICIPAL:
        return DashboardPageSpec(
            active_page="home",
            title="Inicio",
            description="Resumen inicial del municipio. Desde acá vas a ir organizando la atención y los casos.",
        )
    return DashboardPageSpec(
        active_page="home",
        title="Inicio",
        description="Resumen rápido del local, con métricas y los pedidos más recientes.",
    )


def hours_page_copy(store: StoreProfileSnapshot) -> DashboardPageSpec:
    """Return the dashboard copy for weekly schedule settings."""
    if store.vertical == StoreVertical.MUNICIPAL:
        return DashboardPageSpec(
            active_page="hours",
            title="Agenda semanal",
            description="Este municipio no usa la agenda comercial. Las excepciones y turnos llegan después.",
        )
    return DashboardPageSpec(
        active_page="hours",
        title="Agenda semanal",
        description="Configurá los horarios de apertura del local por día de la semana.",
    )


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

    @app.get("/", response_model=None)
    async def read_root(request: Request) -> Response | dict[str, Any]:
        runtime = get_runtime(request)
        payload = {
            "app": "ruperto",
            "version": get_version(),
            "environment": runtime.settings.environment,
            "store_name": runtime.settings.store_name,
            "bot_name": runtime.settings.bot_name,
            "store_locale": runtime.settings.store_locale,
        }
        if not request_prefers_html(request):
            return payload
        return TEMPLATES.TemplateResponse(
            request=request,
            name="home.html",
            context=home_signup_context(request=request),
        )

    @app.post("/signup", response_class=HTMLResponse)
    async def post_home_signup(request: Request) -> Response:
        runtime = get_runtime(request)
        form = await request.form()
        submitted_values = {
            "store_name": str(form.get("store_name", "")).strip(),
            "full_name": str(form.get("full_name", "")).strip(),
            "email": normalize_email(str(form.get("email", ""))),
            "vertical": str(form.get("vertical", "")).strip(),
        }
        try:
            payload = HomeSignupRequest(
                store_name=submitted_values["store_name"],
                full_name=submitted_values["full_name"],
                email=submitted_values["email"],
                password=str(form.get("password", "")),
                vertical=StoreVertical(str(form.get("vertical", ""))),
            )
        except ValidationError as error:
            if request_prefers_html(request):
                context = home_signup_context(
                    request=request,
                    error_message="Completá nombre, responsable, correo, contraseña y el tipo de organización.",
                    status_code=422,
                    form_values=submitted_values,
                )
                return TEMPLATES.TemplateResponse(
                    request=request,
                    name="home.html",
                    context=context,
                    status_code=422,
                )
            raise HTTPException(status_code=422, detail=error.errors()) from error

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            existing_staff_user = await repository.get_staff_user_by_email(payload.email)
            if existing_staff_user is not None:
                context = home_signup_context(
                    request=request,
                    error_message="Ya existe una cuenta con ese correo.",
                    status_code=409,
                    form_values=submitted_values,
                )
                return TEMPLATES.TemplateResponse(
                    request=request,
                    name="home.html",
                    context=context,
                    status_code=409,
                )

            store_defaults = build_signup_store_defaults(
                store_name=payload.store_name,
                vertical=payload.vertical,
            )
            store = await repository.create_store_profile(
                store_name=payload.store_name,
                bot_name=cast(str, store_defaults["bot_name"]),
                store_description=cast(str, store_defaults["store_description"]),
                assistant_personality=cast(str, store_defaults["assistant_personality"]),
                vertical=payload.vertical,
                transfer_alias=store_defaults["transfer_alias"],
            )
            staff_user = await repository.ensure_staff_user(
                email=payload.email,
                full_name=payload.full_name,
                password=payload.password,
                store_id=store.id,
                role=StaffRole.OWNER,
            )
            await seed_store_bootstrap_data(
                session=session,
                repository=repository,
                store_id=store.id,
                vertical=store.vertical,
            )
            if runtime.settings.smtp_configured:
                assert runtime.settings.smtp_server is not None
                assert runtime.settings.smtp_port is not None
                assert runtime.settings.smtp_user is not None
                assert runtime.settings.smtp_password is not None
                dashboard_url = str(request.url_for("read_dashboard_home"))
                demo_chat_url = str(request.url_for("read_demo_chat_for_store", store_slug=store.slug))
                try:
                    await asyncio.to_thread(
                        send_signup_welcome_email,
                        smtp_server=runtime.settings.smtp_server,
                        smtp_port=runtime.settings.smtp_port,
                        smtp_user=runtime.settings.smtp_user,
                        smtp_password=runtime.settings.smtp_password.get_secret_value(),
                        recipient_email=payload.email,
                        recipient_name=payload.full_name,
                        store_name=store.store_name,
                        store_slug=store.slug,
                        vertical=store.vertical,
                        dashboard_url=dashboard_url,
                        demo_chat_url=demo_chat_url,
                    )
                except SignupEmailDeliveryError as error:
                    if request_prefers_html(request):
                        context = home_signup_context(
                            request=request,
                            error_message=(
                                "No pudimos enviar el correo de bienvenida. "
                                "Revisá la configuración SMTP e intentá de nuevo."
                            ),
                            status_code=503,
                            form_values=submitted_values,
                        )
                        return TEMPLATES.TemplateResponse(
                            request=request,
                            name="home.html",
                            context=context,
                            status_code=503,
                        )
                    raise HTTPException(status_code=503, detail="Could not deliver signup email.") from error
            await session.commit()

        request.session.clear()
        request.session[SESSION_STAFF_USER_ID_KEY] = staff_user.id
        request.session[SESSION_STORE_ID_KEY] = store.id
        return RedirectResponse(url="/dashboard", status_code=303)

    @app.get("/healthz")
    async def healthcheck(request: Request) -> dict[str, str]:
        runtime = get_runtime(request)
        await ping_database(runtime.database)
        return {"status": "ok", "database": "ok"}

    @app.get("/demo/chat", response_class=HTMLResponse)
    async def read_demo_chat(request: Request) -> Response:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=runtime.settings.default_store_id)
        return RedirectResponse(url=f"/demo/chat/{store.slug}", status_code=303)

    @app.get("/demo/chat/{store_slug}", response_class=HTMLResponse)
    async def read_demo_chat_for_store(request: Request, store_slug: str) -> Response:
        store = await get_store_profile_by_slug_or_404(request=request, store_slug=store_slug)
        context = demo_chat_page_context(request=request, store=store)
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
            orders = await repository.list_orders(limit=limit, store_id=identity.active_store_id)
            customers = await repository.list_customers(limit=50, store_id=identity.active_store_id)
            menu_items = await repository.list_menu_items(only_available=False)

        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=home_page_copy(store),
            store=store,
        )
        if store.vertical == StoreVertical.MUNICIPAL:
            context["placeholder_points"] = [
                "Este municipio ya puede recibir conversaciones en su propio espacio.",
                "Las personas que escriben quedan asociadas solo a este municipio.",
                "Podés cargar áreas y categorías para empezar a ordenar la atención.",
            ]
            return TEMPLATES.TemplateResponse(
                request=request, name="dashboard_vertical_placeholder.html", context=context
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
            customers = await repository.list_customers(limit=200, store_id=identity.active_store_id)

        filtered_customers = [
            customer for customer in customers if not query or matches_customer_query(customer, query)
        ]
        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=customers_page_copy(store),
            store=store,
        )
        context.update(
            {
                "customers": filtered_customers,
                "customers_query": q,
                "customers_empty_message": (
                    "Todavía no hay personas registradas para este municipio."
                    if store.vertical == StoreVertical.MUNICIPAL
                    else "No hay clientes que coincidan con esa búsqueda."
                ),
                "customers_entity_badge": "Persona" if store.vertical == StoreVertical.MUNICIPAL else "Cliente",
                "customers_fallback_name": (
                    "Persona sin nombre" if store.vertical == StoreVertical.MUNICIPAL else "Cliente sin nombre"
                ),
                "customers_id_label": "Registro" if store.vertical == StoreVertical.MUNICIPAL else "Cliente",
                "customers_query_label": (
                    "Buscar persona" if store.vertical == StoreVertical.MUNICIPAL else "Buscar cliente"
                ),
                "customers_query_placeholder": (
                    "Nombre, teléfono o referencia"
                    if store.vertical == StoreVertical.MUNICIPAL
                    else "Nombre, teléfono o dirección"
                ),
                "customers_results_label": (
                    "personas encontradas" if store.vertical == StoreVertical.MUNICIPAL else "clientes encontrados"
                ),
                "results_count": len(filtered_customers),
            }
        )
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_customers.html", context=context)

    @app.get("/dashboard/kanban", response_class=HTMLResponse)
    async def read_dashboard_kanban(
        request: Request,
        flash: str | None = None,
    ) -> Response:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url=request.url.path, flash="login-required")

        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)
            areas = await repository.list_municipal_areas(store_id=identity.active_store_id, only_active=True)

        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=DashboardPageSpec(
                active_page="kanban",
                title="Tablero Kanban",
                description="Gestioná los casos municipales por estado y área.",
            ),
            store=store,
        )
        context.update(
            {
                "areas": areas,
            }
        )
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_kanban.html", context=context)

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
            if store.vertical == StoreVertical.MUNICIPAL:
                areas = await repository.list_municipal_areas(store_id=identity.active_store_id)
                categories = await repository.list_municipal_categories(store_id=identity.active_store_id)
            else:
                menu_items = await repository.list_menu_items(only_available=False)

        context = dashboard_page_context(
            request=request,
            identity=identity,
            flash=flash,
            page=menu_page_copy(store),
            store=store,
        )
        if store.vertical == StoreVertical.MUNICIPAL:
            context.update(
                {
                    "municipal_areas": areas,
                    "municipal_request_kind_options": [
                        {"value": MunicipalRequestKind.COMPLAINT.value, "label": "Reclamo"},
                        {"value": MunicipalRequestKind.REQUEST.value, "label": "Solicitud"},
                    ],
                    "municipal_area_options": areas,
                    "municipal_categories_by_area": {
                        area.id: [item for item in categories if item.area_id == area.id] for area in areas
                    },
                }
            )
            return TEMPLATES.TemplateResponse(
                request=request,
                name="dashboard_settings_municipal_catalog.html",
                context=context,
            )
        categories = sorted({item.category for item in menu_items})
        filtered_items = [item for item in menu_items if matches_menu_query(item, query, category=selected_category)]
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
            page=profile_page_copy(store),
            store=store,
        )
        return TEMPLATES.TemplateResponse(request=request, name="dashboard_settings_profile.html", context=context)

    @app.post("/dashboard/settings/municipal/areas")
    async def post_dashboard_municipal_area(request: Request) -> RedirectResponse:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url="/dashboard/settings/menu", flash="login-required")

        runtime = get_runtime(request)
        form = await request.form()
        display_order = parse_session_int(form.get("display_order"))
        try:
            payload = MunicipalAreaCreateRequest(
                name=str(form.get("name", "")),
                description=str(form.get("description", "")) or None,
                display_order=display_order or 0,
            )
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors()) from error

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)
            if store.vertical != StoreVertical.MUNICIPAL:
                raise HTTPException(status_code=404, detail="Municipal catalog not enabled.")
            await repository.create_municipal_area(store_id=identity.active_store_id, payload=payload)
            await session.commit()
        return dashboard_redirect(path="/dashboard/settings/menu", flash="municipal-area-created")

    @app.post("/dashboard/settings/municipal/categories")
    async def post_dashboard_municipal_category(request: Request) -> RedirectResponse:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url="/dashboard/settings/menu", flash="login-required")

        runtime = get_runtime(request)
        form = await request.form()
        area_id = parse_session_int(form.get("area_id"))
        if area_id is None:
            raise HTTPException(status_code=422, detail="Invalid municipal area id.")
        display_order = parse_session_int(form.get("display_order"))
        try:
            payload = MunicipalCategoryCreateRequest(
                name=str(form.get("name", "")),
                description=str(form.get("description", "")) or None,
                request_kind=MunicipalRequestKind(str(form.get("request_kind", MunicipalRequestKind.COMPLAINT.value))),
                display_order=display_order or 0,
                requires_precise_location=str(form.get("requires_precise_location", "")).lower() == "on",
                is_fallback=str(form.get("is_fallback", "")).lower() == "on",
            )
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors()) from error

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=identity.active_store_id)
            if store.vertical != StoreVertical.MUNICIPAL:
                raise HTTPException(status_code=404, detail="Municipal catalog not enabled.")
            try:
                await repository.create_municipal_category(area_id=area_id, payload=payload)
            except MunicipalAreaNotFoundError as error:
                raise HTTPException(status_code=404, detail="Municipal area not found.") from error
            await session.commit()
        return dashboard_redirect(path="/dashboard/settings/menu", flash="municipal-category-created")

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
            channel_connection = await repository.get_store_channel_connection(
                store_id=identity.active_store_id,
                channel=Channel.WHATSAPP,
                provider=ChannelProvider.KAPSO,
            )

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
        context["channel_connection"] = channel_connection
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

    @app.post("/dashboard/settings/agent/channel")
    async def post_dashboard_agent_channel_settings(request: Request) -> RedirectResponse:
        identity = await load_dashboard_identity(request)
        if identity is None:
            return dashboard_login_redirect(next_url="/dashboard/settings/agent", flash="login-required")

        runtime = get_runtime(request)
        form = await request.form()
        payload = StoreChannelConnectionUpdateRequest(
            phone_number_id=str(form.get("kapso_phone_number_id", "")) or None,
            api_key=str(form.get("kapso_api_key", "")) or None,
            webhook_secret=str(form.get("kapso_webhook_secret", "")) or None,
            is_active=form.get("kapso_is_active") == "on",
        )

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            await repository.update_store_channel_connection(
                store_id=identity.active_store_id,
                channel=Channel.WHATSAPP,
                provider=ChannelProvider.KAPSO,
                payload=payload,
            )
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
            page=hours_page_copy(store),
            store=store,
        )
        if store.vertical == StoreVertical.MUNICIPAL:
            context["placeholder_points"] = [
                "La agenda comercial queda desactivada para municipios.",
                "Más adelante sumamos excepciones por fecha y turnos presenciales.",
            ]
            return TEMPLATES.TemplateResponse(
                request=request, name="dashboard_vertical_placeholder.html", context=context
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
        with suppress(ChannelDeliveryError):
            await deliver_order_notifications(
                session_factory=runtime.database.session_factory,
                settings=runtime.settings,
                order_id=order_id,
            )
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
            return await repository.list_customers(limit=limit, store_id=runtime.settings.default_store_id)

    @app.get("/api/orders", response_model=list[OrderSnapshot])
    async def read_orders(
        request: Request,
        limit: LimitQuery = 50,
        status: OrderStatusQuery = None,
    ) -> list[OrderSnapshot]:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            return await repository.list_orders(
                limit=limit,
                status=status,
                store_id=runtime.settings.default_store_id,
            )

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
                await repository.update_order_status(order_id, payload.status)
            except OrderNotFoundError as error:
                raise HTTPException(status_code=404, detail="Order not found.") from error
            await session.commit()
        with suppress(ChannelDeliveryError):
            await deliver_order_notifications(
                session_factory=runtime.database.session_factory,
                settings=runtime.settings,
                order_id=order_id,
            )
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            return await repository.get_order(order_id)

    @app.post("/webhooks/whatsapp/kapso")
    async def post_kapso_whatsapp_webhook(request: Request) -> dict[str, Any]:
        runtime = get_runtime(request)
        raw_payload = await request.body()
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from error
        payload = enrich_kapso_payload_from_headers(payload=payload, headers=request.headers)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid Kapso webhook payload.")
        payload = cast(dict[str, object], payload)

        phone_number_id = extract_kapso_phone_number_id(payload)
        gateway, store_id = await build_whatsapp_gateway_for_phone_number(
            session_factory=runtime.database.session_factory,
            settings=runtime.settings,
            phone_number_id=phone_number_id,
        )
        if gateway is None or store_id is None:
            raise HTTPException(status_code=503, detail="Kapso WhatsApp is not configured.")

        signature = request.headers.get("X-Webhook-Signature")
        if not gateway.verify_webhook(raw_payload=raw_payload, signature=signature):
            raise HTTPException(status_code=401, detail="Invalid Kapso webhook signature.")

        inbound_messages = gateway.parse_inbound_messages(payload)
        handled = 0
        for inbound_message in inbound_messages:
            result = await handle_inbound_customer_message(
                session_factory=runtime.database.session_factory,
                settings=runtime.settings,
                inbound_message=inbound_message,
                store_id=store_id,
            )
            await gateway.send_text(
                OutboundCustomerMessage(
                    channel=Channel.WHATSAPP,
                    external_user_id=inbound_message.external_user_id,
                    message_text=result.reply.reply_text,
                )
            )
            handled += 1

        return {"status": "ok", "processed": handled}

    @app.post("/api/dev/messages", response_model=AssistantTurnResult)
    async def post_dev_message(request: Request, payload: DevMessageRequest) -> AssistantTurnResult:
        runtime = get_runtime(request)
        resolved_external_user_id = resolve_dev_demo_identity(
            external_user_id=payload.external_user_id,
            phone_number=payload.phone_number,
            use_phone_identity=payload.use_phone_identity,
        )
        return await handle_inbound_customer_message(
            session_factory=runtime.database.session_factory,
            settings=runtime.settings,
            inbound_message=InboundCustomerMessage(
                channel=Channel.DEV,
                external_user_id=resolved_external_user_id,
                message_text=payload.message_text,
                metadata={"phone_number": payload.phone_number} if payload.phone_number else {},
            ),
        )

    @app.post("/api/dev/messages/{store_slug}", response_model=AssistantTurnResult)
    async def post_dev_message_for_store(
        request: Request,
        store_slug: str,
        payload: DevMessageRequest,
    ) -> AssistantTurnResult:
        runtime = get_runtime(request)
        store = await get_store_profile_by_slug_or_404(request=request, store_slug=store_slug)
        resolved_external_user_id = resolve_dev_demo_identity(
            external_user_id=payload.external_user_id,
            phone_number=payload.phone_number,
            use_phone_identity=payload.use_phone_identity,
        )
        return await handle_inbound_customer_message(
            session_factory=runtime.database.session_factory,
            settings=runtime.settings,
            inbound_message=InboundCustomerMessage(
                channel=Channel.DEV,
                external_user_id=build_scoped_dev_external_user_id(
                    store_slug=store.slug,
                    external_user_id=resolved_external_user_id,
                ),
                message_text=payload.message_text,
                metadata={"phone_number": payload.phone_number} if payload.phone_number else {},
            ),
            store_id=store.id,
        )

    @app.get("/api/dev/notifications", response_model=list[OutboundNotificationSnapshot])
    async def get_dev_notifications(
        request: Request,
        external_user_id: str,
        phone_number: str | None = None,
        use_phone_identity: bool = False,
    ) -> list[OutboundNotificationSnapshot]:
        runtime = get_runtime(request)
        payload = DevNotificationPollRequest.model_validate(
            {
                "external_user_id": external_user_id,
                "phone_number": phone_number,
                "use_phone_identity": use_phone_identity,
            }
        )
        resolved_external_user_id = resolve_dev_demo_identity(
            external_user_id=payload.external_user_id,
            phone_number=payload.phone_number,
            use_phone_identity=payload.use_phone_identity,
        )
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            notifications = await repository.list_pending_notifications(
                channel=Channel.DEV,
                external_id=resolved_external_user_id,
            )
            await session.commit()
            return notifications

    @app.get("/api/dev/notifications/{store_slug}", response_model=list[OutboundNotificationSnapshot])
    async def get_dev_notifications_for_store(
        request: Request,
        store_slug: str,
        external_user_id: str,
        phone_number: str | None = None,
        use_phone_identity: bool = False,
    ) -> list[OutboundNotificationSnapshot]:
        runtime = get_runtime(request)
        store = await get_store_profile_by_slug_or_404(request=request, store_slug=store_slug)
        payload = DevNotificationPollRequest.model_validate(
            {
                "external_user_id": external_user_id,
                "phone_number": phone_number,
                "use_phone_identity": use_phone_identity,
            }
        )
        resolved_external_user_id = resolve_dev_demo_identity(
            external_user_id=payload.external_user_id,
            phone_number=payload.phone_number,
            use_phone_identity=payload.use_phone_identity,
        )
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            notifications = await repository.list_pending_notifications(
                channel=Channel.DEV,
                external_id=build_scoped_dev_external_user_id(
                    store_slug=store.slug,
                    external_user_id=resolved_external_user_id,
                ),
            )
            await session.commit()
            return notifications

    # Include API routers
    app.include_router(kanban.router)

    return app


app = create_app()
