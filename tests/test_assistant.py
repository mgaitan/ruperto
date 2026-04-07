"""Tests for the transactional ordering assistant."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ruperto.assistant import (
    MODEL_UNAVAILABLE_REPLY,
    InformationalTurnMutationError,
    MissingConfirmationSignalError,
    OrderingAssistantService,
    TurnPolicy,
    add_item_to_current_order,
    build_google_model,
    confirm_current_order,
    get_store_availability,
    reset_current_order,
    set_order_delivery_address,
    set_order_delivery_type,
    set_order_payment_method,
)
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import Channel, DeliveryType, OrderStatus, PaymentMethod, StoreVertical
from ruperto.repository import BusinessRepository, IncompleteOrderError
from ruperto.schemas import (
    AssistantNextStep,
    AssistantReply,
    CustomerSnapshot,
    DelayEstimateSnapshot,
    OrderItemSnapshot,
    OrderSnapshot,
    StoreAvailabilitySnapshot,
    StoreBusinessHoursSnapshot,
    StoreProfileSnapshot,
)

pytestmark = pytest.mark.anyio
MIN_HISTORY_MESSAGES = 10
RETRY_SUCCESS_CALLS = 2
STORE_TIMEZONE = "America/Argentina/Cordoba"


def build_settings(tmp_path: Path) -> Settings:
    """Create test settings for assistant integration tests."""
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'assistant.db'}",
        store_name="Rotisería Test",
        bot_name="Ruperto Test",
        gemini_model="gemini-2.5-flash-lite",
        gemini_api_key=SecretStr("test-key"),
    )


def extract_tool_returns(messages: list[ModelMessage]) -> dict[str, Any]:
    """Collect tool-return payloads from the latest model request."""
    latest_request = next(message for message in reversed(messages) if isinstance(message, ModelRequest))
    return {part.tool_name: part.content for part in latest_request.parts if isinstance(part, ToolReturnPart)}


def build_store_profile() -> StoreProfileSnapshot:
    """Return a minimal store profile snapshot for deterministic helper tests."""
    return StoreProfileSnapshot(
        id=1,
        slug="rotiseria-test",
        store_name="Rotisería Test",
        bot_name="Ruperto Test",
        store_location="Córdoba",
        store_description="Comida casera",
        assistant_personality="Amable",
        vertical=StoreVertical.ORDERING,
        locale="es_AR",
        currency_code="ARS",
        transfer_alias="rotiseria.test",
    )


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
    """Drive one full checkout turn up to the explicit confirmation prompt."""
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
    ]

    for tool_name, arguments in expected_sequence:
        if tool_name not in tool_returns:
            return ModelResponse(
                parts=[ToolCallPart(tool_name, arguments)],
                model_name="function:test-transaction",
            )

    customer = tool_returns["update_customer_name"]
    order = tool_returns["set_order_payment_method"]
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": (
                        f"Hola {customer.name}, ya quedó armado el pedido por {order.total_amount_display}. "
                        "Si está bien, confirmámelo."
                    ),
                    "next_step": "confirm_order",
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


def add_item_only_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Create a draft order without confirming it."""
    tool_returns = collect_tool_returns(messages)
    if "get_current_order" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("get_current_order", {})],
            model_name="function:test-add-item-only",
        )
    if "add_item_to_current_order" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("add_item_to_current_order", {"sku": "hamburguesa-completa", "quantity": 1})],
            model_name="function:test-add-item-only",
        )
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": "Anoté la hamburguesa.",
                    "next_step": "choose_delivery",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-add-item-only",
    )


def price_comparison_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Answer a simple menu comparison without asking for the customer's name."""
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": "El lomito completo sale más que la milanesa completa.",
                    "next_step": "choose_items",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-price-comparison",
    )


def colloquial_not_found_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Pretend the model missed a colloquial menu alias."""
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": "No encontré ese producto en el menú.",
                    "next_step": "choose_items",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-colloquial-not-found",
    )


def unsupported_customization_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Pretend the model is wrongly promising an unsupported customization."""
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": (
                        "Sí, ya anotamos tu hamburguesa con doble picante y triple cheddar. La vamos a preparar así."
                    ),
                    "next_step": "choose_items",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-unsupported-customization",
    )


def unsupported_customization_checkout_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Pretend the model ignored the feasibility question and jumped to checkout."""
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": (
                        "Anotado, Nico: hasta ahora va 1 x Hamburguesa picante por $ 12.100. "
                        "¿Querés envío o retirás por el local?"
                    ),
                    "next_step": "choose_delivery",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-unsupported-customization-checkout",
    )


def delivery_info_handoff_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Return a poor delivery-info handoff so the service can stabilize it."""
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": "Te paso con una persona.",
                    "next_step": "handoff",
                    "handoff": True,
                },
            )
        ],
        model_name="function:test-delivery-info-handoff",
    )


def empty_draft_failure_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Create an empty draft via the tool and then crash to exercise cleanup."""
    tool_returns = collect_tool_returns(messages)
    if "get_current_order" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("get_current_order", {})],
            model_name="function:test-empty-draft-failure",
        )
    failure = RuntimeError("synthetic model failure after creating an empty draft")
    raise failure


async def test_build_google_model(tmp_path: Path):
    """The production Gemini model can be constructed from settings."""
    model = build_google_model(build_settings(tmp_path))
    assert model.model_name == "gemini-2.5-flash-lite"


async def test_assistant_returns_handoff_when_model_times_out(tmp_path: Path):
    """Timeouts from the model should degrade into a deterministic handoff reply."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    baseline_service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        baseline_service,
        channel=Channel.DEV,
        external_user_id="timeout-user",
        name="Martina",
    )

    class SlowAgent:
        async def run(self, *args: Any, **kwargs: Any):
            await asyncio.sleep(0.05)
            raise AssertionError

    service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, SlowAgent()),
    )
    service.settings.assistant_model_timeout_seconds = 0.001
    service.settings.assistant_model_retry_attempts = 1

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="timeout-user",
        message_text="decime qué tenés",
    )

    assert result.reply.reply_text == MODEL_UNAVAILABLE_REPLY
    assert result.reply.next_step == AssistantNextStep.HANDOFF
    assert result.reply.handoff is True
    assert result.current_order is None

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="timeout-user",
            customer_id=result.customer.id,
        )
        history = await repository.load_conversation_messages(conversation.id)
        assert service._extract_latest_assistant_text(history) == MODEL_UNAVAILABLE_REPLY

    await runtime.engine.dispose()


async def test_assistant_returns_handoff_when_model_errors(tmp_path: Path):
    """Provider failures should degrade into a deterministic handoff reply."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    baseline_service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        baseline_service,
        channel=Channel.DEV,
        external_user_id="error-user",
        name="Martina",
    )

    class FailingAgent:
        async def run(self, *args: Any, **kwargs: Any):
            raise RuntimeError

    service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, FailingAgent()),
    )
    service.settings.assistant_model_retry_attempts = 1

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="error-user",
        message_text="decime qué tenés",
    )

    assert result.reply.reply_text == MODEL_UNAVAILABLE_REPLY
    assert result.reply.next_step == AssistantNextStep.HANDOFF
    assert result.reply.handoff is True
    assert result.current_order is None

    await runtime.engine.dispose()


async def test_model_failure_recovers_parseable_orders_instead_of_leaving_empty_drafts(tmp_path: Path):
    """A failed turn should recover a parseable order instead of leaving an empty draft behind."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="empty-draft-failure-user",
        name="Martina",
    )

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="empty-draft-failure-user",
        message_text="Quiero una hamburguesa doble cheddar para retirar.",
        model=FunctionModel(empty_draft_failure_model),
    )

    assert "Hamburguesa doble cheddar" in result.reply.reply_text
    assert result.current_order is not None
    assert [item.name for item in result.current_order.items] == ["Hamburguesa doble cheddar"]

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="empty-draft-failure-user")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="empty-draft-failure-user",
            customer_id=customer.id,
        )
        latest_order = await repository.get_latest_order(customer.id, conversation.id)
        assert latest_order is not None
        assert [item.name for item in latest_order.items] == ["Hamburguesa doble cheddar"]

    await runtime.engine.dispose()


async def test_assistant_retries_once_before_fallback(tmp_path: Path):
    """Transient provider failures should be retried before degrading the turn."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    baseline_service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        baseline_service,
        channel=Channel.DEV,
        external_user_id="retry-user",
        name="Martina",
    )

    class FlakyAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: Any, **kwargs: Any):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError
            return type(
                "RunResult",
                (),
                {
                    "output": AssistantReply(
                        reply_text="Hola Martina, te tomo ese pedido 🍕",
                        next_step=AssistantNextStep.CHOOSE_ITEMS,
                        handoff=False,
                    ),
                    "new_messages": staticmethod(list),
                },
            )()

    flaky_agent = FlakyAgent()
    service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, flaky_agent),
    )
    service.settings.assistant_model_retry_attempts = 1

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="retry-user",
        message_text="quiero una hamburguesa",
    )

    assert flaky_agent.calls == RETRY_SUCCESS_CALLS
    assert "Hola Martina, te tomo ese pedido 🍕" in result.reply.reply_text
    assert result.reply.handoff is False

    await runtime.engine.dispose()


async def test_assistant_recovers_from_guardrail_errors_with_useful_replies(tmp_path: Path):
    """Guardrail exceptions should recover into deterministic next-step guidance."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    baseline_service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        baseline_service,
        channel=Channel.DEV,
        external_user_id="guardrail-user",
        name="Martina",
    )

    class GuardrailAgent:
        async def run(self, *args: Any, **kwargs: Any):
            raise MissingConfirmationSignalError

    service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, GuardrailAgent()),
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="guardrail-user")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="guardrail-user",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(customer.id, conversation.id, sku="pizza-rucula-crudo", quantity=1)
        await session.commit()

    confirmation_result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="guardrail-user",
        message_text="retiro",
    )
    assert confirmation_result.reply.next_step == AssistantNextStep.CHOOSE_PAYMENT
    assert "efectivo, transferencia o link de pago" in confirmation_result.reply.reply_text.lower()

    class InformationalGuardrailAgent:
        async def run(self, *args: Any, **kwargs: Any):
            raise InformationalTurnMutationError

    informational_service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, InformationalGuardrailAgent()),
    )

    informational_result = await informational_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="guardrail-user",
        message_text="Hola, ¿tenés para enviar?",
    )
    assert informational_result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "hacemos envíos" in informational_result.reply.reply_text.lower()

    menu_result = await informational_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="guardrail-user",
        message_text="¿Tenés postres?",
    )
    assert menu_result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "tenemos postres" in menu_result.reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_missing_confirmation_without_a_draft_still_raises(tmp_path: Path):
    """Missing-confirmation recovery should only run when a real draft exists."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    baseline_service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        baseline_service,
        channel=Channel.DEV,
        external_user_id="missing-confirmation-no-draft",
        name="Martina",
    )

    class GuardrailAgent:
        async def run(self, *args: Any, **kwargs: Any):
            raise MissingConfirmationSignalError

    service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, GuardrailAgent()),
    )

    with pytest.raises(MissingConfirmationSignalError):
        await service.handle_customer_message(
            channel=Channel.DEV,
            external_user_id="missing-confirmation-no-draft",
            message_text="confirmalo",
        )

    await runtime.engine.dispose()


async def test_assistant_handles_order_flow_in_spanish(tmp_path: Path):
    """The service can review a draft and confirm it only after explicit approval."""
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

    draft_result = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="+54 351 555 7788",
        message_text="Hola, quiero una hamburguesa",
        model=FunctionModel(transactional_model),
    )
    result = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="+54 351 555 7788",
        message_text="Confirmá el pedido.",
        model=FunctionModel(explicit_confirmation_model),
    )

    assert draft_result.customer.name == "Martina"
    assert draft_result.customer.phone_number == "+543515557788"
    assert draft_result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert draft_result.current_order is not None
    assert draft_result.current_order.status.value == "draft"
    assert "bebida o un postre" in draft_result.reply.reply_text.lower()
    assert result.customer.phone_number == "+543515557788"
    assert result.reply.next_step == AssistantNextStep.COMPLETE
    assert "**Pedido**" in result.reply.reply_text
    assert "**Pago**" in result.reply.reply_text
    assert result.current_order is not None
    assert result.current_order.payment_method is not None
    assert result.current_order.status.value == "confirmed"
    assert result.current_order.delivery_address == "Olegario Andrade 330"
    assert result.current_order.payment_method.value == "transfer"
    assert result.current_order.items[0].notes == "sin cebolla"

    await runtime.engine.dispose()


async def test_assistant_schedules_a_closed_store_order_for_a_future_time(tmp_path: Path):
    """A requested ready time should stay visible even if the assistant still offers an add-on."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="cliente-programado",
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
                    opens_at="00:00",
                    closes_at="23:59",
                    closed=False,
                )
                for weekday in range(7)
            ]
        )
        await session.commit()

    local_ready = datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0) + timedelta(days=1)
    local_ready = local_ready.replace(hour=12, minute=0)
    time_text = "12:00"
    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-programado",
        message_text=f"Quiero una hamburguesa para mañana a las {time_text}",
        model=FunctionModel(transactional_model),
    )

    assert reply.current_order is not None
    assert reply.current_order.requested_ready_at is not None
    assert reply.current_order.preparation_starts_at is not None
    assert reply.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "programado" in reply.reply.reply_text.lower()
    assert "12:00" in reply.reply.reply_text

    await runtime.engine.dispose()


