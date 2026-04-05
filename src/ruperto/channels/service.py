"""Channel orchestration helpers shared by API routes and background flows."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ruperto.assistant import OrderingAssistantService
from ruperto.channels.base import InboundCustomerMessage, OutboundCustomerMessage
from ruperto.channels.kapso_whatsapp import KapsoWhatsAppGateway
from ruperto.config import Settings
from ruperto.models import Channel
from ruperto.repository import BusinessRepository
from ruperto.schemas import AssistantTurnResult

SessionFactory = async_sessionmaker[AsyncSession]


def build_channel_gateway(*, channel: Channel, settings: Settings) -> KapsoWhatsAppGateway | None:
    """Return the configured gateway for the requested channel when available."""
    if channel == Channel.WHATSAPP:
        return KapsoWhatsAppGateway.from_settings(settings)
    return None


async def seed_customer_name_from_inbound_message(
    *,
    session_factory: SessionFactory,
    inbound_message: InboundCustomerMessage,
) -> None:
    """Persist a channel-provided customer name when the customer is still unnamed."""
    if not inbound_message.sender_name:
        return
    async with session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=inbound_message.channel,
            external_id=inbound_message.external_user_id,
            phone_number=inbound_message.external_user_id if inbound_message.channel == Channel.WHATSAPP else None,
        )
        if customer.name:
            return
        await repository.update_customer_name(customer.id, inbound_message.sender_name)
        await session.commit()


async def handle_inbound_customer_message(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    inbound_message: InboundCustomerMessage,
) -> AssistantTurnResult:
    """Process one inbound text message through the ordering assistant."""
    await seed_customer_name_from_inbound_message(
        session_factory=session_factory,
        inbound_message=inbound_message,
    )
    service = OrderingAssistantService(session_factory=session_factory, settings=settings)
    return await service.handle_customer_message(
        channel=inbound_message.channel,
        external_user_id=inbound_message.external_user_id,
        message_text=inbound_message.message_text,
    )


async def deliver_pending_notifications(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    channel: Channel,
    external_user_id: str,
) -> int:
    """Deliver queued notifications for one channel identity."""
    gateway = build_channel_gateway(channel=channel, settings=settings)
    if gateway is None:
        return 0

    async with session_factory() as session:
        repository = BusinessRepository(session)
        notifications = await repository.peek_pending_notifications(channel=channel, external_id=external_user_id)

    if not notifications:
        return 0

    delivered_ids: list[int] = []
    for notification in notifications:
        await gateway.send_text(
            OutboundCustomerMessage(
                channel=channel,
                external_user_id=external_user_id,
                message_text=notification.message_text,
            )
        )
        delivered_ids.append(notification.id)

    async with session_factory() as session:
        repository = BusinessRepository(session)
        await repository.mark_notifications_delivered(delivered_ids)
        await session.commit()

    return len(delivered_ids)


async def deliver_order_notifications(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    order_id: int,
) -> int:
    """Deliver queued notifications for the conversation attached to one order."""
    async with session_factory() as session:
        repository = BusinessRepository(session)
        target = await repository.get_order_conversation_target(order_id)

    if target is None:
        return 0

    return await deliver_pending_notifications(
        session_factory=session_factory,
        settings=settings,
        channel=target.channel,
        external_user_id=target.external_id,
    )
