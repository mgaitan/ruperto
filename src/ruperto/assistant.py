"""Transactional ordering assistant built with PydanticAI."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ruperto.config import Settings
from ruperto.models import Channel, DeliveryType, PaymentMethod
from ruperto.repository import BusinessRepository, IncompleteOrderError
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
    StoreProfileSnapshot,
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
- Si no conocés el nombre del cliente, no frenes preguntas informativas solo para pedirlo.
- Pedí el nombre cuando ya haga falta para seguir armando o confirmar el pedido.
- Si el cliente manda varias definiciones en un solo mensaje, resolvé en ese mismo turno
  todo lo explícito que puedas: nombre, items, cantidades, precio consultado y preferencia de pago.
- Prioriza cerrar el pedido con la menor fricción posible.
- Si el cliente ya es conocido y hay una memoria útil, podés mencionarla con naturalidad.
- Si preguntan por demora o tiempo estimado, usá la herramienta de demora disponible.
- Si el local está cerrado, podés seguir ayudando pero avisá claramente cuándo vuelve a abrir.
- No repitas el aviso de local cerrado en todos los turnos: alcanza con mencionarlo al inicio
  o cuando el horario realmente cambie la respuesta.
- Los avisos de estado salen automáticamente por este medio
  cuando el pedido está casi listo, listo para retirar o en reparto.
- Si el cliente ya eligió una comida y todavía no sumó bebida ni postre,
  podés sugerir una opción de bebida o postre de forma breve y natural.
- Si el cliente ya expresó una preferencia de pago en el mensaje actual, no la repreguntes.
- Registrar el medio de pago no alcanza para cerrar el pedido:
  recién lo confirmás cuando el cliente lo aprueba explícitamente en ese turno.
- Si el cliente pregunta si hay una categoría o producto, no respondas solo sí o no:
  mencioná opciones concretas y precios.
- Si el cliente pregunta cuánto sale sobre una categoría o producto, respondé el precio
  de esa opción o listá variantes con precios.
- Solo informá el total o subtotal actual cuando esté claro que pregunta por el pedido,
  no cuando pregunta por una categoría o producto del menú.
- Si el cliente corrige algo del pedido, ajustá el borrador actual en vez de sumar más líneas por error.
- Si hace falta rehacer las líneas del pedido por una corrección, vaciá el borrador y cargalo de nuevo.
- Si el cliente pide programar un pedido para una hora puntual, tratá ese horario como prioridad.
- Evitá repetir muletillas como "Perfecto" o copiar exactamente la misma frase en cada turno.
- Cuando confirmes un pedido, mostrálos con aire visual: resumen separado, líneas cortas y datos fáciles de escanear.
- Si el cliente pide algo fuera del alcance del bot, deriva a una persona.
""".strip()