async def test_assistant_reports_schedule_errors_without_overwriting_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An invalid requested time should surface the scheduling error instead of normal checkout guidance."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="cliente-hora-invalida",
        name="Martina",
    )

    fixed_local_now = datetime(2026, 4, 4, 12, 0, tzinfo=ZoneInfo(STORE_TIMEZONE))
    local_ready = fixed_local_now + timedelta(minutes=5)
    monkeypatch.setattr(
        service,
        "_extract_requested_ready_at",
        lambda message_text, *, timezone_name: local_ready.astimezone(UTC),
    )
    schedule_error = ValueError("Horario inválido de prueba.")

    async def raise_schedule_error(*args: Any, **kwargs: Any) -> OrderSnapshot:
        raise schedule_error

    monkeypatch.setattr(BusinessRepository, "set_order_requested_ready_at", raise_schedule_error)
    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-hora-invalida",
        message_text="Quiero una hamburguesa para las 12:05",
        model=FunctionModel(add_item_only_model),
    )

    assert "Horario inválido de prueba." in reply.reply.reply_text
    assert reply.reply.next_step == AssistantNextStep.CHOOSE_DELIVERY

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
    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-dev",
        message_text="Confirmá el pedido.",
        model=FunctionModel(explicit_confirmation_model),
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
    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-demora",
        message_text="Confirmá el pedido.",
        model=FunctionModel(explicit_confirmation_model),
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
        model=FunctionModel(resumed_order_model),
    )

    assert first_reply.reply.next_step == AssistantNextStep.ASK_NAME
    assert "Ruperto Test" in first_reply.reply.reply_text
    assert "Rotisería Test" in first_reply.reply.reply_text
    assert (
        "tu nombre" in first_reply.reply.reply_text.lower() or "cómo te llamás" in first_reply.reply.reply_text.lower()
    )
    assert second_reply.customer.name == "Martina"
    assert second_reply.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "hamburguesa completa" in second_reply.reply.reply_text.lower()
    assert "¿querés sumar algo más?" in second_reply.reply.reply_text.lower()
    assert second_reply.current_order is not None

    await runtime.engine.dispose()


async def test_assistant_allows_informational_first_turns_without_asking_for_name(tmp_path: Path):
    """Informational questions should not be blocked by the onboarding name gate."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-informativo",
        message_text="hola tenés para enviar?",
        model=FunctionModel(informational_delivery_model),
    )

    assert reply.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "hacemos envíos" in reply.reply.reply_text.lower()
    assert "tu nombre" not in reply.reply.reply_text.lower()
    assert reply.customer.name is None

    await runtime.engine.dispose()


async def test_assistant_strips_unsolicited_open_store_greeting(tmp_path: Path):
    """Generic greetings should not volunteer the current open-store notice."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    open_availability = StoreAvailabilitySnapshot(is_open=True, message_text="Estamos abiertos", next_open_text=None)

    async def fake_open_availability(self, *, store_id: int = 1, timezone_name: str | None = None):
        return open_availability

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(BusinessRepository, "get_store_availability", fake_open_availability)

    try:
        reply = await service.handle_customer_message(
            channel=Channel.DEV,
            external_user_id="cliente-saludo-generico",
            message_text="buenas noches",
            model=FunctionModel(open_greeting_model),
        )
    finally:
        monkeypatch.undo()

    assert "abiertos ahora" not in reply.reply.reply_text.lower()
    assert "¿en qué puedo ayudarte hoy?" in reply.reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_assistant_keeps_open_store_status_when_the_user_asks_for_it(tmp_path: Path):
    """Open-store status should remain when the customer explicitly asked about it."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-pregunta-horario",
        message_text="¿Están abiertos ahora?",
        model=FunctionModel(open_greeting_model),
    )

    assert "abiertos ahora" in reply.reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_assistant_does_not_ask_for_name_mid_conversation_when_order_starts_later(tmp_path: Path):
    """A nameless conversation that already started should not be reset by onboarding mid-flow."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-sin-nombre-en-flujo",
        message_text="hola tenés para enviar?",
        model=FunctionModel(informational_delivery_model),
    )
    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-sin-nombre-en-flujo",
        message_text="quiero una pizza",
        model=FunctionModel(transactional_model),
    )

    assert reply.reply.next_step != AssistantNextStep.ASK_NAME
    assert "tu nombre" not in reply.reply.reply_text.lower()

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


def informational_delivery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Answer an informational question without requiring a name upfront."""
    tool_returns = collect_tool_returns(messages)
    if "get_store_availability" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("get_store_availability", {})],
            model_name="function:test-informational-delivery",
        )

    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": "Sí, hacemos envíos. Si querés, decime qué te gustaría pedir y te paso opciones.",
                    "next_step": "choose_items",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-informational-delivery",
    )


def open_greeting_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Return a greeting that over-eagerly mentions the store is open."""
    tool_returns = collect_tool_returns(messages)
    if "get_store_availability" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("get_store_availability", {})],
            model_name="function:test-open-greeting",
        )

    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": (
                        "¡Hola! 👋 Buenas noches. Estamos abiertos ahora 🍽️ hasta las 23:00. ¿En qué puedo ayudarte hoy?"
                    ),
                    "next_step": "choose_items",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-open-greeting",
    )


def resumed_order_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Drive the second turn after onboarding resumes a previously pending request."""
    tool_returns = collect_tool_returns(messages)
    expected_sequence: list[tuple[str, dict[str, Any]]] = [
        ("lookup_customer", {}),
        ("search_menu", {"query": "hamburguesa"}),
        ("get_current_order", {}),
        ("add_item_to_current_order", {"sku": "hamburguesa-completa", "quantity": 1}),
    ]
    for tool_name, arguments in expected_sequence:
        if tool_name not in tool_returns:
            return ModelResponse(
                parts=[ToolCallPart(tool_name, arguments)],
                model_name="function:test-resumed-order",
            )

    customer = tool_returns["lookup_customer"]
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": (
                        f"Perfecto, {customer.name}: te sumé una hamburguesa completa. ¿Querés algo para tomar?"
                    ),
                    "next_step": "choose_delivery",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-resumed-order",
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
        message_text="quiero una pizza",
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
        model=FunctionModel(onboarding_model),
    )

    assert reply.customer.name == "Pedro"
    assert reply.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "Hola Pedro" in reply.reply.reply_text

    await runtime.engine.dispose()


async def test_assistant_resumes_pending_request_after_collecting_name(tmp_path: Path):
    """The assistant should remember the first request while asking for the customer's name."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    first_reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-pendiente",
        message_text="quiero una hamburguesa. ¿tenés bebida?",
        model=FunctionModel(transactional_model),
    )
    resumed_reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-pendiente",
        message_text="Martín",
        model=FunctionModel(resumed_order_model),
    )

    assert first_reply.reply.next_step == AssistantNextStep.ASK_NAME
    assert "así sigo con lo que me pediste" in first_reply.reply.reply_text.lower()
    assert resumed_reply.customer.name == "Martín"
    assert resumed_reply.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "hamburguesa completa" in resumed_reply.reply.reply_text.lower()
    assert "¿querés sumar algo más?" in resumed_reply.reply.reply_text.lower()
    assert resumed_reply.current_order is not None
    assert resumed_reply.current_order.items[0].name == "Hamburguesa completa"
    assert resumed_reply.current_order.delivery_type is None


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
    assert service._extract_customer_name("sí, soy martín") == "Martín"
    assert service._extract_customer_name("si, soy martin") == "Martin"
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
    assert service._has_prior_conversation([]) is False
    assert (
        service._has_prior_conversation(
            cast(list[ModelMessage], [ModelResponse(parts=[TextPart(content="Hola")], model_name="test")])
        )
        is True
    )
    assert service._detect_payment_method_hint("te pago acá") is PaymentMethod.CASH
    assert service._detect_payment_method_hint("cash") is PaymentMethod.CASH
    assert service._detect_payment_method_hint("te transfiero") is PaymentMethod.TRANSFER
    assert service._detect_payment_method_hint("te pido link de pago") is PaymentMethod.CARD_LINK
    assert service._detect_payment_method_hint("hola") is None
    assert service._message_requests_total("cuánto es?") is True
    assert service._message_requests_total("dame una pizza") is False
    assert "Ruperto Test" in service._build_name_prompt(conversation_id=1)
    assert "Rotisería Test" in service._build_name_prompt(conversation_id=1)
    assert service._should_store_pending_message_before_name("quiero una hamburguesa y bebida") is True
    assert service._should_store_pending_message_before_name("tenés postre?") is True
    assert service._should_store_pending_message_before_name("tenés menú?") is True
    assert service._should_store_pending_message_before_name("cuánto es?") is True
    assert service._should_store_pending_message_before_name("combo familiar") is True
    assert service._should_store_pending_message_before_name("   ") is False
    assert service._should_require_name_before_continuing("Quiero una pizza napolitana.") is True
    assert service._should_require_name_before_continuing("pizza") is True
    assert (
        service._should_require_name_before_continuing("¿Qué sale más, el lomito completo o la milanesa completa?")
        is False
    )
    assert service._should_require_name_before_continuing("Hola, ¿tenés para enviar?") is False
    assert service._message_is_informational_menu_question("¿Cuánto sale una pizza?") is True
    assert service._detect_delivery_type_hint("Hacé el envío, por favor.") is DeliveryType.DELIVERY
    assert (
        "así sigo con lo que me pediste"
        in service._build_name_prompt(
            conversation_id=1,
            remembers_pending_message=True,
        ).lower()
    )

    await runtime.engine.dispose()


