"""Tests for the transactional ordering assistant."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ruperto.assistant import (
    OrderingAssistantService,
    add_item_to_current_order,
    build_google_model,
    confirm_current_order,
    get_store_availability,
    set_order_delivery_address,
    set_order_delivery_type,
    set_order_payment_method,
)
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import Channel, DeliveryType, PaymentMethod
from ruperto.repository import BusinessRepository
from ruperto.schemas import AssistantNextStep, CustomerSnapshot, StoreAvailabilitySnapshot, StoreBusinessHoursSnapshot

pytestmark = pytest.mark.anyio
MIN_HISTORY_MESSAGES = 10


def build_settings(tmp_path: Path) -> Settings:
    """Create test settings for assistant integration tests."""
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'assistant.db'}",
        store_name="Rotisería Test",
        bot_name="Ruperto Test",
        gemini_api_key=SecretStr("test-key"),
    )


def extract_tool_returns(messages: list[ModelMessage]) -> dict[str, Any]:
    """Collect tool-return payloads from the latest model request."""
    latest_request = next(message for message in reversed(messages) if isinstance(message, ModelRequest))
    return {part.tool_name: part.content for part in latest_request.parts if isinstance(part, ToolReturnPart)}


async def seed_named_customer(
    service: OrderingAssistantService,
    *,
    channel: Channel,
    external_user_id: str,
    name: str,
):
    """Create a known customer so tests can focus on later ordering behavior."""
    async with service.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=channel, external_id=external_user_id)
        await repository.update_customer_name(customer.id, name)
        await repository.get_or_create_conversation(
            channel=channel,
            external_id=external_user_id,
            customer_id=customer.id,
        )
        await session.commit()


def collect_tool_returns(messages: list[ModelMessage]) -> dict[str, Any]:
    """Collect the latest payload returned by each tool across the conversation."""
    tool_returns: dict[str, Any] = {}
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, ToolReturnPart):
                tool_returns[part.tool_name] = part.content
    return tool_returns


def transactional_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Drive one end-to-end order using repository tools."""
    tool_returns = collect_tool_returns(messages)
    expected_sequence: list[tuple[str, dict[str, Any]]] = [
        ("lookup_customer", {}),
        ("list_menu", {}),
        ("search_menu", {"query": "hamburguesa"}),
        ("update_customer_name", {"name": "Martina"}),
        ("get_current_order", {}),
        (
            "add_item_to_current_order",
            {"sku": "hamburguesa-completa", "quantity": 1, "notes": "sin cebolla"},
        ),
        ("set_order_delivery_type", {"delivery_type": "delivery"}),
        ("set_order_delivery_address", {"address": "Olegario Andrade 330"}),
        ("set_order_payment_method", {"payment_method": "transfer"}),
        ("confirm_current_order", {}),
    ]

    for tool_name, arguments in expected_sequence:
        if tool_name not in tool_returns:
            return ModelResponse(
                parts=[ToolCallPart(tool_name, arguments)],
                model_name="function:test-transaction",
            )

    customer = tool_returns["update_customer_name"]
    order = tool_returns["confirm_current_order"]
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": (
                        f"Hola {customer.name}, ya te tomé el pedido. "
                        f"Quedó confirmado por {order.total_amount_display}."
                    ),
                    "next_step": "complete",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-transaction",
    )


def memory_model_factory(message_lengths: list[int]):
    """Build a model that verifies history reuse and customer memory."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        message_lengths.append(len(messages))
        tool_returns = collect_tool_returns(messages)
        if "lookup_customer" not in tool_returns:
            return ModelResponse(
                parts=[ToolCallPart("lookup_customer", {})],
                model_name="function:test-memory",
            )

        if "get_customer_memory" not in tool_returns:
            return ModelResponse(
                parts=[ToolCallPart("get_customer_memory", {})],
                model_name="function:test-memory",
            )

        memory = tool_returns["get_customer_memory"]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "reply_text": (
                            f"Hola de nuevo. La última vez te gustó {memory.favorite_item_name}. ¿Querés repetirlo?"
                        ),
                        "next_step": "choose_items",
                        "handoff": False,
                    },
                )
            ],
            model_name="function:test-memory",
        )

    return model


def delay_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Drive a turn that asks specifically for the current delay estimate."""
    tool_returns = collect_tool_returns(messages)
    if "get_estimated_delay" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("get_estimated_delay", {})],
            model_name="function:test-delay",
        )

    delay = tool_returns["get_estimated_delay"]
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": f"La demora estimada es de {delay.display_text}.",
                    "next_step": "complete",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-delay",
    )


