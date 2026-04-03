"""Transactional ordering assistant built with PydanticAI."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ruperto.config import Settings
from ruperto.models import Channel, DeliveryType, PaymentMethod
from ruperto.repository import BusinessRepository
from ruperto.schemas import (
    AssistantNextStep,
    AssistantReply,
    AssistantTurnResult,
    CustomerMemorySnapshot,
    CustomerSnapshot,
    DelayEstimateSnapshot,
    MenuItemSnapshot,
    OrderSnapshot,
    StoreAvailabilitySnapshot,
)

logger = logging.getLogger(__name__)

BASE_INSTRUCTIONS = """
Sos el asistente virtual de un local de comida en Argentina.

Reglas operativas:
- Respondés siempre en español de Argentina, con tono amable, claro y breve.
- Usá algunos emojis simples y útiles cuando sumen calidez o claridad, sin exagerar.
- No inventes productos, precios, stock, direcciones, tiempos ni pagos.
- Para cualquier dato del negocio tenés que usar herramientas.
- Hace una sola pregunta por vez cuando falten datos.
- Si no conocés el nombre del cliente, pedilo al comienzo antes de avanzar con el pedido.
- Si el cliente manda varias definiciones en un solo mensaje, resolvé en ese mismo turno
  todo lo explícito que puedas: nombre, items, cantidades, precio consultado y preferencia de pago.
- Prioriza cerrar el pedido con la menor fricción posible.
- Si el cliente ya es conocido y hay una memoria útil, podés mencionarla con naturalidad.
- Si preguntan por demora o tiempo estimado, usá la herramienta de demora disponible.
- Si el local está cerrado, podés seguir ayudando pero avisá claramente cuándo vuelve a abrir.
- Si el cliente ya eligió una comida y todavía no sumó bebida ni postre,
  podés sugerir una opción de bebida o postre de forma breve y natural.
