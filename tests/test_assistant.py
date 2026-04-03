"""Tests for the transactional ordering assistant."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ruperto.assistant import OrderingAssistantService, build_google_model
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import Channel
from ruperto.repository import BusinessRepository
from ruperto.schemas import AssistantNextStep

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
    assert model.model_name == "gemini-2.5-flash"


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
    assert "tu nombre" in first_reply.reply.reply_text.lower()
    assert second_reply.customer.name == "Martina"
    assert second_reply.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "¿Qué querés pedir hoy?" in second_reply.reply.reply_text

    await runtime.engine.dispose()


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


async def test_name_candidate_heuristics_cover_edge_cases(tmp_path: Path):
    """Name extraction accepts short names and rejects noisy ordering content."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    assert service._extract_name_candidate("martina") == "Martina"
    assert service._extract_name_candidate("   ") is None
    assert service._extract_name_candidate("Juan 123") is None
    assert service._extract_name_candidate("hola quiero pizza") is None
    assert service._extract_name_candidate("Juan Carlos Perez Gomez") is None
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

    await runtime.engine.dispose()
