"""Tests for the MVP repository layer."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import create_engine, func, select, text

from ruperto.config import Settings
from ruperto.db import DatabaseRuntime, _ensure_schema_columns, create_database_runtime, init_database, ping_database
from ruperto.models import (
    Channel,
    ChannelProvider,
    DeliveryType,
    MenuItem,
    MunicipalCaseStatus,
    Order,
    OrderStatus,
    OutboundNotification,
    PaymentMethod,
    StaffRole,
    StaffUser,
    StoreVertical,
)
from ruperto.repository import (
    STORE_HOURS_SLOT_ORDER_MESSAGE,
    STORE_HOURS_SLOT_REQUIRES_BOTH_TIMES_MESSAGE,
    BusinessRepository,
    MunicipalAreaNotFoundError,
    MunicipalCaseNotFoundError,
    MunicipalCategoryMismatchError,
    MunicipalCategoryNotFoundError,
    normalize_phone_number,
    round_delay_minutes,
)
from ruperto.schemas import (
    MunicipalAreaCreateRequest,
    MunicipalCaseCreateRequest,
    MunicipalCaseStatusUpdateRequest,
    MunicipalCategoryCreateRequest,
    StoreBusinessHoursSnapshot,
    StoreChannelConnectionUpdateRequest,
    StoreProfileUpdateRequest,
)

pytestmark = pytest.mark.anyio
EXPECTED_HISTORY_MESSAGES = 2
EXPECTED_ORDER_TOTAL = 1900000
EXPECTED_SINGLE_BURGER_TOTAL = 950000
EXPECTED_DELAY_MINUTES = 22
ROUNDED_DELAY_MINUTES = 25
DEFAULT_DELAY_MINUTES = 25
DRAFT_DELAY_MINUTES = 15
PICKUP_DELAY_MINUTES = 15
LARGE_ORDER_DELAY_MINUTES = 31
ROUNDED_LARGE_ORDER_DELAY_MINUTES = 35
KITCHEN_LOAD_DELAY_MINUTES = 18
ROUNDED_KITCHEN_LOAD_DELAY_MINUTES = 20
EMPTY_DRAFT_DELAY_MINUTES = 15
MIN_ROUNDED_DELAY_MINUTES = 5
DEFAULT_WEEKLY_HOURS = 7
MIN_MENU_ITEMS = 35
TENANT_STORE_ID = 7
STORE_TIMEZONE = "America/Argentina/Cordoba"


async def build_repository(tmp_path: Path) -> tuple[BusinessRepository, DatabaseRuntime]:
    """Create an initialized repository backed by a temporary SQLite database."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'repo.db'}",
        store_name="Rotisería Test",
        bot_name="Ruperto Test",
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    session = runtime.session_factory()
    repository = BusinessRepository(session)
    return repository, runtime


async def build_municipal_repository(tmp_path: Path) -> tuple[BusinessRepository, DatabaseRuntime]:
    """Create an initialized repository for the municipal vertical."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'municipal.db'}",
        store_name="Municipio Test",
        bot_name="Moony Test",
        store_vertical=StoreVertical.MUNICIPAL,
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    session = runtime.session_factory()
    repository = BusinessRepository(session)
    return repository, runtime


async def close_repository(repository: BusinessRepository, runtime: DatabaseRuntime):
    """Close the session used by a test repository."""
    await repository.session.close()
    await runtime.engine.dispose()


async def test_bootstrap_seeds_store_menu_and_empty_memory(tmp_path: Path):
    """Database bootstrap creates the store profile and demo menu."""
    repository, runtime = await build_repository(tmp_path)

    store = await repository.get_store_profile()
    menu = await repository.list_menu_items()
    search = await repository.search_menu_items("hamburguesa")
    customer = await repository.get_or_create_customer(
        channel=Channel.WHATSAPP,
        external_id="+54 351 555 7788",
        phone_number="+54 351 555 7788",
    )
    memory = await repository.get_customer_memory(customer.id)

    assert store.locale == "es-AR"
    assert len(await repository.list_store_business_hours()) == DEFAULT_WEEKLY_HOURS
    assert len(menu) >= MIN_MENU_ITEMS
    assert {item.sku for item in search} >= {"hamburguesa-completa", "hamburguesa-doble", "hamburguesa-bbq"}
    assert any(item.category == "Bebidas" for item in menu)
    assert any(item.category == "Postres" for item in menu)
    assert memory.favorite_item_name is None
    assert normalize_phone_number("+54 351-555-7788") == "+543515557788"
    assert normalize_phone_number("351 555 7788") == "3515557788"
    assert normalize_phone_number("   ") is None

    await close_repository(repository, runtime)


async def test_round_delay_minutes_never_returns_less_than_five_minutes():
    """Delay rounding keeps zero or negative estimates user-friendly."""
    assert round_delay_minutes(0) == MIN_ROUNDED_DELAY_MINUTES
    assert round_delay_minutes(-7) == MIN_ROUNDED_DELAY_MINUTES


async def test_store_profile_can_be_updated_with_empty_optional_fields(tmp_path: Path):
    """Store customization trims optional empty fields down to null."""
    repository, runtime = await build_repository(tmp_path)

    updated = await repository.update_store_profile(
        StoreProfileUpdateRequest(
            store_name="Panel Rotisería",
            bot_name="Panel Bot",
            store_location=None,
            store_description="Updated from the dashboard.",
            assistant_personality="Steady and direct.",
            vertical=StoreVertical.MUNICIPAL,
            transfer_alias=None,
        )
    )

    assert updated.store_name == "Panel Rotisería"
    assert updated.store_location is None
    assert updated.transfer_alias is None
    assert updated.vertical == StoreVertical.MUNICIPAL

    await close_repository(repository, runtime)


async def test_store_channel_connection_can_be_created_and_loaded(tmp_path: Path):
    """Store-scoped channel credentials are persisted separately from app settings."""
    repository, runtime = await build_repository(tmp_path)

    initial = await repository.get_store_channel_connection(
        store_id=1,
        channel=Channel.WHATSAPP,
        provider=ChannelProvider.KAPSO,
    )
    updated = await repository.update_store_channel_connection(
        store_id=1,
        channel=Channel.WHATSAPP,
        provider=ChannelProvider.KAPSO,
        payload=StoreChannelConnectionUpdateRequest(
            phone_number_id="phone-id-1",
            api_key="kapso-key-1",
            webhook_secret="kapso-secret-1",
            is_active=True,
        ),
    )
    runtime_config = await repository.get_store_channel_runtime_config(
        store_id=1,
        channel=Channel.WHATSAPP,
        provider=ChannelProvider.KAPSO,
    )
    by_phone = await repository.get_channel_runtime_config_by_phone_number(
        channel=Channel.WHATSAPP,
        provider=ChannelProvider.KAPSO,
        phone_number_id="phone-id-1",
    )

    assert initial.id is None
    assert updated.is_active is True
    assert updated.api_key_configured is True
    assert updated.webhook_secret_configured is True
    assert runtime_config is not None
    assert runtime_config.phone_number_id == "phone-id-1"
    assert by_phone is not None
    assert by_phone.store_id == 1

    await close_repository(repository, runtime)


async def test_reset_current_order_rebuilds_a_draft_from_scratch(tmp_path: Path):
    """Draft corrections can clear current items before the order is rebuilt."""
    repository, runtime = await build_repository(tmp_path)

    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="reset-draft-user")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="reset-draft-user",
        customer_id=customer.id,
    )
    await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="lomito-especial",
        quantity=2,
    )

    reset_order = await repository.reset_current_order(customer.id, conversation.id)

    assert reset_order.items == []
    assert reset_order.total_amount_cents == 0
    assert reset_order.total_amount_display == "$ 0"

    rebuilt_order = await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="lomito-completo",
        quantity=1,
    )
    assert [item.name for item in rebuilt_order.items] == ["Lomito completo"]

    await close_repository(repository, runtime)


async def test_get_or_create_conversation_updates_store_scope(tmp_path: Path):
    """Existing conversations can be reassigned to another store when the active local changes."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="tenant-switch-user")
    second_store = await repository.create_store_profile(
        store_name="Sucursal Dos",
        bot_name="Ruperto Dos",
        store_description="Segundo local para scope de conversación.",
        assistant_personality="Calm and direct.",
    )
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="tenant-switch-user",
        customer_id=customer.id,
        store_id=1,
    )

    reassigned = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="tenant-switch-user",
        customer_id=customer.id,
        store_id=second_store.id,
    )

    assert conversation.id == reassigned.id
    assert reassigned.store_id == second_store.id

    await close_repository(repository, runtime)


