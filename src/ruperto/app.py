"""FastAPI application factory for the Ruperto service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ruperto import get_version
from ruperto.assistant import OrderingAssistantService
from ruperto.config import Settings
from ruperto.db import DatabaseRuntime, create_database_runtime, init_database, ping_database
from ruperto.models import Channel, OrderStatus
from ruperto.repository import BusinessRepository, OrderNotFoundError
from ruperto.schemas import (
    AssistantTurnResult,
    CustomerSnapshot,
    DevMessageRequest,
    MenuItemSnapshot,
    OrderSnapshot,
    OrderStatusUpdateRequest,
    StoreBusinessHoursSnapshot,
    StoreBusinessHoursUpdateEntry,
    StoreBusinessHoursUpdateRequest,
    StoreProfileSnapshot,
    StoreProfileUpdateRequest,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
DASHBOARD_ORDER_STATUS_LABELS = {
    OrderStatus.DRAFT: "Draft",
    OrderStatus.CONFIRMED: "Confirmed",
    OrderStatus.IN_PREPARATION: "In preparation",
    OrderStatus.ALMOST_READY: "Almost ready",
    OrderStatus.READY_FOR_PICKUP: "Ready for pickup",
    OrderStatus.OUT_FOR_DELIVERY: "Out for delivery",
    OrderStatus.DELIVERED: "Delivered",
    OrderStatus.CANCELLED: "Cancelled",
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
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}
DASHBOARD_FLASH_MESSAGES = {
    "profile-updated": "Store profile updated.",
    "hours-updated": "Opening hours updated.",
    "order-updated": "Order status updated.",
}


@dataclass(slots=True)
class ApplicationRuntime:
    """Long-lived objects attached to the FastAPI lifespan."""

    settings: Settings
    database: DatabaseRuntime


def get_runtime(request: Request) -> ApplicationRuntime:
    """Return the initialized runtime from the current request."""
    return request.app.state.runtime


def dashboard_redirect(*, flash: str | None = None) -> RedirectResponse:
    """Redirect back to the dashboard using the PRG pattern."""
    query = urlencode({"flash": flash}) if flash is not None else ""
    url = f"/dashboard?{query}" if query else "/dashboard"
    return RedirectResponse(url=url, status_code=303)


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
        "delivery_type": order.delivery_type.value.replace("_", " ").title()
        if order.delivery_type is not None
        else "—",
        "delivery_address": order.delivery_address or "—",
        "payment_method": order.payment_method.value.replace("_", " ").title()
        if order.payment_method is not None
        else "—",
        "requested_ready_at": format_dashboard_datetime(order.requested_ready_at, timezone_name),
        "preparation_starts_at": format_dashboard_datetime(order.preparation_starts_at, timezone_name),
        "total_amount_display": order.total_amount_display,
        "item_summary": ", ".join(f"{item.quantity} x {item.name}" for item in order.items),
    }


def serialize_store_hours_for_dashboard(hours: list[StoreBusinessHoursSnapshot]) -> list[dict[str, Any]]:
    """Return the weekly schedule ordered and ready for form rendering."""
    return [
        {
            "weekday": row.weekday,
            "label": DASHBOARD_WEEKDAYS[row.weekday],
            "opens_at": row.opens_at or "",
            "closes_at": row.closes_at or "",
            "closed": row.closed,
        }
        for row in sorted(hours, key=lambda item: item.weekday)
    ]


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
    app = FastAPI(
        title="Ruperto API",
        version=get_version(),
        lifespan=lifespan,
        summary="Conversational ordering backend for food businesses.",
    )
    app.state.settings = settings or Settings()

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

    @app.get("/dashboard", response_class=HTMLResponse)
    async def read_dashboard(
        request: Request,
        limit: LimitQuery = 20,
        flash: str | None = None,
    ) -> HTMLResponse:
        runtime = get_runtime(request)
        store_id = runtime.settings.default_store_id
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=store_id)
            hours = await repository.list_store_business_hours(store_id=store_id)
            orders = await repository.list_orders(limit=limit)
            customers = await repository.list_customers(limit=12)
            menu_items = await repository.list_menu_items(only_available=False)

        context = {
            "request": request,
            "store": store,
            "hours": serialize_store_hours_for_dashboard(hours),
            "orders": [serialize_order_for_dashboard(order, runtime.settings.store_timezone) for order in orders],
            "customers": customers,
            "order_statuses": [
                {"value": status.value, "label": DASHBOARD_ORDER_STATUS_LABELS[status]} for status in OrderStatus
            ],
            "flash_message": DASHBOARD_FLASH_MESSAGES.get(flash or ""),
            "stats": {
                "orders": len(orders),
                "customers": len(customers),
                "available_menu_items": sum(1 for item in menu_items if item.available),
                "catalog_items": len(menu_items),
            },
            "settings_snapshot": runtime.settings.public_settings(),
        }
        return TEMPLATES.TemplateResponse(request=request, name="dashboard.html", context=context)

    @app.post("/dashboard/store-profile")
    async def post_dashboard_store_profile(request: Request) -> RedirectResponse:
        runtime = get_runtime(request)
        form = await request.form()
        try:
            payload = StoreProfileUpdateRequest(
                store_name=str(form.get("store_name", "")),
                bot_name=str(form.get("bot_name", "")),
                store_location=str(form.get("store_location", "")) or None,
                store_description=str(form.get("store_description", "")),
                assistant_personality=str(form.get("assistant_personality", "")),
                transfer_alias=str(form.get("transfer_alias", "")) or None,
            )
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors()) from error

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            await repository.update_store_profile(payload, store_id=runtime.settings.default_store_id)
            await session.commit()
        return dashboard_redirect(flash="profile-updated")

    @app.post("/dashboard/store-hours")
    async def post_dashboard_store_hours(request: Request) -> RedirectResponse:
        runtime = get_runtime(request)
        form = await request.form()
        payload = StoreBusinessHoursUpdateRequest(
            hours=[
                StoreBusinessHoursUpdateEntry(
                    weekday=weekday,
                    opens_at=None if f"closed_{weekday}" in form else str(form.get(f"opens_at_{weekday}", "")) or None,
                    closes_at=None
                    if f"closed_{weekday}" in form
                    else str(form.get(f"closes_at_{weekday}", "")) or None,
                    closed=f"closed_{weekday}" in form,
                )
                for weekday in DASHBOARD_WEEKDAYS
            ]
        )

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            await repository.replace_store_business_hours(
                hours=[
                    StoreBusinessHoursSnapshot(
                        id=0,
                        store_id=runtime.settings.default_store_id,
                        weekday=row.weekday,
                        opens_at=row.opens_at,
                        closes_at=row.closes_at,
                        closed=row.closed,
                    )
                    for row in payload.hours
                ],
                store_id=runtime.settings.default_store_id,
            )
            await session.commit()
        return dashboard_redirect(flash="hours-updated")

    @app.post("/dashboard/orders/{order_id}/status")
    async def post_dashboard_order_status(request: Request, order_id: int) -> RedirectResponse:
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
        return dashboard_redirect(flash="order-updated")

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
            updated_hours = await repository.replace_store_business_hours(
                hours=[
                    StoreBusinessHoursSnapshot(
                        id=0,
                        store_id=runtime.settings.default_store_id,
                        weekday=row.weekday,
                        opens_at=row.opens_at,
                        closes_at=row.closes_at,
                        closed=row.closed,
                    )
                    for row in payload.hours
                ],
                store_id=runtime.settings.default_store_id,
            )
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

    return app


app = create_app()