async def test_name_capture_without_pending_message_replies_with_confirmation(tmp_path: Path, mocker):
    """If no pending request exists, a provided name gets a simple confirmation reply."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    repository = mocker.AsyncMock()
    updated_customer = CustomerSnapshot(id=1, name="Ana", phone_number=None, default_address=None)
    repository.update_customer_name.return_value = updated_customer

    history = cast(
        list[ModelMessage],
        [
            ModelResponse(
                parts=[TextPart(content="🙂 Necesito tu nombre para seguir. ¿Cómo te llamás?")],
                model_name="test",
            )
        ],
    )
    result = await service._maybe_handle_missing_customer_name(
        repository=repository,
        customer=CustomerSnapshot(id=1, name=None, phone_number=None, default_address=None),
        conversation_id=1,
        history=history,
        message_text="Ana",
        availability=StoreAvailabilitySnapshot(is_open=True, message_text=""),
        pending_customer_message=None,
        latest_assistant_text="🙂 Necesito tu nombre para seguir. ¿Cómo te llamás?",
    )

    assert result.direct_reply is not None
    assert result.direct_reply.reply.reply_text == "¡Gracias, Ana! 😄 ¿Qué querés pedir hoy?"
    repository.set_pending_customer_message.assert_not_called()

    await runtime.engine.dispose()


async def test_waiting_for_name_with_pending_message_resumes_without_direct_reply(tmp_path: Path, mocker):
    """If there was a pending request, providing the name should resume that request."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    repository = mocker.AsyncMock()
    updated_customer = CustomerSnapshot(id=1, name="Ana", phone_number=None, default_address=None)
    repository.update_customer_name.return_value = updated_customer

    result = await service._maybe_handle_missing_customer_name(
        repository=repository,
        customer=CustomerSnapshot(id=1, name=None, phone_number=None, default_address=None),
        conversation_id=1,
        history=cast(
            list[ModelMessage],
            [
                ModelResponse(
                    parts=[TextPart(content="🙂 Necesito tu nombre para seguir. ¿Cómo te llamás?")],
                    model_name="test",
                )
            ],
        ),
        message_text="Ana",
        availability=StoreAvailabilitySnapshot(is_open=True, message_text=""),
        pending_customer_message="quiero una pizza",
        latest_assistant_text="🙂 Necesito tu nombre para seguir. ¿Cómo te llamás?",
    )

    assert result.direct_reply is None
    assert result.resumed_pending_message == "quiero una pizza"
    repository.set_pending_customer_message.assert_awaited_once_with(1, None)

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


async def test_assistant_answers_price_comparisons_without_requiring_name(tmp_path: Path):
    """Pure menu comparisons should not trigger the onboarding gate."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="comparison-user",
        message_text="¿Qué sale más, el lomito completo o la milanesa completa?",
        model=FunctionModel(price_comparison_model),
    )

    assert result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "lomito completo sale más" in result.reply.reply_text.lower()
    assert result.customer.name is None

    await runtime.engine.dispose()


async def test_turn_context_hint_guides_compact_customer_messages(tmp_path: Path):
    """Compact first-turn messages generate safe context hints for the model."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Martín", phone_number=None, default_address=None),
        message_text="Hola, soy Martín Gaitán, mandame 2 pizzas muzza. ¿Cuánto es? Te pago acá.",
        current_order=None,
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
        current_order=None,
    )
    card_link_hint = service._build_turn_context_hint(
        customer=customer,
        message_text="Hola, soy Martín. Quiero una pizza y mandame link de pago.",
        current_order=None,
    )

    assert transfer_hint is not None
    assert "transferencia" in transfer_hint
    assert card_link_hint is not None
    assert "link o tarjeta" in card_link_hint

    await runtime.engine.dispose()


async def test_turn_context_hint_for_menu_prices_prefers_category_over_order_total(tmp_path: Path):
    """Price follow-ups about one category should not be reframed as order totals."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Martín", phone_number=None, default_address=None),
        message_text="¿Cuánto sale?",
        current_order=None,
        previous_user_message="¿Tenés gaseosas?",
    )

    assert hint is not None
    assert "consultando por gaseosas" in hint
    assert "no al total del pedido" in hint

    await runtime.engine.dispose()


async def test_turn_context_hint_marks_order_corrections_and_variant_splits(tmp_path: Path):
    """Corrections like `uno de cada` should be treated as fixes, not extra items."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    current_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1420000,
        total_amount_display="$ 14.200",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Lomito especial",
                quantity=1,
                unit_price_cents=1420000,
                unit_price_display="$ 14.200",
                notes=None,
            )
        ],
    )

    hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Ana", phone_number=None, default_address=None),
        message_text="dije uno de cada uno",
        current_order=current_order,
        latest_assistant_text=(
            "¿Qué tipo de lomo te gustaría, Ana? Tenemos:\n\nLomito completo ($13.200)\nLomito especial ($14.200)"
        ),
    )

    assert hint is not None
    assert "corrigiendo el pedido actual" in hint
    assert "reset_current_order" in hint
    assert "una unidad de cada opción" in hint

    await runtime.engine.dispose()


async def test_turn_context_hint_keeps_split_corrections_ambiguous_when_multiple_groups_were_offered(tmp_path: Path):
    """`Uno de cada` should ask for clarification if the previous turn mixed multiple product groups."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    current_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1420000,
        total_amount_display="$ 14.200",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Lomito especial",
                quantity=1,
                unit_price_cents=1420000,
                unit_price_display="$ 14.200",
                notes=None,
            )
        ],
    )

    hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Ana", phone_number=None, default_address=None),
        message_text="dije uno de cada uno",
        current_order=current_order,
        latest_assistant_text=(
            "Ana, tenemos dos opciones de lomos: Lomito completo ($13.200) y Lomito especial ($14.200). "
            "¿Cuáles preferís? Y para las papas, "
            "¿quisieras Papas fritas clásicas ($4.300) o Papas cheddar y bacon ($6.900)?"
        ),
    )

    assert hint is not None
    assert "sigue siendo ambiguo" in hint
    assert "interpretalo como una unidad de cada opción" not in hint

    await runtime.engine.dispose()


async def test_turn_context_hint_describes_missing_checkout_fields(tmp_path: Path):
    """Draft orders add explicit hints about whichever checkout field is still missing."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    draft_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1150000,
        total_amount_display="$ 11.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa BBQ",
                quantity=1,
                unit_price_cents=1150000,
                unit_price_display="$ 11.500",
                notes=None,
            )
        ],
    )

    hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Martín", phone_number=None, default_address="Lavalle 12333"),
        message_text="Efectivo",
        current_order=draft_order,
    )

    assert hint is not None
    assert "Pedido en curso" in hint
    assert "envío o retiro" in hint

    await runtime.engine.dispose()


async def test_turn_context_hint_describes_missing_address_and_payment(tmp_path: Path):
    """Draft context hints also cover missing address and missing payment details."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.DELIVERY,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=950000,
        total_amount_display="$ 9.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa completa",
                quantity=1,
                unit_price_cents=950000,
                unit_price_display="$ 9.500",
                notes=None,
            )
        ],
    )

    address_hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Martín", phone_number=None, default_address=None),
        message_text="Efectivo",
        current_order=order,
    )
    assert address_hint is not None
    assert "Falta pedir la dirección" in address_hint

    order.delivery_address = "Lavalle 12333"
    payment_hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Martín", phone_number=None, default_address=None),
        message_text="Efectivo",
        current_order=order,
    )
    assert payment_hint is not None
    assert "medio de pago" in payment_hint

    await runtime.engine.dispose()


async def test_turn_context_hint_mentions_requested_ready_time(tmp_path: Path):
    """Draft hints should mention when the customer already asked for a ready time."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    requested_ready_at = datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0) + timedelta(days=2)
    order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        requested_ready_at=requested_ready_at.astimezone(UTC),
        preparation_starts_at=(requested_ready_at - timedelta(minutes=15)).astimezone(UTC),
        total_amount_cents=950000,
        total_amount_display="$ 9.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa completa",
                quantity=1,
                unit_price_cents=950000,
                unit_price_display="$ 9.500",
                notes=None,
            )
        ],
    )

    hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Martín", phone_number=None, default_address=None),
        message_text="Te confirmo después",
        current_order=order,
    )

    assert hint is not None
    assert "tenerlo listo" in hint
    assert "el" in hint or "mañana" in hint

    await runtime.engine.dispose()


async def test_turn_context_hint_warns_against_repeating_closed_store_notice(tmp_path: Path):
    """Turn hints should mention when the assistant already warned about closed hours."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    hint = service._build_turn_context_hint(
        customer=CustomerSnapshot(id=1, name="Martín", phone_number=None, default_address=None),
        message_text="¿Y hacen envíos?",
        current_order=None,
        latest_assistant_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00. ¿Qué querés pedir?",
    )

    assert hint is not None
    assert "No repitas ese aviso" in hint

    await runtime.engine.dispose()


async def test_turn_policy_blocks_order_mutations_for_menu_only_questions(tmp_path: Path):
    """Pure menu questions should not be allowed to mutate or confirm orders."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    policy = service._analyze_turn_policy("Hola Ruperto, soy Martín. ¿Tenés pizzas?", current_order=None)

    assert policy.allow_order_mutations is False
    assert policy.allow_order_confirmation is False

    await runtime.engine.dispose()


async def test_answer_menu_information_turn_lists_real_options_for_gaseosas(tmp_path: Path):
    """Informational drink questions should be answered with concrete options and prices."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        reply = await service._answer_menu_information_turn(
            AssistantReply(reply_text="Sí, tenemos.", next_step=AssistantNextStep.CHOOSE_ITEMS),
            repository=repository,
            message_text="¿Tenés gaseosas?",
            previous_user_message=None,
            turn_policy=TurnPolicy(allow_order_mutations=False, allow_order_confirmation=False),
        )

    lowered_reply = reply.reply_text.lower()
    assert "tenemos gaseosas" in lowered_reply
    assert "gaseosa cola 1.5l" in lowered_reply
    assert "$ 3.200" in lowered_reply
    assert "sumo una al pedido" in lowered_reply

    await runtime.engine.dispose()


async def test_answer_menu_information_turn_uses_previous_focus_for_price_follow_up(tmp_path: Path):
    """A follow-up `¿cuánto sale?` should reuse the previous category in focus."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        reply = await service._answer_menu_information_turn(
            AssistantReply(
                reply_text="Tu pedido actual suma $ 11.900.",
                next_step=AssistantNextStep.CHOOSE_ITEMS,
            ),
            repository=repository,
            message_text="¿Cuánto sale?",
            previous_user_message="¿Tenés gaseosas?",
            turn_policy=TurnPolicy(allow_order_mutations=False, allow_order_confirmation=False),
        )

    lowered_reply = reply.reply_text.lower()
    assert "tenemos gaseosas" in lowered_reply
    assert "tu pedido actual suma" not in lowered_reply
    assert "¿cuál te gustaría sumar?" in lowered_reply

    await runtime.engine.dispose()


async def test_answer_menu_information_turn_respects_non_alcoholic_constraints(tmp_path: Path):
    """Category suggestions should filter out alcoholic drinks when the customer asks for non-alcoholic options."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        reply = await service._answer_menu_information_turn(
            AssistantReply(reply_text="Sí, tenemos bebidas.", next_step=AssistantNextStep.CHOOSE_ITEMS),
            repository=repository,
            message_text="¿Tenés algo para tomar sin alcohol?",
            previous_user_message="Dame un tiramisú y un cheesecake.",
            turn_policy=TurnPolicy(allow_order_mutations=False, allow_order_confirmation=False),
        )

    lowered_reply = reply.reply_text.lower()
    assert "agua" in lowered_reply
    assert "gaseosa" in lowered_reply
    assert "cerveza" not in lowered_reply

    await runtime.engine.dispose()


async def test_filter_menu_options_for_unknown_focus_returns_empty_list(tmp_path: Path):
    """Unknown informational focuses should not match any menu items."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    assert service._filter_menu_options_for_focus([], focus="desconocido") == []

    await runtime.engine.dispose()