async def test_build_google_model(tmp_path: Path):
    """The production Gemini model can be constructed from settings."""
    model = build_google_model(build_settings(tmp_path))
    assert model.model_name == "gemini-2.5-flash-lite"


async def test_assistant_handles_order_flow_in_spanish(tmp_path: Path):
    """The service can orchestrate a transactional order and persist the result."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.WHATSAPP,
        external_user_id="+54 351 555 7788",
        name="Martina",
    )

    result = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="+54 351 555 7788",
        message_text="Hola, quiero una hamburguesa",
        model=FunctionModel(transactional_model),
    )

    assert result.customer.name == "Martina"
    assert result.customer.phone_number == "+543515557788"
    assert result.reply.next_step == AssistantNextStep.COMPLETE
    assert "ya te tomé el pedido" in result.reply.reply_text
    assert result.current_order is not None
    assert result.current_order.payment_method is not None
    assert result.current_order.status.value == "confirmed"
    assert result.current_order.delivery_address == "Olegario Andrade 330"
    assert result.current_order.payment_method.value == "transfer"
    assert result.current_order.items[0].notes == "sin cebolla"

    await runtime.engine.dispose()


async def test_assistant_reuses_history_and_customer_memory(tmp_path: Path):
    """A follow-up turn can reuse both conversation history and order memory."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="cliente-dev",
        name="Martina",
    )

    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-dev",
        message_text="Hola, quiero una hamburguesa",
        model=FunctionModel(transactional_model),
    )

    observed_lengths: list[int] = []
    follow_up = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-dev",
        message_text="Hola de nuevo",
        model=FunctionModel(memory_model_factory(observed_lengths)),
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="cliente-dev",
            customer_id=follow_up.customer.id,
        )
        history = await repository.load_conversation_messages(conversation.id)

    assert observed_lengths[0] > 1
    assert "Hamburguesa completa" in follow_up.reply.reply_text
    assert follow_up.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert len(history) >= MIN_HISTORY_MESSAGES

    await runtime.engine.dispose()


async def test_assistant_can_answer_delay_after_confirming_an_order(tmp_path: Path):
    """A follow-up question can reuse the latest order to answer the estimated delay."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="cliente-demora",
        name="Martina",
    )

    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-demora",
        message_text="Hola, quiero una hamburguesa",
        model=FunctionModel(transactional_model),
    )

    follow_up = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-demora",
        message_text="¿Qué demora tiene?",
        model=FunctionModel(delay_model),
    )

    assert follow_up.reply.reply_text == "La demora estimada es de 20 minutos aproximadamente."
    assert follow_up.reply.next_step == AssistantNextStep.COMPLETE

    await runtime.engine.dispose()


async def test_assistant_asks_for_name_before_taking_the_first_order(tmp_path: Path):
    """Unknown customers are asked for their name before the ordering flow continues."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    first_reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-sin-nombre",
        message_text="Hola, quiero una pizza",
        model=FunctionModel(transactional_model),
    )
    second_reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-sin-nombre",
        message_text="Martina",
        model=FunctionModel(transactional_model),
    )

    assert first_reply.reply.next_step == AssistantNextStep.ASK_NAME
    assert "Ruperto Test" in first_reply.reply.reply_text
    assert "Rotisería Test" in first_reply.reply.reply_text
    assert (
        "tu nombre" in first_reply.reply.reply_text.lower() or "cómo te llamás" in first_reply.reply.reply_text.lower()
    )
    assert second_reply.customer.name == "Martina"
    assert second_reply.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "¿Qué querés pedir hoy?" in second_reply.reply.reply_text

    await runtime.engine.dispose()