- Si el cliente ya expresó una preferencia de pago en el mensaje actual, no la repreguntes.
- Si el cliente pregunta cuánto sale, incluí el total o subtotal actual cuando ya lo puedas calcular.
- Si el cliente pide algo fuera del alcance del bot, deriva a una persona.
""".strip()

NAME_CONFIRMATION_TEMPLATE = "¡Gracias, {name}! 😄 ¿Qué querés pedir hoy?"
MODEL_UNAVAILABLE_REPLY = (
    "Se me complicó responder justo ahora 😓 Si querés, probá de nuevo en unos segundos o te derivo con una persona."
)
MAX_NAME_WORDS = 3
CLOSED_STORE_PREFIX = "{message_text} "
NAME_TOKEN_PATTERN = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ'-]+$")
INTRO_NAME_PATTERNS = (
    re.compile(r"\bsoy\s+(?P<tail>.+)", re.IGNORECASE),
    re.compile(r"\bme\s+llamo\s+(?P<tail>.+)", re.IGNORECASE),
    re.compile(r"\bmi\s+nombre\s+es\s+(?P<tail>.+)", re.IGNORECASE),
)
INTRO_NAME_STOP_WORDS = {
    "y",
    "me",
    "mandás",
    "mandame",
    "mandáme",
    "quiero",
    "quisiera",
    "te",
    "pido",
    "pedir",
    "para",
    "con",
    "sin",
    "cuánto",
    "cuanto",
    "sale",
    "es",
    "acá",
    "aca",
    "pago",
}
SUSPICIOUS_INFORMATIONAL_REPLY_PATTERNS = (
    re.compile(r"\bregistr[eé]\b", re.IGNORECASE),
    re.compile(r"\btu pedido\b", re.IGNORECASE),
    re.compile(r"\bpedido confirmado\b", re.IGNORECASE),
    re.compile(r"\bse env[ií]a\b", re.IGNORECASE),
    re.compile(r"\blo enviamos\b", re.IGNORECASE),
    re.compile(r"\bpago es\b", re.IGNORECASE),
    re.compile(r"\bgracias por tu compra\b", re.IGNORECASE),
    re.compile(r"\babonar[áa]s?\b", re.IGNORECASE),
)
ORDER_INTENT_HINTS = (
    "quiero",
    "quisiera",
    "mandame",
    "mandáme",
    "sumame",
    "sumáme",
    "agregame",
    "agregáme",
    "dame",
    "pedime",
    "pedíme",
    "para pedir",
    "te pido",
    "llevo",
)
INFORMATIONAL_MENU_HINTS = (
    "tenés",
    "tenes",
    "tienen",
    "hay",
    "qué tienen",
    "que tienen",
    "qué hay",
    "que hay",
    "mostrame",
    "mostrame el menú",
    "menu",
)
EXPLICIT_CONFIRMATION_HINTS = (
    "confirmo",
    "confirmá",
    "confirma",
    "dale",
    "listo",
    "cerralo",
    "cerrá el pedido",
    "confirmar pedido",
)
NAME_PROMPT_VARIANTS = (
    "👋 Hola, soy {bot_name}, el asistente de pedidos de {store_name}. Antes de seguir, ¿me decís tu nombre?",
    "🍽️ Hola, te habla {bot_name}, el asistente de pedidos de {store_name}. Para arrancar, ¿cómo te llamás?",
)


@dataclass(slots=True)
class AssistantDeps:
    """Dependencies injected into the assistant tools."""

    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    store_id: int
    customer_id: int
    conversation_id: int
    turn_context_hint: str | None = None
    allow_order_mutations: bool = True
    allow_order_confirmation: bool = False


@dataclass(slots=True, frozen=True)
class TurnPolicy:
    """Deterministic guardrails derived from the latest customer message."""

    allow_order_mutations: bool
    allow_order_confirmation: bool


@dataclass(slots=True)
class NameHandlingResult:
    """Outcome of the deterministic onboarding guardrails for one turn."""

    customer: CustomerSnapshot
    direct_reply: AssistantTurnResult | None = None
    resumed_pending_message: str | None = None


@dataclass(slots=True)
class ModelRunContext:
    """Inputs required to execute one agent run with retries."""

    message_text: str
    deps: AssistantDeps
    history: list[ModelMessage]
    model: Model | str
    external_user_id: str
    conversation_id: int


class AgentRunResult(Protocol):
    """Minimal protocol required from one completed agent run."""

    output: AssistantReply

    def new_messages(self) -> list[ModelMessage]:
        """Return the messages produced during the run."""


class InformationalTurnMutationError(ValueError):
    """Raised when the model tries to mutate an order during an informational turn."""

    def __init__(self) -> None:
        super().__init__("Este turno es solo informativo: no cambies el pedido todavía.")


class MissingConfirmationSignalError(ValueError):
    """Raised when the model tries to confirm without an explicit user confirmation."""

    def __init__(self) -> None:
        super().__init__("No confirmes el pedido sin una señal explícita del cliente en este turno.")


async def with_repository[RepositoryResult](
    ctx: RunContext[AssistantDeps],
    operation: Callable[[BusinessRepository], Awaitable[RepositoryResult]],
    *,
    commit: bool = False,
) -> RepositoryResult:
    """Run one repository operation inside a short-lived session."""
    async with ctx.deps.session_factory() as session:
        repository = BusinessRepository(session)
        result = await operation(repository)
        if commit:
            await session.commit()
        return result


ordering_agent = cast(
    Agent[AssistantDeps, AssistantReply],
    Agent(
        None,
        deps_type=AssistantDeps,
        output_type=AssistantReply,
        instructions=BASE_INSTRUCTIONS,
        defer_model_check=True,
        max_concurrency=1,
    ),
)


@ordering_agent.instructions
async def business_context(ctx: RunContext[AssistantDeps]) -> str:
    """Provide dynamic business context to the model."""
    store = await with_repository(ctx, lambda repository: repository.get_store_profile(store_id=ctx.deps.store_id))
    context = (
        f"Atendes {store.store_name}. "
        f"Bot: {store.bot_name}. "
        f"Ubicacion: {store.store_location or 'No especificada'}. "
        f"Descripcion: {store.store_description}. "
        f"Personalidad deseada: {store.assistant_personality}. "
        f"Idioma de interfaz: {store.locale}."
    )
    if ctx.deps.turn_context_hint is None:
        return context
    return f"{context} Contexto del turno: {ctx.deps.turn_context_hint}"


@ordering_agent.tool
async def lookup_customer(ctx: RunContext[AssistantDeps]) -> CustomerSnapshot:
    """Return the current customer profile."""
    return await with_repository(ctx, lambda repository: repository.get_customer(ctx.deps.customer_id))


@ordering_agent.tool
async def update_customer_name(ctx: RunContext[AssistantDeps], name: str) -> CustomerSnapshot:
    """Persist the customer name."""
    return await with_repository(
        ctx,
        lambda repository: repository.update_customer_name(ctx.deps.customer_id, name),
        commit=True,
    )


@ordering_agent.tool
async def list_menu(ctx: RunContext[AssistantDeps]) -> list[MenuItemSnapshot]:
    """List the visible menu."""
    return await with_repository(ctx, lambda repository: repository.list_menu_items())


@ordering_agent.tool
async def search_menu(ctx: RunContext[AssistantDeps], query: str) -> list[MenuItemSnapshot]:
    """Search menu items by name."""
    return await with_repository(ctx, lambda repository: repository.search_menu_items(query))


@ordering_agent.tool
async def get_customer_memory(ctx: RunContext[AssistantDeps]) -> CustomerMemorySnapshot:
    """Return lightweight memory derived from previous confirmed orders."""
    return await with_repository(ctx, lambda repository: repository.get_customer_memory(ctx.deps.customer_id))


@ordering_agent.tool
async def get_estimated_delay(ctx: RunContext[AssistantDeps]) -> DelayEstimateSnapshot:
    """Return a deterministic operational delay estimate for the current conversation."""
    return await with_repository(
        ctx,
        lambda repository: repository.get_estimated_delay(
            ctx.deps.customer_id,
            ctx.deps.conversation_id,
        ),
    )


@ordering_agent.tool
async def get_store_availability(ctx: RunContext[AssistantDeps]) -> StoreAvailabilitySnapshot:
    """Return whether the store is open and the next opening time."""
    return await with_repository(
        ctx,
        lambda repository: repository.get_store_availability(
            timezone_name=ctx.deps.settings.store_timezone,
            store_id=ctx.deps.store_id,
        ),
    )


@ordering_agent.tool
async def get_current_order(ctx: RunContext[AssistantDeps]) -> OrderSnapshot:
    """Return the current draft order, creating it if needed."""
    order = await with_repository(
        ctx,
        lambda repository: repository.get_current_order(
            ctx.deps.customer_id,
            ctx.deps.conversation_id,
            create_if_missing=True,
        ),
        commit=True,
    )
    assert order is not None
    return order


@ordering_agent.tool
async def add_item_to_current_order(
    ctx: RunContext[AssistantDeps],
    sku: str,
    quantity: int,
    notes: str | None = None,
) -> OrderSnapshot:
    """Add one menu item to the current draft order."""
    if not ctx.deps.allow_order_mutations:
        raise InformationalTurnMutationError
    return await with_repository(
        ctx,
        lambda repository: repository.add_item_to_current_order(
            ctx.deps.customer_id,
            ctx.deps.conversation_id,
            sku=sku,
            quantity=quantity,
            notes=notes,
        ),
        commit=True,
    )


@ordering_agent.tool
async def set_order_delivery_type(
    ctx: RunContext[AssistantDeps],
    delivery_type: DeliveryType,
) -> OrderSnapshot:
    """Set the delivery mode for the active order."""
    if not ctx.deps.allow_order_mutations:
        raise InformationalTurnMutationError
    return await with_repository(
        ctx,
        lambda repository: repository.set_order_delivery_type(
            ctx.deps.customer_id,
            ctx.deps.conversation_id,
            delivery_type,
        ),
        commit=True,
    )


@ordering_agent.tool
async def set_order_delivery_address(
    ctx: RunContext[AssistantDeps],
    address: str,
) -> OrderSnapshot:
    """Set the delivery address for the active order."""
    if not ctx.deps.allow_order_mutations:
        raise InformationalTurnMutationError
    return await with_repository(
        ctx,
        lambda repository: repository.set_order_delivery_address(
            ctx.deps.customer_id,
            ctx.deps.conversation_id,
            address,
        ),
        commit=True,
    )


@ordering_agent.tool
async def set_order_payment_method(
    ctx: RunContext[AssistantDeps],
    payment_method: PaymentMethod,
) -> OrderSnapshot:
    """Set the payment method for the active order."""
    if not ctx.deps.allow_order_mutations:
        raise InformationalTurnMutationError
    return await with_repository(
        ctx,
        lambda repository: repository.set_order_payment_method(
            ctx.deps.customer_id,
            ctx.deps.conversation_id,
            payment_method,
        ),
        commit=True,
    )


@ordering_agent.tool
async def confirm_current_order(ctx: RunContext[AssistantDeps]) -> OrderSnapshot:
    """Confirm the active order once it contains items."""
    if not ctx.deps.allow_order_confirmation:
        raise MissingConfirmationSignalError
    return await with_repository(
        ctx,
        lambda repository: repository.confirm_current_order(
            ctx.deps.customer_id,
            ctx.deps.conversation_id,
        ),
        commit=True,
    )


def build_google_model(settings: Settings) -> GoogleModel:
    """Build the production Gemini model from the configured settings."""
    api_key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key is not None else None
    provider = GoogleProvider(api_key=api_key)
    return GoogleModel(settings.gemini_model, provider=provider)


class OrderingAssistantService:
    """Application service that orchestrates one customer turn."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        settings: Settings,
        agent: Agent[AssistantDeps, AssistantReply] = ordering_agent,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.agent = agent

    async def _build_model_unavailable_result(
        self,
        *,
        conversation_id: int,
        customer: CustomerSnapshot,
        user_text: str,
    ) -> AssistantTurnResult:
        """Persist and return a friendly fallback when the model is unavailable."""
        fallback_reply = AssistantReply(
            reply_text=MODEL_UNAVAILABLE_REPLY,
            next_step=AssistantNextStep.HANDOFF,
            handoff=True,
        )
        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            await self._persist_direct_reply(
                repository=repository,
                conversation_id=conversation_id,
                user_text=user_text,
                reply_text=fallback_reply.reply_text,
            )
            await session.commit()
        return AssistantTurnResult(
            conversation_id=conversation_id,
            customer=customer,
            reply=fallback_reply,
            current_order=None,
        )

    async def _run_agent_with_retries(
        self,
        *,
        run_context: ModelRunContext,
    ) -> AgentRunResult:
        """Run the agent with timeout logging and one or more retry attempts."""
        attempts = self.settings.assistant_model_retry_attempts + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(self.settings.assistant_model_timeout_seconds):
                    return await self.agent.run(
                        run_context.message_text,
                        deps=run_context.deps,
                        message_history=run_context.history,
                        model=run_context.model,
                    )
            except TimeoutError as error:
                logger.warning(
                    "Assistant model timed out",
                    extra={
                        "external_user_id": run_context.external_user_id,
                        "conversation_id": run_context.conversation_id,
                        "attempt": attempt,
                        "max_attempts": attempts,
                    },
                )
                last_error = error
            except Exception as error:
                if isinstance(error, (InformationalTurnMutationError, MissingConfirmationSignalError)):
                    raise
                logger.warning(
                    "Assistant model failed",
                    exc_info=error,
                    extra={
                        "external_user_id": run_context.external_user_id,
                        "conversation_id": run_context.conversation_id,
                        "attempt": attempt,
                        "max_attempts": attempts,
                    },
                )
                last_error = error

            if attempt < attempts:
                await asyncio.sleep(min(0.25 * attempt, 1.0))

        assert last_error is not None
        raise last_error

    async def handle_customer_message(
        self,
        *,
        channel: Channel,
        external_user_id: str,
        message_text: str,
        model: Model | str | None = None,
        store_id: int | None = None,
    ) -> AssistantTurnResult:
        """Process one customer message and persist the resulting turn."""
        resolved_store_id = store_id if store_id is not None else self.settings.default_store_id
        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            customer = await repository.get_or_create_customer(
                channel=channel,
                external_id=external_user_id,
                phone_number=external_user_id if channel == Channel.WHATSAPP else None,
            )
            conversation = await repository.get_or_create_conversation(
                channel=channel,
                external_id=external_user_id,
                customer_id=customer.id,
            )
            history = await repository.load_conversation_messages(conversation.id)
            pending_customer_message = await repository.get_pending_customer_message(conversation.id)
            current_order_before_run = await repository.get_current_order(
                customer.id,
                conversation.id,
                create_if_missing=False,
            )
            customer = await self._capture_customer_name_from_message(
                repository=repository,
                customer=customer,
                history=history,
                message_text=message_text,
            )
            availability = await repository.get_store_availability(
                timezone_name=self.settings.store_timezone,
                store_id=resolved_store_id,
            )
            name_handling = await self._maybe_handle_missing_customer_name(
                repository=repository,
                customer=customer,
                conversation_id=conversation.id,
                history=history,
                message_text=message_text,
                availability=availability,
                pending_customer_message=pending_customer_message,
            )
            if name_handling.direct_reply is not None:
                await session.commit()
                return name_handling.direct_reply

            customer = name_handling.customer
            resumed_pending_message = name_handling.resumed_pending_message
            turn_policy = self._analyze_turn_policy(
                resumed_pending_message or message_text,
                current_order=current_order_before_run,
            )
            deps = AssistantDeps(
                settings=self.settings,
                session_factory=self.session_factory,
                store_id=resolved_store_id,
                customer_id=customer.id,
                conversation_id=conversation.id,
                turn_context_hint=self._build_turn_context_hint(
                    customer=customer,
                    message_text=message_text,
                    current_order=current_order_before_run,
                    pending_customer_message=resumed_pending_message,
                ),
                allow_order_mutations=turn_policy.allow_order_mutations,
                allow_order_confirmation=turn_policy.allow_order_confirmation,
            )

            await session.commit()
            customer_id = customer.id
            conversation_id = conversation.id

        active_model = model if model is not None else build_google_model(self.settings)
        try:
            result = await self._run_agent_with_retries(
                run_context=ModelRunContext(
                    message_text=message_text,
                    deps=deps,
                    history=history,
                    model=active_model,
                    external_user_id=external_user_id,
                    conversation_id=conversation_id,
                ),
            )
        except TimeoutError:
            return await self._build_model_unavailable_result(
                conversation_id=conversation_id,
                customer=customer,
                user_text=message_text,
            )
        except Exception as error:
            if isinstance(error, (InformationalTurnMutationError, MissingConfirmationSignalError)):
                raise
            return await self._build_model_unavailable_result(
                conversation_id=conversation_id,
                customer=customer,
                user_text=message_text,
            )

        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            await repository.append_conversation_messages(conversation_id, result.new_messages())
            refreshed_customer = await repository.get_customer(customer_id)
            current_order = await repository.get_latest_order(
                customer_id,
                conversation_id,
            )
            delay = (
                await repository.get_estimated_delay(customer_id, conversation_id)
                if current_order is not None and current_order.items
                else None
            )
            reply = self._decorate_reply_with_store_availability(result.output, availability)
            reply = self._ground_reply_for_turn_policy(reply, turn_policy, current_order=current_order)
            reply = self._guide_reply_with_current_order(
                reply,
                customer=refreshed_customer,
                current_order=current_order,
                delay=delay,
            )
            if not turn_policy.allow_order_mutations:
                current_order = None
            await session.commit()
            return AssistantTurnResult(
                conversation_id=conversation_id,
                customer=refreshed_customer,
                reply=reply,
                current_order=current_order,
            )

    async def _maybe_handle_missing_customer_name(  # noqa: PLR0913
        self,
        *,
        repository: BusinessRepository,
        customer: CustomerSnapshot,
        conversation_id: int,
        history: list[ModelMessage],
        message_text: str,
        availability: StoreAvailabilitySnapshot,
        pending_customer_message: str | None,
    ) -> NameHandlingResult:
        """Handle the initial name-capture flow deterministically before invoking the model."""
        if customer.name is not None:
            return NameHandlingResult(customer=customer)

        if self._is_waiting_for_name(history):
            if name := self._extract_customer_name(message_text):
                updated_customer = await repository.update_customer_name(customer.id, name)
                if pending_customer_message is not None:
                    await repository.set_pending_customer_message(conversation_id, None)
                    return NameHandlingResult(
                        customer=updated_customer,
                        resumed_pending_message=pending_customer_message,
                    )

                reply = AssistantReply(
                    reply_text=self._decorate_closed_store_text(
                        NAME_CONFIRMATION_TEMPLATE.format(name=updated_customer.name),
                        availability,
                    ),
                    next_step=AssistantNextStep.CHOOSE_ITEMS,
                    handoff=False,
                )
                await self._persist_direct_reply(
                    repository=repository,
                    conversation_id=conversation_id,
                    user_text=message_text,
                    reply_text=reply.reply_text,
                )
                return NameHandlingResult(
                    customer=updated_customer,
                    direct_reply=AssistantTurnResult(
                        conversation_id=conversation_id,
                        customer=updated_customer,
                        reply=reply,
                        current_order=None,
                    ),
                )

            reply = AssistantReply(
                reply_text=self._decorate_closed_store_text(
                    "🙂 Necesito tu nombre para seguir con lo que me pediste. ¿Cómo te llamás?",
                    availability,
                ),
                next_step=AssistantNextStep.ASK_NAME,
                handoff=False,
            )
            await self._persist_direct_reply(
                repository=repository,
                conversation_id=conversation_id,
                user_text=message_text,
                reply_text=reply.reply_text,
            )
            return NameHandlingResult(
                customer=customer,
                direct_reply=AssistantTurnResult(
                    conversation_id=conversation_id,
                    customer=customer,
                    reply=reply,
                    current_order=None,
                ),
            )

        if self._should_store_pending_message_before_name(message_text):
            await repository.set_pending_customer_message(conversation_id, message_text)
        reply = AssistantReply(
            reply_text=self._decorate_closed_store_text(
                self._build_name_prompt(
                    conversation_id=conversation_id,
                    remembers_pending_message=self._should_store_pending_message_before_name(message_text),
                ),
                availability,
            ),
            next_step=AssistantNextStep.ASK_NAME,
            handoff=False,
        )
        await self._persist_direct_reply(
            repository=repository,
            conversation_id=conversation_id,
            user_text=message_text,
            reply_text=reply.reply_text,
        )
        return NameHandlingResult(
            customer=customer,
            direct_reply=AssistantTurnResult(
                conversation_id=conversation_id,
                customer=customer,
                reply=reply,
                current_order=None,
            ),
        )

    async def _capture_customer_name_from_message(
        self,
        *,
        repository: BusinessRepository,
        customer: CustomerSnapshot,
        history: list[ModelMessage],
        message_text: str,
    ) -> CustomerSnapshot:
        """Persist a detected customer name before the rest of the turn continues."""
        if customer.name is not None:
            return customer
        if self._is_waiting_for_name(history):
            return customer
        if name := self._extract_name_from_introduction(message_text):
            return await repository.update_customer_name(customer.id, name)
        return customer

    async def _persist_direct_reply(
        self,
        *,
        repository: BusinessRepository,
        conversation_id: int,
        user_text: str,
        reply_text: str,
    ) -> None:
        """Persist a deterministic user/assistant exchange outside the model loop."""
        await repository.append_conversation_messages(
            conversation_id,
            [
                ModelRequest(parts=[UserPromptPart(content=user_text)]),
                ModelResponse(parts=[TextPart(content=reply_text)], model_name="ruperto:system"),
            ],
        )

    def _is_waiting_for_name(self, history: list[ModelMessage]) -> bool:
        """Return whether the latest assistant reply explicitly requested the customer's name."""
        latest_assistant_text = self._extract_latest_assistant_text(history)
        if latest_assistant_text is None:
            return False
        lowered = latest_assistant_text.lower()
        return "tu nombre" in lowered or "cómo te llamás" in lowered or "como te llamas" in lowered

    def _extract_latest_assistant_text(self, history: list[ModelMessage]) -> str | None:
        """Extract the latest text response persisted in the conversation history."""
        for message in reversed(history):
            if not isinstance(message, ModelResponse):
                continue
            for part in reversed(message.parts):
                if isinstance(part, TextPart):
                    return part.content
        return None

    def _extract_name_candidate(self, message_text: str) -> str | None:
        """Infer a plausible first-name style answer from a short user message."""
        cleaned = " ".join(message_text.split()).strip(" .,!?:;")
        if not cleaned:
            return None
        words = cleaned.split()
        if len(words) > MAX_NAME_WORDS:
            return None
        if any(char.isdigit() for char in cleaned):
            return None
        lowered = cleaned.lower()
        blocked_terms = {
            "hola",
            "quiero",
            "pedido",
            "pizza",
            "hamburguesa",
            "empanadas",
            "milanesa",
            "retiro",
            "delivery",
            "transferencia",
            "efectivo",
        }
        if any(term in lowered for term in blocked_terms):
            return None
        return cleaned.title()

    def _extract_customer_name(self, message_text: str) -> str | None:
        """Extract a customer name from either a short reply or a longer introduction."""
        return self._extract_name_from_introduction(message_text) or self._extract_name_candidate(message_text)

    def _should_store_pending_message_before_name(self, message_text: str) -> bool:
        """Return whether an unnamed first turn already contains useful order intent to resume later."""
        lowered = message_text.casefold()
        if not lowered.strip():
            return False
        if any(hint in lowered for hint in ORDER_INTENT_HINTS):
            return True
        if any(hint in lowered for hint in INFORMATIONAL_MENU_HINTS):
            return True
        if self._message_requests_total(message_text):
            return True
        return any(keyword in lowered for keyword in ("bebida", "postre", "combo", "hamburguesa", "pizza", "empanada"))

    def _extract_name_from_introduction(self, message_text: str) -> str | None:
        """Extract a first name from a longer self-introduction message."""
        cleaned = " ".join(message_text.split())
        if not cleaned:
            return None

        for pattern in INTRO_NAME_PATTERNS:
            match = pattern.search(cleaned)
            if match is None:
                continue
            if name := self._extract_first_name_from_intro_tail(match.group("tail")):
                return name
        return None

    def _extract_first_name_from_intro_tail(self, tail: str) -> str | None:
        """Return the first valid name token found after an introduction phrase."""
        candidate_tokens: list[str] = []
        normalized_tail = tail.replace(",", " ").replace(".", " ").replace(";", " ").replace(":", " ")
        for raw_token in normalized_tail.split():
            token = raw_token.strip("¡!¿?()[]{}\"'")
            if not token:
                continue
            lowered = token.casefold()
            if candidate_tokens and lowered in INTRO_NAME_STOP_WORDS:
                break
            if not NAME_TOKEN_PATTERN.fullmatch(token):
                break
            candidate_tokens.append(token)
            if len(candidate_tokens) >= MAX_NAME_WORDS:
                break

        if not candidate_tokens:
            return None
        return candidate_tokens[0].title()

    def _analyze_turn_policy(self, message_text: str, *, current_order: OrderSnapshot | None) -> TurnPolicy:
        """Return deterministic mutation/confirmation guardrails for the current turn."""
        lowered = message_text.casefold()
        has_order_intent = any(hint in lowered for hint in ORDER_INTENT_HINTS)
        asks_menu_information = any(hint in lowered for hint in INFORMATIONAL_MENU_HINTS)
        explicit_confirmation = any(hint in lowered for hint in EXPLICIT_CONFIRMATION_HINTS)
        payment_hint = self._detect_payment_method_hint(message_text)

        allow_order_mutations = has_order_intent or not asks_menu_information
        allow_order_confirmation = (
            explicit_confirmation
            or has_order_intent
            or (payment_hint is not None and current_order is not None and bool(current_order.items))
        )
        return TurnPolicy(
            allow_order_mutations=allow_order_mutations,
            allow_order_confirmation=allow_order_confirmation,
        )

    def _build_turn_context_hint(
        self,
        *,
        customer: CustomerSnapshot,
        message_text: str,
        current_order: OrderSnapshot | None,
        pending_customer_message: str | None = None,
    ) -> str | None:
        """Build safe guidance for dense first-turn customer messages."""
        hints: list[str] = []
        introduced_name = self._extract_name_from_introduction(message_text)
        if customer.name is not None and introduced_name == customer.name:
            hints.append(
                f"El cliente ya se presentó en este mensaje como {customer.name}. No vuelvas a pedirle el nombre."
            )

        payment_hint = self._detect_payment_method_hint(message_text)
        if payment_hint is PaymentMethod.CASH:
            hints.append(
                "El cliente expresó intención de pagar en efectivo al recibir o retirar. "
                "Si el pedido ya está suficientemente definido, podés registrar esa forma de pago."
            )
        elif payment_hint is PaymentMethod.TRANSFER:
            hints.append(
                "El cliente expresó intención de pagar por transferencia. "
                "Si el pedido ya está suficientemente definido, podés registrar esa forma de pago."
            )
        elif payment_hint is PaymentMethod.CARD_LINK:
            hints.append(
                "El cliente pidió pagar con link o tarjeta. "
                "Si el pedido ya está suficientemente definido, podés registrar esa forma de pago."
            )

        if self._message_requests_total(message_text):
            hints.append(
                "El cliente quiere saber cuánto sale en este mismo turno. "
                "Si ya podés calcularlo con herramientas, informá el total o subtotal actual."
            )
        if pending_customer_message is not None:
            hints.append(
                "Antes de identificarse, el cliente dejó una consulta pendiente: "
                f"'{pending_customer_message}'. "
                "Retomá eso ahora sin volver a preguntarle qué quiere."
            )
        hints.extend(self._build_current_order_context_hints(customer=customer, current_order=current_order))

        if not hints:
            return None
        return " ".join(hints)

    def _build_current_order_context_hints(
        self,
        *,
        customer: CustomerSnapshot,
        current_order: OrderSnapshot | None,
    ) -> list[str]:
        """Describe the current draft and the next missing checkout field."""
        if current_order is None or not current_order.items:
            return []

        items_text = ", ".join(f"{item.quantity} x {item.name}" for item in current_order.items)
        hints = [f"Pedido en curso: {items_text}. Total actual: {current_order.total_amount_display}."]
        if current_order.delivery_type is None:
            hints.append("Antes de confirmar, falta definir si es envío o retiro.")
            return hints

        if current_order.delivery_type == DeliveryType.DELIVERY and current_order.delivery_address is None:
            if customer.default_address:
                hints.append(
                    "Falta confirmar la dirección de envío. "
                    f"Dirección conocida del cliente: {customer.default_address}."
                )
            else:
                hints.append("Falta pedir la dirección de envío antes de confirmar.")
            return hints

        if current_order.payment_method is None:
            hints.append("Falta definir el medio de pago antes de confirmar.")
        return hints

    def _ground_reply_for_turn_policy(
        self,
        reply: AssistantReply,
        turn_policy: TurnPolicy,
        *,
        current_order: OrderSnapshot | None,
    ) -> AssistantReply:
        """Strip unsupported order-completion claims from informational turns."""
        if turn_policy.allow_order_mutations:
            return reply
        if not any(pattern.search(reply.reply_text) for pattern in SUSPICIOUS_INFORMATIONAL_REPLY_PATTERNS):
            return reply

        safe_sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", reply.reply_text)
            if sentence.strip()
            and not any(pattern.search(sentence) for pattern in SUSPICIOUS_INFORMATIONAL_REPLY_PATTERNS)
        ]
        if safe_sentences:
            safe_text = " ".join(safe_sentences)
        elif current_order is not None and current_order.items:
            safe_text = "Puedo contarte las opciones y los precios, pero no hice cambios en tu pedido en este mensaje."
        else:
            safe_text = "Puedo contarte las opciones y los precios, pero todavía no armé ningún pedido."
        if current_order is None and "todavía no armé ningún pedido" not in safe_text.casefold():
            safe_text = f"{safe_text} Todavía no armé ningún pedido."
        return reply.model_copy(
            update={
                "reply_text": safe_text,
                "next_step": AssistantNextStep.CHOOSE_ITEMS,
            }
        )

    def _guide_reply_with_current_order(
        self,
        reply: AssistantReply,
        *,
        customer: CustomerSnapshot,
        current_order: OrderSnapshot | None,
        delay: DelayEstimateSnapshot | None,
    ) -> AssistantReply:
        """Prefer deterministic checkout guidance once a draft already exists."""
        if current_order is None or not current_order.items or current_order.status.value != "draft":
            return reply

        if current_order.delivery_type is None:
            delay_text = f" La demora estimada es de {delay.display_text}." if delay is not None else ""
            item_summary = ", ".join(f"{item.quantity} x {item.name}" for item in current_order.items)
            return reply.model_copy(
                update={
                    "reply_text": (
                        f"Perfecto, {customer.name or 'che'}: por ahora llevo {item_summary} "
                        f"por {current_order.total_amount_display}.{delay_text} "
                        "¿Querés envío o retirás por el local?"
                    ),
                    "next_step": AssistantNextStep.CHOOSE_DELIVERY,
                }
            )

        if current_order.delivery_type == DeliveryType.DELIVERY and current_order.delivery_address is None:
            if customer.default_address:
                reply_text = (
                    f"Perfecto. ¿Te lo envío a {customer.default_address}? Si preferís otra dirección, pasámela."
                )
            else:
                reply_text = "Perfecto. Pasame la dirección de envío, por favor."
            return reply.model_copy(
                update={
                    "reply_text": reply_text,
                    "next_step": AssistantNextStep.ASK_ADDRESS,
                }
            )

        if current_order.payment_method is None:
            delay_text = f" La demora estimada es de {delay.display_text}." if delay is not None else ""
            return reply.model_copy(
                update={
                    "reply_text": (
                        f"Perfecto. El total es {current_order.total_amount_display}.{delay_text} "
                        "¿Cómo querés pagar: efectivo, transferencia o link de pago?"
                    ),
                    "next_step": AssistantNextStep.CHOOSE_PAYMENT,
                }
            )

        return reply

    def _detect_payment_method_hint(self, message_text: str) -> PaymentMethod | None:
        """Infer payment intent from common Argentine customer phrasing."""
        lowered = message_text.casefold()
        if any(
            phrase in lowered
            for phrase in ("transferencia", "transferir", "te transfiero", "te hago una transferencia")
        ):
            return PaymentMethod.TRANSFER
        if any(
            phrase in lowered
            for phrase in (
                "link de pago",
                "link",
                "tarjeta",
                "mercado pago",
                "mp",
            )
        ):
            return PaymentMethod.CARD_LINK
        if any(
            phrase in lowered
            for phrase in (
                "efectivo",
                "pago acá",
                "te pago acá",
                "pago aca",
                "te pago aca",
                "al recibir",
                "cuando llegue",
                "cuando llegues",
                "al retirar",
            )
        ):
            return PaymentMethod.CASH
        return None

    def _message_requests_total(self, message_text: str) -> bool:
        """Return whether the user explicitly asked for the order amount."""
        lowered = message_text.casefold()
        return any(
            phrase in lowered
            for phrase in (
                "cuánto es",
                "cuanto es",
                "cuánto sale",
                "cuanto sale",
                "cuánto sería",
                "cuanto seria",
                "precio",
                "total",
            )
        )

    def _build_name_prompt(self, *, conversation_id: int, remembers_pending_message: bool = False) -> str:
        """Build the first-contact greeting asking for the customer's name."""
        template = NAME_PROMPT_VARIANTS[conversation_id % len(NAME_PROMPT_VARIANTS)]
        prompt = template.format(
            bot_name=self.settings.bot_name,
            store_name=self.settings.store_name,
        )
        if not remembers_pending_message:
            return prompt
        return f"{prompt} Así sigo con lo que me pediste recién."

    def _decorate_reply_with_store_availability(
        self,
        reply: AssistantReply,
        availability: StoreAvailabilitySnapshot,
    ) -> AssistantReply:
        """Prefix replies when the store is currently closed."""
        if availability.is_open:
            return reply
        return reply.model_copy(
            update={
                "reply_text": self._decorate_closed_store_text(reply.reply_text, availability),
            }
        )

    def _decorate_closed_store_text(self, reply_text: str, availability: StoreAvailabilitySnapshot) -> str:
        """Prefix a reply with the store-closed notice when needed."""
        if availability.is_open:
            return reply_text
        if reply_text.startswith(availability.message_text):
            return reply_text
        return CLOSED_STORE_PREFIX.format(message_text=availability.message_text) + reply_text
