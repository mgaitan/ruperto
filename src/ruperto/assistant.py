"""Transactional ordering assistant built with PydanticAI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ruperto.config import Settings
from ruperto.models import Channel, DeliveryType, PaymentMethod
from ruperto.repository import BusinessRepository
from ruperto.schemas import (
    AssistantReply,
    AssistantTurnResult,
    CustomerMemorySnapshot,
    CustomerSnapshot,
    DelayEstimateSnapshot,
    MenuItemSnapshot,
    OrderSnapshot,
)

BASE_INSTRUCTIONS = """
Sos el asistente virtual de un local de comida en Argentina.

Reglas operativas:
- Respondés siempre en español de Argentina, con tono amable, claro y breve.
- No inventes productos, precios, stock, direcciones, tiempos ni pagos.
- Para cualquier dato del negocio tenés que usar herramientas.
- Hace una sola pregunta por vez cuando falten datos.
- Prioriza cerrar el pedido con la menor fricción posible.
- Si el cliente ya es conocido y hay una memoria útil, podés mencionarla con naturalidad.
- Si preguntan por demora o tiempo estimado, usá la herramienta de demora disponible.
- Si el cliente pide algo fuera del alcance del bot, deriva a una persona.
""".strip()


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
            deps = AssistantDeps(
                settings=self.settings,
                session_factory=self.session_factory,
                customer_id=customer.id,
                conversation_id=conversation.id,
            )
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
            await session.commit()
            return AssistantTurnResult(
                conversation_id=conversation_id,
                customer=refreshed_customer,
                reply=result.output,
                current_order=current_order,
            )
