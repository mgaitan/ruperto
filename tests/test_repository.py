"""Tests for the MVP repository layer."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ruperto.config import Settings
from ruperto.db import DatabaseRuntime, create_database_runtime, init_database
from ruperto.models import Channel, DeliveryType, OrderStatus, PaymentMethod
from ruperto.repository import BusinessRepository, normalize_phone_number

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
    assert menu
    assert search[0].sku == "hamburguesa-completa"
    assert memory.favorite_item_name is None
    assert normalize_phone_number("+54 351-555-7788") == "+543515557788"
    assert normalize_phone_number("351 555 7788") == "3515557788"
    assert normalize_phone_number("   ") is None

    await close_repository(repository, runtime)


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