async def test_discard_empty_draft_order_returns_false_without_existing_order(tmp_path: Path):
    """Discarding an empty draft is a no-op when the customer has no draft yet."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="no-draft-user")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="no-draft-user",
        customer_id=customer.id,
    )

    discarded = await repository.discard_empty_draft_order(customer.id, conversation.id)

    assert discarded is False

    await close_repository(repository, runtime)


async def test_dashboard_admin_bootstrap_and_staff_authentication(tmp_path: Path):
    """Database bootstrap can create the initial dashboard admin and memberships."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'staff.db'}",
        store_name="Rotisería Test",
        bot_name="Ruperto Test",
        dashboard_admin_email="owner@example.com",
        dashboard_admin_password=SecretStr("super-secret"),
        dashboard_admin_name="Owner User",
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        staff_user = await repository.get_staff_user_by_email("OWNER@example.com")
        authenticated = await repository.authenticate_staff_user(email="owner@example.com", password="super-secret")
        memberships = await repository.list_store_memberships_for_staff_user(authenticated.id if authenticated else 0)

        assert staff_user is not None
        assert authenticated is not None
        assert memberships[0].store_id == 1
        assert memberships[0].role == StaffRole.OWNER
        assert await repository.user_can_access_store(staff_user_id=authenticated.id, store_id=1) is True
        assert await repository.user_can_access_store(staff_user_id=authenticated.id, store_id=999) is False

    await runtime.engine.dispose()


async def test_staff_repository_helpers_handle_missing_and_inactive_users(tmp_path: Path):
    """Staff lookups fail closed for missing records and inactive accounts."""
    repository, runtime = await build_repository(tmp_path)
    created = await repository.ensure_staff_user(
        email="staff@example.com",
        full_name="Staff User",
        password="super-secret",
        store_id=1,
    )

    assert await repository.get_staff_user_by_id(9999) is None
    assert await repository.get_staff_user_by_email("missing@example.com") is None

    staff_row = await repository.session.get(StaffUser, created.id)
    assert staff_row is not None
    staff_row.is_active = False
    await repository.session.flush()

    assert await repository.authenticate_staff_user(email="staff@example.com", password="super-secret") is None

    await close_repository(repository, runtime)


async def test_store_staff_memberships_can_be_listed_and_reassigned(tmp_path: Path):
    """Store membership helpers expose staff rows and allow role updates."""
    repository, runtime = await build_repository(tmp_path)
    await repository.ensure_staff_user(
        email="team@example.com",
        full_name="Equipo Local",
        password="super-secret",
        store_id=1,
        role=StaffRole.STAFF,
    )

    memberships = await repository.list_staff_memberships_for_store(store_id=1)
    team_membership = next(membership for membership in memberships if membership.email == "team@example.com")
    updated_membership = await repository.update_store_membership_role(
        membership_id=team_membership.membership_id,
        store_id=1,
        role=StaffRole.MANAGER,
    )

    assert team_membership.role == StaffRole.STAFF
    assert updated_membership.role == StaffRole.MANAGER

    await close_repository(repository, runtime)


async def test_update_store_membership_role_raises_for_missing_membership(tmp_path: Path):
    """Updating an unknown store membership fails with a clear error."""
    repository, runtime = await build_repository(tmp_path)

    with pytest.raises(ValueError, match=r"Store membership not found\."):
        await repository.update_store_membership_role(
            membership_id=999,
            store_id=1,
            role=StaffRole.MANAGER,
        )

    await close_repository(repository, runtime)


async def test_init_database_backfills_missing_demo_menu_items(tmp_path: Path):
    """Bootstrap adds missing demo items even when the menu already has rows."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'repo-backfill.db'}",
        store_name="Rotisería Test",
        bot_name="Ruperto Test",
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    async with runtime.session_factory() as session:
        pizza = await session.scalar(select(MenuItem).where(MenuItem.sku == "pizza-especial"))
        assert pizza is not None
        await session.delete(pizza)
        await session.commit()

    await init_database(settings=settings, runtime=runtime)

    async with runtime.session_factory() as session:
        restored_pizza = await session.scalar(select(MenuItem).where(MenuItem.sku == "pizza-especial"))
        assert restored_pizza is not None

    await runtime.engine.dispose()


async def test_customer_identity_and_message_history_are_persisted(tmp_path: Path):
    """A customer can be resolved again and conversation history survives round trips."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(
        channel=Channel.WHATSAPP,
        external_id="3515557788",
        phone_number="351-555-7788",
    )
    same_customer = await repository.get_or_create_customer(
        channel=Channel.WHATSAPP,
        external_id="3515557788",
        phone_number="3515557788",
    )
    conversation = await repository.get_or_create_conversation(
        channel=Channel.WHATSAPP,
        external_id="3515557788",
        customer_id=customer.id,
    )

    await repository.append_conversation_messages(
        conversation.id,
        [
            ModelRequest(parts=[UserPromptPart(content="Hola")]),
            ModelResponse(parts=[TextPart(content="Hola, ¿cómo te llamás?")], model_name="test"),
        ],
    )
    restored_messages = await repository.load_conversation_messages(conversation.id)

    assert same_customer.id == customer.id
    assert same_customer.phone_number == "3515557788"
    assert len(restored_messages) == EXPECTED_HISTORY_MESSAGES
    assert isinstance(restored_messages[0].parts[0], UserPromptPart)
    assert isinstance(restored_messages[1].parts[0], TextPart)
    assert restored_messages[0].parts[0].content == "Hola"
    assert restored_messages[1].parts[0].content == "Hola, ¿cómo te llamás?"

    await close_repository(repository, runtime)


async def test_conversation_state_can_remember_a_pending_customer_message(tmp_path: Path):
    """Conversation state stores deferred intent while onboarding is incomplete."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="cliente-pendiente")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="cliente-pendiente",
        customer_id=customer.id,
    )

    assert await repository.get_pending_customer_message(conversation.id) is None
    assert (
        await repository.set_pending_customer_message(conversation.id, "quiero una hamburguesa y una coca")
        == "quiero una hamburguesa y una coca"
    )
    assert await repository.get_pending_customer_message(conversation.id) == "quiero una hamburguesa y una coca"
    assert await repository.set_pending_customer_message(conversation.id, None) is None
    assert await repository.get_pending_customer_message(conversation.id) is None

    await close_repository(repository, runtime)


async def test_customer_phone_and_conversation_can_be_completed_later(tmp_path: Path):
    """Existing identities can gain a phone number and conversations can be re-linked."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="cliente-dev")
    updated_customer = await repository.get_or_create_customer(
        channel=Channel.DEV,
        external_id="cliente-dev",
        phone_number="+54 351 444 3322",
    )
    another_customer = await repository.get_or_create_customer(
        channel=Channel.WHATSAPP,
        external_id="+54 11 5555 0000",
        phone_number="+54 11 5555 0000",
    )
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="shared-thread",
        customer_id=customer.id,
    )
    reassigned = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="shared-thread",
        customer_id=another_customer.id,
    )

    assert updated_customer.id == customer.id
    assert updated_customer.phone_number == "+543514443322"
    assert reassigned.id == conversation.id
    assert reassigned.customer_id == another_customer.id

    await close_repository(repository, runtime)


