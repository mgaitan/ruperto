"""Assistant selection helpers based on the active tenant vertical."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ruperto.assistant import OrderingAssistantService
from ruperto.config import Settings
from ruperto.models import Channel, StoreVertical
from ruperto.municipal import MunicipalAssistantService
from ruperto.repository import BusinessRepository
from ruperto.schemas import AssistantTurnResult


async def handle_customer_message_for_store(  # noqa: PLR0913
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    channel: Channel,
    external_user_id: str,
    message_text: str,
    store_id: int | None = None,
) -> AssistantTurnResult:
    """Route one customer turn to the assistant configured for the active tenant."""
    resolved_store_id = store_id or settings.default_store_id
    async with session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile(store_id=resolved_store_id)

    if store.vertical == StoreVertical.MUNICIPAL:
        service = MunicipalAssistantService(session_factory=session_factory, settings=settings)
    else:
        service = OrderingAssistantService(session_factory=session_factory, settings=settings)

    return await service.handle_customer_message(
        channel=channel,
        external_user_id=external_user_id,
        message_text=message_text,
        store_id=resolved_store_id,
    )