async def test_parse_menu_helpers_skip_unknown_or_duplicate_segments(tmp_path: Path):
    """Deterministic order parsing should ignore unknown chunks and duplicate item mentions."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        menu_items = await repository.list_menu_items()

    parsed = service._parse_menu_lines_from_message(
        "Quiero 2 pizzas muzza, 2 pizzas muzza y algo raro.",
        menu_items=menu_items,
    )
    unknown_quantity, unknown_item = service._parse_menu_line_segment("!!!", menu_items=menu_items)
    low_score_quantity, low_score_item = service._parse_menu_line_segment("sorpresa alien", menu_items=menu_items)

    assert [(item.name, quantity) for item, quantity in parsed] == [("Pizza muzzarella", 2)]
    assert unknown_quantity == 1
    assert unknown_item is None
    assert low_score_quantity == 1
    assert low_score_item is None
    assert service._alias_match_score({"pizza"}, set()) == 0.0

    await runtime.engine.dispose()


async def test_answer_menu_information_turn_keeps_original_reply_when_no_items_match(tmp_path: Path):
    """If a focus yields no concrete menu items, the original reply should survive."""
    service = OrderingAssistantService(
        session_factory=cast(Any, None),
        settings=build_settings(tmp_path),
    )
    repository = AsyncMock()
    repository.list_menu_items.return_value = []

    reply = await service._answer_menu_information_turn(
        AssistantReply(reply_text="Sí, tenemos eso.", next_step=AssistantNextStep.CHOOSE_ITEMS),
        repository=cast(Any, repository),
        message_text="¿Tenés papas?",
        previous_user_message=None,
        turn_policy=TurnPolicy(allow_order_mutations=False, allow_order_confirmation=False),
    )

    assert reply.reply_text == "Sí, tenemos eso."


async def test_turn_policy_allows_order_building_but_not_confirmation_by_default(tmp_path: Path):
    """Normal ordering intents can mutate the draft without auto-confirming it."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    policy = service._analyze_turn_policy("Hola, quiero 2 pizzas muzza y te pago acá", current_order=None)

    assert policy.allow_order_mutations is True
    assert policy.allow_order_confirmation is False

    await runtime.engine.dispose()


async def test_turn_policy_allows_confirmation_when_customer_explicitly_confirms(tmp_path: Path):
    """Confirmation should require an explicit customer signal in the latest turn."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    policy = service._analyze_turn_policy("Dale, confirmá el pedido", current_order=None)

    assert policy.allow_order_mutations is True
    assert policy.allow_order_confirmation is True

    await runtime.engine.dispose()


async def test_turn_policy_keeps_payment_answers_as_non_confirming(tmp_path: Path):
    """A payment-only answer should not close a draft without explicit confirmation."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    current_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.PICKUP,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1150000,
        total_amount_display="$ 11.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa BBQ",
                quantity=1,
                unit_price_cents=1150000,
                unit_price_display="$ 11.500",
                notes=None,
            )
        ],
    )

    policy = service._analyze_turn_policy("efectivo", current_order=current_order)

    assert policy.allow_order_mutations is True
    assert policy.allow_order_confirmation is False

    await runtime.engine.dispose()


async def test_turn_policy_treats_delivery_cost_questions_as_informational(tmp_path: Path):
    """Delivery-fee questions should not keep advancing the checkout state machine."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    current_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.DELIVERY,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=950000,
        total_amount_display="$ 9.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa completa",
                quantity=1,
                unit_price_cents=950000,
                unit_price_display="$ 9.500",
                notes=None,
            )
        ],
    )

    policy = service._analyze_turn_policy("¿El envío tiene costo?", current_order=current_order)

    assert policy.allow_order_mutations is False
    assert policy.allow_order_confirmation is False

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
    with pytest.raises(ValueError, match="solo informativo"):
        await reset_current_order(ctx)

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

    assert "cerrad" in reply.reply.reply_text.lower()
    assert "abrimos" in reply.reply.reply_text.lower()

    await runtime.engine.dispose()


def informational_hallucination_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Return a menu answer polluted with unsupported order-completion claims."""
    tool_returns = collect_tool_returns(messages)
    if "lookup_customer" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("lookup_customer", {})],
            model_name="function:test-informational-hallucination",
        )
    if "search_menu" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("search_menu", {"query": "hamburguesa"})],
            model_name="function:test-informational-hallucination",
        )
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": (
                        "Tenemos hamburguesas. La completa sale $9.500 y la doble $11.900. "
                        "Ya registré tu pedido y lo enviamos a Lavalle 12333. El pago es en efectivo."
                    ),
                    "next_step": "choose_items",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-informational-hallucination",
    )


async def test_informational_turn_strips_hallucinated_order_claims(tmp_path: Path):
    """Informational turns must not claim a completed order without actual order actions."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cliente-info-limpia",
        message_text="Hola, soy Martín. ¿Tenés hamburguesas? ¿Cuánto salen?",
        model=FunctionModel(informational_hallucination_model),
    )

    assert "Lavalle" not in reply.reply.reply_text
    assert "pago es" not in reply.reply.reply_text.lower()
    assert "Todavía no armé ningún pedido." in reply.reply.reply_text
    assert reply.current_order is None

    await runtime.engine.dispose()


async def test_ground_reply_keeps_safe_informational_text(tmp_path: Path):
    """Safe informational replies pass through unchanged."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    original = service._ground_reply_for_turn_policy(
        AssistantReply(reply_text="Tenemos hamburguesas y pizzas 🍔🍕", next_step=AssistantNextStep.CHOOSE_ITEMS),
        service._analyze_turn_policy("¿Tenés hamburguesas?", current_order=None),
        current_order=None,
    )

    assert original.reply_text == "Tenemos hamburguesas y pizzas 🍔🍕"

    await runtime.engine.dispose()


async def test_ground_reply_falls_back_when_every_sentence_is_suspicious(tmp_path: Path):
    """Informational replies fallback to a safe text when nothing salvageable remains."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    grounded = service._ground_reply_for_turn_policy(
        AssistantReply(
            reply_text="Ya registré tu pedido. El pago es en efectivo. Gracias por tu compra.",
            next_step=AssistantNextStep.COMPLETE,
        ),
        service._analyze_turn_policy("¿Tenés hamburguesas?", current_order=None),
        current_order=None,
    )

    assert grounded.reply_text == "Puedo contarte las opciones y los precios, pero todavía no armé ningún pedido."
    assert grounded.next_step == AssistantNextStep.CHOOSE_ITEMS

    await runtime.engine.dispose()


async def test_ground_reply_does_not_claim_no_order_when_a_draft_already_exists(tmp_path: Path):
    """Informational cleanup should avoid saying there is no order when a draft already exists."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    current_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1150000,
        total_amount_display="$ 11.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa BBQ",
                quantity=1,
                unit_price_cents=1150000,
                unit_price_display="$ 11.500",
                notes=None,
            )
        ],
    )

    grounded = service._ground_reply_for_turn_policy(
        AssistantReply(
            reply_text="No tenemos bebidas. Ya registré tu pedido y el pago es en efectivo.",
            next_step=AssistantNextStep.CHOOSE_ITEMS,
        ),
        service._analyze_turn_policy("¿Tenés bebidas?", current_order=current_order),
        current_order=current_order,
    )

    assert "Todavía no armé ningún pedido" not in grounded.reply_text

    await runtime.engine.dispose()


async def test_ground_reply_falls_back_to_no_changes_when_draft_exists(tmp_path: Path):
    """If every suspicious sentence is removed but a draft exists, mention no changes instead of no order."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    current_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1150000,
        total_amount_display="$ 11.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa BBQ",
                quantity=1,
                unit_price_cents=1150000,
                unit_price_display="$ 11.500",
                notes=None,
            )
        ],
    )

    grounded = service._ground_reply_for_turn_policy(
        AssistantReply(
            reply_text="Ya registré tu pedido. El pago es en efectivo.",
            next_step=AssistantNextStep.COMPLETE,
        ),
        service._analyze_turn_policy("¿Tenés bebidas?", current_order=current_order),
        current_order=current_order,
    )

    assert "no hice cambios en tu pedido" in grounded.reply_text.lower()

    await runtime.engine.dispose()


async def test_guide_reply_with_current_order_offers_add_on_before_checkout(tmp_path: Path):
    """Simple drafts should first offer one add-on before moving into checkout."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Pirulo", phone_number=None, default_address="Lavalle 12333")
    store = build_store_profile()
    delay = DelayEstimateSnapshot(
        active_orders_ahead=1,
        base_minutes=15,
        estimated_minutes=18,
        display_text="18 minutos aproximadamente",
    )
    order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1150000,
        total_amount_display="$ 11.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa BBQ",
                quantity=1,
                unit_price_cents=1150000,
                unit_price_display="$ 11.500",
                notes=None,
            )
        ],
    )

    add_on_reply = service._guide_reply_with_current_order(
        AssistantReply(reply_text="Texto libre", next_step=AssistantNextStep.CHOOSE_ITEMS),
        customer=customer,
        current_order=order,
        message_text="quiero una hamburguesa BBQ",
        delay=delay,
        store=store,
        item_lines_changed_during_turn=True,
    )
    assert add_on_reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "papas, una bebida o un postre" in add_on_reply.reply_text.lower()
    assert (
        "¿querés sumar algo más?" in add_on_reply.reply_text.lower()
        or "te tienta algo más" in add_on_reply.reply_text.lower()
    )
    assert service._pick_add_on_suggestion(order) == "unas papas, una bebida o un postre"

    order.items.append(
        OrderItemSnapshot(
            menu_item_id=2,
            name="Agua sin gas 500ml",
            quantity=1,
            unit_price_cents=180000,
            unit_price_display="$ 1.800",
            notes=None,
        )
    )

    delivery_reply = service._guide_reply_with_current_order(
        AssistantReply(reply_text="Texto libre", next_step=AssistantNextStep.CHOOSE_ITEMS),
        customer=customer,
        current_order=order,
        message_text="nada más",
        delay=delay,
        store=store,
        item_lines_changed_during_turn=False,
    )
    assert delivery_reply.next_step == AssistantNextStep.CHOOSE_DELIVERY
    assert "envío o retirás" in delivery_reply.reply_text.lower()

    order.delivery_type = DeliveryType.DELIVERY
    address_reply = service._guide_reply_with_current_order(
        AssistantReply(reply_text="Texto libre", next_step=AssistantNextStep.CHOOSE_ITEMS),
        customer=customer,
        current_order=order,
        message_text="envío",
        delay=delay,
        store=store,
        item_lines_changed_during_turn=False,
    )
    assert address_reply.next_step == AssistantNextStep.ASK_ADDRESS
    assert "lavalle 12333" in address_reply.reply_text.lower()

    order.delivery_address = "Lavalle 12333"
    payment_reply = service._guide_reply_with_current_order(
        AssistantReply(reply_text="Texto libre", next_step=AssistantNextStep.CHOOSE_ITEMS),
        customer=customer,
        current_order=order,
        message_text="Lavalle 12333",
        delay=delay,
        store=store,
        item_lines_changed_during_turn=False,
    )
    assert payment_reply.next_step == AssistantNextStep.CHOOSE_PAYMENT
    assert "efectivo" in payment_reply.reply_text.lower()

    order.payment_method = PaymentMethod.CASH
    unchanged_reply = service._guide_reply_with_current_order(
        AssistantReply(reply_text="Texto libre", next_step=AssistantNextStep.CHOOSE_ITEMS),
        customer=customer,
        current_order=order,
        message_text="link de pago",
        delay=delay,
        store=store,
        item_lines_changed_during_turn=False,
    )
    assert unchanged_reply.next_step == AssistantNextStep.CONFIRM_ORDER
    assert "confirmámelo" in unchanged_reply.reply_text.lower()

    order.delivery_address = None
    customer_without_default = CustomerSnapshot(id=1, name="Pirulo", phone_number=None, default_address=None)
    address_without_default = service._guide_reply_with_current_order(
        AssistantReply(reply_text="Texto libre", next_step=AssistantNextStep.CHOOSE_ITEMS),
        customer=customer_without_default,
        current_order=order.model_copy(update={"payment_method": None}),
        message_text="envío",
        delay=delay,
        store=store,
        item_lines_changed_during_turn=False,
    )
    assert "pasame la dirección de envío" in address_without_default.reply_text.lower()

    pizza_order = order.model_copy(
        update={
            "items": [
                OrderItemSnapshot(
                    menu_item_id=3,
                    name="Pizza especial",
                    quantity=1,
                    unit_price_cents=1390000,
                    unit_price_display="$ 13.900",
                    notes=None,
                )
            ],
            "delivery_type": None,
            "delivery_address": None,
            "payment_method": None,
        }
    )
    assert service._pick_add_on_suggestion(pizza_order) == "una bebida o un postre"

    generic_order = order.model_copy(
        update={
            "items": [
                OrderItemSnapshot(
                    menu_item_id=4,
                    name="Ensalada César",
                    quantity=1,
                    unit_price_cents=870000,
                    unit_price_display="$ 8.700",
                    notes=None,
                )
            ],
            "delivery_type": None,
            "delivery_address": None,
            "payment_method": None,
        }
    )
    assert service._pick_add_on_suggestion(generic_order) == "una bebida o un postre"
    assert service._should_offer_add_on(order.model_copy(update={"items": []})) is False
    assert (
        service._pick_add_on_suggestion(
            order.model_copy(
                update={
                    "items": [
                        OrderItemSnapshot(
                            menu_item_id=5,
                            name="Hamburguesa completa",
                            quantity=1,
                            unit_price_cents=950000,
                            unit_price_display="$ 9.500",
                            notes=None,
                        ),
                        OrderItemSnapshot(
                            menu_item_id=6,
                            name="Cerveza rubia lata",
                            quantity=1,
                            unit_price_cents=290000,
                            unit_price_display="$ 2.900",
                            notes=None,
                        ),
                    ]
                }
            )
        )
        == "un postre"
    )
    assert (
        service._pick_add_on_suggestion(
            order.model_copy(
                update={
                    "items": [
                        OrderItemSnapshot(
                            menu_item_id=7,
                            name="Hamburguesa completa",
                            quantity=1,
                            unit_price_cents=950000,
                            unit_price_display="$ 9.500",
                            notes=None,
                        ),
                        OrderItemSnapshot(
                            menu_item_id=8,
                            name="Helado 1/4 kg",
                            quantity=1,
                            unit_price_cents=420000,
                            unit_price_display="$ 4.200",
                            notes=None,
                        ),
                    ]
                }
            )
        )
        == "una bebida"
    )
    assert (
        service._pick_add_on_suggestion(
            order.model_copy(
                update={
                    "items": [
                        OrderItemSnapshot(
                            menu_item_id=9,
                            name="Hamburguesa completa",
                            quantity=1,
                            unit_price_cents=950000,
                            unit_price_display="$ 9.500",
                            notes=None,
                        ),
                        OrderItemSnapshot(
                            menu_item_id=10,
                            name="Gaseosa cola 1.5L",
                            quantity=1,
                            unit_price_cents=320000,
                            unit_price_display="$ 3.200",
                            notes=None,
                        ),
                        OrderItemSnapshot(
                            menu_item_id=11,
                            name="Brownie con nuez",
                            quantity=1,
                            unit_price_cents=310000,
                            unit_price_display="$ 3.100",
                            notes=None,
                        ),
                    ]
                }
            )
        )
        == "algo más para acompañar"
    )

    await runtime.engine.dispose()