async def test_init_database_respects_the_configured_default_store_id(tmp_path: Path):
    """Bootstrap should create the default store profile and schedule with the configured store id."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'repo-store.db'}",
        store_name="Rotisería Tenant",
        bot_name="Ruperto Tenant",
        default_store_id=TENANT_STORE_ID,
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    session = runtime.session_factory()
    repository = BusinessRepository(session)

    store = await repository.get_store_profile(store_id=TENANT_STORE_ID)
    hours = await repository.list_store_business_hours(store_id=TENANT_STORE_ID)

    assert store.id == TENANT_STORE_ID
    assert store.store_name == "Rotisería Tenant"
    assert len(hours) == DEFAULT_WEEKLY_HOURS
    assert all(row.store_id == TENANT_STORE_ID for row in hours)

    await close_repository(repository, runtime)


async def test_order_lifecycle_and_customer_memory(tmp_path: Path):
    """Orders can be drafted, updated, confirmed, and then reused as memory."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="cli-user")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="cli-user",
        customer_id=customer.id,
    )

    assert await repository.get_current_order(customer.id, conversation.id, create_if_missing=False) is None

    named_customer = await repository.update_customer_name(customer.id, "Martina")
    assert named_customer.name == "Martina"

    draft = await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.DELIVERY)
    with pytest.raises(ValueError):
        await repository.confirm_current_order(customer.id, conversation.id)
    with pytest.raises(ValueError):
        await repository.add_item_to_current_order(
            customer.id,
            conversation.id,
            sku="producto-inexistente",
            quantity=1,
        )

    draft = await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="hamburguesa-completa",
        quantity=2,
        notes="sin cebolla",
    )
    draft = await repository.set_order_delivery_address(customer.id, conversation.id, "Olegario Andrade 330")
    draft = await repository.set_order_payment_method(customer.id, conversation.id, PaymentMethod.TRANSFER)
    confirmed = await repository.confirm_current_order(customer.id, conversation.id)
    latest = await repository.get_latest_order(customer.id, conversation.id)
    memory = await repository.get_customer_memory(customer.id)
    delay = await repository.get_estimated_delay(customer.id, conversation.id)

    assert draft.delivery_type == DeliveryType.DELIVERY
    assert draft.delivery_address == "Olegario Andrade 330"
    assert draft.payment_method == PaymentMethod.TRANSFER
    assert confirmed.status.value == "confirmed"
    assert latest is not None
    assert latest.status.value == "confirmed"
    assert confirmed.total_amount_cents == EXPECTED_ORDER_TOTAL
    assert confirmed.items[0].notes == "sin cebolla"
    assert delay.base_minutes == EXPECTED_DELAY_MINUTES
    assert delay.active_orders_ahead == 0
    assert delay.estimated_minutes == ROUNDED_DELAY_MINUTES
    assert delay.display_text == "25 minutos aproximadamente"
    assert memory.favorite_item_name == "Hamburguesa completa"
    assert memory.recent_items == ["Hamburguesa completa"]

    await close_repository(repository, runtime)


async def test_set_order_requested_ready_at_persists_schedule_and_updates_prep_start(tmp_path: Path):
    """Scheduled orders keep the requested ready time and derived preparation start."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="schedule-ok")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="schedule-ok",
        customer_id=customer.id,
    )
    await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=weekday,
                opens_at="00:00",
                closes_at="23:59",
                closed=False,
            )
            for weekday in range(7)
        ]
    )
    await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="hamburguesa-completa",
        quantity=1,
    )

    local_ready = datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0) + timedelta(hours=2)
    scheduled = await repository.set_order_requested_ready_at(
        customer.id,
        conversation.id,
        local_ready.astimezone(UTC),
        timezone_name=STORE_TIMEZONE,
    )

    assert scheduled.requested_ready_at is not None
    assert scheduled.preparation_starts_at is not None
    assert scheduled.requested_ready_at == local_ready.astimezone(UTC)
    assert scheduled.preparation_starts_at == scheduled.requested_ready_at - timedelta(minutes=PICKUP_DELAY_MINUTES)

    delivery_scheduled = await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.DELIVERY)

    assert delivery_scheduled.preparation_starts_at is not None
    assert scheduled.requested_ready_at - delivery_scheduled.preparation_starts_at == timedelta(minutes=20)

    await close_repository(repository, runtime)


async def test_order_snapshot_normalizes_sqlite_schedule_timestamps_to_utc(tmp_path: Path):
    """Order snapshots should reattach UTC tzinfo when SQLite returns naive datetimes."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="schedule-naive")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="schedule-naive",
        customer_id=customer.id,
    )
    ready_at = datetime(2026, 4, 5, 15, 0)
    prep_at = datetime(2026, 4, 5, 14, 40)
    order = Order(
        customer_id=customer.id,
        conversation_id=conversation.id,
        status=OrderStatus.DRAFT,
        total_amount_cents=0,
        requested_ready_at=ready_at,
        preparation_starts_at=prep_at,
    )
    repository.session.add(order)
    await repository.session.flush()

    snapshot = await repository._build_order_snapshot(order)

    assert snapshot.requested_ready_at == ready_at.replace(tzinfo=UTC)
    assert snapshot.preparation_starts_at == prep_at.replace(tzinfo=UTC)

    await close_repository(repository, runtime)


async def test_set_order_requested_ready_at_rejects_closed_or_too_soon_slots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Scheduling fails outside store hours or without enough preparation lead time."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="schedule-invalid")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="schedule-invalid",
        customer_id=customer.id,
    )
    await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="hamburguesa-completa",
        quantity=1,
    )

    fixed_local_now = datetime(2026, 4, 4, 12, 0, tzinfo=ZoneInfo(STORE_TIMEZONE))
    monkeypatch.setattr("ruperto.repository.utc_now", lambda: fixed_local_now.astimezone(UTC))

    local_ready = fixed_local_now + timedelta(hours=2)
    await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=weekday,
                opens_at=None,
                closes_at=None,
                closed=True,
            )
            for weekday in range(7)
        ]
    )
    with pytest.raises(ValueError, match="solo dentro del horario"):
        await repository.set_order_requested_ready_at(
            customer.id,
            conversation.id,
            local_ready.astimezone(UTC),
            timezone_name=STORE_TIMEZONE,
        )

    reset_order = await repository.get_current_order(customer.id, conversation.id)
    assert reset_order is not None
    assert reset_order.requested_ready_at is None
    assert reset_order.preparation_starts_at is None

    await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=weekday,
                opens_at="00:00",
                closes_at="23:59",
                closed=False,
            )
            for weekday in range(7)
        ]
    )
    too_soon = fixed_local_now + timedelta(minutes=5)
    with pytest.raises(ValueError, match="necesito un poco más de tiempo"):
        await repository.set_order_requested_ready_at(
            customer.id,
            conversation.id,
            too_soon.astimezone(UTC),
            timezone_name=STORE_TIMEZONE,
        )

    await close_repository(repository, runtime)


