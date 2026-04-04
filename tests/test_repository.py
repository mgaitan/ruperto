"""Tests for the MVP repository layer."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import create_engine, select, text

from ruperto.config import Settings
from ruperto.db import DatabaseRuntime, create_database_runtime, init_database
from ruperto.models import Channel, DeliveryType, MenuItem, OrderStatus, PaymentMethod
from ruperto.repository import BusinessRepository, normalize_phone_number
from ruperto.schemas import StoreBusinessHoursSnapshot

pytestmark = pytest.mark.anyio
EXPECTED_HISTORY_MESSAGES = 2
EXPECTED_ORDER_TOTAL = 1900000
EXPECTED_SINGLE_BURGER_TOTAL = 950000
EXPECTED_DELAY_MINUTES = 22
DEFAULT_DELAY_MINUTES = 25
DRAFT_DELAY_MINUTES = 15
PICKUP_DELAY_MINUTES = 15
LARGE_ORDER_DELAY_MINUTES = 31
KITCHEN_LOAD_DELAY_MINUTES = 18
EMPTY_DRAFT_DELAY_MINUTES = 15
DEFAULT_WEEKLY_HOURS = 7
UPDATED_WEEKLY_HOURS = 2
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
    assert delay.estimated_minutes == EXPECTED_DELAY_MINUTES
    assert delay.display_text == "22 minutos aproximadamente"
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
    assert scheduled.requested_ready_at.replace(tzinfo=None) - delivery_scheduled.preparation_starts_at == timedelta(
        minutes=20
    )

    await close_repository(repository, runtime)


async def test_set_order_requested_ready_at_rejects_closed_or_too_soon_slots(tmp_path: Path):
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

    local_ready = datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0) + timedelta(hours=2)
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
    too_soon = datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0) + timedelta(minutes=5)
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
    assert delay.estimated_minutes == LARGE_ORDER_DELAY_MINUTES
    assert delay.display_text == "31 minutos aproximadamente"

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
    assert delay.estimated_minutes == KITCHEN_LOAD_DELAY_MINUTES
    assert delay.display_text == "18 minutos aproximadamente"

    await close_repository(repository, runtime)


async def test_update_order_status_requires_an_existing_order(tmp_path: Path):
    """Staff operations fail clearly when the order id does not exist."""
    repository, runtime = await build_repository(tmp_path)

    with pytest.raises(ValueError, match=re.escape("No se encontró el pedido solicitado.")):
        await repository.update_order_status(999, OrderStatus.ALMOST_READY)

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
                opens_at="18:00",
                closes_at="23:30",
                closed=False,
            ),
        ]
    )

    assert len(updated) == UPDATED_WEEKLY_HOURS
    assert updated[0].closed is True
    assert updated[1].opens_at == "18:00"

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
