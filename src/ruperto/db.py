"""Database bootstrap helpers for the ordering MVP."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ruperto.bootstrap import DEFAULT_BUSINESS_HOURS, DEMO_MENU_ITEMS
from ruperto.config import Settings
from ruperto.models import Base, MenuItem, StoreBusinessHours, StoreProfile


@dataclass(slots=True)
class DatabaseRuntime:
    """Database primitives shared across the FastAPI app lifespan."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def create_database_runtime(settings: Settings) -> DatabaseRuntime:
    """Create the async engine and session factory for the configured database."""
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return DatabaseRuntime(engine=engine, session_factory=session_factory)


async def init_database(*, settings: Settings, runtime: DatabaseRuntime | None = None) -> DatabaseRuntime:
    """Create tables and ensure the store profile exists.

    If a runtime is not provided, this function creates and returns one so the
    caller can keep using it afterwards.
    """

    active_runtime = runtime or create_database_runtime(settings)
    async with active_runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_ensure_schema_columns)

    async with active_runtime.session_factory() as session:
        result = await session.execute(select(StoreProfile).where(StoreProfile.id == settings.default_store_id))
        profile = result.scalar_one_or_none()
        if profile is None:
            session.add(
                StoreProfile(
                    id=settings.default_store_id,
                    store_name=settings.store_name,
                    bot_name=settings.bot_name,
                    store_location=settings.store_location,
                    store_description=settings.store_description,
                    assistant_personality=settings.assistant_personality,
                    locale=settings.store_locale,
                    transfer_alias=settings.store_transfer_alias,
                )
            )
        elif profile.transfer_alias is None and settings.store_transfer_alias is not None:
            profile.transfer_alias = settings.store_transfer_alias
        existing_skus = set((await session.scalars(select(MenuItem.sku))).all())
        missing_menu_items = [item for item in DEMO_MENU_ITEMS if item.sku not in existing_skus]
        if missing_menu_items:
            session.add_all(
                [
                    MenuItem(
                        sku=item.sku,
                        name=item.name,
                        description=item.description,
                        category=item.category,
                        price_cents=item.price_cents,
                        image_url=item.image_url,
                    )
                    for item in missing_menu_items
                ]
            )
        hours_count = await session.scalar(select(func.count(StoreBusinessHours.id)))
        if hours_count == 0:
            session.add_all(
                [
                    StoreBusinessHours(
                        store_id=settings.default_store_id,
                        weekday=row.weekday,
                        opens_at=row.opens_at,
                        closes_at=row.closes_at,
                        closed=row.closed,
                    )
                    for row in DEFAULT_BUSINESS_HOURS
                ]
            )
        await session.commit()
    return active_runtime


async def ping_database(runtime: DatabaseRuntime) -> None:
    """Verify the configured database is reachable."""
    async with runtime.session_factory() as session:
        await session.execute(select(1))


def _ensure_schema_columns(connection: Connection) -> None:
    """Backfill additive columns for databases created by older MVP versions."""
    inspector = inspect(connection)
    if "store_profile" in inspector.get_table_names():
        store_profile_columns = {column["name"] for column in inspector.get_columns("store_profile")}
        if "transfer_alias" not in store_profile_columns:
            connection.execute(text("ALTER TABLE store_profile ADD COLUMN transfer_alias VARCHAR(120)"))

    if "customer_order" in inspector.get_table_names():
        order_columns = {column["name"] for column in inspector.get_columns("customer_order")}
        if "requested_ready_at" not in order_columns:
            connection.execute(text("ALTER TABLE customer_order ADD COLUMN requested_ready_at DATETIME"))
        if "preparation_starts_at" not in order_columns:
            connection.execute(text("ALTER TABLE customer_order ADD COLUMN preparation_starts_at DATETIME"))