async def test_init_database_backfills_schedule_and_transfer_columns_for_legacy_sqlite(tmp_path: Path):
    """Legacy SQLite schemas gain the new scheduling and transfer columns during init."""
    database_path = tmp_path / "legacy.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE store_profile (
                    id INTEGER PRIMARY KEY,
                    store_name VARCHAR(120),
                    bot_name VARCHAR(120),
                    store_location VARCHAR(255),
                    store_description VARCHAR(500),
                    assistant_personality VARCHAR(255),
                    locale VARCHAR(32),
                    currency_code VARCHAR(8),
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO store_profile (
                    id, store_name, bot_name, store_location, store_description,
                    assistant_personality, locale, currency_code, created_at, updated_at
                ) VALUES (
                    1, 'Legacy Rotisería', 'Legacy Bot', NULL, 'Perfil viejo',
                    'Amable', 'es-AR', 'ARS', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE customer_order (
                    id INTEGER PRIMARY KEY,
                    customer_id INTEGER,
                    conversation_id INTEGER,
                    status VARCHAR(32),
                    delivery_type VARCHAR(32),
                    delivery_address VARCHAR(255),
                    payment_method VARCHAR(32),
                    total_amount_cents INTEGER,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
    sync_engine.dispose()

    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        store_name="Legacy Rotisería",
        bot_name="Legacy Bot",
        store_transfer_alias="legacy.alias",
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    with sqlite3.connect(database_path) as sqlite_connection:
        store_columns = {row[1] for row in sqlite_connection.execute("PRAGMA table_info(store_profile)")}
        order_columns = {row[1] for row in sqlite_connection.execute("PRAGMA table_info(customer_order)")}
    assert "transfer_alias" in store_columns
    assert "requested_ready_at" in order_columns
    assert "preparation_starts_at" in order_columns

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile()
        assert store.transfer_alias == "legacy.alias"

    await runtime.engine.dispose()


async def test_init_database_updates_existing_store_vertical_from_settings(tmp_path: Path):
    """Bootstrap updates the default store vertical when settings request a different tenant type."""
    database_path = tmp_path / "vertical-sync.db"
    base_settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        store_name="Rotisería Test",
        bot_name="Ruperto Test",
    )
    runtime = create_database_runtime(base_settings)
    await init_database(settings=base_settings, runtime=runtime)
    await runtime.engine.dispose()

    municipal_settings = base_settings.model_copy(update={"store_vertical": StoreVertical.MUNICIPAL})
    runtime = create_database_runtime(municipal_settings)
    await init_database(settings=municipal_settings, runtime=runtime)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile()

    assert store.vertical == StoreVertical.MUNICIPAL
    await runtime.engine.dispose()


async def test_legacy_business_hours_table_gains_slot_index_column(tmp_path: Path):
    """Legacy opening-hours rows are migrated to the multi-slot schema."""
    database_path = tmp_path / "legacy-hours.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE store_profile (
                    id INTEGER PRIMARY KEY,
                    store_name VARCHAR(120),
                    bot_name VARCHAR(120),
                    store_location VARCHAR(255),
                    store_description VARCHAR(500),
                    assistant_personality VARCHAR(255),
                    locale VARCHAR(32),
                    currency_code VARCHAR(8),
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE store_business_hours (
                    id INTEGER NOT NULL PRIMARY KEY,
                    store_id INTEGER NOT NULL,
                    weekday INTEGER NOT NULL,
                    opens_at TIME,
                    closes_at TIME,
                    closed BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    FOREIGN KEY(store_id) REFERENCES store_profile (id) ON DELETE CASCADE,
                    CONSTRAINT uq_store_business_hours UNIQUE (store_id, weekday)
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
                    opens_at,
                    closes_at,
                    closed,
                    created_at,
                    updated_at
                ) VALUES (
                    1,
                    1,
                    1,
                    '11:00',
                    '15:00',
                    0,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            )
        )
        _ensure_schema_columns(connection)
    sync_engine.dispose()

    with sqlite3.connect(database_path) as sqlite_connection:
        columns = {row[1] for row in sqlite_connection.execute("PRAGMA table_info(store_business_hours)")}
        migrated_row = sqlite_connection.execute(
            """
            SELECT weekday, slot_index, opens_at, closes_at, closed
            FROM store_business_hours
            """
        ).fetchone()

    assert "slot_index" in columns
    assert migrated_row == (1, 0, "11:00", "15:00", 0)


async def test_legacy_conversation_table_gains_store_id_column(tmp_path: Path):
    """Legacy conversations are backfilled with the default store id during bootstrap."""
    database_path = tmp_path / "legacy-conversation.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE store_profile (
                    id INTEGER PRIMARY KEY,
                    store_name VARCHAR(120),
                    bot_name VARCHAR(120),
                    store_location VARCHAR(255),
                    store_description VARCHAR(500),
                    assistant_personality VARCHAR(255),
                    locale VARCHAR(32),
                    currency_code VARCHAR(8),
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO store_profile (
                    id, store_name, bot_name, store_location, store_description,
                    assistant_personality, locale, currency_code, created_at, updated_at
                ) VALUES (
                    1, 'Legacy Rotisería', 'Legacy Bot', NULL, 'Perfil viejo',
                    'Amable', 'es-AR', 'ARS', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE customer (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120),
                    phone_number VARCHAR(32),
                    default_address VARCHAR(255),
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO customer (
                    id, name, phone_number, default_address, created_at, updated_at
                ) VALUES (
                    1, 'Cliente viejo', NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE conversation (
                    id INTEGER PRIMARY KEY,
                    channel VARCHAR(32),
                    external_id VARCHAR(120),
                    customer_id INTEGER,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_conversation_identity UNIQUE (channel, external_id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO conversation (
                    id, channel, external_id, customer_id, created_at, updated_at
                ) VALUES (
                    1, 'dev', 'legacy-user', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        _ensure_schema_columns(connection)
    sync_engine.dispose()

    with sqlite3.connect(database_path) as sqlite_connection:
        columns = {row[1] for row in sqlite_connection.execute("PRAGMA table_info(conversation)")}
        migrated_row = sqlite_connection.execute(
            """
            SELECT store_id
            FROM conversation
            WHERE id = 1
            """
        ).fetchone()

    assert "store_id" in columns
    assert migrated_row == (1,)


async def test_schedule_validation_helpers_cover_past_missing_prep_and_relative_day_text(tmp_path: Path):
    """Scheduling validation rejects past times and the local-text helper covers tomorrow and weekdays."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="schedule-helper")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="schedule-helper",
        customer_id=customer.id,
    )
    await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=weekday,
                opens_at="00:00",
                closes_at="23:59",
                closed=False,
            )
            for weekday in range(7)
        ]
    )

    created = await repository.set_order_requested_ready_at(
        customer.id,
        conversation.id,
        (datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0) + timedelta(hours=2)).astimezone(UTC),
        timezone_name=STORE_TIMEZONE,
    )
    assert created.requested_ready_at is not None

    order = await repository._require_current_order(customer.id, conversation.id)
    order.requested_ready_at = (
        datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0) - timedelta(minutes=5)
    ).astimezone(UTC)
    with pytest.raises(ValueError, match="ya pasó"):
        await repository._validate_requested_ready_at(order, timezone_name=STORE_TIMEZONE, store_id=1)

    order.requested_ready_at = (
        datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0) + timedelta(hours=3)
    ).astimezone(UTC)
    order.preparation_starts_at = None
    await repository._validate_requested_ready_at(order, timezone_name=STORE_TIMEZONE, store_id=1)

    local_now = datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0)
    assert repository._describe_local_datetime(local_now, local_now).startswith("hoy")
    assert repository._describe_local_datetime(local_now + timedelta(days=1), local_now).startswith("mañana")
    assert repository._describe_local_datetime(local_now + timedelta(days=2), local_now).startswith("el ")

    await close_repository(repository, runtime)


async def test_order_creation_and_failures_have_explicit_messages(tmp_path: Path):
    """Order helpers create drafts lazily and expose domain errors with clear text."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="cli-user-2")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="cli-user-2",
        customer_id=customer.id,
    )

    assert await repository.get_latest_order(customer.id, conversation.id) is None

    created_from_item = await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="hamburguesa-completa",
        quantity=1,
    )
    assert created_from_item.total_amount_cents == EXPECTED_SINGLE_BURGER_TOTAL

    second_customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="cli-user-3")
    second_conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="cli-user-3",
        customer_id=second_customer.id,
    )

    with pytest.raises(ValueError, match=re.escape("No hay un pedido abierto para confirmar.")):
        await repository.confirm_current_order(second_customer.id, second_conversation.id)

    await repository.get_current_order(second_customer.id, second_conversation.id)
    with pytest.raises(ValueError, match=re.escape("No se puede confirmar un pedido vacío.")):
        await repository.confirm_current_order(second_customer.id, second_conversation.id)

    with pytest.raises(ValueError, match=re.escape("El producto pedido no existe o no está disponible.")):
        await repository.add_item_to_current_order(
            second_customer.id,
            second_conversation.id,
            sku="producto-inexistente",
            quantity=1,
        )

    await close_repository(repository, runtime)


async def test_discard_empty_draft_order_keeps_non_empty_drafts(tmp_path: Path):
    """Empty-draft cleanup should not delete a draft that already has line items."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="non-empty-draft")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="non-empty-draft",
        customer_id=customer.id,
    )

    await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="hamburguesa-completa",
        quantity=1,
    )

    assert await repository.discard_empty_draft_order(customer.id, conversation.id) is False
    assert await repository.get_latest_order(customer.id, conversation.id) is not None

    await close_repository(repository, runtime)


async def test_discard_empty_draft_order_removes_empty_drafts(tmp_path: Path):
    """Empty-draft cleanup should delete a draft that has no line items yet."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="empty-draft-delete")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="empty-draft-delete",
        customer_id=customer.id,
    )

    created = await repository.get_current_order(customer.id, conversation.id)
    assert created is not None

    assert await repository.discard_empty_draft_order(customer.id, conversation.id) is True
    assert await repository.get_latest_order(customer.id, conversation.id) is None

    await close_repository(repository, runtime)