async def test_guide_reply_with_current_order_preserves_item_clarifications_when_order_did_not_change(tmp_path: Path):
    """Checkout guidance should not overwrite a clarification if the draft stayed the same."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Ana", phone_number=None, default_address=None)
    store = build_store_profile()
    order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=2740000,
        total_amount_display="$ 27.400",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Lomito especial",
                quantity=1,
                unit_price_cents=1420000,
                unit_price_display="$ 14.200",
                notes=None,
            ),
            OrderItemSnapshot(
                menu_item_id=2,
                name="Lomito completo",
                quantity=1,
                unit_price_cents=1320000,
                unit_price_display="$ 13.200",
                notes=None,
            ),
        ],
    )

    clarification_reply = service._guide_reply_with_current_order(
        AssistantReply(
            reply_text="¿Querés papas clásicas o papas cheddar y bacon?",
            next_step=AssistantNextStep.CHOOSE_ITEMS,
        ),
        customer=customer,
        current_order=order,
        message_text="sumame unas papas",
        delay=None,
        store=store,
        order_changed_during_turn=False,
        item_lines_changed_during_turn=False,
    )

    assert clarification_reply.reply_text == "¿Querés papas clásicas o papas cheddar y bacon?"
    assert clarification_reply.next_step == AssistantNextStep.CHOOSE_ITEMS

    await runtime.engine.dispose()


async def test_guide_reply_with_current_order_offers_add_on_even_when_delivery_is_already_known(tmp_path: Path):
    """A freshly added main dish can still trigger upsell before asking for the address."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Pedro", phone_number=None, default_address=None)
    store = build_store_profile()
    order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.DELIVERY,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1680000,
        total_amount_display="$ 16.800",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Pizza rúcula y crudo",
                quantity=1,
                unit_price_cents=1680000,
                unit_price_display="$ 16.800",
                notes=None,
            )
        ],
    )

    reply = service._guide_reply_with_current_order(
        AssistantReply(
            reply_text="Dale. Pasame la dirección de envío, por favor.", next_step=AssistantNextStep.ASK_ADDRESS
        ),
        customer=customer,
        current_order=order,
        message_text="rucula y crudo",
        delay=None,
        store=store,
        item_lines_changed_during_turn=True,
    )

    assert reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "bebida o un postre" in reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_guide_reply_with_current_order_offers_drink_or_dessert_after_main_and_side(tmp_path: Path):
    """A draft with a main dish and a side can still trigger one more useful add-on suggestion."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Pedro", phone_number=None, default_address=None)
    store = build_store_profile()
    order = OrderSnapshot(
        id=7,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1320000,
        total_amount_display="$ 13.200",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Sanguche de milanesa",
                quantity=1,
                unit_price_cents=890000,
                unit_price_display="$ 8.900",
                notes=None,
            ),
            OrderItemSnapshot(
                menu_item_id=2,
                name="Papas fritas clásicas",
                quantity=1,
                unit_price_cents=430000,
                unit_price_display="$ 4.300",
                notes=None,
            ),
        ],
    )

    reply = service._guide_reply_with_current_order(
        AssistantReply(
            reply_text=(
                "Anotado, Pedro: hasta ahora va 1 x Sanguche de milanesa, "
                "1 x Papas fritas clásicas por $ 13.200. "
                "¿Querés envío o retirás por el local?"
            ),
            next_step=AssistantNextStep.CHOOSE_DELIVERY,
        ),
        customer=customer,
        current_order=order,
        message_text="1 sanguche de mila y unas papas clásicas",
        delay=None,
        store=store,
        item_lines_changed_during_turn=True,
    )

    assert reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "bebida o un postre" in reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_guide_reply_with_current_order_answers_delivery_questions_without_forcing_checkout(tmp_path: Path):
    """Delivery-fee questions should get an informational reply instead of the checkout script."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Lucas", phone_number=None, default_address=None)
    store = build_store_profile()
    order = OrderSnapshot(
        id=8,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.DELIVERY,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=950000,
        total_amount_display="$ 9.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa completa",
                quantity=1,
                unit_price_cents=950000,
                unit_price_display="$ 9.500",
                notes=None,
            )
        ],
    )

    reply = service._guide_reply_with_current_order(
        AssistantReply(
            reply_text="Dale. Pasame la dirección de envío, por favor.",
            next_step=AssistantNextStep.ASK_ADDRESS,
        ),
        customer=customer,
        current_order=order,
        message_text="¿El envío tiene costo?",
        delay=None,
        store=store,
        item_lines_changed_during_turn=False,
    )

    assert reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "costo fijo" in reply.reply_text.lower()
    assert "dirección o la zona" in reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_delivery_information_reply_recovers_from_handoff(tmp_path: Path):
    """Delivery-info stabilization should replace an unnecessary handoff with a useful answer."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    reply = service._stabilize_delivery_information_reply(
        AssistantReply(
            reply_text="Te paso con una persona.",
            next_step=AssistantNextStep.HANDOFF,
            handoff=True,
        ),
        message_text="¿El envío tiene costo?",
    )

    assert reply.handoff is False
    assert reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "costo fijo" in reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_delivery_information_reply_keeps_useful_non_checkout_reply(tmp_path: Path):
    """Delivery-info stabilization should preserve already-useful answers."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    original = AssistantReply(
        reply_text="Hacemos envíos. Si me decís la zona, te confirmo cobertura.",
        next_step=AssistantNextStep.CHOOSE_ITEMS,
        handoff=False,
    )
    reply = service._stabilize_delivery_information_reply(original, message_text="¿Llegan hasta Alta Córdoba?")

    assert reply is original

    await runtime.engine.dispose()


async def test_handle_customer_message_stabilizes_delivery_info_questions_over_draft_orders(tmp_path: Path):
    """Informational delivery questions should stay helpful even when the model falls back to handoff."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(service, channel=Channel.DEV, external_user_id="delivery-info-user", name="Martina")

    async with service.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="delivery-info-user")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="delivery-info-user",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(customer.id, conversation.id, sku="hamburguesa-completa", quantity=1)
        await session.commit()

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="delivery-info-user",
        message_text="¿El envío tiene costo?",
        model=FunctionModel(delivery_info_handoff_model),
    )

    assert result.reply.handoff is False
    assert result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "costo fijo" in result.reply.reply_text.lower()
    assert result.current_order is not None
    assert result.current_order.items

    await runtime.engine.dispose()


async def test_handle_customer_message_stabilizes_delivery_info_questions_without_a_draft(tmp_path: Path):
    """Delivery-info questions should stay informational even without an active order."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="delivery-info-no-draft-user",
        message_text="¿Y más o menos por qué zona llegan?",
        model=FunctionModel(delivery_info_handoff_model),
    )

    assert result.reply.handoff is False
    assert result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "hacemos envíos por la zona" in result.reply.reply_text.lower()
    assert "alcance puntual" in result.reply.reply_text.lower()
    assert result.current_order is None

    await runtime.engine.dispose()


async def test_handle_customer_message_recovers_colloquial_item_aliases(tmp_path: Path):
    """Colloquial exact aliases should still add the intended item when the model misses them."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="colloquial-item-user",
        name="Fer",
    )

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="colloquial-item-user",
        message_text="Quiero una burger veggie.",
        model=FunctionModel(colloquial_not_found_model),
    )

    assert result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "Hamburguesa veggie" in result.reply.reply_text
    assert result.current_order is not None
    assert [item.name for item in result.current_order.items] == ["Hamburguesa veggie"]

    await runtime.engine.dispose()


async def test_handle_customer_message_recovers_colloquial_category_aliases(tmp_path: Path):
    """Colloquial beverage aliases should fall back to concrete menu options instead of a miss."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="colloquial-category-user",
        name="Fer",
    )

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="colloquial-category-user",
        message_text="Sumame una cervecita.",
        model=FunctionModel(colloquial_not_found_model),
    )

    assert result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "si querías una cerveza" in result.reply.reply_text.lower()
    assert "Cerveza rubia lata" in result.reply.reply_text
    assert result.current_order is None

    await runtime.engine.dispose()


