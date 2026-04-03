"""FastAPI application factory for the Ruperto service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request

from ruperto import get_version
from ruperto.assistant import OrderingAssistantService
from ruperto.config import Settings
from ruperto.db import DatabaseRuntime, create_database_runtime, init_database, ping_database
from ruperto.models import Channel, OrderStatus
from ruperto.repository import BusinessRepository
from ruperto.schemas import (
    AssistantTurnResult,
    CustomerSnapshot,
    DevMessageRequest,
    MenuItemSnapshot,
    OrderSnapshot,
    StoreProfileSnapshot,
)


@dataclass(slots=True)
class ApplicationRuntime:
    """Long-lived objects attached to the FastAPI lifespan."""

    settings: Settings
    database: DatabaseRuntime


def get_runtime(request: Request) -> ApplicationRuntime:
    """Return the initialized runtime from the current request."""
    return request.app.state.runtime


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


def create_app(settings: Settings | None = None) -> FastAPI:
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

    @app.get("/api/store-profile", response_model=StoreProfileSnapshot)
    async def read_store_profile(request: Request) -> StoreProfileSnapshot:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            return await repository.get_store_profile()

    @app.get("/api/menu-items", response_model=list[MenuItemSnapshot])
    async def read_menu_items(
        request: Request,
        only_available: AvailableMenuQuery = True,
    ) -> list[MenuItemSnapshot]:
        runtime = get_runtime(request)
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            return await repository.list_menu_items(only_available=only_available)

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
