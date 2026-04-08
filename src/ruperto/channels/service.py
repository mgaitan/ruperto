"""Channel orchestration helpers shared by API routes and background flows."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ruperto.assistant_router import handle_customer_message_for_store
from ruperto.channels.base import InboundCustomerMessage, OutboundCustomerMessage
from ruperto.channels.kapso_whatsapp import KapsoWhatsAppGateway
from ruperto.config import Settings
from ruperto.models import Channel, ChannelProvider
from ruperto.repository import BusinessRepository
from ruperto.schemas import AssistantTurnResult, StoreChannelConnectionRuntimeConfig

SessionFactory = async_sessionmaker[AsyncSession]


def build_channel_gateway(*, channel: Channel, settings: Settings) -> KapsoWhatsAppGateway | None:
    """Return the configured gateway for the requested channel when available."""
    if channel == Channel.WHATSAPP:
        return KapsoWhatsAppGateway.from_settings(settings)
    return None


def build_store_channel_gateway_from_runtime_config(
    *,
    channel_config: StoreChannelConnectionRuntimeConfig,
) -> KapsoWhatsAppGateway | None:
    """Build one concrete gateway from a store-scoped runtime config snapshot."""
    if channel_config.channel != Channel.WHATSAPP or channel_config.provider != ChannelProvider.KAPSO:
        return None
    return KapsoWhatsAppGateway(
        kapso_api_key=channel_config.api_key,
        phone_number_id=channel_config.phone_number_id,
        webhook_secret=channel_config.webhook_secret,
    )


async def build_store_channel_gateway(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    store_id: int,
    channel: Channel,
) -> KapsoWhatsAppGateway | None:
    """Resolve one active store-scoped gateway, falling back to app settings when needed."""
    async with session_factory() as session:
        repository = BusinessRepository(session)
        channel_config = await repository.get_store_channel_runtime_config(
            store_id=store_id,
            channel=channel,
        )
    if channel_config is not None:
        return build_store_channel_gateway_from_runtime_config(channel_config=channel_config)
    return build_channel_gateway(channel=channel, settings=settings)


async def build_whatsapp_gateway_for_phone_number(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    phone_number_id: str | None,
) -> tuple[KapsoWhatsAppGateway | None, int | None]:
    """Resolve the Kapso WhatsApp gateway for one inbound phone number."""
    if phone_number_id:
        async with session_factory() as session:
            repository = BusinessRepository(session)
            channel_config = await repository.get_channel_runtime_config_by_phone_number(
                channel=Channel.WHATSAPP,
                provider=ChannelProvider.KAPSO,
                phone_number_id=phone_number_id,
            )
        if channel_config is not None:
            gateway = build_store_channel_gateway_from_runtime_config(channel_config=channel_config)
            return gateway, channel_config.store_id

    fallback_gateway = build_channel_gateway(channel=Channel.WHATSAPP, settings=settings)
    if fallback_gateway is None:
        return None, None
    if phone_number_id and settings.kapso_phone_number_id != phone_number_id:
        return None, None
    return fallback_gateway, settings.default_store_id


async def seed_customer_name_from_inbound_message(
    *,
    session_factory: SessionFactory,
    inbound_message: InboundCustomerMessage,
    store_id: int,
) -> None:
    """Persist a channel-provided customer name when the customer is still unnamed."""
    if not inbound_message.sender_name:
        return
    async with session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=inbound_message.channel,
            external_id=inbound_message.external_user_id,
            store_id=store_id,
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
    store_id: int | None = None,
) -> AssistantTurnResult:
    """Process one inbound text message through the tenant-selected assistant."""
    resolved_store_id = store_id or settings.default_store_id
    await seed_customer_name_from_inbound_message(
        session_factory=session_factory,
        inbound_message=inbound_message,
        store_id=resolved_store_id,
    )
    return await handle_customer_message_for_store(
        session_factory=session_factory,
        settings=settings,
        channel=inbound_message.channel,
        external_user_id=inbound_message.external_user_id,
        message_text=inbound_message.message_text,
        store_id=resolved_store_id,
    )


async def deliver_pending_notifications(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    store_id: int,
    channel: Channel,
    external_user_id: str,
) -> int:
    """Deliver queued notifications for one channel identity."""
    gateway = await build_store_channel_gateway(
        session_factory=session_factory,
        settings=settings,
        store_id=store_id,
        channel=channel,
    )
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
        store_id=target.store_id,
        channel=target.channel,
        external_user_id=target.external_id,
    )


async def deliver_municipal_case_notifications(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    case_id: int,
) -> int:
    """Deliver queued notifications for the conversation attached to one municipal case."""
    async with session_factory() as session:
        repository = BusinessRepository(session)
        target = await repository.get_municipal_case_conversation_target(case_id)

    if target is None:
        return 0

    return await deliver_pending_notifications(
        session_factory=session_factory,
        settings=settings,
        store_id=target.store_id,
        channel=target.channel,
        external_user_id=target.external_id,
    )