async def test_recover_colloquial_menu_reply_keeps_original_when_no_alias_matches(tmp_path: Path):
    """Unknown colloquial wording should keep the original reply unchanged."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.WHATSAPP, external_id="alias-fallback")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.WHATSAPP,
            external_id="alias-fallback",
            customer_id=customer.id,
        )
        store = await repository.get_store_profile()
        reply = AssistantReply(
            reply_text="No encontré ese producto en el menú.",
            next_step=AssistantNextStep.CHOOSE_ITEMS,
            handoff=False,
        )

        recovered_reply, recovered_order = await service._recover_colloquial_menu_reply(
            reply,
            repository=repository,
            message_text="Quiero una limonada.",
            customer=customer,
            conversation_id=conversation.id,
            current_order=None,
            delay=None,
            store=store,
            turn_policy=TurnPolicy(allow_order_mutations=True, allow_order_confirmation=False),
        )

        assert recovered_reply == reply
        assert recovered_order is None

    await runtime.engine.dispose()


async def test_guide_reply_with_current_order_keeps_informational_delivery_answer_when_it_is_already_useful(
    tmp_path: Path,
):
    """A useful delivery-info reply should pass through untouched."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Lucas", phone_number=None, default_address=None)
    store = build_store_profile()
    order = OrderSnapshot(
        id=9,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.DELIVERY,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=950000,
        total_amount_display="$ 9.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa completa",
                quantity=1,
                unit_price_cents=950000,
                unit_price_display="$ 9.500",
                notes=None,
            )
        ],
    )

    reply = service._guide_reply_with_current_order(
        AssistantReply(
            reply_text="Hacemos envíos. Si me pasás la zona, te digo si llegamos.",
            next_step=AssistantNextStep.CHOOSE_ITEMS,
        ),
        customer=customer,
        current_order=order,
        message_text="¿Llegan hasta Alta Córdoba?",
        delay=None,
        store=store,
        item_lines_changed_during_turn=False,
    )

    assert reply.reply_text == "Hacemos envíos. Si me pasás la zona, te digo si llegamos."
    assert reply.next_step == AssistantNextStep.CHOOSE_ITEMS

    await runtime.engine.dispose()


async def test_guide_reply_with_current_order_returns_original_reply_when_nothing_else_applies(tmp_path: Path):
    """The helper should leave unrelated free-form replies untouched when checkout guidance is complete."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Pedro", phone_number=None, default_address=None)
    store = build_store_profile()
    order = OrderSnapshot(
        id=10,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.PICKUP,
        delivery_address=None,
        payment_method=PaymentMethod.CASH,
        total_amount_cents=1190000,
        total_amount_display="$ 11.900",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa doble cheddar",
                quantity=1,
                unit_price_cents=1190000,
                unit_price_display="$ 11.900",
                notes=None,
            )
        ],
    )

    reply = service._guide_reply_with_current_order(
        AssistantReply(reply_text="Confirmame cuando quieras.", next_step=AssistantNextStep.CONFIRM_ORDER),
        customer=customer,
        current_order=order,
        message_text="ok",
        delay=None,
        store=store,
        order_changed_during_turn=False,
        item_lines_changed_during_turn=False,
    )

    assert reply.reply_text == "Confirmame cuando quieras."
    assert reply.next_step == AssistantNextStep.CONFIRM_ORDER

    await runtime.engine.dispose()


async def test_turn_context_hint_mentions_known_delivery_address_for_draft(tmp_path: Path):
    """Turn hints should mention the saved address when delivery still needs confirmation."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Pirulo", phone_number=None, default_address="Lavalle 12333")
    order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.DELIVERY,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1150000,
        total_amount_display="$ 11.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa BBQ",
                quantity=1,
                unit_price_cents=1150000,
                unit_price_display="$ 11.500",
                notes=None,
            )
        ],
    )

    hint = service._build_turn_context_hint(
        customer=customer,
        message_text="¿Cuánto falta?",
        current_order=order,
    )

    assert hint is not None
    assert "dirección conocida del cliente: lavalle 12333" in hint.lower()

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


async def test_closed_store_text_helpers_cover_open_and_exact_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Closed-store decoration helpers should pass through open replies and exact existing prefixes."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    open_availability = StoreAvailabilitySnapshot(is_open=True, message_text="Estamos abiertos", next_open_text=None)
    closed_availability = StoreAvailabilitySnapshot(
        is_open=False,
        message_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00.",
        next_open_text="mañana a las 11:00",
    )
    assert service._decorate_closed_store_text("Mensaje libre", open_availability) == "Mensaje libre"
    monkeypatch.setattr(service, "_build_closed_store_notice", lambda availability, conversation_id: "Prefijo: ")
    assert (
        service._decorate_closed_store_text("Prefijo: mensaje ya armado", closed_availability, conversation_id=0)
        == "Prefijo: mensaje ya armado"
    )

    await runtime.engine.dispose()


async def test_closed_store_text_helpers_cover_recent_notice_branches(tmp_path: Path):
    """Closed-store helpers should skip re-prefixing when the notice was already sent recently."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    availability = StoreAvailabilitySnapshot(
        is_open=False,
        message_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00.",
        next_open_text="mañana a las 11:00",
    )
    reply = AssistantReply(reply_text="Seguimos con el pedido", next_step=AssistantNextStep.CHOOSE_ITEMS, handoff=False)

    decorated_reply = service._decorate_reply_with_store_availability(
        reply,
        availability,
        conversation_id=1,
        latest_assistant_text=None,
        current_order=None,
    )
    recent_notice = service._decorate_closed_store_text(
        "Seguimos con el pedido",
        availability,
        latest_assistant_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00. Hola",
    )

    assert "cerrad" in decorated_reply.reply_text.lower()
    assert recent_notice == "Seguimos con el pedido"

    await runtime.engine.dispose()


async def test_strip_redundant_closed_store_notice_removes_leading_repeat(tmp_path: Path):
    """Redundant closed-store prefixes from the model should be stripped after the first notice."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    stripped = service._strip_redundant_closed_store_notice(
        "Ahora estamos cerrados 😴 Abrimos mañana a las 11:00. Dale, tengo una pizza napolitana.",
        latest_assistant_text="Justo ahora el local está cerrado 😴 Abrimos mañana a las 11:00. ¿Qué querés pedir?",
    )

    assert stripped == "Dale, tengo una pizza napolitana."

    await runtime.engine.dispose()


async def test_strip_redundant_closed_store_notice_handles_sentence_fallback(tmp_path: Path):
    """Closed-store stripping should fall back to removing the first sentence when needed."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    stripped = service._strip_redundant_closed_store_notice(
        "Recordá: estamos cerrados y abrimos mañana a las 11:00. Tenemos wraps y ensaladas.",
        latest_assistant_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00. ¿Qué querés pedir?",
    )

    assert stripped == "Tenemos wraps y ensaladas."

    await runtime.engine.dispose()


async def test_strip_redundant_closed_store_notice_keeps_original_without_safe_split(tmp_path: Path):
    """Closed-store stripping should keep the original reply when no safe cleanup applies."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    stripped = service._strip_redundant_closed_store_notice(
        "Recordá que estamos cerrados y abrimos mañana a las 11:00",
        latest_assistant_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00. ¿Qué querés pedir?",
    )

    assert stripped == "Recordá que estamos cerrados y abrimos mañana a las 11:00"

    await runtime.engine.dispose()


async def test_decorate_reply_with_store_availability_strips_repeated_notice(tmp_path: Path):
    """Repeated closed-store prefixes should be removed before returning the reply."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    availability = StoreAvailabilitySnapshot(
        is_open=False,
        message_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00.",
        next_open_text="mañana a las 11:00",
    )

    reply = service._decorate_reply_with_store_availability(
        AssistantReply(
            reply_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00. Dale, tengo una pizza napolitana.",
            next_step=AssistantNextStep.CHOOSE_ITEMS,
        ),
        availability,
        conversation_id=1,
        latest_assistant_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00. ¿Qué querés pedir?",
        current_order=None,
    )

    assert reply.reply_text == "Dale, tengo una pizza napolitana."

    await runtime.engine.dispose()


async def test_decorate_reply_with_store_availability_keeps_reply_after_recent_notice(tmp_path: Path):
    """Closed stores should not prepend the schedule again when it was just said."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    availability = StoreAvailabilitySnapshot(
        is_open=False,
        message_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00.",
        next_open_text="mañana a las 11:00",
    )

    reply = service._decorate_reply_with_store_availability(
        AssistantReply(
            reply_text="Seguimos con el pedido.",
            next_step=AssistantNextStep.CHOOSE_ITEMS,
        ),
        availability,
        conversation_id=1,
        latest_assistant_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00. ¿Qué querés pedir?",
        current_order=None,
    )

    assert reply.reply_text == "Seguimos con el pedido."

    await runtime.engine.dispose()


async def test_closed_store_availability_skips_scheduled_or_confirmed_orders(tmp_path: Path):
    """Closed-store decoration should skip scheduled drafts and already confirmed orders."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    availability = StoreAvailabilitySnapshot(
        is_open=False,
        message_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00.",
        next_open_text="mañana a las 11:00",
    )
    scheduled_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.PICKUP,
        delivery_address=None,
        payment_method=PaymentMethod.CASH,
        requested_ready_at=datetime.now(UTC) + timedelta(days=1),
        preparation_starts_at=None,
        total_amount_cents=950000,
        total_amount_display="$ 9.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa completa",
                quantity=1,
                unit_price_cents=950000,
                unit_price_display="$ 9.500",
                notes=None,
            )
        ],
    )
    confirmed_order = scheduled_order.model_copy(update={"requested_ready_at": None, "status": OrderStatus.CONFIRMED})
    reply = AssistantReply(
        reply_text="Seguimos con el pedido", next_step=AssistantNextStep.CONFIRM_ORDER, handoff=False
    )

    scheduled_reply = service._decorate_reply_with_store_availability(
        reply,
        availability,
        conversation_id=1,
        latest_assistant_text=None,
        current_order=scheduled_order,
    )
    confirmed_reply = service._decorate_reply_with_store_availability(
        reply,
        availability,
        conversation_id=1,
        latest_assistant_text=None,
        current_order=confirmed_order,
    )

    assert scheduled_reply.reply_text == "Seguimos con el pedido"
    assert confirmed_reply.reply_text == "Seguimos con el pedido"

    await runtime.engine.dispose()


async def test_scheduling_and_next_step_helpers_cover_remaining_branches(tmp_path: Path):
    """Scheduling helpers should parse tomorrow, reject invalid hours, and infer next steps."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    requested_ready_at = datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(second=0, microsecond=0) + timedelta(days=1)
    base_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        requested_ready_at=requested_ready_at.astimezone(UTC),
        preparation_starts_at=(requested_ready_at - timedelta(minutes=15)).astimezone(UTC),
        total_amount_cents=950000,
        total_amount_display="$ 9.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa completa",
                quantity=1,
                unit_price_cents=950000,
                unit_price_display="$ 9.500",
                notes=None,
            )
        ],
    )

    parsed_tomorrow = service._extract_requested_ready_at(
        "Quiero una hamburguesa mañana a las 12", timezone_name=STORE_TIMEZONE
    )

    assert service._next_step_for_current_order(None) == AssistantNextStep.CHOOSE_ITEMS
    assert service._next_step_for_current_order(base_order) == AssistantNextStep.CHOOSE_DELIVERY
    assert (
        service._next_step_for_current_order(base_order.model_copy(update={"delivery_type": DeliveryType.DELIVERY}))
        == AssistantNextStep.ASK_ADDRESS
    )
    assert (
        service._next_step_for_current_order(
            base_order.model_copy(update={"delivery_type": DeliveryType.DELIVERY, "delivery_address": "Lavalle 12333"})
        )
        == AssistantNextStep.CHOOSE_PAYMENT
    )
    assert (
        service._next_step_for_current_order(
            base_order.model_copy(
                update={
                    "delivery_type": DeliveryType.DELIVERY,
                    "delivery_address": "Lavalle 12333",
                    "payment_method": PaymentMethod.CASH,
                }
            )
        )
        == AssistantNextStep.CONFIRM_ORDER
    )
    assert (
        service._next_step_for_current_order(
            base_order.model_copy(
                update={
                    "status": OrderStatus.CONFIRMED,
                    "delivery_type": DeliveryType.DELIVERY,
                    "delivery_address": "Lavalle 12333",
                    "payment_method": PaymentMethod.CASH,
                }
            )
        )
        == AssistantNextStep.COMPLETE
    )
    assert service._build_timing_prompt_fragment(current_order=base_order, delay=None).startswith(" Lo dejo programado")
    assert (
        service._build_timing_prompt_fragment(
            current_order=base_order.model_copy(update={"requested_ready_at": None}),
            delay=DelayEstimateSnapshot(
                active_orders_ahead=0,
                base_minutes=17,
                estimated_minutes=20,
                display_text="20 minutos aproximadamente",
            ),
        )
        == ""
    )
    assert (
        service._build_timing_prompt_fragment(
            current_order=base_order.model_copy(update={"requested_ready_at": None}),
            delay=None,
        )
        == ""
    )
    assert service._extract_requested_ready_at("", timezone_name=STORE_TIMEZONE) is None
    assert service._extract_requested_ready_at("para las 25", timezone_name=STORE_TIMEZONE) is None
    assert parsed_tomorrow is not None
    assert service._describe_order_ready_time(parsed_tomorrow).startswith("mañana")
    today_ready = datetime.now(ZoneInfo(STORE_TIMEZONE)).replace(hour=13, minute=0, second=0, microsecond=0)
    assert service._describe_order_ready_time(today_ready.astimezone(UTC)).startswith("hoy")
    assert service._format_local_time(today_ready.astimezone(UTC)) == today_ready.strftime("%H:%M")

    await runtime.engine.dispose()