async def test_confirm_current_order_requires_delivery_choice_address_and_payment(tmp_path: Path):
    """Draft confirmation requires the core checkout fields before closing the order."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="checkout-user")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="checkout-user",
        customer_id=customer.id,
    )

    await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="hamburguesa-bbq",
        quantity=1,
    )
    with pytest.raises(ValueError, match=re.escape("Necesito saber si es envío o retiro antes de confirmar.")):
        await repository.confirm_current_order(customer.id, conversation.id)

    await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.DELIVERY)
    with pytest.raises(ValueError, match=re.escape("Necesito la dirección de entrega antes de confirmar.")):
        await repository.confirm_current_order(customer.id, conversation.id)

    await repository.set_order_delivery_address(customer.id, conversation.id, "Lavalle 12333")
    with pytest.raises(ValueError, match=re.escape("Necesito definir el medio de pago antes de confirmar.")):
        await repository.confirm_current_order(customer.id, conversation.id)

    await close_repository(repository, runtime)


async def test_delay_estimate_without_order_returns_store_default(tmp_path: Path):
    """The repository can provide a generic delay estimate before any order exists."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="delay-user")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="delay-user",
        customer_id=customer.id,
    )

    delay = await repository.get_estimated_delay(customer.id, conversation.id)

    assert delay.base_minutes == DEFAULT_DELAY_MINUTES
    assert delay.active_orders_ahead == 0
    assert delay.estimated_minutes == DEFAULT_DELAY_MINUTES
    assert delay.display_text == "25 minutos aproximadamente"

    await close_repository(repository, runtime)


async def test_delay_estimate_with_empty_draft_uses_base_preparation(tmp_path: Path):
    """An empty draft still returns the default preparation base."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="delay-empty")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="delay-empty",
        customer_id=customer.id,
    )

    await repository.get_current_order(customer.id, conversation.id)
    delay = await repository.get_estimated_delay(customer.id, conversation.id)

    assert delay.base_minutes == EMPTY_DRAFT_DELAY_MINUTES
    assert delay.active_orders_ahead == 0
    assert delay.estimated_minutes == EMPTY_DRAFT_DELAY_MINUTES

    await close_repository(repository, runtime)


async def test_delay_estimate_uses_draft_order_rules(tmp_path: Path):
    """The delay estimate uses the current draft order when one exists."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="delay-draft")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="delay-draft",
        customer_id=customer.id,
    )

    await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="hamburguesa-completa",
        quantity=1,
    )
    draft_delay = await repository.get_estimated_delay(customer.id, conversation.id)
    await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
    pickup_delay = await repository.get_estimated_delay(customer.id, conversation.id)

    assert draft_delay.base_minutes == DRAFT_DELAY_MINUTES
    assert draft_delay.estimated_minutes == DRAFT_DELAY_MINUTES
    assert draft_delay.display_text == "15 minutos aproximadamente"
    assert pickup_delay.base_minutes == PICKUP_DELAY_MINUTES
    assert pickup_delay.estimated_minutes == PICKUP_DELAY_MINUTES
    assert pickup_delay.display_text == "15 minutos aproximadamente"

    await close_repository(repository, runtime)


async def test_delay_estimate_adds_extra_time_for_large_orders(tmp_path: Path):
    """Large confirmed orders add a small surcharge to the delay estimate."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="delay-large")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="delay-large",
        customer_id=customer.id,
    )

    await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="milanesa-napolitana",
        quantity=3,
    )
    await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.DELIVERY)
    await repository.set_order_delivery_address(customer.id, conversation.id, "Lavalle 12333")
    await repository.set_order_payment_method(customer.id, conversation.id, PaymentMethod.CASH)
    await repository.confirm_current_order(customer.id, conversation.id)
    delay = await repository.get_estimated_delay(customer.id, conversation.id)

    assert delay.base_minutes == LARGE_ORDER_DELAY_MINUTES
    assert delay.estimated_minutes == ROUNDED_LARGE_ORDER_DELAY_MINUTES
    assert delay.display_text == "35 minutos aproximadamente"

    await close_repository(repository, runtime)


async def test_delay_estimate_reflects_kitchen_load_from_previous_active_orders(tmp_path: Path):
    """Earlier active orders increase the delay estimate for later orders."""
    repository, runtime = await build_repository(tmp_path)

    first_customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="load-1")
    first_conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="load-1",
        customer_id=first_customer.id,
    )
    await repository.add_item_to_current_order(
        first_customer.id,
        first_conversation.id,
        sku="pizza-muzzarella",
        quantity=1,
    )
    await repository.set_order_delivery_type(first_customer.id, first_conversation.id, DeliveryType.PICKUP)
    await repository.set_order_payment_method(first_customer.id, first_conversation.id, PaymentMethod.CASH)
    first_order = await repository.confirm_current_order(first_customer.id, first_conversation.id)
    assert first_order.status == OrderStatus.CONFIRMED

    second_customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="load-2")
    second_conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="load-2",
        customer_id=second_customer.id,
    )
    await repository.add_item_to_current_order(
        second_customer.id,
        second_conversation.id,
        sku="hamburguesa-completa",
        quantity=1,
    )
    delay = await repository.get_estimated_delay(second_customer.id, second_conversation.id)

    assert delay.base_minutes == DRAFT_DELAY_MINUTES
    assert delay.active_orders_ahead == 1
    assert delay.estimated_minutes == ROUNDED_KITCHEN_LOAD_DELAY_MINUTES
    assert delay.display_text == "20 minutos aproximadamente"

    await close_repository(repository, runtime)


async def test_update_order_status_requires_an_existing_order(tmp_path: Path):
    """Staff operations fail clearly when the order id does not exist."""
    repository, runtime = await build_repository(tmp_path)

    with pytest.raises(ValueError, match=re.escape("No se encontró el pedido solicitado.")):
        await repository.update_order_status(999, OrderStatus.ALMOST_READY)

    await close_repository(repository, runtime)


async def test_update_order_status_queues_automatic_notifications(tmp_path: Path):
    """Relevant status transitions queue one outbound message for the conversation."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="notify-user")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="notify-user",
        customer_id=customer.id,
    )
    await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="hamburguesa-completa",
        quantity=1,
    )
    await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
    await repository.set_order_payment_method(customer.id, conversation.id, PaymentMethod.CASH)
    confirmed = await repository.confirm_current_order(customer.id, conversation.id)

    almost_ready = await repository.update_order_status(confirmed.id, OrderStatus.ALMOST_READY)
    ready = await repository.update_order_status(confirmed.id, OrderStatus.READY_FOR_PICKUP)
    first_poll = await repository.list_pending_notifications(channel=Channel.DEV, external_id="notify-user")
    second_poll = await repository.list_pending_notifications(channel=Channel.DEV, external_id="notify-user")

    assert almost_ready.notify_when_ready is True
    assert ready.status == OrderStatus.READY_FOR_PICKUP
    assert [notification.event_type for notification in first_poll] == ["order_almost_ready", "order_ready"]
    assert [notification.message_text for notification in first_poll] == [
        "Tu pedido ya casi está 👀",
        "Tu pedido ya está listo para retirar 🙌",
    ]
    assert second_poll == []

    await close_repository(repository, runtime)


