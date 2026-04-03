"""FastAPI application factory for the Ruperto service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request

from ruperto import get_version
from ruperto.config import Settings
from ruperto.db import DatabaseRuntime, create_database_runtime, init_database, ping_database


@dataclass(slots=True)
class ApplicationRuntime:
    """Long-lived objects attached to the FastAPI lifespan."""

    settings: Settings
    database: DatabaseRuntime


def get_runtime(request: Request) -> ApplicationRuntime:
    """Return the initialized runtime from the current request."""
    return request.app.state.runtime


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

    return app


app = create_app()
