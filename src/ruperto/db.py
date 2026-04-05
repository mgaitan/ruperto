"""Database bootstrap helpers for the ordering MVP."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ruperto.bootstrap import DEFAULT_BUSINESS_HOURS, DEMO_MENU_ITEMS
from ruperto.config import Settings
from ruperto.models import (
    Base,
    Channel,
    ChannelProvider,
    MenuItem,
    StaffRole,
    StoreBusinessHours,
    StoreProfile,
)
from ruperto.repository import BusinessRepository
from ruperto.schemas import StoreChannelConnectionUpdateRequest


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
        repository = BusinessRepository(session)
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
            await session.flush()
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
                        slot_index=row.slot_index,
                        opens_at=row.opens_at,
                        closes_at=row.closes_at,
                        closed=row.closed,
                    )
                    for row in DEFAULT_BUSINESS_HOURS
                ]
            )
        if settings.dashboard_admin_email and settings.dashboard_admin_password is not None:
            await repository.ensure_staff_user(
                email=settings.dashboard_admin_email,
                full_name=settings.dashboard_admin_name,
                password=settings.dashboard_admin_password.get_secret_value(),
                store_id=settings.default_store_id,
                role=StaffRole.OWNER,
            )
        if settings.kapso_api_key is not None and settings.kapso_phone_number_id is not None:
            existing_channel = await repository.get_store_channel_connection(
                store_id=settings.default_store_id,
                channel=Channel.WHATSAPP,
                provider=ChannelProvider.KAPSO,
            )
            if existing_channel.id is None:
                await repository.update_store_channel_connection(
                    store_id=settings.default_store_id,
                    channel=Channel.WHATSAPP,
                    provider=ChannelProvider.KAPSO,
                    payload=StoreChannelConnectionUpdateRequest(
                        phone_number_id=settings.kapso_phone_number_id,
                        api_key=settings.kapso_api_key.get_secret_value(),
                        webhook_secret=(
                            settings.kapso_webhook_secret.get_secret_value()
                            if settings.kapso_webhook_secret is not None
                            else None
                        ),
                        is_active=True,
                    ),
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
    _ensure_store_profile_columns(connection, inspector=inspector)
    _ensure_customer_order_columns(connection, inspector=inspector)
    _ensure_outbound_notification_table(connection, inspector=inspector)
    _ensure_conversation_columns(connection, inspector=inspector)
    _ensure_store_channel_connection_table(connection, inspector=inspector)
    _ensure_store_business_hours_shape(connection, inspector=inspector)


def _ensure_store_profile_columns(connection: Connection, *, inspector: Inspector) -> None:
    """Additive migration for store profile fields."""
    if "store_profile" in inspector.get_table_names():
        store_profile_columns = {column["name"] for column in inspector.get_columns("store_profile")}
        if "transfer_alias" not in store_profile_columns:
            connection.execute(text("ALTER TABLE store_profile ADD COLUMN transfer_alias VARCHAR(120)"))


def _ensure_customer_order_columns(connection: Connection, *, inspector: Inspector) -> None:
    """Additive migration for scheduling and notification order fields."""
    if "customer_order" in inspector.get_table_names():
        order_columns = {column["name"] for column in inspector.get_columns("customer_order")}
        if "notify_when_ready" not in order_columns:
            connection.execute(
                text("ALTER TABLE customer_order ADD COLUMN notify_when_ready BOOLEAN NOT NULL DEFAULT 1")
            )
        if "requested_ready_at" not in order_columns:
            connection.execute(text("ALTER TABLE customer_order ADD COLUMN requested_ready_at DATETIME"))
        if "preparation_starts_at" not in order_columns:
            connection.execute(text("ALTER TABLE customer_order ADD COLUMN preparation_starts_at DATETIME"))


def _ensure_outbound_notification_table(connection: Connection, *, inspector: Inspector) -> None:
    """Create the outbound notification table on older databases."""
    if "outbound_notification" not in inspector.get_table_names():
        connection.execute(
            text(
                """
                CREATE TABLE outbound_notification (
                    id INTEGER NOT NULL PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    conversation_id INTEGER NOT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    message_text TEXT NOT NULL,
                    delivered_at DATETIME,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES customer_order (id) ON DELETE CASCADE,
                    FOREIGN KEY(conversation_id) REFERENCES conversation (id) ON DELETE CASCADE,
                    CONSTRAINT uq_outbound_notification_order_event UNIQUE (order_id, event_type)
                )
                """
            )
        )


def _ensure_conversation_columns(connection: Connection, *, inspector: Inspector) -> None:
    """Backfill conversation tenancy data for older installs."""
    if "conversation" in inspector.get_table_names():
        conversation_columns = {column["name"] for column in inspector.get_columns("conversation")}
        if "store_id" not in conversation_columns:
            connection.execute(
                text("ALTER TABLE conversation ADD COLUMN store_id INTEGER REFERENCES store_profile (id)")
            )
            connection.execute(text("UPDATE conversation SET store_id = 1 WHERE store_id IS NULL"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_conversation_store_id ON conversation (store_id)"))


def _ensure_store_channel_connection_table(connection: Connection, *, inspector: Inspector) -> None:
    """Create the store-scoped channel-connection table on older installs."""
    if "store_channel_connection" not in inspector.get_table_names():
        connection.execute(
            text(
                """
                CREATE TABLE store_channel_connection (
                    id INTEGER NOT NULL PRIMARY KEY,
                    store_id INTEGER NOT NULL,
                    channel VARCHAR(32) NOT NULL,
                    provider VARCHAR(32) NOT NULL,
                    phone_number_id VARCHAR(120),
                    api_key TEXT,
                    webhook_secret TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    FOREIGN KEY(store_id) REFERENCES store_profile (id) ON DELETE CASCADE,
                    CONSTRAINT uq_store_channel_connection_store UNIQUE (store_id, channel, provider),
                    CONSTRAINT uq_store_channel_connection_phone UNIQUE (channel, provider, phone_number_id)
                )
                """
            )
        )


def _ensure_store_business_hours_shape(connection: Connection, *, inspector: Inspector) -> None:
    """Upgrade weekly business hours to the multi-slot shape when needed."""
    if "store_business_hours" in inspector.get_table_names():
        hours_columns = {column["name"] for column in inspector.get_columns("store_business_hours")}
        if "slot_index" not in hours_columns:
            _migrate_store_business_hours_table(connection)


def _migrate_store_business_hours_table(connection: Connection) -> None:
    """Migrate business hours to support multiple daily slots."""
    connection.execute(text("ALTER TABLE store_business_hours RENAME TO store_business_hours_legacy"))
    connection.execute(
        text(
            """
            CREATE TABLE store_business_hours (
                id INTEGER NOT NULL PRIMARY KEY,
                store_id INTEGER NOT NULL,
                weekday INTEGER NOT NULL,
                slot_index INTEGER NOT NULL DEFAULT 0,
                opens_at TIME,
                closes_at TIME,
                closed BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(store_id) REFERENCES store_profile (id) ON DELETE CASCADE,
                CONSTRAINT uq_store_business_hours UNIQUE (store_id, weekday, slot_index)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO store_business_hours (
                id,
                store_id,
                weekday,
                slot_index,
                opens_at,
                closes_at,
                closed,
                created_at,
                updated_at
            )
            SELECT
                id,
                store_id,
                weekday,
                0,
                opens_at,
                closes_at,
                closed,
                created_at,
                updated_at
            FROM store_business_hours_legacy
            """
        )
    )
    connection.execute(text("DROP TABLE store_business_hours_legacy"))