async def test_set_order_notify_when_ready_handles_new_draft_and_latest_order(tmp_path: Path):
    """The notification preference can be set before ordering, on a draft, or on the latest confirmed order."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="notify-setup-user")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="notify-setup-user",
        customer_id=customer.id,
    )

    created = await repository.set_order_notify_when_ready(customer.id, conversation.id, enabled=False)
    assert created.notify_when_ready is False
    assert created.items == []

    await repository.add_item_to_current_order(
        customer.id,
        conversation.id,
        sku="hamburguesa-completa",
        quantity=1,
    )
    updated_draft = await repository.set_order_notify_when_ready(customer.id, conversation.id, enabled=True)
    assert updated_draft.notify_when_ready is True

    await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
    await repository.set_order_payment_method(customer.id, conversation.id, PaymentMethod.CASH)
    await repository.confirm_current_order(customer.id, conversation.id)

    updated_latest = await repository.set_order_notify_when_ready(customer.id, conversation.id, enabled=False)
    assert updated_latest.notify_when_ready is False
    assert updated_latest.status == OrderStatus.CONFIRMED

    await close_repository(repository, runtime)


async def test_notification_queue_helper_covers_early_return_branches(tmp_path: Path):
    """Notification queueing skips disabled, unchanged, duplicate, unmapped, and orphaned cases."""
    repository, runtime = await build_repository(tmp_path)
    customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="notify-helper-user")
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="notify-helper-user",
        customer_id=customer.id,
    )

    disabled_order = Order(
        customer_id=customer.id,
        conversation_id=conversation.id,
        status=OrderStatus.ALMOST_READY,
        notify_when_ready=False,
    )
    unchanged_order = Order(
        customer_id=customer.id,
        conversation_id=conversation.id,
        status=OrderStatus.READY_FOR_PICKUP,
        notify_when_ready=True,
    )
    unmapped_order = Order(
        customer_id=customer.id,
        conversation_id=conversation.id,
        status=OrderStatus.CONFIRMED,
        notify_when_ready=True,
    )
    duplicate_order = Order(
        customer_id=customer.id,
        conversation_id=conversation.id,
        status=OrderStatus.ALMOST_READY,
        notify_when_ready=True,
    )
    orphaned_order = Order(
        customer_id=customer.id,
        conversation_id=99999,
        status=OrderStatus.OUT_FOR_DELIVERY,
        notify_when_ready=True,
    )
    repository.session.add_all([disabled_order, unchanged_order, unmapped_order, duplicate_order, orphaned_order])
    await repository.session.flush()

    await repository._queue_status_notification_if_needed(disabled_order, previous_status=OrderStatus.CONFIRMED)
    await repository._queue_status_notification_if_needed(unchanged_order, previous_status=OrderStatus.READY_FOR_PICKUP)
    await repository._queue_status_notification_if_needed(unmapped_order, previous_status=OrderStatus.DRAFT)
    await repository._queue_status_notification_if_needed(duplicate_order, previous_status=OrderStatus.CONFIRMED)
    await repository._queue_status_notification_if_needed(duplicate_order, previous_status=OrderStatus.CONFIRMED)
    await repository._queue_status_notification_if_needed(orphaned_order, previous_status=OrderStatus.READY_FOR_PICKUP)

    assert repository._build_status_notification_text(orphaned_order) == "Tu pedido ya salió y va en camino 🚚"
    assert repository._build_status_notification_text(disabled_order) == "Tu pedido ya casi está 👀"
    notifications_in_db = await repository.session.scalar(select(func.count(OutboundNotification.id)))
    notifications = await repository.list_pending_notifications(channel=Channel.DEV, external_id="notify-helper-user")
    assert notifications_in_db == 1
    assert [notification.event_type for notification in notifications] == ["order_almost_ready"]

    await close_repository(repository, runtime)


async def test_store_availability_reports_open_and_closed_windows(tmp_path: Path):
    """The repository can tell whether the store is open and when it opens next."""
    repository, runtime = await build_repository(tmp_path)

    open_availability = await repository.get_store_availability(
        timezone_name="America/Argentina/Cordoba",
        now=datetime(2026, 4, 6, 15, 0, tzinfo=UTC),
    )
    closed_availability = await repository.get_store_availability(
        timezone_name="America/Argentina/Cordoba",
        now=datetime(2026, 4, 6, 5, 0, tzinfo=UTC),
    )

    assert open_availability.is_open is True
    assert "abiertos ahora" in open_availability.message_text.lower()
    assert closed_availability.is_open is False
    assert closed_availability.next_open_text == "hoy a las 11:00"
    assert "abrimos hoy a las 11:00" in closed_availability.message_text.lower()

    await close_repository(repository, runtime)


async def test_store_business_hours_can_be_replaced(tmp_path: Path):
    """Staff-defined schedules replace the seeded weekly hours."""
    repository, runtime = await build_repository(tmp_path)

    updated = await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=0,
                opens_at=None,
                closes_at=None,
                closed=True,
            ),
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=1,
                slot_index=0,
                opens_at="11:30",
                closes_at="15:00",
                closed=False,
            ),
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=1,
                slot_index=1,
                opens_at="18:00",
                closes_at="23:30",
                closed=False,
            ),
        ]
    )

    assert len(updated) == DEFAULT_WEEKLY_HOURS + 1
    assert updated[0].closed is True
    assert updated[1].opens_at == "11:30"
    assert updated[2].opens_at == "18:00"

    await close_repository(repository, runtime)


async def test_store_availability_can_report_tomorrow_or_later_openings(tmp_path: Path):
    """The next opening text distinguishes tomorrow from a later weekday."""
    repository, runtime = await build_repository(tmp_path)
    await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(id=0, store_id=1, weekday=0, opens_at=None, closes_at=None, closed=True),
            StoreBusinessHoursSnapshot(id=0, store_id=1, weekday=1, opens_at="18:00", closes_at="23:00", closed=False),
            StoreBusinessHoursSnapshot(id=0, store_id=1, weekday=2, opens_at="12:00", closes_at="23:00", closed=False),
        ]
    )

    tomorrow_availability = await repository.get_store_availability(
        timezone_name="America/Argentina/Cordoba",
        now=datetime(2026, 4, 6, 5, 0, tzinfo=UTC),
    )
    assert tomorrow_availability.next_open_text == "mañana a las 18:00"

    await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(id=0, store_id=1, weekday=0, opens_at=None, closes_at=None, closed=True),
            StoreBusinessHoursSnapshot(id=0, store_id=1, weekday=1, opens_at=None, closes_at=None, closed=True),
            StoreBusinessHoursSnapshot(id=0, store_id=1, weekday=2, opens_at="12:00", closes_at="23:00", closed=False),
        ]
    )
    weekday_availability = await repository.get_store_availability(
        timezone_name="America/Argentina/Cordoba",
        now=datetime(2026, 4, 6, 5, 0, tzinfo=UTC),
    )

    assert weekday_availability.next_open_text == "el miércoles a las 12:00"

    await close_repository(repository, runtime)


async def test_store_availability_skips_today_when_the_shift_already_closed(tmp_path: Path):
    """If today's shift already ended, the next opening should move to a later day."""
    repository, runtime = await build_repository(tmp_path)
    await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(id=0, store_id=1, weekday=0, opens_at="11:00", closes_at="12:00", closed=False),
            StoreBusinessHoursSnapshot(id=0, store_id=1, weekday=1, opens_at="18:00", closes_at="23:00", closed=False),
        ]
    )

    availability = await repository.get_store_availability(
        timezone_name="America/Argentina/Cordoba",
        now=datetime(2026, 4, 6, 16, 0, tzinfo=UTC),
    )

    assert availability.next_open_text == "mañana a las 18:00"

    await close_repository(repository, runtime)


async def test_store_availability_handles_multiple_daily_slots(tmp_path: Path):
    """Availability should respect later slots on the same day."""
    repository, runtime = await build_repository(tmp_path)
    await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=0,
                slot_index=0,
                opens_at="11:00",
                closes_at="15:00",
                closed=False,
            ),
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=0,
                slot_index=1,
                opens_at="19:00",
                closes_at="23:00",
                closed=False,
            ),
        ]
    )

    afternoon_closed = await repository.get_store_availability(
        timezone_name="America/Argentina/Cordoba",
        now=datetime(2026, 4, 6, 20, 0, tzinfo=UTC),
    )
    evening_open = await repository.get_store_availability(
        timezone_name="America/Argentina/Cordoba",
        now=datetime(2026, 4, 6, 23, 30, tzinfo=UTC),
    )

    assert afternoon_closed.next_open_text == "hoy a las 19:00"
    assert evening_open.is_open is True
    assert "hasta las 23:00" in evening_open.message_text

    await close_repository(repository, runtime)


async def test_store_business_hours_reject_overlapping_slots(tmp_path: Path):
    """Overlapping slots on the same day should fail fast."""
    repository, runtime = await build_repository(tmp_path)

    with pytest.raises(ValueError, match=r"Business-hours slots cannot overlap on the same day\."):
        await repository.replace_store_business_hours(
            hours=[
                StoreBusinessHoursSnapshot(
                    id=0,
                    store_id=1,
                    weekday=0,
                    slot_index=0,
                    opens_at="11:00",
                    closes_at="15:00",
                    closed=False,
                ),
                StoreBusinessHoursSnapshot(
                    id=0,
                    store_id=1,
                    weekday=0,
                    slot_index=1,
                    opens_at="14:00",
                    closes_at="18:00",
                    closed=False,
                ),
            ]
        )

    await close_repository(repository, runtime)


