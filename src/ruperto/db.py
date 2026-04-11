"""Database bootstrap helpers for the shared platform and demo verticals."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ruperto.bootstrap import (
    DEFAULT_BUSINESS_HOURS,
    DEMO_MENU_ITEMS,
    DEMO_MUNICIPAL_AREAS,
    DEMO_MUNICIPAL_CATEGORIES,
)
from ruperto.config import Settings
from ruperto.models import (
    Base,
    Channel,
    ChannelProvider,
    MenuItem,
    MunicipalArea,
    StaffRole,
    StoreBusinessHours,
    StoreProfile,
    StoreVertical,
)
from ruperto.repository import BusinessRepository
from ruperto.schemas import MunicipalCategoryCreateRequest, StoreChannelConnectionUpdateRequest


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
        profile = await _ensure_default_store_profile(session=session, settings=settings)
        await _seed_demo_menu_if_needed(session=session)
        await seed_store_bootstrap_data(
            session=session,
            repository=repository,
            store_id=profile.id,
            vertical=profile.vertical,
        )
        await _seed_dashboard_admin_if_needed(repository=repository, settings=settings)
        await _seed_default_channel_connection_if_needed(repository=repository, settings=settings)
        await session.commit()
    return active_runtime


async def _ensure_default_store_profile(*, session: AsyncSession, settings: Settings) -> StoreProfile:
    """Create or refresh the default tenant profile for local development."""
    result = await session.execute(select(StoreProfile).where(StoreProfile.id == settings.default_store_id))
    profile = result.scalar_one_or_none()
    requested_slug = _slugify_tenant_name(settings.store_slug or settings.store_name)
    if profile is None:
        profile = StoreProfile(
            id=settings.default_store_id,
            slug=requested_slug,
            store_name=settings.store_name,
            bot_name=settings.bot_name,
            store_location=settings.store_location,
            store_description=settings.store_description,
            assistant_personality=settings.assistant_personality,
            vertical=settings.store_vertical,
            locale=settings.store_locale,
            transfer_alias=settings.store_transfer_alias,
        )
        session.add(profile)
        await session.flush()
        return profile

    if profile.transfer_alias is None and settings.store_transfer_alias is not None:
        profile.transfer_alias = settings.store_transfer_alias
    if not profile.slug:
        profile.slug = await _build_unique_bootstrap_slug(
            session=session,
            preferred_slug=requested_slug,
            store_id=profile.id,
        )
    return profile


async def _seed_demo_menu_if_needed(*, session: AsyncSession) -> None:
    """Insert demo menu items only when they are still missing."""
    existing_skus = set((await session.scalars(select(MenuItem.sku))).all())
    missing_menu_items = [item for item in DEMO_MENU_ITEMS if item.sku not in existing_skus]
    if not missing_menu_items:
        return

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


async def seed_store_bootstrap_data(
    *,
    session: AsyncSession,
    repository: BusinessRepository,
    store_id: int,
    vertical: StoreVertical,
) -> None:
    """Insert the store-scoped defaults required by a freshly created tenant."""
    await _seed_store_business_hours_if_needed(session=session, store_id=store_id)
    await _seed_municipal_catalog_if_needed(
        session=session,
        repository=repository,
        store_id=store_id,
        vertical=vertical,
    )


async def _seed_store_business_hours_if_needed(*, session: AsyncSession, store_id: int) -> None:
    """Insert the default weekly schedule when one tenant still has no rows."""
    hours_count = await session.scalar(
        select(func.count(StoreBusinessHours.id)).where(StoreBusinessHours.store_id == store_id)
    )
    if hours_count != 0:
        return

    session.add_all(
        [
            StoreBusinessHours(
                store_id=store_id,
                weekday=row.weekday,
                slot_index=row.slot_index,
                opens_at=row.opens_at,
                closes_at=row.closes_at,
                closed=row.closed,
            )
            for row in DEFAULT_BUSINESS_HOURS
        ]
    )


async def _seed_municipal_catalog_if_needed(
    *,
    session: AsyncSession,
    repository: BusinessRepository,
    store_id: int,
    vertical: StoreVertical,
) -> None:
    """Insert a demo municipal catalog for municipal tenants with an empty catalog."""
    if vertical != StoreVertical.MUNICIPAL:
        return

    areas_count = await session.scalar(select(func.count(MunicipalArea.id)).where(MunicipalArea.store_id == store_id))
    if areas_count != 0:
        return

    area_rows_by_key: dict[str, MunicipalArea] = {}
    for area_seed in DEMO_MUNICIPAL_AREAS:
        area_row = MunicipalArea(
            store_id=store_id,
            name=area_seed.name,
            description=area_seed.description,
            display_order=area_seed.display_order,
        )
        session.add(area_row)
        await session.flush()
        area_rows_by_key[area_seed.key] = area_row

    for category_seed in DEMO_MUNICIPAL_CATEGORIES:
        area_row = area_rows_by_key[category_seed.area_key]
        await repository.create_municipal_category(
            area_id=area_row.id,
            payload=MunicipalCategoryCreateRequest(
                name=category_seed.name,
                description=category_seed.description,
                request_kind=category_seed.request_kind,
                requires_precise_location=category_seed.requires_precise_location,
                is_fallback=category_seed.is_fallback,
                display_order=category_seed.display_order,
            ),
        )


async def _seed_dashboard_admin_if_needed(*, repository: BusinessRepository, settings: Settings) -> None:
    """Create the bootstrap dashboard owner when env-based credentials are present."""
    if not settings.dashboard_admin_email or settings.dashboard_admin_password is None:
        return

    await repository.ensure_staff_user(
        email=settings.dashboard_admin_email,
        full_name=settings.dashboard_admin_name,
        password=settings.dashboard_admin_password.get_secret_value(),
        store_id=settings.default_store_id,
        role=StaffRole.OWNER,
    )


async def _seed_default_channel_connection_if_needed(
    *,
    repository: BusinessRepository,
    settings: Settings,
) -> None:
    """Create the default Kapso connection from env vars when provided."""
    if settings.kapso_api_key is None or settings.kapso_phone_number_id is None:
        return

    existing_channel = await repository.get_store_channel_connection(
        store_id=settings.default_store_id,
        channel=Channel.WHATSAPP,
        provider=ChannelProvider.KAPSO,
    )
    if existing_channel.id is not None:
        return

    await repository.update_store_channel_connection(
        store_id=settings.default_store_id,
        channel=Channel.WHATSAPP,
        provider=ChannelProvider.KAPSO,
        payload=StoreChannelConnectionUpdateRequest(
            phone_number_id=settings.kapso_phone_number_id,
            api_key=settings.kapso_api_key.get_secret_value(),
            webhook_secret=(
                settings.kapso_webhook_secret.get_secret_value() if settings.kapso_webhook_secret is not None else None
            ),
            is_active=True,
        ),
    )


async def ping_database(runtime: DatabaseRuntime) -> None:
    """Verify the configured database is reachable."""
    async with runtime.session_factory() as session:
        await session.execute(select(1))


def _ensure_schema_columns(connection: Connection) -> None:
    """Backfill additive columns for databases created by older MVP versions."""
    inspector = inspect(connection)
    _ensure_store_profile_columns(connection, inspector=inspector)
    _ensure_customer_order_columns(connection, inspector=inspector)
    _ensure_customer_tenancy_columns(connection, inspector=inspector)
    _ensure_outbound_notification_table(connection, inspector=inspector)
    _ensure_conversation_columns(connection, inspector=inspector)
    _ensure_conversation_state_columns(connection, inspector=inspector)
    _ensure_store_channel_connection_table(connection, inspector=inspector)
    _ensure_store_business_hours_shape(connection, inspector=inspector)
    _ensure_municipal_category_columns(connection, inspector=inspector)


def _ensure_store_profile_columns(connection: Connection, *, inspector: Inspector) -> None:
    """Additive migration for store profile fields."""
    if "store_profile" in inspector.get_table_names():
        store_profile_columns = {column["name"] for column in inspector.get_columns("store_profile")}
        if "transfer_alias" not in store_profile_columns:
            connection.execute(text("ALTER TABLE store_profile ADD COLUMN transfer_alias VARCHAR(120)"))
        if "vertical" not in store_profile_columns:
            connection.execute(
                text("ALTER TABLE store_profile ADD COLUMN vertical VARCHAR(32) NOT NULL DEFAULT 'ordering'")
            )
        if "slug" not in store_profile_columns:
            connection.execute(text("ALTER TABLE store_profile ADD COLUMN slug VARCHAR(120)"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_store_profile_slug ON store_profile (slug)"))


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


def _ensure_customer_tenancy_columns(connection: Connection, *, inspector: Inspector) -> None:
    """Backfill store scoping columns for customers and identities."""
    if "customer" in inspector.get_table_names():
        customer_columns = {column["name"] for column in inspector.get_columns("customer")}
        if "store_id" not in customer_columns:
            connection.execute(text("ALTER TABLE customer ADD COLUMN store_id INTEGER REFERENCES store_profile (id)"))
            connection.execute(text("UPDATE customer SET store_id = 1 WHERE store_id IS NULL"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_customer_store_id ON customer (store_id)"))

    if "customer_identity" in inspector.get_table_names():
        identity_columns = {column["name"] for column in inspector.get_columns("customer_identity")}
        if "store_id" not in identity_columns:
            connection.execute(
                text("ALTER TABLE customer_identity ADD COLUMN store_id INTEGER REFERENCES store_profile (id)")
            )
            connection.execute(text("UPDATE customer_identity SET store_id = 1 WHERE store_id IS NULL"))
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_customer_identity_store_id ON customer_identity (store_id)")
            )


def _ensure_outbound_notification_table(connection: Connection, *, inspector: Inspector) -> None:
    """Create the outbound notification table on older databases."""
    if "outbound_notification" not in inspector.get_table_names():
        connection.execute(
            text(
                """
                CREATE TABLE outbound_notification (
                    id INTEGER NOT NULL PRIMARY KEY,
                    order_id INTEGER,
                    municipal_case_id INTEGER,
                    conversation_id INTEGER NOT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    message_text TEXT NOT NULL,
                    delivered_at DATETIME,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES customer_order (id) ON DELETE CASCADE,
                    FOREIGN KEY(municipal_case_id) REFERENCES municipal_case (id) ON DELETE CASCADE,
                    FOREIGN KEY(conversation_id) REFERENCES conversation (id) ON DELETE CASCADE,
                    CONSTRAINT uq_outbound_notification_order_event UNIQUE (order_id, event_type),
                    CONSTRAINT uq_outbound_notification_case_event UNIQUE (municipal_case_id, event_type)
                )
                """
            )
        )
        return

    notification_columns = {column["name"] for column in inspector.get_columns("outbound_notification")}
    order_id_column = next(
        column for column in inspector.get_columns("outbound_notification") if column["name"] == "order_id"
    )
    if "municipal_case_id" not in notification_columns or not order_id_column.get("nullable", True):
        _migrate_outbound_notification_table(connection)


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


def _ensure_conversation_state_columns(connection: Connection, *, inspector: Inspector) -> None:
    """Additive migration for persisted human-handoff conversation state."""
    if "conversation_state" not in inspector.get_table_names():
        return

    state_columns = {column["name"] for column in inspector.get_columns("conversation_state")}
    if "awaiting_human" not in state_columns:
        connection.execute(text("ALTER TABLE conversation_state ADD COLUMN awaiting_human BOOLEAN NOT NULL DEFAULT 0"))
    if "handoff_reason" not in state_columns:
        connection.execute(text("ALTER TABLE conversation_state ADD COLUMN handoff_reason TEXT"))
    if "handoff_requested_at" not in state_columns:
        connection.execute(text("ALTER TABLE conversation_state ADD COLUMN handoff_requested_at DATETIME"))
    if "handoff_latest_customer_message" not in state_columns:
        connection.execute(text("ALTER TABLE conversation_state ADD COLUMN handoff_latest_customer_message TEXT"))
    if "handoff_last_customer_message_at" not in state_columns:
        connection.execute(text("ALTER TABLE conversation_state ADD COLUMN handoff_last_customer_message_at DATETIME"))
    if "handoff_last_operator_reply_at" not in state_columns:
        connection.execute(text("ALTER TABLE conversation_state ADD COLUMN handoff_last_operator_reply_at DATETIME"))


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


def _ensure_municipal_category_columns(connection: Connection, *, inspector: Inspector) -> None:
    """Additive migration for municipal category semantics."""
    if "municipal_category" in inspector.get_table_names():
        municipal_category_columns = {column["name"] for column in inspector.get_columns("municipal_category")}
        if "request_kind" not in municipal_category_columns:
            connection.execute(
                text("ALTER TABLE municipal_category ADD COLUMN request_kind VARCHAR(32) NOT NULL DEFAULT 'complaint'")
            )
        connection.execute(
            text(
                """
                UPDATE municipal_category
                SET request_kind = 'request'
                WHERE name IN ('Solicitud de lomo de burro', 'Falta de agua', 'Poda o ramas')
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE municipal_category
                SET request_kind = 'request'
                WHERE name = 'Otro'
                  AND area_id IN (
                      SELECT id
                      FROM municipal_area
                      WHERE name IN ('Solicitud de agua', 'Higiene urbana')
                  )
                """
            )
        )


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


def _migrate_outbound_notification_table(connection: Connection) -> None:
    """Migrate outbound notifications to support municipal case events."""
    connection.execute(text("ALTER TABLE outbound_notification RENAME TO outbound_notification_legacy"))
    connection.execute(
        text(
            """
            CREATE TABLE outbound_notification (
                id INTEGER NOT NULL PRIMARY KEY,
                order_id INTEGER,
                municipal_case_id INTEGER,
                conversation_id INTEGER NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                message_text TEXT NOT NULL,
                delivered_at DATETIME,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(order_id) REFERENCES customer_order (id) ON DELETE CASCADE,
                FOREIGN KEY(municipal_case_id) REFERENCES municipal_case (id) ON DELETE CASCADE,
                FOREIGN KEY(conversation_id) REFERENCES conversation (id) ON DELETE CASCADE,
                CONSTRAINT uq_outbound_notification_order_event UNIQUE (order_id, event_type),
                CONSTRAINT uq_outbound_notification_case_event UNIQUE (municipal_case_id, event_type)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO outbound_notification (
                id,
                order_id,
                municipal_case_id,
                conversation_id,
                event_type,
                message_text,
                delivered_at,
                created_at
            )
            SELECT
                id,
                order_id,
                NULL,
                conversation_id,
                event_type,
                message_text,
                delivered_at,
                created_at
            FROM outbound_notification_legacy
            """
        )
    )
    connection.execute(text("DROP TABLE outbound_notification_legacy"))


async def _build_unique_bootstrap_slug(
    *,
    session: AsyncSession,
    preferred_slug: str,
    store_id: int,
) -> str:
    """Return a unique slug for a bootstrapped tenant, allowing the current row."""
    candidate = preferred_slug
    suffix = 2
    while True:
        existing = await session.scalar(select(StoreProfile.id).where(StoreProfile.slug == candidate))
        if existing is None or existing == store_id:
            return candidate
        candidate = f"{preferred_slug}-{suffix}"
        suffix += 1


def _slugify_tenant_name(value: str) -> str:
    """Convert a configured tenant name into a stable public slug."""
    normalized = unicodedata.normalize("NFKD", value.strip()).encode("ascii", "ignore").decode("ascii")
    compact = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return compact or "tenant"