async def test_finalize_confirmed_order_reply_covers_schedule_closed_and_transfer_details(tmp_path: Path):
    """Confirmed-order summaries should include schedule details and closed-store decoration."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Martina", phone_number=None, default_address=None)
    store = build_store_profile()
    availability = StoreAvailabilitySnapshot(
        is_open=False,
        message_text="Ahora estamos cerrados 😴 Abrimos mañana a las 11:00.",
        next_open_text="mañana a las 11:00",
    )
    scheduled_order = OrderSnapshot(
        id=1,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.CONFIRMED,
        delivery_type=DeliveryType.PICKUP,
        delivery_address=None,
        payment_method=PaymentMethod.TRANSFER,
        requested_ready_at=datetime.now(UTC) + timedelta(days=1),
        preparation_starts_at=datetime.now(UTC) + timedelta(days=1, minutes=-15),
        total_amount_cents=950000,
        total_amount_display="$ 9.500",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa completa",
                quantity=1,
                unit_price_cents=950000,
                unit_price_display="$ 9.500",
                notes=None,
            )
        ],
    )

    scheduled_reply = service._finalize_confirmed_order_reply(
        AssistantReply(reply_text="Texto libre", next_step=AssistantNextStep.COMPLETE),
        customer=customer,
        current_order=scheduled_order,
        delay=None,
        store=store,
        just_confirmed=True,
        availability=availability,
        conversation_id=1,
        latest_assistant_text=None,
    )
    immediate_reply = service._finalize_confirmed_order_reply(
        AssistantReply(reply_text="Texto libre", next_step=AssistantNextStep.COMPLETE),
        customer=customer,
        current_order=scheduled_order.model_copy(update={"requested_ready_at": None, "preparation_starts_at": None}),
        delay=None,
        store=store,
        just_confirmed=True,
        availability=availability,
        conversation_id=2,
        latest_assistant_text=None,
    )

    assert "**Horario**" in scheduled_reply.reply_text
    assert "Empezamos a prepararlo cerca de las" in scheduled_reply.reply_text
    assert "`rotiseria.test`" in scheduled_reply.reply_text
    assert "cerrad" not in scheduled_reply.reply_text.lower()
    assert "abrimos" in immediate_reply.reply_text.lower()
    assert "fuera de horario" in immediate_reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_delivery_information_helpers_cover_checkout_prompt_and_zone_copy(tmp_path: Path):
    """Delivery-info helpers should detect checkout prompts and zone guidance copy."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    assert service._reply_is_checkout_prompt(
        AssistantReply(reply_text="¿Querés envío o retirás por el local?", next_step=AssistantNextStep.CHOOSE_ITEMS)
    )
    reply_text = service._build_delivery_information_reply("¿Llegan a barrio centro?")
    assert reply_text.startswith("Hacemos envíos por la zona.")
    assert "barrio o dirección" in reply_text

    await runtime.engine.dispose()


async def test_greeting_and_open_notice_helpers_cover_edge_cases(tmp_path: Path):
    """Greeting and open-notice helpers should handle empty text and fully stripped replies."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    assert service._message_is_generic_greeting("") is False
    assert service._message_is_generic_greeting("hola che") is True
    assert (
        service._strip_unsolicited_open_store_notice("Estamos abiertos ahora 🍽️ hasta las 23:00.")
        == "¡Hola! 👋 ¿Qué te gustaría pedir hoy?"
    )

    await runtime.engine.dispose()


def cash_checkout_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Drive the last checkout turn by setting cash and confirming the draft."""
    tool_returns = collect_tool_returns(messages)
    if "set_order_payment_method" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("set_order_payment_method", {"payment_method": "cash"})],
            model_name="function:test-cash-checkout",
        )
    if "confirm_current_order" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("confirm_current_order", {})],
            model_name="function:test-cash-checkout",
        )
    order = tool_returns["confirm_current_order"]
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": f"Pedido confirmado por {order.total_amount_display}.",
                    "next_step": "complete",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-cash-checkout",
    )


def cash_payment_only_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Only register the cash payment hint and leave confirmation for the next turn."""
    tool_returns = collect_tool_returns(messages)
    if "set_order_payment_method" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("set_order_payment_method", {"payment_method": "cash"})],
            model_name="function:test-cash-payment-only",
        )
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": "Pago anotado.",
                    "next_step": "confirm_order",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-cash-payment-only",
    )


def explicit_confirmation_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Confirm an already prepared draft when the customer explicitly approves it."""
    tool_returns = collect_tool_returns(messages)
    if "confirm_current_order" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("confirm_current_order", {})],
            model_name="function:test-explicit-confirmation",
        )
    order = tool_returns["confirm_current_order"]
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": f"Pedido confirmado por {order.total_amount_display}.",
                    "next_step": "complete",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-explicit-confirmation",
    )


def corrected_lomito_mix_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Correct a mistaken draft by rebuilding it as one of each lomito."""
    tool_returns = collect_tool_returns(messages)
    if "reset_current_order" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("reset_current_order", {})],
            model_name="function:test-corrected-lomito-mix",
        )
    if "add_item_to_current_order" not in tool_returns:
        return ModelResponse(
            parts=[ToolCallPart("add_item_to_current_order", {"sku": "lomito-completo", "quantity": 1})],
            model_name="function:test-corrected-lomito-mix",
        )
    add_calls = [
        part
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_name == "add_item_to_current_order"
    ]
    if len(add_calls) == 1:
        return ModelResponse(
            parts=[ToolCallPart("add_item_to_current_order", {"sku": "lomito-especial", "quantity": 1})],
            model_name="function:test-corrected-lomito-mix",
        )
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {
                    "reply_text": "Listo, te dejo uno de cada lomito.",
                    "next_step": "choose_delivery",
                    "handoff": False,
                },
            )
        ],
        model_name="function:test-corrected-lomito-mix",
    )


async def test_cash_payment_hint_allows_checkout_confirmation(tmp_path: Path):
    """A mixed Spanish/English cash hint should review the draft and wait for confirmation."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="cash-checkout-user",
        name="Martín",
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="cash-checkout-user")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="cash-checkout-user",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(
            customer.id,
            conversation.id,
            sku="hamburguesa-doble",
            quantity=1,
        )
        await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
        await session.commit()

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cash-checkout-user",
        message_text="pago en el local, cash",
        model=FunctionModel(cash_payment_only_model),
    )

    assert "**pedido**" in result.reply.reply_text.lower()
    assert "hamburguesa doble cheddar" in result.reply.reply_text.lower()
    assert "**pago**" in result.reply.reply_text.lower()
    assert "efectivo" in result.reply.reply_text.lower()
    assert "confirmámelo" in result.reply.reply_text.lower()
    assert result.reply.next_step == AssistantNextStep.CONFIRM_ORDER
    assert result.current_order is not None
    assert result.current_order.payment_method is PaymentMethod.CASH
    assert result.current_order.status is OrderStatus.DRAFT

    await runtime.engine.dispose()


async def test_payment_turn_recovers_to_review_when_model_tries_to_confirm_early(tmp_path: Path):
    """If the model jumps to confirmation after payment, the service should recover gracefully."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="cash-recovery-user",
        name="Martín",
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="cash-recovery-user")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="cash-recovery-user",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(
            customer.id,
            conversation.id,
            sku="hamburguesa-doble",
            quantity=1,
        )
        await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
        await session.commit()

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="cash-recovery-user",
        message_text="pago en el local, cash",
        model=FunctionModel(cash_checkout_model),
    )

    assert result.reply.next_step == AssistantNextStep.CONFIRM_ORDER
    assert "confirmámelo" in result.reply.reply_text.lower()
    assert result.current_order is not None
    assert result.current_order.payment_method is PaymentMethod.CASH
    assert result.current_order.status is OrderStatus.DRAFT

    await runtime.engine.dispose()


async def test_recover_missing_confirmation_falls_back_when_no_draft_exists(tmp_path: Path):
    """Missing-confirmation recovery should hand off if no draft can be reviewed."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="missing-confirmation-fallback",
        name="Martina",
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.DEV,
            external_id="missing-confirmation-fallback",
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="missing-confirmation-fallback",
            customer_id=customer.id,
        )
        await session.commit()

    result = await service._recover_missing_confirmation_result(
        conversation_id=conversation.id,
        customer_id=customer.id,
        customer=customer,
        store_id=settings.default_store_id,
        user_text="confirmá",
    )

    assert result.reply.next_step == AssistantNextStep.HANDOFF
    assert result.current_order is None

    await runtime.engine.dispose()


async def test_incomplete_checkout_recovery_guides_to_delivery_choice(tmp_path: Path):
    """Repository-level incomplete confirmations should recover into delivery guidance."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    baseline_service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        baseline_service,
        channel=Channel.DEV,
        external_user_id="incomplete-delivery-type-user",
        name="Martina",
    )

    class IncompleteCheckoutAgent:
        async def run(self, *args: Any, **kwargs: Any):
            raise IncompleteOrderError.missing_delivery_type()

    service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, IncompleteCheckoutAgent()),
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.DEV,
            external_id="incomplete-delivery-type-user",
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="incomplete-delivery-type-user",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(customer.id, conversation.id, sku="pizza-rucula-crudo", quantity=1)
        await session.commit()

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="incomplete-delivery-type-user",
        message_text="pago en efectivo",
    )

    assert result.reply.next_step == AssistantNextStep.CHOOSE_DELIVERY
    assert "envío o retirás" in result.reply.reply_text.lower()
    assert result.current_order is not None
    assert result.current_order.payment_method is PaymentMethod.CASH
    assert result.current_order.delivery_type is None

    await runtime.engine.dispose()


async def test_incomplete_checkout_recovery_guides_to_address(tmp_path: Path):
    """Repository-level incomplete confirmations should recover into address guidance."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    baseline_service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        baseline_service,
        channel=Channel.DEV,
        external_user_id="incomplete-address-user",
        name="Martina",
    )

    class IncompleteCheckoutAgent:
        async def run(self, *args: Any, **kwargs: Any):
            raise IncompleteOrderError.missing_delivery_address()

    service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, IncompleteCheckoutAgent()),
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="incomplete-address-user")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="incomplete-address-user",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(customer.id, conversation.id, sku="hamburguesa-completa", quantity=1)
        await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.DELIVERY)
        await session.commit()

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="incomplete-address-user",
        message_text="confirmalo",
    )

    assert result.reply.next_step == AssistantNextStep.ASK_ADDRESS
    assert "dirección" in result.reply.reply_text.lower()
    assert result.current_order is not None
    assert result.current_order.delivery_type is DeliveryType.DELIVERY
    assert result.current_order.delivery_address is None

    await runtime.engine.dispose()