async def test_store_business_hours_ignore_empty_open_rows_and_keep_day_closed(tmp_path: Path):
    """Rows without any times are ignored when normalizing slots."""
    repository, runtime = await build_repository(tmp_path)

    updated = await repository.replace_store_business_hours(
        hours=[
            StoreBusinessHoursSnapshot(
                id=0,
                store_id=1,
                weekday=0,
                slot_index=0,
                opens_at=None,
                closes_at=None,
                closed=False,
            )
        ]
    )

    monday_row = next(row for row in updated if row.weekday == 0)
    assert monday_row.closed is True
    assert monday_row.opens_at is None

    await close_repository(repository, runtime)


async def test_store_business_hours_reject_partial_slots(tmp_path: Path):
    """A slot must provide both opening and closing times."""
    repository, runtime = await build_repository(tmp_path)

    with pytest.raises(ValueError, match=re.escape(STORE_HOURS_SLOT_REQUIRES_BOTH_TIMES_MESSAGE)):
        await repository.replace_store_business_hours(
            hours=[
                StoreBusinessHoursSnapshot(
                    id=0,
                    store_id=1,
                    weekday=0,
                    slot_index=0,
                    opens_at="11:00",
                    closes_at=None,
                    closed=False,
                )
            ]
        )

    await close_repository(repository, runtime)


async def test_store_business_hours_reject_inverted_slots(tmp_path: Path):
    """A slot cannot close before it opens."""
    repository, runtime = await build_repository(tmp_path)

    with pytest.raises(ValueError, match=re.escape(STORE_HOURS_SLOT_ORDER_MESSAGE)):
        await repository.replace_store_business_hours(
            hours=[
                StoreBusinessHoursSnapshot(
                    id=0,
                    store_id=1,
                    weekday=0,
                    slot_index=0,
                    opens_at="15:00",
                    closes_at="11:00",
                    closed=False,
                )
            ]
        )

    await close_repository(repository, runtime)


async def test_municipal_bootstrap_seeds_demo_areas_and_categories(tmp_path: Path):
    """Municipal tenants start with a useful service catalog for the PoC."""
    repository, runtime = await build_municipal_repository(tmp_path)

    store = await repository.get_store_profile()
    areas = await repository.list_municipal_areas(store_id=store.id)
    categories = await repository.list_municipal_categories(store_id=store.id)

    assert store.vertical == StoreVertical.MUNICIPAL
    assert {area.name for area in areas} >= {
        "Alumbrado público",
        "Mantenimiento de calles",
        "Solicitud de agua",
    }
    assert any(category.is_fallback for category in categories)
    assert any(category.requires_precise_location for category in categories)

    await close_repository(repository, runtime)


async def test_municipal_cases_default_to_the_area_fallback_category(tmp_path: Path):
    """Creating a case without subcategory should pick the area's fallback when available."""
    repository, runtime = await build_municipal_repository(tmp_path)
    store = await repository.get_store_profile()
    customer = await repository.get_or_create_customer(
        channel=Channel.DEV,
        external_id="municipal-neighbor",
        phone_number="+54 351 111 2233",
    )
    conversation = await repository.get_or_create_conversation(
        channel=Channel.DEV,
        external_id="municipal-neighbor",
        customer_id=customer.id,
        store_id=store.id,
    )
    area = next(
        area
        for area in await repository.list_municipal_areas(store_id=store.id)
        if area.name == "Mantenimiento de calles"
    )
    categories = await repository.list_municipal_categories(store_id=store.id, area_id=area.id)
    fallback_category = next(category for category in categories if category.is_fallback)

    created = await repository.create_municipal_case(
        store_id=store.id,
        payload=MunicipalCaseCreateRequest(
            area_id=area.id,
            customer_id=customer.id,
            conversation_id=conversation.id,
            title="Necesitamos revisar una calle",
            description="La calle del barrio está muy rota y hace falta mantenimiento.",
            reporter_name="Elena",
            reporter_phone_number="+54 351 111 2233",
            location_text="Pasaje Los Aromos 1200",
        ),
    )

    assert created.category_id == fallback_category.id
    assert created.reporter_phone_number == "+543511112233"
    assert created.status == MunicipalCaseStatus.NEW

    await close_repository(repository, runtime)


async def test_municipal_cases_reject_categories_from_another_area(tmp_path: Path):
    """A case cannot mix one area with a category that belongs elsewhere."""
    repository, runtime = await build_municipal_repository(tmp_path)
    store = await repository.get_store_profile()
    areas = await repository.list_municipal_areas(store_id=store.id)
    lighting_area = next(area for area in areas if area.name == "Alumbrado público")
    streets_area = next(area for area in areas if area.name == "Mantenimiento de calles")
    lighting_category = next(
        category
        for category in await repository.list_municipal_categories(store_id=store.id, area_id=lighting_area.id)
        if category.name == "Lámpara apagada"
    )

    with pytest.raises(MunicipalCategoryMismatchError):
        await repository.create_municipal_case(
            store_id=store.id,
            payload=MunicipalCaseCreateRequest(
                area_id=streets_area.id,
                category_id=lighting_category.id,
                title="Cruce oscuro",
                description="No funciona la lámpara en la esquina.",
            ),
        )

    await close_repository(repository, runtime)


async def test_municipal_case_status_and_assignment_can_be_updated(tmp_path: Path):
    """Municipal cases support assignee changes and kanban-friendly statuses."""
    repository, runtime = await build_municipal_repository(tmp_path)
    store = await repository.get_store_profile()
    owner = await repository.ensure_staff_user(
        email="municipal-owner@example.com",
        full_name="Municipal Owner",
        password="super-secret",
        store_id=store.id,
        role=StaffRole.OWNER,
    )
    area = next(
        area for area in await repository.list_municipal_areas(store_id=store.id) if area.name == "Solicitud de agua"
    )
    created = await repository.create_municipal_case(
        store_id=store.id,
        payload=MunicipalCaseCreateRequest(
            area_id=area.id,
            title="Falta de agua en barrio centro",
            description="Desde anoche no sale agua en toda la cuadra.",
            reporter_name="Nora",
        ),
    )

    assigned = await repository.assign_municipal_case(created.id, staff_user_id=owner.id)
    updated = await repository.update_municipal_case_status(
        created.id,
        MunicipalCaseStatusUpdateRequest(status=MunicipalCaseStatus.IN_PROGRESS),
    )
    filtered = await repository.list_municipal_cases(
        store_id=store.id,
        status=MunicipalCaseStatus.IN_PROGRESS,
        assignee_staff_user_id=owner.id,
    )

    assert assigned.assignee_staff_user_id == owner.id
    assert updated.status == MunicipalCaseStatus.IN_PROGRESS
    assert [case.id for case in filtered] == [created.id]

    await close_repository(repository, runtime)


async def test_municipal_areas_and_categories_can_be_created_manually(tmp_path: Path):
    """Stores can extend the demo municipal catalog with tenant-specific entries."""
    repository, runtime = await build_municipal_repository(tmp_path)
    store = await repository.get_store_profile()

    area = await repository.create_municipal_area(
        store_id=store.id,
        payload=MunicipalAreaCreateRequest(
            name="Espacios verdes",
            description="Plazas, juegos y arbolado.",
            display_order=10,
        ),
    )
    category = await repository.create_municipal_category(
        area_id=area.id,
        payload=MunicipalCategoryCreateRequest(
            name="Juegos rotos",
            description="Mantenimiento de hamacas y juegos infantiles.",
            requires_precise_location=True,
            display_order=0,
        ),
    )

    assert area.name == "Espacios verdes"
    assert category.area_id == area.id
    assert category.requires_precise_location is True

    await close_repository(repository, runtime)


async def test_municipal_categories_require_a_valid_area(tmp_path: Path):
    """Categories must always belong to an existing municipal area."""
    repository, runtime = await build_municipal_repository(tmp_path)

    with pytest.raises(MunicipalAreaNotFoundError):
        await repository.create_municipal_category(
            area_id=999,
            payload=MunicipalCategoryCreateRequest(name="Imposible"),
        )

    await close_repository(repository, runtime)