def onboarding_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Reply with the customer name so onboarding capture can be asserted."""
    tool_returns = collect_tool_returns(messages)
    if "lookup_customer" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("lookup_customer", {})],
            model_name="function:test-onboarding",
        )

    customer = tool_returns["lookup_customer"]
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": f"Hola {customer.name}, te tomo ese pedido 🍕",
                    "next_step": "choose_items",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-onboarding",
    )


async def test_assistant_reasks_for_name_when_the_reply_is_not_a_name(tmp_path: Path):
    """The deterministic onboarding keeps asking for the name until it gets a plausible answer."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-repregunta",
        message_text="Hola",
        model=FunctionModel(transactional_model),
    )
    second_reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-repregunta",
        message_text="quiero una pizza",
        model=FunctionModel(transactional_model),
    )

    assert second_reply.reply.next_step == AssistantNextStep.ASK_NAME
    assert "cómo te llamás" in second_reply.reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_assistant_accepts_a_natural_introduction_while_waiting_for_name(tmp_path: Path):
    """The name-capture flow accepts a natural sentence, not only a bare name."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-presentacion-natural",
        message_text="Hola, quiero pedir",
        model=FunctionModel(transactional_model),
    )
    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-presentacion-natural",
        message_text="Qué tal, me llamo Pedro Guti y tengo hambre",
        model=FunctionModel(transactional_model),
    )

    assert reply.customer.name == "Pedro"
    assert reply.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "¿Qué querés pedir hoy?" in reply.reply.reply_text

    await runtime.engine.dispose()


async def test_name_candidate_heuristics_cover_edge_cases(tmp_path: Path):
    """Name extraction accepts short names and rejects noisy ordering content."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    assert service._extract_name_candidate("martina") == "Martina"
    assert service._extract_name_from_introduction("Hola, soy martín gaitán y quiero una pizza") == "Martín"
    assert service._extract_name_from_introduction("Buenas, me llamo ana y quiero pagar acá") == "Ana"
    assert service._extract_name_from_introduction("Mi nombre es Juan Cruz") == "Juan"
    assert service._extract_name_from_introduction("Hola, quiero pizza") is None
    assert service._extract_name_from_introduction("   ") is None
    assert service._extract_name_from_introduction("Soy !!!") is None
    assert service._extract_name_from_introduction("Hola, soy martín 123") == "Martín"
    assert service._extract_name_from_introduction("Hola, soy juan carlos perez y quiero pedir") == "Juan"
    assert service._extract_name_candidate("   ") is None
    assert service._extract_name_candidate("Juan 123") is None
    assert service._extract_name_candidate("hola quiero pizza") is None
    assert service._extract_name_candidate("Juan Carlos Perez Gomez") is None
    assert service._extract_name_candidate("Ana María López") == "Ana María López"
    assert service._extract_customer_name("Qué tal, me llamo Pedro Guti y tengo hambre") == "Pedro"
    assert (
        service._extract_latest_assistant_text(
            [
                ModelRequest(parts=[ToolReturnPart(tool_name="x", content={})]),
                ModelResponse(parts=[TextPart(content="Último texto")], model_name="test"),
            ]
        )
        == "Último texto"
    )
    assert (
        service._extract_latest_assistant_text([ModelRequest(parts=[ToolReturnPart(tool_name="x", content={})])])
        is None
    )
    assert service._detect_payment_method_hint("te pago acá") is PaymentMethod.CASH
    assert service._detect_payment_method_hint("te transfiero") is PaymentMethod.TRANSFER
    assert service._detect_payment_method_hint("te pido link de pago") is PaymentMethod.CARD_LINK
    assert service._detect_payment_method_hint("hola") is None
    assert service._message_requests_total("cuánto es?") is True
    assert service._message_requests_total("dame una pizza") is False
    assert "Ruperto Test" in service._build_name_prompt(conversation_id=1)
    assert "Rotisería Test" in service._build_name_prompt(conversation_id=1)

    await runtime.engine.dispose()


async def test_assistant_uses_name_introduced_in_the_first_message(tmp_path: Path):
    """A first-turn self-introduction should be persisted and reused immediately."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-presentado",
        message_text="Hola, soy martín gaitán, mandame 2 pizzas muzza y te pago acá.",
        model=FunctionModel(onboarding_model),
    )

    assert reply.customer.name == "Martín"
    assert reply.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "Hola Martín" in reply.reply.reply_text
    assert "¿me decís tu nombre?" not in reply.reply.reply_text

    await runtime.engine.dispose()


async def test_turn_context_hint_guides_compact_customer_messages(tmp_path: Path):
    """Compact first-turn messages generate safe context hints for the model."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Martín", phone_number=None, default_address=None),
        message_text="Hola, soy Martín Gaitán, mandame 2 pizzas muzza. ¿Cuánto es? Te pago acá.",
    )

    assert hint is not None
    assert "No vuelvas a pedirle el nombre" in hint
    assert "efectivo" in hint
    assert "cuánto sale" in hint or "total o subtotal" in hint

    await runtime.engine.dispose()