async def test_incomplete_checkout_recovery_guides_to_payment(tmp_path: Path):
    """Repository-level incomplete confirmations should recover into payment guidance."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    baseline_service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        baseline_service,
        channel=Channel.DEV,
        external_user_id="incomplete-payment-user",
        name="Martina",
    )

    class IncompleteCheckoutAgent:
        async def run(self, *args: Any, **kwargs: Any):
            raise IncompleteOrderError.missing_payment_method()

    service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, IncompleteCheckoutAgent()),
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="incomplete-payment-user")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="incomplete-payment-user",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(customer.id, conversation.id, sku="lomito-especial", quantity=1)
        await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
        await session.commit()

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="incomplete-payment-user",
        message_text="confirmalo",
    )

    assert result.reply.next_step == AssistantNextStep.CHOOSE_PAYMENT
    assert "efectivo, transferencia o link de pago" in result.reply.reply_text.lower()
    assert result.current_order is not None
    assert result.current_order.payment_method is None
    assert result.current_order.delivery_type is DeliveryType.PICKUP

    await runtime.engine.dispose()


async def test_order_review_reply_includes_schedule_and_transfer_alias(tmp_path: Path):
    """Draft reviews should include transfer alias and scheduled-ready information when available."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    customer = CustomerSnapshot(id=1, name="Martina", phone_number=None, default_address=None)
    store = build_store_profile().model_copy(update={"transfer_alias": "demo.rotiseria"})
    scheduled_time = datetime(2026, 4, 5, 12, 0, tzinfo=ZoneInfo(STORE_TIMEZONE))
    order = OrderSnapshot(
        id=11,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=DeliveryType.PICKUP,
        delivery_address=None,
        payment_method=PaymentMethod.TRANSFER,
        total_amount_cents=950000,
        total_amount_display="$ 9.500",
        requested_ready_at=scheduled_time,
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa completa",
                quantity=1,
                unit_price_cents=950000,
                unit_price_display="$ 9.500",
                notes=None,
            )
        ],
    )

    review = service._build_order_review_reply(customer=customer, current_order=order, store=store)

    assert "**Horario**" in review
    assert "Pedido programado" in review
    assert "`demo.rotiseria`" in review

    await runtime.engine.dispose()


async def test_stabilize_customization_reply_rejects_unpersisted_variants(tmp_path: Path):
    """The assistant should not promise extra variants that were never stored in the order."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    order = OrderSnapshot(
        id=22,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1210000,
        total_amount_display="$ 12.100",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa picante",
                quantity=1,
                unit_price_cents=1210000,
                unit_price_display="$ 12.100",
                notes=None,
            )
        ],
    )

    reply = service._stabilize_customization_reply(
        AssistantReply(
            reply_text="Sí, ya anotamos tu hamburguesa con doble picante y triple cheddar.",
            next_step=AssistantNextStep.CHOOSE_ITEMS,
            handoff=False,
        ),
        message_text="¿Eso se puede?",
        previous_user_message="Quiero la hamburguesa picante pero con doble picante y triple cheddar.",
        current_order=order,
    )

    assert "no tengo cargadas variantes" in reply.reply_text.lower()
    assert "Hamburguesa picante" in reply.reply_text
    assert reply.handoff is False

    await runtime.engine.dispose()


async def test_stabilize_customization_reply_keeps_unrelated_checkout_prompt(tmp_path: Path):
    """Customization stabilization should ignore unrelated checkout prompts."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    order = OrderSnapshot(
        id=24,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1210000,
        total_amount_display="$ 12.100",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa picante",
                quantity=1,
                unit_price_cents=1210000,
                unit_price_display="$ 12.100",
                notes=None,
            )
        ],
    )

    reply = service._stabilize_customization_reply(
        AssistantReply(
            reply_text="¿Querés envío o retirás por el local?",
            next_step=AssistantNextStep.CHOOSE_DELIVERY,
            handoff=False,
        ),
        message_text="Quiero doble picante.",
        previous_user_message="Quiero la hamburguesa picante.",
        current_order=order,
    )

    assert reply.reply_text == "¿Querés envío o retirás por el local?"
    assert reply.next_step == AssistantNextStep.CHOOSE_DELIVERY

    await runtime.engine.dispose()


async def test_stabilize_customization_reply_replaces_generic_checkout_when_customization_is_unresolved(tmp_path: Path):
    """A vague feasibility question should not be redirected to checkout when the variant was never stored."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))
    order = OrderSnapshot(
        id=23,
        customer_id=1,
        conversation_id=1,
        status=OrderStatus.DRAFT,
        delivery_type=None,
        delivery_address=None,
        payment_method=None,
        total_amount_cents=1210000,
        total_amount_display="$ 12.100",
        items=[
            OrderItemSnapshot(
                menu_item_id=1,
                name="Hamburguesa picante",
                quantity=1,
                unit_price_cents=1210000,
                unit_price_display="$ 12.100",
                notes=None,
            )
        ],
    )

    reply = service._stabilize_customization_reply(
        AssistantReply(
            reply_text="Anotado, Nico: hasta ahora va 1 x Hamburguesa picante por $ 12.100. ¿Querés envío o retirás?",
            next_step=AssistantNextStep.CHOOSE_DELIVERY,
            handoff=False,
        ),
        message_text="¿Eso se puede?",
        previous_user_message="Quiero la hamburguesa picante pero con doble picante y triple cheddar.",
        current_order=order,
    )

    assert "no tengo cargadas variantes" in reply.reply_text.lower()
    assert reply.next_step == AssistantNextStep.CHOOSE_ITEMS

    await runtime.engine.dispose()


async def test_handle_customer_message_stabilizes_unresolved_customization_questions(tmp_path: Path):
    """A follow-up feasibility question should not be pushed to checkout when the variant was not persisted."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="customization-follow-up-user",
        name="Nico",
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.DEV,
            external_id="customization-follow-up-user",
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="customization-follow-up-user",
            customer_id=customer.id,
        )
        await repository.append_conversation_messages(
            conversation.id,
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(content="Quiero la hamburguesa picante pero con doble picante y triple cheddar.")
                    ]
                ),
            ],
        )
        await repository.add_item_to_current_order(customer.id, conversation.id, sku="hamburguesa-picante", quantity=1)
        await session.commit()

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="customization-follow-up-user",
        message_text="¿Eso se puede?",
        model=FunctionModel(unsupported_customization_checkout_model),
    )

    assert "no tengo cargadas variantes" in result.reply.reply_text.lower()
    assert result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert result.current_order is not None
    assert [item.name for item in result.current_order.items] == ["Hamburguesa picante"]

    await runtime.engine.dispose()


async def test_customer_correction_can_rebuild_the_current_order(tmp_path: Path):
    """Corrections should be able to replace mistaken draft lines instead of stacking more items."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="lomito-correction-user",
        name="Ana",
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="lomito-correction-user")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="lomito-correction-user",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(
            customer.id,
            conversation.id,
            sku="lomito-especial",
            quantity=1,
        )
        await repository.append_conversation_messages(
            conversation.id,
            [
                ModelResponse(
                    parts=[
                        TextPart(
                            content=(
                                "¿Qué tipo de lomo te gustaría, Ana? Tenemos:\n\n"
                                "Lomito completo ($13.200)\n"
                                "Lomito especial ($14.200)"
                            )
                        )
                    ],
                    model_name="ruperto:test",
                )
            ],
        )
        await session.commit()

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="lomito-correction-user",
        message_text="dije uno de cada uno",
        model=FunctionModel(corrected_lomito_mix_model),
    )

    assert result.current_order is not None
    assert [item.name for item in result.current_order.items] == ["Lomito completo", "Lomito especial"]
    assert result.current_order.total_amount_display == "$ 27.400"
    assert result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "papas, una bebida o un postre" in result.reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_model_failure_recovers_large_multi_item_orders_deterministically(tmp_path: Path):
    """Large first-turn orders should degrade into a deterministic draft instead of a generic handoff."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    baseline_service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        baseline_service,
        channel=Channel.DEV,
        external_user_id="large-order-recovery-user",
        name="Diego",
    )

    class FailingAgent:
        async def run(self, *args: Any, **kwargs: Any):
            raise RuntimeError

    service = OrderingAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=cast(Any, FailingAgent()),
    )
    service.settings.assistant_model_retry_attempts = 1

    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="large-order-recovery-user",
        message_text="Quiero 2 pizzas muzza, 1 docena de empanadas clásicas y 2 gaseosas cola 1.5L.",
    )

    assert result.reply.handoff is False
    assert result.reply.next_step == AssistantNextStep.CHOOSE_ITEMS
    assert "2 x Pizza muzzarella" in result.reply.reply_text
    assert "1 x Docena de empanadas clásicas" in result.reply.reply_text
    assert "2 x Gaseosa cola 1.5L" in result.reply.reply_text
    assert result.current_order is not None
    assert [item.name for item in result.current_order.items] == [
        "Pizza muzzarella",
        "Docena de empanadas clásicas",
        "Gaseosa cola 1.5L",
    ]
    assert [item.quantity for item in result.current_order.items] == [2, 1, 2]

    await runtime.engine.dispose()


async def test_apply_checkout_hints_can_fill_payment_from_the_message(tmp_path: Path):
    """Checkout-hint recovery should also persist payment hints from the latest turn."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="checkout-hints-user",
        name="Martina",
    )

    async with service.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(channel=Channel.DEV, external_id="checkout-hints-user")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="checkout-hints-user",
            customer_id=customer.id,
        )
        current_order = await repository.add_item_to_current_order(
            customer.id,
            conversation.id,
            sku="hamburguesa-completa",
            quantity=1,
        )
        current_order = await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
        recovered_order = await service._apply_checkout_hints_from_message(
            repository=repository,
            customer_id=customer.id,
            conversation_id=conversation.id,
            current_order=current_order,
            message_text="Pago cash.",
        )
        await session.commit()

    assert recovered_order.payment_method is PaymentMethod.CASH

    await runtime.engine.dispose()


async def test_recover_large_order_after_model_failure_returns_none_when_no_menu_lines_parse(tmp_path: Path):
    """Model-failure recovery should stop when the message mentions no recognizable menu items."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="large-order-weak-parse-user",
        name="Diego",
    )

    async with service.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.DEV, external_id="large-order-weak-parse-user"
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="large-order-weak-parse-user",
            customer_id=customer.id,
        )
        await session.commit()

    result = await service._recover_large_order_after_model_failure(
        conversation_id=conversation.id,
        customer_id=customer.id,
        customer=customer,
        user_text="Quiero algo rico y sorpresa.",
        store_id=settings.default_store_id,
    )

    assert result is None

    await runtime.engine.dispose()


async def test_large_order_detection_helper_covers_positive_and_negative_cases(tmp_path: Path):
    """The dense-order detector should distinguish large multi-line asks from simple messages."""
    runtime = create_database_runtime(build_settings(tmp_path))
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=build_settings(tmp_path))

    assert service._looks_like_large_order_attempt("Quiero 2 pizzas muzza, 1 docena de empanadas y 2 gaseosas.")
    assert service._looks_like_large_order_attempt("Quiero una hamburguesa completa.") is False
    assert service._looks_like_large_order_attempt("Buenas noches") is False

    await runtime.engine.dispose()


async def test_recover_large_order_after_model_failure_skips_existing_draft_orders(tmp_path: Path):
    """Large-order recovery should not overwrite an existing draft."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    service = OrderingAssistantService(session_factory=runtime.session_factory, settings=settings)
    await seed_named_customer(
        service,
        channel=Channel.DEV,
        external_user_id="large-order-existing-draft-user",
        name="Diego",
    )

    async with service.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.DEV,
            external_id="large-order-existing-draft-user",
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.DEV,
            external_id="large-order-existing-draft-user",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(customer.id, conversation.id, sku="hamburguesa-completa", quantity=1)
        await session.commit()

    result = await service._recover_large_order_after_model_failure(
        conversation_id=conversation.id,
        customer_id=customer.id,
        customer=customer,
        user_text="Quiero 2 pizzas muzza, 1 docena de empanadas clásicas y 2 gaseosas cola 1.5L.",
        store_id=settings.default_store_id,
    )

    assert result is None

    await runtime.engine.dispose()
