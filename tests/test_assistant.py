"""Tests for the transactional ordering assistant."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
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