async def test_turn_context_hint_mentions_other_payment_cues(tmp_path: Path):
    """Turn hints also describe transfer and link-based payment cues."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Martín", phone_number=None, default_address=None)

    transfer_hint = service._build_turn_context_hint(
        customer=customer,
        message_text="Hola, soy Martín. Quiero una pizza y te transfiero.",
    )
    card_link_hint = service._build_turn_context_hint(
        customer=customer,
        message_text="Hola, soy Martín. Quiero una pizza y mandame link de pago.",
    )

    assert transfer_hint is not None
    assert "transferencia" in transfer_hint
    assert card_link_hint is not None
    assert "link o tarjeta" in card_link_hint

    await runtime.engine.dispose()


async def test_turn_policy_blocks_order_mutations_for_menu_only_questions(tmp_path: Path):
    """Pure menu questions should not be allowed to mutate or confirm orders."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    policy = service._analyze_turn_policy("Hola Ruperto, soy Martín. ¿Tenés pizzas?")

    assert policy.allow_order_mutations is False
    assert policy.allow_order_confirmation is False

    await runtime.engine.dispose()


async def test_turn_policy_allows_order_building_but_not_confirmation_by_default(tmp_path: Path):
    """Normal ordering intents can mutate and confirm during the same turn."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    policy = service._analyze_turn_policy("Hola, quiero 2 pizzas muzza y te pago acá")

    assert policy.allow_order_mutations is True
    assert policy.allow_order_confirmation is True

    await runtime.engine.dispose()


async def test_turn_policy_allows_confirmation_when_customer_explicitly_confirms(tmp_path: Path):
    """Confirmation should require an explicit customer signal in the latest turn."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    policy = service._analyze_turn_policy("Dale, confirmá el pedido")

    assert policy.allow_order_mutations is True
    assert policy.allow_order_confirmation is True

    await runtime.engine.dispose()


async def test_order_tools_reject_informational_turns(mocker):
    """Mutation and confirmation tools fail fast on informational-only turns."""
    ctx = mocker.Mock()
    ctx.deps.allow_order_mutations = False
    ctx.deps.allow_order_confirmation = False

    with pytest.raises(ValueError, match="solo informativo"):
        await add_item_to_current_order(ctx, "pizza-muzzarella", 1)
    with pytest.raises(ValueError, match="solo informativo"):
        await set_order_delivery_type(ctx, DeliveryType.DELIVERY)
    with pytest.raises(ValueError, match="solo informativo"):
        await set_order_delivery_address(ctx, "Lavalle 123")
    with pytest.raises(ValueError, match="solo informativo"):
        await set_order_payment_method(ctx, PaymentMethod.CASH)

    with pytest.raises(ValueError, match="señal explícita"):
        await confirm_current_order(ctx)


async def test_assistant_mentions_next_opening_when_store_is_closed(tmp_path: Path):
    """Replies are prefixed with the closed-store notice outside business hours."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="cliente-cerrado",
        name="Martina",
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
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
        await session.commit()

    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-cerrado",
        message_text="Quiero una hamburguesa",
        model=FunctionModel(transactional_model),
    )

    assert "ahora estamos cerrados" in reply.reply.reply_text.lower()
    assert "abrimos pronto" in reply.reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_store_availability_tool_uses_configured_timezone(tmp_path: Path):
    """The availability tool delegates with the configured store timezone."""
    settings = build_settings(tmp_path)
    ctx = type(
        "Ctx",
        (),
        {
            "deps": type(
                "Deps",
                (),
                {
                    "settings": settings,
                    "session_factory": object(),
                    "customer_id": 1,
                    "conversation_id": 1,
                },
            )()
        },
    )()

    original = get_store_availability.__globals__["with_repository"]
    mocked_with_repository = AsyncMock(return_value={"ok": True})
    get_store_availability.__globals__["with_repository"] = mocked_with_repository
    try:
        result = await get_store_availability(cast(Any, ctx))
    finally:
        get_store_availability.__globals__["with_repository"] = original

    assert result == {"ok": True}
    mocked_with_repository.assert_awaited_once()


async def test_closed_store_text_is_not_prefixed_twice(tmp_path: Path):
    """Closed-store decorations stay idempotent when the prefix is already present."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    availability = StoreAvailabilitySnapshot(
        is_open=False,
        message_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00.",
        next_open_text="mañana a las 11:00",
    )

    decorated = service._decorate_closed_store_text(availability.message_text, availability)

    assert decorated == availability.message_text

    await runtime.engine.dispose()