async def test_municipal_category_listing_can_filter_only_active_rows(tmp_path: Path):
    """Inactive municipal areas and categories disappear from active-only listings."""
    repository, runtime = await build_municipal_repository(tmp_path)
    store = await repository.get_store_profile()

    inactive_area = await repository.create_municipal_area(
        store_id=store.id,
        payload=MunicipalAreaCreateRequest(name="Área inactiva", is_active=False),
    )
    await repository.create_municipal_category(
        area_id=inactive_area.id,
        payload=MunicipalCategoryCreateRequest(name="Categoría inactiva", is_active=False),
    )

    active_areas = await repository.list_municipal_areas(store_id=store.id, only_active=True)
    active_categories = await repository.list_municipal_categories(store_id=store.id, only_active=True)

    assert all(area.is_active for area in active_areas)
    assert "Área inactiva" not in {area.name for area in active_areas}
    assert all(category.is_active for category in active_categories)

    await close_repository(repository, runtime)


async def test_municipal_case_creation_validates_missing_area_and_category(tmp_path: Path):
    """Municipal cases fail fast when the selected area or category is missing."""
    repository, runtime = await build_municipal_repository(tmp_path)
    store = await repository.get_store_profile()
    area = next(
        area for area in await repository.list_municipal_areas(store_id=store.id) if area.name == "Solicitud de agua"
    )

    with pytest.raises(MunicipalAreaNotFoundError):
        await repository.create_municipal_case(
            store_id=store.id,
            payload=MunicipalCaseCreateRequest(
                area_id=999,
                title="Sin área",
                description="No debería crearse.",
            ),
        )

    with pytest.raises(MunicipalCategoryNotFoundError):
        await repository.create_municipal_case(
            store_id=store.id,
            payload=MunicipalCaseCreateRequest(
                area_id=area.id,
                category_id=999,
                title="Sin categoría",
                description="No debería crearse.",
            ),
        )

    await close_repository(repository, runtime)


async def test_municipal_case_missing_rows_raise_not_found_errors(tmp_path: Path):
    """Reading or mutating unknown municipal cases should raise domain-specific errors."""
    repository, runtime = await build_municipal_repository(tmp_path)

    with pytest.raises(MunicipalCaseNotFoundError):
        await repository.get_municipal_case(999)
    with pytest.raises(MunicipalCaseNotFoundError):
        await repository.update_municipal_case_status(
            999,
            MunicipalCaseStatusUpdateRequest(status=MunicipalCaseStatus.TRIAGED),
        )
    with pytest.raises(MunicipalCaseNotFoundError):
        await repository.assign_municipal_case(999, staff_user_id=None)

    await close_repository(repository, runtime)


async def test_municipal_case_listing_can_filter_by_area(tmp_path: Path):
    """Area filtering keeps the municipal kanban scoped to one service area."""
    repository, runtime = await build_municipal_repository(tmp_path)
    store = await repository.get_store_profile()
    areas = await repository.list_municipal_areas(store_id=store.id)
    lighting_area = next(area for area in areas if area.name == "Alumbrado público")
    streets_area = next(area for area in areas if area.name == "Mantenimiento de calles")

    await repository.create_municipal_case(
        store_id=store.id,
        payload=MunicipalCaseCreateRequest(
            area_id=lighting_area.id,
            title="Farola apagada",
            description="No enciende desde anoche.",
        ),
    )
    street_case = await repository.create_municipal_case(
        store_id=store.id,
        payload=MunicipalCaseCreateRequest(
            area_id=streets_area.id,
            title="Bache grande",
            description="Se agrandó después de la lluvia.",
        ),
    )

    filtered = await repository.list_municipal_cases(store_id=store.id, area_id=streets_area.id)

    assert [case.id for case in filtered] == [street_case.id]

    await close_repository(repository, runtime)


async def test_get_municipal_case_returns_the_persisted_snapshot(tmp_path: Path):
    """Municipal cases can be loaded back explicitly by primary key."""
    repository, runtime = await build_municipal_repository(tmp_path)
    store = await repository.get_store_profile()
    area = next(
        area for area in await repository.list_municipal_areas(store_id=store.id) if area.name == "Alumbrado público"
    )
    created = await repository.create_municipal_case(
        store_id=store.id,
        payload=MunicipalCaseCreateRequest(
            area_id=area.id,
            title="Semáforo sin luz",
            description="Hace falta revisar la luminaria del cruce.",
        ),
    )

    loaded = await repository.get_municipal_case(created.id)

    assert loaded.id == created.id
    assert loaded.title == "Semáforo sin luz"

    await close_repository(repository, runtime)


async def test_store_channel_runtime_returns_none_for_incomplete_connections(tmp_path: Path):
    """Inactive or incomplete channel rows should not be treated as runtime-ready."""
    repository, runtime = await build_repository(tmp_path)

    await repository.update_store_channel_connection(
        store_id=1,
        channel=Channel.WHATSAPP,
        provider=ChannelProvider.KAPSO,
        payload=StoreChannelConnectionUpdateRequest(
            phone_number_id="only-phone-id",
            api_key=None,
            webhook_secret=None,
            is_active=True,
        ),
    )

    assert (
        await repository.get_store_channel_runtime_config(
            store_id=1,
            channel=Channel.WHATSAPP,
            provider=ChannelProvider.KAPSO,
        )
        is None
    )
    assert (
        await repository.get_channel_runtime_config_by_phone_number(
            channel=Channel.WHATSAPP,
            provider=ChannelProvider.KAPSO,
            phone_number_id="only-phone-id",
        )
        is None
    )

    await close_repository(repository, runtime)


async def test_staff_user_refresh_rehashes_password_and_updates_role(tmp_path: Path):
    """Refreshing an existing staff user updates both credentials and membership role."""
    repository, runtime = await build_repository(tmp_path)

    created = await repository.ensure_staff_user(
        email="staff@example.com",
        full_name="Staff One",
        password="first-secret",
        store_id=1,
        role=StaffRole.STAFF,
    )
    refreshed = await repository.ensure_staff_user(
        email="staff@example.com",
        full_name="Staff Updated",
        password="second-secret",
        store_id=1,
        role=StaffRole.MANAGER,
    )
    by_id = await repository.get_staff_user_by_id(created.id)
    invalid_login = await repository.authenticate_staff_user(email="staff@example.com", password="first-secret")
    memberships = await repository.list_store_memberships_for_staff_user(created.id)

    assert refreshed.full_name == "Staff Updated"
    assert by_id is not None
    assert by_id.full_name == "Staff Updated"
    assert invalid_login is None
    assert memberships[0].role == StaffRole.MANAGER

    await close_repository(repository, runtime)


async def test_get_customer_and_list_customers_roundtrip(tmp_path: Path):
    """Customers can be loaded explicitly and listed by recent activity."""
    repository, runtime = await build_repository(tmp_path)
    first = await repository.get_or_create_customer(channel=Channel.DEV, external_id="cust-1", phone_number="3511")
    second = await repository.get_or_create_customer(channel=Channel.DEV, external_id="cust-2", phone_number="3512")

    loaded = await repository.get_customer(first.id)
    customers = await repository.list_customers(limit=10)

    assert loaded.id == first.id
    assert {customer.id for customer in customers} >= {first.id, second.id}

    await close_repository(repository, runtime)


async def test_ping_database_and_municipal_bootstrap_are_idempotent(tmp_path: Path):
    """Re-running init_database should keep municipal seeds and connectivity healthy."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'municipal-idempotent.db'}",
        store_name="Municipio Test",
        bot_name="Moony Test",
        store_vertical=StoreVertical.MUNICIPAL,
        kapso_api_key=SecretStr("kapso-seeded"),
        kapso_phone_number_id="phone-id-seeded",
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    await ping_database(runtime)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        initial_areas = await repository.list_municipal_areas(store_id=settings.default_store_id)
        initial_connection = await repository.get_store_channel_connection(
            store_id=settings.default_store_id,
            channel=Channel.WHATSAPP,
            provider=ChannelProvider.KAPSO,
        )
        await session.commit()

    await init_database(settings=settings, runtime=runtime)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        rerun_areas = await repository.list_municipal_areas(store_id=settings.default_store_id)
        rerun_connection = await repository.get_store_channel_connection(
            store_id=settings.default_store_id,
            channel=Channel.WHATSAPP,
            provider=ChannelProvider.KAPSO,
        )
        await session.commit()

    assert len(rerun_areas) == len(initial_areas)
    assert rerun_connection.id == initial_connection.id

    await runtime.engine.dispose()
