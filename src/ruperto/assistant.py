"""Transactional ordering assistant built with PydanticAI."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

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

BASE_INSTRUCTIONS = """
Sos el asistente virtual de un local de comida en Argentina.

Reglas operativas:
- Respondés siempre en español de Argentina, con tono amable, claro y breve.
- Usá algunos emojis simples y útiles cuando sumen calidez o claridad, sin exagerar.
- No inventes productos, precios, stock, direcciones, tiempos ni pagos.
- Para cualquier dato del negocio tenés que usar herramientas.
- Hace una sola pregunta por vez cuando falten datos.
- Si no conocés el nombre del cliente, pedilo al comienzo antes de avanzar con el pedido.
- Prioriza cerrar el pedido con la menor fricción posible.
- Si el cliente ya es conocido y hay una memoria útil, podés mencionarla con naturalidad.
- Si preguntan por demora o tiempo estimado, usá la herramienta de demora disponible.
- Si el local está cerrado, podés seguir ayudando pero avisá claramente cuándo vuelve a abrir.
- Si el cliente ya eligió una comida y todavía no sumó bebida ni postre,
  podés sugerir una opción de bebida o postre de forma breve y natural.
- Si el cliente pide algo fuera del alcance del bot, deriva a una persona.
""".strip()

NAME_CONFIRMATION_TEMPLATE = "¡Gracias, {name}! 😄 ¿Qué querés pedir hoy?"
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
NAME_PROMPT_VARIANTS = (
    "👋 Hola, soy {bot_name}, el asistente de pedidos de {store_name}. Antes de seguir, ¿me decís tu nombre?",
    "🍽️ Hola, te habla {bot_name}, el asistente de pedidos de {store_name}. Para arrancar, ¿cómo te llamás?",
)


@dataclass(slots=True)
class AssistantDeps:
    """Dependencies injected into the assistant tools."""

    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    customer_id: int
    conversation_id: int


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
    store = await with_repository(ctx, lambda repository: repository.get_store_profile())
    return (
        f"Atendes {store.store_name}. "
        f"Bot: {store.bot_name}. "
        f"Ubicacion: {store.store_location or 'No especificada'}. "
        f"Descripcion: {store.store_description}. "
        f"Personalidad deseada: {store.assistant_personality}. "
        f"Idioma de interfaz: {store.locale}."
    )


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

    async def handle_customer_message(
        self,
        *,
        channel: Channel,
        external_user_id: str,
        message_text: str,
        model: Model | str | None = None,
    ) -> AssistantTurnResult:
        """Process one customer message and persist the resulting turn."""
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
            customer = await self._capture_customer_name_from_message(
                repository=repository,
                customer=customer,
                history=history,
                message_text=message_text,
            )
            availability = await repository.get_store_availability(timezone_name=self.settings.store_timezone)
            deps = AssistantDeps(
                settings=self.settings,
                session_factory=self.session_factory,
                customer_id=customer.id,
                conversation_id=conversation.id,
            )

            direct_reply = await self._maybe_handle_missing_customer_name(
                repository=repository,
                customer=customer,
                conversation_id=conversation.id,
                history=history,
                message_text=message_text,
                availability=availability,
            )
            if direct_reply is not None:
                await session.commit()
                return direct_reply

            await session.commit()
            customer_id = customer.id
            conversation_id = conversation.id

        active_model = model if model is not None else build_google_model(self.settings)
        result = await self.agent.run(
            message_text,
            deps=deps,
            message_history=history,
            model=active_model,
        )

        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            await repository.append_conversation_messages(conversation_id, result.new_messages())
            refreshed_customer = await repository.get_customer(customer_id)
            current_order = await repository.get_latest_order(
                customer_id,
                conversation_id,
            )
            reply = self._decorate_reply_with_store_availability(result.output, availability)
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
    ) -> AssistantTurnResult | None:
        """Handle the initial name-capture flow deterministically before invoking the model."""
        if customer.name is not None:
            return None

        if self._is_waiting_for_name(history):
            if name := self._extract_name_candidate(message_text):
                updated_customer = await repository.update_customer_name(customer.id, name)
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
                return AssistantTurnResult(
                    conversation_id=conversation_id,
                    customer=updated_customer,
                    reply=reply,
                    current_order=None,
                )

            reply = AssistantReply(
                reply_text=self._decorate_closed_store_text(
                    "🙂 Necesito tu nombre para seguir con el pedido. ¿Cómo te llamás?",
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
            return AssistantTurnResult(
                conversation_id=conversation_id,
                customer=customer,
                reply=reply,
                current_order=None,
            )

        reply = AssistantReply(
            reply_text=self._decorate_closed_store_text(
                self._build_name_prompt(conversation_id=conversation_id),
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
        return AssistantTurnResult(
            conversation_id=conversation_id,
            customer=customer,
            reply=reply,
            current_order=None,
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

    def _build_name_prompt(self, *, conversation_id: int) -> str:
        """Build the first-contact greeting asking for the customer's name."""
        template = NAME_PROMPT_VARIANTS[conversation_id % len(NAME_PROMPT_VARIANTS)]
        return template.format(
            bot_name=self.settings.bot_name,
            store_name=self.settings.store_name,
        )

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
