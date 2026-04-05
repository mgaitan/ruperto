"""Municipal vertical scaffolding built on top of the shared conversation core."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ruperto.config import Settings
from ruperto.models import Channel
from ruperto.repository import BusinessRepository
from ruperto.schemas import AssistantNextStep, AssistantReply, AssistantTurnResult


class MunicipalAssistantService:
    """Minimal municipal assistant used while the municipal domain is being built."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings

    async def handle_customer_message(
        self,
        *,
        channel: Channel,
        external_user_id: str,
        message_text: str,
        store_id: int | None = None,
    ) -> AssistantTurnResult:
        """Persist the conversation identity and answer with a municipal placeholder."""
        resolved_store_id = store_id or self.settings.default_store_id
        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=resolved_store_id)
            customer = await repository.get_or_create_customer(
                channel=channel,
                external_id=external_user_id,
                phone_number=external_user_id if channel == Channel.WHATSAPP else None,
            )
            conversation = await repository.get_or_create_conversation(
                channel=channel,
                external_id=external_user_id,
                customer_id=customer.id,
                store_id=resolved_store_id,
            )
            await session.commit()

        reply_text = (
            f"Hola, soy el asistente municipal de {store.store_name}. "
            "La entrada multicanal ya está preparada para este vertical, "
            "pero la toma de reclamos y solicitudes todavía está en construcción. "
            "En el próximo paso vamos a habilitar áreas, categorías y seguimiento de casos."
        )
        return AssistantTurnResult(
            conversation_id=conversation.id,
            customer=customer,
            reply=AssistantReply(
                reply_text=reply_text,
                next_step=AssistantNextStep.HANDOFF,
                handoff=True,
            ),
            current_order=None,
        )