NAME_CONFIRMATION_TEMPLATE = "¡Gracias, {name}! 😄 ¿Qué querés pedir hoy?"
MODEL_UNAVAILABLE_REPLY = (
    "Se me complicó responder justo ahora 😓 Si querés, probá de nuevo en unos segundos o te derivo con una persona."
)
MAX_NAME_WORDS = 3
MAX_SCHEDULE_HOUR = 23
MAX_SCHEDULE_MINUTE = 59
NAME_TOKEN_PATTERN = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ'-]+$")
REQUESTED_READY_TIME_PATTERNS = (
    re.compile(
        r"\b(?:para|a)\s+las?\s+(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?:hs?|horas?)?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bpara\s+(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*hs?\b", re.IGNORECASE),
)
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
MENU_NOT_FOUND_REPLY_PATTERNS = (
    re.compile(r"\bno encontr[ée]\b", re.IGNORECASE),
    re.compile(r"\bno tenemos\b", re.IGNORECASE),
    re.compile(r"\bno figura\b", re.IGNORECASE),
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
MENU_INFORMATION_GROUPS = {
    "cervezas": {
        "message_hints": ("cerveza", "cervezas", "birra", "birrita", "cervecita", "ipa", "rubia", "roja"),
        "item_hints": ("cerveza", "ipa", "rubia", "roja"),
        "category_hints": ("bebidas",),
    },
    "gaseosas": {
        "message_hints": ("gaseosa", "gaseosas", "coca", "cola", "sprite", "lima-limón", "naranja"),
        "item_hints": ("gaseosa", "cola", "lima-limón", "naranja"),
        "category_hints": ("bebidas",),
    },
    "bebidas": {
        "message_hints": ("bebida", "bebidas", "para tomar", "tomar"),
        "item_hints": ("gaseosa", "agua", "cerveza", "saborizada"),
        "category_hints": ("bebidas",),
    },
    "papas": {
        "message_hints": ("papas", "papas fritas", "fritas"),
        "item_hints": ("papas",),
        "category_hints": ("guarniciones",),
    },
    "postres": {
        "message_hints": ("postre", "postres", "helado", "brownie", "flan", "tiramisú", "cheesecake"),
        "item_hints": ("budín", "brownie", "flan", "helado", "tiramisú", "cheesecake"),
        "category_hints": ("postres",),
    },
}
MENU_CONSTRAINT_PATTERNS = {
    "non_alcoholic": (
        "sin alcohol",
        "no alcohol",
        "sin cerveza",
        "no cerveza",
        "sin birra",
        "sin bebidas alcoholicas",
        "sin bebidas alcohólicas",
    ),
}
COLLOQUIAL_MENU_ITEM_ALIASES = {
    "burger veggie": "hamburguesa-veg",
    "burguer veggie": "hamburguesa-veg",
}
COLLOQUIAL_MENU_CATEGORY_ALIASES = {
    "cervecita": "cervezas",
    "cervecitas": "cervezas",
    "birrita": "cervezas",
    "birra": "cervezas",
}
UNSUPPORTED_CUSTOMIZATION_HINTS = (
    "doble picante",
    "triple cheddar",
    "doble cheddar",
    "triple picante",
    "extra cheddar",
    "extra picante",
)
CUSTOMIZATION_ACCEPTANCE_PATTERNS = (
    re.compile(r"\bya anot", re.IGNORECASE),
    re.compile(r"\banotamos\b", re.IGNORECASE),
    re.compile(r"\blo (?:vamos a )?preparar as[ií]\b", re.IGNORECASE),
    re.compile(r"\bqued[oó] con\b", re.IGNORECASE),
)
COMMON_MENU_SYNONYMS = {
    "muzzarella": ("muzza",),
    "hamburguesa": ("burger", "burguer"),
    "milanesa": ("mila",),
}
MIN_LARGE_ORDER_SEGMENTS = 2
MIN_LARGE_ORDER_QUANTITY_MENTIONS = 2
MIN_PARSED_FALLBACK_LINES = 2
MIN_ALIAS_MATCH_SCORE = 0.55
MIN_PLURAL_TOKEN_LENGTH = 3
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
ORDER_CORRECTION_HINTS = (
    "dije",
    "quise decir",
    "mejor dicho",
    "en realidad",
    "corrijo",
    "corrección",
    "corregí",
    "corregi",
    "no, era",
    "no, quise decir",
    "uno de cada",
    "uno y uno",
)
NAME_PROMPT_VARIANTS = (
    "👋 Hola, soy {bot_name}, el asistente de pedidos de {store_name}. Antes de seguir, ¿me decís tu nombre?",
    "🍽️ Hola, te habla {bot_name}, el asistente de pedidos de {store_name}. Para arrancar, ¿cómo te llamás?",
)
CLOSED_STORE_NOTICE_VARIANTS = (
    "Ahora estamos cerrados 😴 Abrimos {next_open_text}. ",
    "Justo ahora el local está cerrado 😴 Abrimos {next_open_text}. ",
    "En este momento estamos fuera de horario 😴 Abrimos {next_open_text}. ",
)
DELIVERY_PROMPT_VARIANTS = (
    "{lead}, {name}: por ahora llevo {items} por {total}.{timing_text} ¿Querés envío o retirás por el local?",
    "Anotado, {name}: hasta ahora va {items} por {total}.{timing_text} ¿Querés envío o retirás por el local?",
    "Bien, {name}: tengo {items} por {total}.{timing_text} ¿Querés envío o retirás por el local?",
)
ADD_ON_PROMPT_VARIANTS = (
    "{lead}, {name}: tengo {items} por {total}.{timing_text} Si querés, podés sumar {suggestion}. ¿Te tienta algo más?",
    (
        "Anotado, {name}: va {items} por {total}.{timing_text} "
        "Si te copa, le podés agregar {suggestion}. ¿Querés sumar algo más?"
    ),
    (
        "Buenísimo, {name}: llevo {items} por {total}. "
        "{timing_text} Si querés completar el pedido, podés sumar {suggestion}. ¿Te sirve algo más?"
    ),
)
ADDRESS_PROMPT_VARIANTS = (
    "Dale. ¿Te lo envío a {address}? Si preferís otra dirección, pasámela.",
    "Buenísimo. ¿Va a {address}? Si querés otro domicilio, decímelo.",
    "Listo. ¿Te sirve mandarlo a {address}? Si no, pasame la dirección correcta.",
)
PAYMENT_PROMPT_VARIANTS = (
    "{lead}. El total es {total}.{timing_text} ¿Cómo querés pagar: efectivo, transferencia o link de pago?",
    "Son {total}.{timing_text} ¿Preferís efectivo, transferencia o link de pago?",
    "Ya tengo {total}.{timing_text} ¿Lo resolvemos con efectivo, transferencia o link de pago?",
)
WEEKDAY_LABELS_ES = {
    0: "lunes",
    1: "martes",
    2: "miércoles",
    3: "jueves",
    4: "viernes",
    5: "sábado",
    6: "domingo",
}


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
async def reset_current_order(ctx: RunContext[AssistantDeps]) -> OrderSnapshot:
    """Clear the current draft items so the order can be rebuilt after a correction."""
    if not ctx.deps.allow_order_mutations:
        raise InformationalTurnMutationError
    return await with_repository(
        ctx,
        lambda repository: repository.reset_current_order(
            ctx.deps.customer_id,
            ctx.deps.conversation_id,
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
            timezone_name=ctx.deps.settings.store_timezone,
            store_id=ctx.deps.store_id,
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
        customer_id: int,
        customer: CustomerSnapshot,
        user_text: str,
        store_id: int,
    ) -> AssistantTurnResult:
        """Persist and return a friendly fallback when the model is unavailable."""
        recovered_result = await self._recover_large_order_after_model_failure(
            conversation_id=conversation_id,
            customer_id=customer_id,
            customer=customer,
            user_text=user_text,
            store_id=store_id,
        )
        if recovered_result is not None:
            return recovered_result
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

    async def _recover_large_order_after_model_failure(
        self,
        *,
        conversation_id: int,
        customer_id: int,
        customer: CustomerSnapshot,
        user_text: str,
        store_id: int,
    ) -> AssistantTurnResult | None:
        """Try a deterministic recovery for large multi-item orders after a model failure."""
        if not self._looks_like_large_order_attempt(user_text):
            return None

        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            menu_items = await repository.list_menu_items()
            parsed_lines = self._parse_menu_lines_from_message(user_text, menu_items=menu_items)
            if len(parsed_lines) < MIN_PARSED_FALLBACK_LINES:
                return None

            current_order = await repository.get_current_order(customer_id, conversation_id, create_if_missing=False)
            if current_order is not None and current_order.items:
                return None

            rebuilt_order: OrderSnapshot | None = None
            for menu_item, quantity in parsed_lines:
                rebuilt_order = await repository.add_item_to_current_order(
                    customer_id,
                    conversation_id,
                    sku=menu_item.sku,
                    quantity=quantity,
                )
            assert rebuilt_order is not None

            store = await repository.get_store_profile(store_id=store_id)
            delay = await repository.get_estimated_delay(customer_id, conversation_id)
            reply = self._guide_reply_with_current_order(
                AssistantReply(
                    reply_text="Anotado.",
                    next_step=AssistantNextStep.CHOOSE_ITEMS,
                    handoff=False,
                ),
                customer=customer,
                current_order=rebuilt_order,
                message_text=user_text,
                delay=delay,
                store=store,
                order_changed_during_turn=True,
                item_lines_changed_during_turn=True,
            )
            await self._persist_direct_reply(
                repository=repository,
                conversation_id=conversation_id,
                user_text=user_text,
                reply_text=reply.reply_text,
            )
            await session.commit()
            return AssistantTurnResult(
                conversation_id=conversation_id,
                customer=customer,
                reply=reply,
                current_order=rebuilt_order,
            )

    async def _recover_missing_confirmation_result(
        self,
        *,
        conversation_id: int,
        customer_id: int,
        customer: CustomerSnapshot,
        store_id: int,
        user_text: str,
    ) -> AssistantTurnResult:
        """Recover gracefully when checkout confirmation happens before the draft is complete."""
        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            current_order = await repository.get_latest_order(customer_id, conversation_id)
            store = await repository.get_store_profile(store_id=store_id)
            if current_order is None or not current_order.items or current_order.status.value != "draft":
                return await self._build_model_unavailable_result(
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    customer=customer,
                    user_text=user_text,
                    store_id=store_id,
                )
            current_order = await self._apply_checkout_hints_from_message(
                repository=repository,
                customer_id=customer_id,
                conversation_id=conversation_id,
                current_order=current_order,
                message_text=user_text,
            )
            delay = await repository.get_estimated_delay(customer_id, conversation_id)
            next_step = self._next_step_for_current_order(current_order)
            if next_step is AssistantNextStep.CONFIRM_ORDER:
                reply = AssistantReply(
                    reply_text=self._build_order_review_reply(
                        customer=customer,
                        current_order=current_order,
                        store=store,
                    ),
                    next_step=AssistantNextStep.CONFIRM_ORDER,
                    handoff=False,
                )
            else:
                reply = self._guide_reply_with_current_order(
                    AssistantReply(
                        reply_text="Seguimos con el pedido.",
                        next_step=AssistantNextStep.CHOOSE_ITEMS,
                        handoff=False,
                    ),
                    customer=customer,
                    current_order=current_order,
                    message_text=user_text,
                    delay=delay,
                    store=store,
                    order_changed_during_turn=True,
                    item_lines_changed_during_turn=False,
                )
            await self._persist_direct_reply(
                repository=repository,
                conversation_id=conversation_id,
                user_text=user_text,
                reply_text=reply.reply_text,
            )
            await session.commit()
        return AssistantTurnResult(
            conversation_id=conversation_id,
            customer=customer,
            reply=reply,
            current_order=current_order,
        )

    async def _recover_informational_turn_result(
        self,
        *,
        conversation_id: int,
        customer_id: int,
        customer: CustomerSnapshot,
        user_text: str,
    ) -> AssistantTurnResult:
        """Recover a useful informational reply when the model tried to mutate state."""
        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            current_order = await repository.get_latest_order(customer_id, conversation_id)
            reply = AssistantReply(
                reply_text="Puedo ayudarte con información del menú o del envío sin tocar el pedido todavía.",
                next_step=AssistantNextStep.CHOOSE_ITEMS,
                handoff=False,
            )
            if self._message_requests_delivery_information(user_text):
                reply = reply.model_copy(update={"reply_text": self._build_delivery_information_reply(user_text)})
            else:
                reply = await self._answer_menu_information_turn(
                    reply,
                    repository=repository,
                    message_text=user_text,
                    previous_user_message=self._extract_latest_user_text(
                        await repository.load_conversation_messages(conversation_id)
                    ),
                    turn_policy=TurnPolicy(allow_order_mutations=False, allow_order_confirmation=False),
                )
            await self._persist_direct_reply(
                repository=repository,
                conversation_id=conversation_id,
                user_text=user_text,
                reply_text=reply.reply_text,
            )
            await session.commit()
        return AssistantTurnResult(
            conversation_id=conversation_id,
            customer=customer,
            reply=reply,
            current_order=current_order if current_order is not None and current_order.items else None,
        )

    async def _apply_checkout_hints_from_message(
        self,
        *,
        repository: BusinessRepository,
        customer_id: int,
        conversation_id: int,
        current_order: OrderSnapshot,
        message_text: str,
    ) -> OrderSnapshot:
        """Apply deterministic checkout hints from the current customer message."""
        delivery_hint = self._detect_delivery_type_hint(message_text)
        if delivery_hint is not None and current_order.delivery_type is None:
            current_order = await repository.set_order_delivery_type(customer_id, conversation_id, delivery_hint)

        payment_hint = self._detect_payment_method_hint(message_text)
        if payment_hint is not None and current_order.payment_method is None:
            current_order = await repository.set_order_payment_method(customer_id, conversation_id, payment_hint)
        return current_order

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

    async def handle_customer_message(  # noqa: C901, PLR0911, PLR0912, PLR0915
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
            latest_assistant_text = self._extract_latest_assistant_text(history)
            latest_user_text = self._extract_latest_user_text(history)
            closed_store_context_text = (
                latest_assistant_text
                if latest_assistant_text is not None or not self._has_prior_conversation(history)
                else "closed-store-notice already shown"
            )
            pending_customer_message = await repository.get_pending_customer_message(conversation.id)
            current_order_before_run = await repository.get_current_order(
                customer.id,
                conversation.id,
                create_if_missing=False,
            )
            latest_order_before_run = await repository.get_latest_order(
                customer.id,
                conversation.id,
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
                latest_assistant_text=latest_assistant_text,
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
                    previous_user_message=latest_user_text,
                    latest_assistant_text=closed_store_context_text,
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
        except InformationalTurnMutationError:
            return await self._recover_informational_turn_result(
                conversation_id=conversation_id,
                customer_id=customer_id,
                customer=customer,
                user_text=message_text,
            )
        except MissingConfirmationSignalError:
            if current_order_before_run is None:
                raise
            return await self._recover_missing_confirmation_result(
                conversation_id=conversation_id,
                customer_id=customer_id,
                customer=customer,
                store_id=resolved_store_id,
                user_text=message_text,
            )
        except IncompleteOrderError:
            return await self._recover_missing_confirmation_result(
                conversation_id=conversation_id,
                customer_id=customer_id,
                customer=customer,
                store_id=resolved_store_id,
                user_text=message_text,
            )
        except TimeoutError:
            return await self._build_model_unavailable_result(
                conversation_id=conversation_id,
                customer_id=customer_id,
                customer=customer,
                user_text=message_text,
                store_id=resolved_store_id,
            )
        except Exception:  # noqa: BLE001
            return await self._build_model_unavailable_result(
                conversation_id=conversation_id,
                customer_id=customer_id,
                customer=customer,
                user_text=message_text,
                store_id=resolved_store_id,
            )

        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            await repository.append_conversation_messages(conversation_id, result.new_messages())
            refreshed_customer = await repository.get_customer(customer_id)
            store = await repository.get_store_profile(store_id=resolved_store_id)
            current_order = await repository.get_latest_order(
                customer_id,
                conversation_id,
            )
            schedule_error_text: str | None = None
            requested_ready_at = self._extract_requested_ready_at(
                message_text,
                timezone_name=self.settings.store_timezone,
            )
            if requested_ready_at is not None and current_order is not None and current_order.items:
                try:
                    current_order = await repository.set_order_requested_ready_at(
                        customer_id,
                        conversation_id,
                        requested_ready_at,
                        timezone_name=self.settings.store_timezone,
                        store_id=resolved_store_id,
                    )
                except ValueError as error:
                    schedule_error_text = str(error)
            delay = (
                await repository.get_estimated_delay(customer_id, conversation_id)
                if current_order is not None and current_order.items
                else None
            )
            reply = result.output
            if schedule_error_text is not None:
                reply = AssistantReply(
                    reply_text=schedule_error_text,
                    next_step=self._next_step_for_current_order(current_order),
                    handoff=False,
                )
            reply = self._decorate_reply_with_store_availability(
                reply,
                availability,
                conversation_id=conversation_id,
                latest_assistant_text=closed_store_context_text,
                current_order=current_order,
            )
            reply = self._ground_reply_for_turn_policy(reply, turn_policy, current_order=current_order)
            if schedule_error_text is None:
                reply = await self._answer_menu_information_turn(
                    reply,
                    repository=repository,
                    message_text=message_text,
                    previous_user_message=latest_user_text,
                    turn_policy=turn_policy,
                )
                reply, current_order = await self._recover_colloquial_menu_reply(
                    reply,
                    repository=repository,
                    message_text=message_text,
                    customer=refreshed_customer,
                    conversation_id=conversation_id,
                    current_order=current_order,
                    delay=delay,
                    store=store,
                    turn_policy=turn_policy,
                )
                reply = self._guide_reply_with_current_order(
                    reply,
                    customer=refreshed_customer,
                    current_order=current_order,
                    message_text=message_text,
                    delay=delay,
                    store=store,
                    order_changed_during_turn=self._order_changed_during_turn(
                        previous_order=current_order_before_run,
                        current_order=current_order,
                    ),
                    item_lines_changed_during_turn=self._order_items_changed_during_turn(
                        previous_order=current_order_before_run,
                        current_order=current_order,
                    ),
                )
                reply = self._finalize_confirmed_order_reply(
                    reply,
                    customer=refreshed_customer,
                    current_order=current_order,
                    delay=delay,
                    store=store,
                    just_confirmed=(
                        current_order is not None
                        and current_order.status.value == "confirmed"
                        and (latest_order_before_run is None or latest_order_before_run.status.value != "confirmed")
                    ),
                    availability=availability,
                    conversation_id=conversation_id,
                    latest_assistant_text=closed_store_context_text,
                )
                reply = self._stabilize_customization_reply(
                    reply,
                    message_text=message_text,
                    previous_user_message=latest_user_text,
                    current_order=current_order,
                )
                reply = self._decorate_reply_with_store_availability(
                    reply,
                    availability,
                    conversation_id=conversation_id,
                    latest_assistant_text=closed_store_context_text,
                    current_order=current_order,
                )
            delivery_info_request = self._message_requests_delivery_information(message_text)
            if delivery_info_request:
                reply = self._stabilize_delivery_information_reply(reply, message_text=message_text)
            if not turn_policy.allow_order_mutations and not (
                delivery_info_request and current_order is not None and current_order.items
            ):
                current_order = None
            await session.commit()
            return AssistantTurnResult(
                conversation_id=conversation_id,
                customer=refreshed_customer,
                reply=reply,
                current_order=current_order,
            )

    async def _maybe_handle_missing_customer_name(  # noqa: PLR0911, PLR0913
        self,
        *,
        repository: BusinessRepository,
        customer: CustomerSnapshot,
        conversation_id: int,
        history: list[ModelMessage],
        message_text: str,
        availability: StoreAvailabilitySnapshot,
        pending_customer_message: str | None,
        latest_assistant_text: str | None,
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
                        conversation_id=conversation_id,
                        latest_assistant_text=latest_assistant_text,
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
                    conversation_id=conversation_id,
                    latest_assistant_text=latest_assistant_text,
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

        if self._has_prior_conversation(history):
            return NameHandlingResult(customer=customer)

        if not self._should_require_name_before_continuing(message_text):
            return NameHandlingResult(customer=customer)

        if self._should_store_pending_message_before_name(message_text):
            await repository.set_pending_customer_message(conversation_id, message_text)
        reply = AssistantReply(
            reply_text=self._decorate_closed_store_text(
                self._build_name_prompt(
                    conversation_id=conversation_id,
                    remembers_pending_message=self._should_store_pending_message_before_name(message_text),
                ),
                availability,
                conversation_id=conversation_id,
                latest_assistant_text=latest_assistant_text,
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

    def _extract_latest_user_text(self, history: list[ModelMessage]) -> str | None:
        """Extract the latest customer prompt persisted in the conversation history."""
        for message in reversed(history):
            if not isinstance(message, ModelRequest):
                continue
            for part in reversed(message.parts):
                if isinstance(part, UserPromptPart):
                    return part.content if isinstance(part.content, str) else None
        return None

    def _has_prior_conversation(self, history: list[ModelMessage]) -> bool:
        """Return whether the customer is already past the very first turn."""
        return bool(history)

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

    def _should_require_name_before_continuing(self, message_text: str) -> bool:
        """Return whether the current turn needs the customer's name before proceeding."""
        lowered = message_text.casefold()
        if self._message_is_informational_menu_question(message_text):
            return False
        if self._message_requests_delivery_information(message_text):
            return False
        if any(hint in lowered for hint in ORDER_INTENT_HINTS):
            return True
        return any(keyword in lowered for keyword in ("hamburguesa", "pizza", "empanada", "lomito", "milanesa"))

    def _message_is_informational_menu_question(self, message_text: str) -> bool:
        """Return whether the customer is browsing or comparing menu items without ordering yet."""
        lowered = message_text.casefold()
        if "?" not in message_text and "¿" not in message_text:
            return False
        if any(hint in lowered for hint in ORDER_INTENT_HINTS):
            return False
        if self._message_requests_total(message_text):
            return True
        return any(
            keyword in lowered
            for keyword in (
                "hamburguesa",
                "pizza",
                "empanada",
                "lomito",
                "milanesa",
                "wrap",
                "ensalada",
                "gaseosa",
                "cerveza",
                "postre",
                "helado",
            )
        )

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
        asks_delivery_information = self._message_requests_delivery_information(message_text)
        explicit_confirmation = any(hint in lowered for hint in EXPLICIT_CONFIRMATION_HINTS)

        allow_order_mutations = has_order_intent or (not asks_menu_information and not asks_delivery_information)
        allow_order_confirmation = explicit_confirmation
        return TurnPolicy(
            allow_order_mutations=allow_order_mutations,
            allow_order_confirmation=allow_order_confirmation,
        )

    def _build_turn_context_hint(  # noqa: C901, PLR0912, PLR0913
        self,
        *,
        customer: CustomerSnapshot,
        message_text: str,
        current_order: OrderSnapshot | None,
        pending_customer_message: str | None = None,
        previous_user_message: str | None = None,
        latest_assistant_text: str | None = None,
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

        menu_information_focus = self._detect_menu_information_focus(
            message_text,
            previous_user_message=previous_user_message,
        )
        if menu_information_focus is not None:
            hints.append(
                "El cliente está consultando por "
                f"{menu_information_focus}. "
                "Usá el menú para responder con 2 a 4 opciones concretas y sus precios, no solo con un sí o un no."
            )
        if item_alias_sku := self._detect_colloquial_item_alias(message_text):
            hints.append(
                "El cliente usó un alias coloquial del menú. "
                f"Interpretalo como el SKU `{item_alias_sku}` y no respondas que no existe."
            )
        if category_alias := self._detect_colloquial_category_alias(message_text):
            hints.append(
                "El cliente usó una forma coloquial de pedir una categoría del menú. "
                f"Interpretala como `{category_alias}` y ofrecé opciones concretas."
            )
        if self._message_requests_unsupported_customization(message_text, previous_user_message):
            hints.append(
                "El cliente está pidiendo una personalización que no está modelada como variante del menú. "
                "No prometas que ya quedó aceptada si no la pudiste persistir explícitamente."
            )

        if self._message_requests_total(message_text):
            if menu_information_focus is not None:
                hints.append(
                    "Acá 'cuánto sale' se refiere a esa categoría o producto del menú, "
                    "no al total del pedido. Respondé con precios concretos o un rango útil."
                )
            else:
                hints.append(
                    "El cliente quiere saber cuánto sale en este mismo turno. "
                    "Si ya podés calcularlo con herramientas, informá el total o subtotal actual."
                )
        if self._message_requests_delivery_information(message_text):
            hints.append(
                "El cliente está haciendo una consulta informativa sobre envío o zona. "
                "Respondé eso sin empujarlo al próximo paso del checkout ni asumir que ya confirmó el pedido."
            )
        if self._contains_recent_closed_store_notice(latest_assistant_text):
            hints.append(
                "Ya avisaste hace poco que el local está cerrado. "
                "No repitas ese aviso salvo que sea necesario para explicar una restricción horaria."
            )
        if pending_customer_message is not None:
            hints.append(
                "Antes de identificarse, el cliente dejó una consulta pendiente: "
                f"'{pending_customer_message}'. "
                "Retomá eso ahora sin volver a preguntarle qué quiere."
            )
        if self._message_is_order_correction(message_text) and current_order is not None and current_order.items:
            hints.append(
                "El cliente está corrigiendo el pedido actual. "
                "No sumes líneas arriba de lo ya anotado por error: ajustá el borrador actual. "
                "Si hace falta, usá reset_current_order y cargá de nuevo las líneas correctas."
            )
        if (
            self._message_requests_split_across_recent_options(message_text)
            and latest_assistant_text is not None
            and any(
                hint in latest_assistant_text.casefold() for hint in ("tenemos:", "tenemos ", "opciones", "variantes")
            )
        ):
            if self._latest_options_are_ambiguous(latest_assistant_text):
                hints.append(
                    "En el turno anterior ofreciste variantes para más de un grupo de productos. "
                    "Si ahora responde 'uno y uno' o 'uno de cada', sigue siendo ambiguo: "
                    "pedí que aclare a cuál se refiere antes de tocar el pedido."
                )
            else:
                hints.append(
                    "En el turno anterior acabás de ofrecer variantes del mismo producto. "
                    "Si ahora responde 'uno y uno' o 'uno de cada', interpretalo como una "
                    "unidad de cada opción recién ofrecida."
                )
        hints.extend(self._build_current_order_context_hints(customer=customer, current_order=current_order))

        if not hints:
            return None
        return " ".join(hints)

    def _reply_denies_menu_match(self, reply_text: str) -> bool:
        """Return whether the assistant just claimed that no menu match was found."""
        return any(pattern.search(reply_text) for pattern in MENU_NOT_FOUND_REPLY_PATTERNS)

    def _message_requests_unsupported_customization(
        self,
        message_text: str,
        previous_user_message: str | None = None,
    ) -> bool:
        """Return whether the customer is asking for extras the current menu model cannot promise."""
        messages = [message_text]
        if previous_user_message is not None:
            messages.append(previous_user_message)

        return any(
            hint in self._normalize_menu_text(raw_message)
            for raw_message in messages
            for hint in UNSUPPORTED_CUSTOMIZATION_HINTS
        )

    def _reply_claims_customization_supported(self, reply_text: str) -> bool:
        """Return whether the assistant is claiming that a customization was already accepted."""
        return any(pattern.search(reply_text) for pattern in CUSTOMIZATION_ACCEPTANCE_PATTERNS)

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
        if current_order.requested_ready_at is not None:
            ready_text = self._describe_order_ready_time(current_order.requested_ready_at)
            hints.append(f"El cliente pidió tenerlo listo {ready_text}.")
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

    def _guide_reply_with_current_order(  # noqa: C901, PLR0911, PLR0913
        self,
        reply: AssistantReply,
        *,
        customer: CustomerSnapshot,
        current_order: OrderSnapshot | None,
        message_text: str,
        delay: DelayEstimateSnapshot | None,
        store: StoreProfileSnapshot,
        order_changed_during_turn: bool = True,
        item_lines_changed_during_turn: bool = True,
    ) -> AssistantReply:
        """Prefer deterministic checkout guidance once a draft already exists."""
        if current_order is None or not current_order.items or current_order.status.value != "draft":
            return reply
        if self._message_requests_delivery_information(message_text):
            if self._reply_is_checkout_prompt(reply):
                return reply.model_copy(
                    update={
                        "reply_text": self._build_delivery_information_reply(message_text),
                        "next_step": AssistantNextStep.CHOOSE_ITEMS,
                    }
                )
            return reply
        if not order_changed_during_turn and reply.next_step == AssistantNextStep.CHOOSE_ITEMS:
            return reply

        timing_text = self._build_timing_prompt_fragment(current_order=current_order, delay=delay)
        if item_lines_changed_during_turn and self._should_offer_add_on(current_order):
            item_summary = ", ".join(f"{item.quantity} x {item.name}" for item in current_order.items)
            template = self._pick_variant(ADD_ON_PROMPT_VARIANTS, seed=current_order.id)
            return reply.model_copy(
                update={
                    "reply_text": template.format(
                        lead=self._pick_lead_phrase(current_order.id),
                        name=customer.name or "che",
                        items=item_summary,
                        total=current_order.total_amount_display,
                        timing_text=timing_text,
                        suggestion=self._pick_add_on_suggestion(current_order),
                    ),
                    "next_step": AssistantNextStep.CHOOSE_ITEMS,
                }
            )

        if current_order.delivery_type is None:
            item_summary = ", ".join(f"{item.quantity} x {item.name}" for item in current_order.items)
            template = self._pick_variant(DELIVERY_PROMPT_VARIANTS, seed=current_order.id)
            return reply.model_copy(
                update={
                    "reply_text": template.format(
                        lead=self._pick_lead_phrase(current_order.id),
                        name=customer.name or "che",
                        items=item_summary,
                        total=current_order.total_amount_display,
                        timing_text=timing_text,
                    ),
                    "next_step": AssistantNextStep.CHOOSE_DELIVERY,
                }
            )

        if current_order.delivery_type == DeliveryType.DELIVERY and current_order.delivery_address is None:
            if customer.default_address:
                template = self._pick_variant(ADDRESS_PROMPT_VARIANTS, seed=current_order.id)
                reply_text = template.format(address=customer.default_address)
            else:
                reply_text = "Dale. Pasame la dirección de envío, por favor."
            return reply.model_copy(
                update={
                    "reply_text": reply_text,
                    "next_step": AssistantNextStep.ASK_ADDRESS,
                }
            )

        if current_order.payment_method is not None and order_changed_during_turn:
            return reply.model_copy(
                update={
                    "reply_text": self._build_order_review_reply(
                        customer=customer,
                        current_order=current_order,
                        store=store,
                    ),
                    "next_step": AssistantNextStep.CONFIRM_ORDER,
                }
            )

        if current_order.payment_method is None:
            template = self._pick_variant(PAYMENT_PROMPT_VARIANTS, seed=current_order.id)
            return reply.model_copy(
                update={
                    "reply_text": template.format(
                        lead=self._pick_lead_phrase(current_order.id + 1),
                        total=current_order.total_amount_display,
                        timing_text=timing_text,
                    ),
                    "next_step": AssistantNextStep.CHOOSE_PAYMENT,
                }
            )

        return reply

    def _order_changed_during_turn(
        self,
        *,
        previous_order: OrderSnapshot | None,
        current_order: OrderSnapshot | None,
    ) -> bool:
        """Return whether the order state materially changed during the latest turn."""
        if previous_order is None or current_order is None:
            return previous_order != current_order
        return previous_order.model_dump() != current_order.model_dump()

    def _order_items_changed_during_turn(
        self,
        *,
        previous_order: OrderSnapshot | None,
        current_order: OrderSnapshot | None,
    ) -> bool:
        """Return whether the latest turn changed the draft line items."""
        previous_items = [] if previous_order is None else previous_order.items
        current_items = [] if current_order is None else current_order.items
        return [item.model_dump() for item in previous_items] != [item.model_dump() for item in current_items]

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
                "cash",
            )
        ):
            return PaymentMethod.CASH
        return None

    def _detect_delivery_type_hint(self, message_text: str) -> DeliveryType | None:
        """Infer whether the customer just specified delivery or pickup."""
        lowered = message_text.casefold()
        if any(phrase in lowered for phrase in ("retiro", "retirar", "paso a buscar", "lo busco")):
            return DeliveryType.PICKUP
        if any(phrase in lowered for phrase in ("envío", "envio", "mandalo", "mandámelo", "mandamelo")):
            return DeliveryType.DELIVERY
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

    def _message_is_order_correction(self, message_text: str) -> bool:
        """Return whether the customer is correcting a previously captured order detail."""
        lowered = message_text.casefold()
        return any(hint in lowered for hint in ORDER_CORRECTION_HINTS)

    def _message_requests_split_across_recent_options(self, message_text: str) -> bool:
        """Return whether the customer is splitting quantity across recently offered variants."""
        lowered = message_text.casefold()
        return "uno y uno" in lowered or "uno de cada" in lowered

    def _message_is_generic_customization_follow_up(self, message_text: str) -> bool:
        """Return whether the customer is asking if a previously mentioned customization is feasible."""
        lowered = self._normalize_menu_text(message_text)
        return "eso se puede" in lowered or lowered.startswith("se puede")

    def _message_requests_delivery_information(self, message_text: str) -> bool:
        """Return whether the user is asking for delivery coverage or delivery pricing."""
        lowered = message_text.casefold()
        if "?" not in message_text and "¿" not in message_text:
            return False
        return any(
            phrase in lowered
            for phrase in (
                "hacen envíos",
                "hacen envios",
                "tenés para enviar",
                "tenes para enviar",
                "envío tiene costo",
                "envio tiene costo",
                "costo de envío",
                "costo de envio",
                "sale el envío",
                "sale el envio",
                "cobran envío",
                "cobran envio",
                "por qué zona",
                "por que zona",
                "qué zona",
                "que zona",
                "llegan",
                "alcance del envío",
                "alcance del envio",
                "hasta dónde",
                "hasta donde",
            )
        )

    def _detect_menu_information_constraints(
        self,
        message_text: str,
        *,
        previous_user_message: str | None = None,
    ) -> set[str]:
        """Infer simple browsing constraints from the latest menu-information turn."""
        messages = [message_text]
        if previous_user_message is not None:
            messages.append(previous_user_message)

        constraints: set[str] = set()
        for raw_message in messages:
            lowered = raw_message.casefold()
            for constraint, hints in MENU_CONSTRAINT_PATTERNS.items():
                if any(hint in lowered for hint in hints):
                    constraints.add(constraint)
        return constraints

    def _detect_menu_information_focus(
        self,
        message_text: str,
        *,
        previous_user_message: str | None = None,
    ) -> str | None:
        """Infer the menu category currently in focus for informational turns."""
        lowered = message_text.casefold()
        if focus := self._match_menu_information_focus(lowered):
            return focus
        if not self._message_requests_total(message_text):
            return None
        if previous_user_message is None:
            return None
        return self._match_menu_information_focus(previous_user_message.casefold())

    def _detect_colloquial_item_alias(self, message_text: str) -> str | None:
        """Return the canonical SKU for one supported colloquial menu alias."""
        lowered = self._normalize_menu_text(message_text)
        for alias, sku in COLLOQUIAL_MENU_ITEM_ALIASES.items():
            if alias in lowered:
                return sku
        return None

    def _detect_colloquial_category_alias(self, message_text: str) -> str | None:
        """Return the canonical menu-information focus for one colloquial category alias."""
        lowered = self._normalize_menu_text(message_text)
        for alias, focus in COLLOQUIAL_MENU_CATEGORY_ALIASES.items():
            if alias in lowered:
                return focus
        return None

    def _match_menu_information_focus(self, lowered_message: str) -> str | None:
        """Return the canonical menu-information focus mentioned in one message."""
        if focus := self._detect_colloquial_category_alias(lowered_message):
            return focus
        for focus, config in MENU_INFORMATION_GROUPS.items():
            if any(hint in lowered_message for hint in config["message_hints"]):
                return focus
        return None

    def _menu_item_matches_constraints(self, item: MenuItemSnapshot, *, constraints: set[str]) -> bool:
        """Return whether one menu item satisfies the requested browsing constraints."""
        return "non_alcoholic" not in constraints or not self._item_is_alcoholic(item.name)

    def _latest_options_are_ambiguous(self, latest_assistant_text: str) -> bool:
        """Return whether the latest options mixed multiple product families."""
        lowered = latest_assistant_text.casefold()
        mentioned_groups = {
            focus
            for focus, config in MENU_INFORMATION_GROUPS.items()
            if any(hint in lowered for hint in config["message_hints"])
        }
        if "lomo" in lowered or "lomito" in lowered:
            mentioned_groups.add("lomitos")
        return len(mentioned_groups) > 1

    def _looks_like_large_order_attempt(self, message_text: str) -> bool:
        """Return whether one message looks like a dense multi-item order."""
        lowered = self._normalize_menu_text(message_text)
        if not any(hint in lowered for hint in ORDER_INTENT_HINTS):
            return False
        segment_count = len([part for part in re.split(r",|\sy\s", lowered) if part.strip()])
        quantity_mentions = len(re.findall(r"\b\d+\b", lowered))
        return segment_count >= MIN_LARGE_ORDER_SEGMENTS or quantity_mentions >= MIN_LARGE_ORDER_QUANTITY_MENTIONS

    def _parse_menu_lines_from_message(
        self,
        message_text: str,
        *,
        menu_items: list[MenuItemSnapshot],
    ) -> list[tuple[MenuItemSnapshot, int]]:
        """Extract simple quantity + menu-item pairs from a dense order message."""
        normalized_message = self._normalize_menu_text(message_text)
        raw_segments = [segment.strip() for segment in re.split(r",|\sy\s", normalized_message) if segment.strip()]
        parsed_lines: list[tuple[MenuItemSnapshot, int]] = []
        seen_skus: set[str] = set()

        for segment in raw_segments:
            quantity, item = self._parse_menu_line_segment(segment, menu_items=menu_items)
            if item is None or item.sku in seen_skus:
                continue
            parsed_lines.append((item, quantity))
            seen_skus.add(item.sku)
        return parsed_lines

    def _parse_menu_line_segment(
        self,
        segment: str,
        *,
        menu_items: list[MenuItemSnapshot],
    ) -> tuple[int, MenuItemSnapshot | None]:
        """Parse one message segment into a quantity and the best menu match."""
        quantity = 1
        cleaned_segment = segment.strip(" .!?")
        for prefix in (
            "quiero ",
            "quisiera ",
            "dame ",
            "mandame ",
            "mandame ",
            "sumame ",
            "agregame ",
            "te pido ",
        ):
            if cleaned_segment.startswith(prefix):
                cleaned_segment = cleaned_segment[len(prefix) :].strip()
                break
        if quantity_match := re.match(r"(?P<qty>\d+)\s+", cleaned_segment):
            quantity = int(quantity_match.group("qty"))
            cleaned_segment = cleaned_segment[quantity_match.end() :].strip()

        segment_tokens = set(self._tokenize_menu_text(cleaned_segment))
        if not segment_tokens:
            return quantity, None

        best_item: MenuItemSnapshot | None = None
        best_score = 0.0
        for item in menu_items:
            aliases = self._menu_item_aliases(item)
            item_score = max(
                (self._alias_match_score(segment_tokens, alias_tokens) for alias_tokens in aliases),
                default=0.0,
            )
            if item_score > best_score:
                best_item = item
                best_score = item_score

        if best_score < MIN_ALIAS_MATCH_SCORE:
            return quantity, None
        return quantity, best_item

    def _menu_item_aliases(self, item: MenuItemSnapshot) -> list[set[str]]:
        """Build a few normalized aliases for deterministic menu matching."""
        normalized_name = self._normalize_menu_text(item.name)
        candidates = {normalized_name, self._normalize_menu_text(item.sku.replace("-", " "))}
        tokens = normalized_name.split()
        if len(tokens) > 1:
            candidates.add(" ".join(tokens[1:]))
        for source, replacements in COMMON_MENU_SYNONYMS.items():
            if source in normalized_name:
                for replacement in replacements:
                    candidates.add(normalized_name.replace(source, replacement))
        return [set(self._tokenize_menu_text(candidate)) for candidate in candidates if candidate]

    def _alias_match_score(self, segment_tokens: set[str], alias_tokens: set[str]) -> float:
        """Score one alias against one normalized message segment."""
        if not alias_tokens:
            return 0.0
        overlap = len(segment_tokens & alias_tokens)
        return overlap / len(alias_tokens)

    def _tokenize_menu_text(self, text: str) -> list[str]:
        """Split normalized text into fuzzy-match tokens."""
        raw_tokens = re.findall(r"[a-z0-9.]+", text)
        tokens: list[str] = []
        for token in raw_tokens:
            if token in {"de", "la", "las", "los", "con", "sin", "por", "para"}:
                continue
            singular = token[:-1] if token.endswith("s") and len(token) > MIN_PLURAL_TOKEN_LENGTH else token
            tokens.append(singular)
        return tokens

    def _normalize_menu_text(self, text: str) -> str:
        """Normalize accents and punctuation for deterministic menu parsing."""
        stripped = unicodedata.normalize("NFKD", text)
        ascii_text = "".join(char for char in stripped if not unicodedata.combining(char))
        return ascii_text.casefold()

    def _filter_menu_options_for_focus(
        self,
        menu_items: list[MenuItemSnapshot],
        *,
        focus: str,
        constraints: set[str] | None = None,
    ) -> list[MenuItemSnapshot]:
        """Return menu items matching one informational focus."""
        config = MENU_INFORMATION_GROUPS.get(focus)
        if config is None:
            return []
        name_matches: list[MenuItemSnapshot] = []
        category_matches: list[MenuItemSnapshot] = []
        for item in menu_items:
            lowered_name = item.name.casefold()
            lowered_category = item.category.casefold()
            if any(hint in lowered_name for hint in config["item_hints"]):
                name_matches.append(item)
                continue
            if any(hint in lowered_category for hint in config["category_hints"]):
                category_matches.append(item)
        matches = name_matches or category_matches
        if not constraints:
            return matches
        return [item for item in matches if self._menu_item_matches_constraints(item, constraints=constraints)]

    async def _answer_menu_information_turn(
        self,
        reply: AssistantReply,
        *,
        repository: BusinessRepository,
        message_text: str,
        previous_user_message: str | None,
        turn_policy: TurnPolicy,
    ) -> AssistantReply:
        """Replace vague informational answers with concrete menu options and prices."""
        if turn_policy.allow_order_mutations:
            return reply

        focus = self._detect_menu_information_focus(
            message_text,
            previous_user_message=previous_user_message,
        )
        if focus is None:
            return reply

        menu_items = await repository.list_menu_items()
        constraints = self._detect_menu_information_constraints(
            message_text,
            previous_user_message=previous_user_message,
        )
        matching_items = self._filter_menu_options_for_focus(
            menu_items,
            focus=focus,
            constraints=constraints,
        )[:4]
        if not matching_items:
            return reply

        focus_label = {
            "cervezas": "cervezas",
            "gaseosas": "gaseosas",
            "bebidas": "bebidas",
            "papas": "papas",
            "postres": "postres",
        }.get(focus, focus)
        options_block = "\n".join(f"- {item.name}: {item.price_display}" for item in matching_items)
        if self._message_requests_total(message_text):
            reply_text = (
                f"Sí, tenemos {focus_label}. Estas son algunas opciones:\n{options_block}\n\n¿Cuál te gustaría sumar?"
            )
        else:
            reply_text = (
                f"Sí, tenemos {focus_label}. Por ejemplo:\n{options_block}\n\nSi querés, te sumo una al pedido."
            )

        return reply.model_copy(
            update={
                "reply_text": reply_text,
                "next_step": AssistantNextStep.CHOOSE_ITEMS,
            }
        )

    async def _recover_colloquial_menu_reply(  # noqa: PLR0913
        self,
        reply: AssistantReply,
        *,
        repository: BusinessRepository,
        message_text: str,
        customer: CustomerSnapshot,
        conversation_id: int,
        current_order: OrderSnapshot | None,
        delay: DelayEstimateSnapshot | None,
        store: StoreProfileSnapshot,
        turn_policy: TurnPolicy,
    ) -> tuple[AssistantReply, OrderSnapshot | None]:
        """Recover useful outcomes when the model misses a common colloquial menu alias."""
        if not turn_policy.allow_order_mutations or not self._reply_denies_menu_match(reply.reply_text):
            return reply, current_order

        if (item_alias_sku := self._detect_colloquial_item_alias(message_text)) and (
            current_order is None or not current_order.items
        ):
            current_order = await repository.add_item_to_current_order(
                customer.id,
                conversation_id,
                sku=item_alias_sku,
                quantity=1,
            )
            delay = await repository.get_estimated_delay(customer.id, conversation_id)
            recovered_reply = self._guide_reply_with_current_order(
                AssistantReply(
                    reply_text="Anotado.",
                    next_step=AssistantNextStep.CHOOSE_ITEMS,
                    handoff=False,
                ),
                customer=customer,
                current_order=current_order,
                message_text=message_text,
                delay=delay,
                store=store,
                order_changed_during_turn=True,
                item_lines_changed_during_turn=True,
            )
            return recovered_reply, current_order

        if focus := self._detect_colloquial_category_alias(message_text):
            matching_items = self._filter_menu_options_for_focus(
                await repository.list_menu_items(),
                focus=focus,
            )[:3]
            if matching_items:
                options_block = "\n".join(f"- {item.name}: {item.price_display}" for item in matching_items)
                return (
                    reply.model_copy(
                        update={
                            "reply_text": (
                                f"Si querías una cerveza, tenemos estas opciones:\n{options_block}\n\n"
                                "¿Cuál te gustaría sumar?"
                            ),
                            "next_step": AssistantNextStep.CHOOSE_ITEMS,
                            "handoff": False,
                        }
                    ),
                    current_order,
                )

        return reply, current_order

    def _stabilize_customization_reply(
        self,
        reply: AssistantReply,
        *,
        message_text: str,
        previous_user_message: str | None,
        current_order: OrderSnapshot | None,
    ) -> AssistantReply:
        """Avoid promising unsupported customizations that were not persisted in the draft."""
        if current_order is None or not current_order.items:
            return reply
        if any(item.notes for item in current_order.items):
            return reply
        if not self._message_requests_unsupported_customization(message_text, previous_user_message):
            return reply
        if not (
            self._reply_claims_customization_supported(reply.reply_text)
            or (
                self._message_is_generic_customization_follow_up(message_text) and self._reply_is_checkout_prompt(reply)
            )
        ):
            return reply

        base_item_name = current_order.items[-1].name
        return reply.model_copy(
            update={
                "reply_text": (
                    f"Puedo dejarte {base_item_name} tal como figura en el menú, "
                    "pero desde acá no tengo cargadas variantes como doble picante o triple cheddar "
                    "para prometerlas automáticamente. Si querés, sigo con la versión estándar o te derivo "
                    "con una persona para confirmarlo."
                ),
                "next_step": AssistantNextStep.CHOOSE_ITEMS,
                "handoff": False,
            }
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
        *,
        conversation_id: int,
        latest_assistant_text: str | None,
        current_order: OrderSnapshot | None,
    ) -> AssistantReply:
        """Prefix replies when the store is currently closed."""
        cleaned_text = self._strip_redundant_closed_store_notice(
            reply.reply_text,
            latest_assistant_text=latest_assistant_text,
        )
        if cleaned_text != reply.reply_text:
            reply = reply.model_copy(update={"reply_text": cleaned_text})
        if availability.is_open:
            return reply
        if current_order is not None and (
            current_order.requested_ready_at is not None or current_order.status.value == "confirmed"
        ):
            return reply
        if latest_assistant_text is not None:
            return reply
        return reply.model_copy(
            update={
                "reply_text": self._decorate_closed_store_text(
                    reply.reply_text,
                    availability,
                    conversation_id=conversation_id,
                    latest_assistant_text=latest_assistant_text,
                ),
            }
        )

    def _decorate_closed_store_text(
        self,
        reply_text: str,
        availability: StoreAvailabilitySnapshot,
        *,
        conversation_id: int = 0,
        latest_assistant_text: str | None = None,
    ) -> str:
        """Prefix a reply with the store-closed notice when needed."""
        if availability.is_open:
            return reply_text
        if self._contains_recent_closed_store_notice(reply_text):
            return reply_text
        if self._contains_recent_closed_store_notice(latest_assistant_text):
            return reply_text
        message_text = self._build_closed_store_notice(availability, conversation_id=conversation_id)
        if reply_text.startswith(message_text):
            return reply_text
        return f"{message_text}{reply_text}"

    def _strip_redundant_closed_store_notice(
        self,
        reply_text: str,
        *,
        latest_assistant_text: str | None,
    ) -> str:
        """Remove one leading closed-store notice when the conversation already received it recently."""
        if latest_assistant_text is None:
            return reply_text
        stripped = reply_text.strip()
        if not self._contains_recent_closed_store_notice(stripped):
            return reply_text

        if match := re.match(
            r"^(?:Ahora|Justo ahora|En este momento)[^.?!]*abrimos[^.?!]*[.?!]\s*",
            stripped,
            re.IGNORECASE,
        ):
            remainder = stripped[match.end() :].lstrip()
            return remainder or reply_text

        sentences = re.split(r"(?<=[.!?])\s+", stripped, maxsplit=1)
        if len(sentences) > 1 and self._contains_recent_closed_store_notice(sentences[0]):
            return sentences[1]
        return reply_text

    def _finalize_confirmed_order_reply(  # noqa: PLR0913
        self,
        reply: AssistantReply,
        *,
        customer: CustomerSnapshot,
        current_order: OrderSnapshot | None,
        delay: DelayEstimateSnapshot | None,
        store: StoreProfileSnapshot,
        just_confirmed: bool,
        availability: StoreAvailabilitySnapshot,
        conversation_id: int,
        latest_assistant_text: str | None,
    ) -> AssistantReply:
        """Replace free-form model confirmations with a consistent final summary."""
        if current_order is None or current_order.status.value != "confirmed" or not just_confirmed:
            return reply

        item_lines = "\n".join(f"- {item.quantity} x {item.name}" for item in current_order.items)
        delivery_text = (
            f"Envío a {current_order.delivery_address}"
            if current_order.delivery_type == DeliveryType.DELIVERY
            else "Retiro por el local"
        )
        assert current_order.payment_method is not None
        payment_text = {
            PaymentMethod.CASH: "Efectivo",
            PaymentMethod.CARD_LINK: "Link de pago",
            PaymentMethod.TRANSFER: "Transferencia",
        }[current_order.payment_method]

        schedule_block = ""
        if current_order.requested_ready_at is not None:
            ready_text = self._describe_order_ready_time(current_order.requested_ready_at)
            schedule_block = f"\n**Horario**\nPedido programado para {ready_text}."
            if current_order.preparation_starts_at is not None:
                start_text = self._format_local_time(current_order.preparation_starts_at)
                schedule_block += f"\nEmpezamos a prepararlo cerca de las {start_text}."

        payment_block = f"\n**Pago**\n{payment_text}"
        if current_order.payment_method is PaymentMethod.TRANSFER and store.transfer_alias:
            payment_block += f" a `{store.transfer_alias}`"
        payment_block += "."

        reply_text = (
            f"Listo, {customer.name or 'te confirmo'}.\n\n"
            f"**Pedido**\n{item_lines}\n\n"
            f"**Entrega**\n{delivery_text}\n"
            f"{schedule_block}\n"
            f"{payment_block}\n\n"
            f"**Total**\n{current_order.total_amount_display}"
        )
        if not availability.is_open and current_order.requested_ready_at is None:
            reply_text = self._decorate_closed_store_text(
                reply_text,
                availability,
                conversation_id=conversation_id,
                latest_assistant_text=latest_assistant_text,
            )

        return reply.model_copy(
            update={
                "reply_text": reply_text,
                "next_step": AssistantNextStep.COMPLETE,
            }
        )

    def _next_step_for_current_order(self, current_order: OrderSnapshot | None) -> AssistantNextStep:
        """Infer the next deterministic checkout step from the current order state."""
        if current_order is None or not current_order.items:
            return AssistantNextStep.CHOOSE_ITEMS
        if current_order.delivery_type is None:
            return AssistantNextStep.CHOOSE_DELIVERY
        if current_order.delivery_type == DeliveryType.DELIVERY and current_order.delivery_address is None:
            return AssistantNextStep.ASK_ADDRESS
        if current_order.payment_method is None:
            return AssistantNextStep.CHOOSE_PAYMENT
        if current_order.status.value == "confirmed":
            return AssistantNextStep.COMPLETE
        return AssistantNextStep.CONFIRM_ORDER

    def _reply_is_checkout_prompt(self, reply: AssistantReply) -> bool:
        """Return whether the reply is steering the customer through checkout steps."""
        if reply.next_step in {
            AssistantNextStep.CHOOSE_DELIVERY,
            AssistantNextStep.ASK_ADDRESS,
            AssistantNextStep.CHOOSE_PAYMENT,
            AssistantNextStep.CONFIRM_ORDER,
            AssistantNextStep.COMPLETE,
        }:
            return True
        lowered = reply.reply_text.casefold()
        return any(
            phrase in lowered
            for phrase in (
                "envío o retirás",
                "pasame la dirección",
                "preferís efectivo",
                "link de pago",
                "confirmamos el pedido",
            )
        )

    def _build_delivery_information_reply(self, message_text: str) -> str:
        """Build a safe fallback for delivery-fee or coverage questions."""
        lowered = message_text.casefold()
        if any(phrase in lowered for phrase in ("costo", "sale", "cobran")):
            return (
                "Hacemos envíos, pero desde acá no tengo cargado un costo fijo para decírtelo automáticamente. "
                "Si querés, pasame la dirección o la zona y lo revisamos antes de cerrar el pedido."
            )
        return (
            "Hacemos envíos. Para decirte si llegamos a tu zona necesito la dirección "
            "o al menos la referencia del barrio. "
            "Si me la pasás, lo revisamos antes de cerrar el pedido."
        )

    def _stabilize_delivery_information_reply(
        self,
        reply: AssistantReply,
        *,
        message_text: str,
    ) -> AssistantReply:
        """Keep delivery-info questions in a helpful informational mode."""
        if not reply.handoff and not self._reply_is_checkout_prompt(reply):
            return reply
        return reply.model_copy(
            update={
                "reply_text": self._build_delivery_information_reply(message_text),
                "next_step": AssistantNextStep.CHOOSE_ITEMS,
                "handoff": False,
            }
        )

    def _build_order_review_reply(
        self,
        *,
        customer: CustomerSnapshot,
        current_order: OrderSnapshot,
        store: StoreProfileSnapshot,
    ) -> str:
        """Render a draft review using the persisted order state before explicit confirmation."""
        item_lines = "\n".join(f"- {item.quantity} x {item.name}" for item in current_order.items)
        delivery_text = (
            f"Envío a {current_order.delivery_address}"
            if current_order.delivery_type == DeliveryType.DELIVERY
            else "Retiro por el local"
        )
        assert current_order.payment_method is not None
        payment_text = {
            PaymentMethod.CASH: "Efectivo",
            PaymentMethod.CARD_LINK: "Link de pago",
            PaymentMethod.TRANSFER: "Transferencia",
        }[current_order.payment_method]
        if current_order.payment_method is PaymentMethod.TRANSFER and store.transfer_alias:
            payment_text = f"{payment_text} a `{store.transfer_alias}`"
        schedule_block = ""
        if current_order.requested_ready_at is not None:
            schedule_block = (
                "\n**Horario**\n"
                f"Pedido programado para {self._describe_order_ready_time(current_order.requested_ready_at)}."
            )
        return (
            f"Así queda, {customer.name or 'che'}:\n\n"
            f"**Pedido**\n{item_lines}\n\n"
            f"**Entrega**\n{delivery_text}\n"
            f"{schedule_block}\n"
            f"\n**Pago**\n{payment_text}.\n\n"
            f"**Total**\n{current_order.total_amount_display}\n\n"
            "Si está bien así, confirmámelo y lo cierro."
        )

    def _build_timing_prompt_fragment(
        self,
        *,
        current_order: OrderSnapshot,
        delay: DelayEstimateSnapshot | None,
    ) -> str:
        """Describe either a scheduled ready time or the current estimated delay."""
        if current_order.requested_ready_at is not None:
            return f" Lo dejo programado para {self._describe_order_ready_time(current_order.requested_ready_at)}."
        return ""

    def _should_offer_add_on(self, current_order: OrderSnapshot) -> bool:
        """Return whether the current draft should nudge one simple add-on."""
        if not current_order.items:
            return False

        item_names = [item.name.casefold() for item in current_order.items]
        has_main = any(self._item_is_main(name) for name in item_names)
        has_beverage = any(self._item_is_beverage(name) for name in item_names)
        has_dessert = any(self._item_is_dessert(name) for name in item_names)
        return has_main and (not has_beverage or not has_dessert)

    def _pick_add_on_suggestion(self, current_order: OrderSnapshot) -> str:
        """Choose a simple complement suggestion based on the current draft."""
        item_names = [item.name.casefold() for item in current_order.items]
        lowered_names = " ".join(item_names)
        has_side = any(self._item_is_side(name) for name in item_names)
        has_beverage = any(self._item_is_beverage(name) for name in item_names)
        has_dessert = any(self._item_is_dessert(name) for name in item_names)

        if not has_beverage and not has_dessert:
            if not has_side and any(
                keyword in lowered_names for keyword in ("hamburguesa", "lomito", "milanesa", "wrap")
            ):
                return "unas papas, una bebida o un postre"
            return "una bebida o un postre"
        if not has_beverage:
            return "una bebida"
        if not has_dessert:
            return "un postre"
        return "algo más para acompañar"

    def _item_is_main(self, item_name: str) -> bool:
        """Return whether one line item should count as a main dish."""
        return any(
            keyword in item_name
            for keyword in (
                "hamburguesa",
                "lomito",
                "milanesa",
                "wrap",
                "pizza",
                "empanada",
                "sanguche",
                "ensalada",
            )
        )

    def _item_is_side(self, item_name: str) -> bool:
        """Return whether one line item is primarily a side dish."""
        return "papas" in item_name

    def _item_is_beverage(self, item_name: str) -> bool:
        """Return whether one line item is a beverage."""
        lowered = item_name.casefold()
        return any(keyword in lowered for keyword in ("gaseosa", "agua", "cerveza", "saborizada"))

    def _item_is_alcoholic(self, item_name: str) -> bool:
        """Return whether one menu item is alcoholic."""
        lowered = item_name.casefold()
        return any(keyword in lowered for keyword in ("cerveza", "ipa", "rubia", "roja"))

    def _item_is_dessert(self, item_name: str) -> bool:
        """Return whether one line item is a dessert."""
        return any(keyword in item_name for keyword in ("budín", "brownie", "flan", "helado", "tiramisú", "cheesecake"))

    def _extract_requested_ready_at(self, message_text: str, *, timezone_name: str) -> datetime | None:
        """Parse a customer request such as `para las 12` into a timezone-aware datetime."""
        normalized = " ".join(message_text.split())
        if not normalized:
            return None

        match = None
        for pattern in REQUESTED_READY_TIME_PATTERNS:
            if candidate := pattern.search(normalized):
                match = candidate
                break
        if match is None:
            return None

        hour = int(match.group("hour"))
        minute_text = match.groupdict().get("minute")
        minute = int(minute_text) if minute_text is not None else 0
        if hour > MAX_SCHEDULE_HOUR or minute > MAX_SCHEDULE_MINUTE:
            return None

        zone = ZoneInfo(timezone_name)
        local_now = datetime.now(zone).replace(second=0, microsecond=0)
        local_target = local_now.replace(hour=hour, minute=minute)
        mentions_tomorrow = "mañana" in normalized.casefold() or "manana" in normalized.casefold()
        if mentions_tomorrow or local_target <= local_now:
            local_target = local_target + timedelta(days=1)
        return local_target.astimezone(UTC)

    def _describe_order_ready_time(self, value: datetime) -> str:
        """Describe a UTC timestamp using today, tomorrow, or weekday in store time."""
        zone = ZoneInfo(self.settings.store_timezone)
        local_now = datetime.now(zone)
        local_value = value.astimezone(zone)
        if local_value.date() == local_now.date():
            day_text = "hoy"
        elif local_value.date() == (local_now + timedelta(days=1)).date():
            day_text = "mañana"
        else:
            day_text = f"el {WEEKDAY_LABELS_ES[local_value.weekday()]}"
        return f"{day_text} a las {local_value.strftime('%H:%M')}"

    def _format_local_time(self, value: datetime) -> str:
        """Format a UTC timestamp as a local `HH:MM` string."""
        return value.astimezone(ZoneInfo(self.settings.store_timezone)).strftime("%H:%M")

    def _build_closed_store_notice(self, availability: StoreAvailabilitySnapshot, *, conversation_id: int) -> str:
        """Select a slightly different closed-store notice for each conversation."""
        next_open_text = availability.next_open_text or "pronto"
        template = self._pick_variant(CLOSED_STORE_NOTICE_VARIANTS, seed=conversation_id)
        return template.format(next_open_text=next_open_text)

    def _contains_recent_closed_store_notice(self, latest_assistant_text: str | None) -> bool:
        """Avoid repeating the same closed-store notice in consecutive turns."""
        if latest_assistant_text is None:
            return False
        lowered = latest_assistant_text.casefold()
        return "cerrad" in lowered and "abrimos" in lowered

    def _pick_variant(self, variants: tuple[str, ...], *, seed: int) -> str:
        """Choose one deterministic variant without adding randomness to tests."""
        return variants[seed % len(variants)]

    def _pick_lead_phrase(self, seed: int) -> str:
        """Return a short natural lead-in that avoids repeating a single filler."""
        return ("Dale", "Perfecto", "Buenísimo")[seed % 3]
