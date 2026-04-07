"""Tests for channel orchestration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from ruperto.channels.base import InboundCustomerMessage
from ruperto.channels.service import (
    build_channel_gateway,
    build_store_channel_gateway_from_runtime_config,
    build_whatsapp_gateway_for_phone_number,
    deliver_order_notifications,
    deliver_pending_notifications,
    seed_customer_name_from_inbound_message,
)
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import Channel, ChannelProvider
from ruperto.repository import BusinessRepository
from ruperto.schemas import StoreChannelConnectionUpdateRequest

pytestmark = pytest.mark.anyio


def build_settings(tmp_path: Path) -> Settings:
    """Create isolated settings for channel-service tests."""
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'channels.db'}",
        auto_init_db=True,
        dashboard_session_secret="test-session-secret",
        kapso_api_key=SecretStr("kapso-key"),
        kapso_phone_number_id="597907523413541",
        kapso_webhook_secret=SecretStr("kapso-secret"),
    )


async def test_build_channel_gateway_returns_none_when_kapso_is_not_configured(tmp_path: Path):
    """The gateway builder leaves unsupported or unconfigured channels disabled."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'channels.db'}",
        auto_init_db=True,
    )

    assert build_channel_gateway(channel=Channel.DEV, settings=settings) is None
    assert build_channel_gateway(channel=Channel.WHATSAPP, settings=settings) is None


async def test_seeded_sender_name_is_persisted_before_whatsapp_turn(tmp_path: Path):
    """Channel metadata can provide the initial customer name for WhatsApp."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    inbound_message = InboundCustomerMessage(
        channel=Channel.WHATSAPP,
        external_user_id="+5493513308454",
        message_text="Hola",
        sender_name="Pedro",
    )

    await seed_customer_name_from_inbound_message(
        session_factory=runtime.session_factory,
        inbound_message=inbound_message,
        store_id=settings.default_store_id,
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            phone_number="+5493513308454",
        )

    await runtime.engine.dispose()

    assert customer.name == "Pedro"


async def test_seeded_sender_name_does_not_override_existing_customer_name(tmp_path: Path):
    """Channel-provided names are ignored once the customer already has one."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            phone_number="+5493513308454",
        )
        await repository.update_customer_name(customer.id, "Martín")
        await session.commit()

    inbound_message = InboundCustomerMessage(
        channel=Channel.WHATSAPP,
        external_user_id="+5493513308454",
        message_text="Hola",
        sender_name="Pedro",
    )

    await seed_customer_name_from_inbound_message(
        session_factory=runtime.session_factory,
        inbound_message=inbound_message,
        store_id=settings.default_store_id,
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            phone_number="+5493513308454",
        )

    await runtime.engine.dispose()

    assert customer.name == "Martín"


async def test_deliver_pending_notifications_returns_zero_without_pending_rows(tmp_path: Path):
    """Notification delivery exits cleanly when there is nothing to send."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    delivered = await deliver_pending_notifications(
        session_factory=runtime.session_factory,
        settings=settings,
        store_id=settings.default_store_id,
        channel=Channel.WHATSAPP,
        external_user_id="+5493513308454",
    )

    await runtime.engine.dispose()

    assert delivered == 0


async def test_deliver_order_notifications_returns_zero_when_order_has_no_conversation(tmp_path: Path):
    """Order-level notification dispatch does nothing without a linked conversation."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.DEV,
            external_id="cliente-demo",
        )
        order = await repository.set_order_notify_when_ready(customer.id, conversation_id=9999, enabled=True)
        await session.commit()

    delivered = await deliver_order_notifications(
        session_factory=runtime.session_factory,
        settings=settings,
        order_id=order.id,
    )

    await runtime.engine.dispose()

    assert delivered == 0


async def test_repository_get_order_and_conversation_target_cover_missing_paths(tmp_path: Path):
    """Repository helpers return the expected null or error values on missing orders."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        with pytest.raises(ValueError):
            await repository.get_order(9999)
        assert await repository.get_order_conversation_target(9999) is None

    await runtime.engine.dispose()


async def test_build_whatsapp_gateway_for_phone_number_prefers_store_connection(tmp_path: Path):
    """Inbound Kapso routing can resolve the active store by phone number id."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "kapso_api_key": None,
            "kapso_phone_number_id": None,
            "kapso_webhook_secret": None,
        }
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        await repository.update_store_channel_connection(
            store_id=settings.default_store_id,
            channel=Channel.WHATSAPP,
            provider=ChannelProvider.KAPSO,
            payload=StoreChannelConnectionUpdateRequest(
                phone_number_id="store-phone-id",
                api_key="store-kapso-key",
                webhook_secret="store-secret",
                is_active=True,
            ),
        )
        await session.commit()

    gateway, store_id = await build_whatsapp_gateway_for_phone_number(
        session_factory=runtime.session_factory,
        settings=settings,
        phone_number_id="store-phone-id",
    )

    await runtime.engine.dispose()

    assert gateway is not None
    assert gateway.phone_number_id == "store-phone-id"
    assert gateway.webhook_secret == "store-secret"
    assert store_id == settings.default_store_id


async def test_build_whatsapp_gateway_for_phone_number_rejects_unknown_fallback_number(tmp_path: Path):
    """Fallback env config is ignored when the inbound phone number points elsewhere."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    gateway, store_id = await build_whatsapp_gateway_for_phone_number(
        session_factory=runtime.session_factory,
        settings=settings,
        phone_number_id="other-phone-id",
    )

    await runtime.engine.dispose()

    assert gateway is None
    assert store_id is None


async def test_build_whatsapp_gateway_for_phone_number_uses_fallback_when_number_matches(tmp_path: Path):
    """Fallback env credentials still work when the inbound phone number matches them."""
    runtime_settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'channels.db'}",
        auto_init_db=True,
        dashboard_session_secret="test-session-secret",
    )
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(runtime_settings)
    await init_database(settings=runtime_settings, runtime=runtime)

    gateway, store_id = await build_whatsapp_gateway_for_phone_number(
        session_factory=runtime.session_factory,
        settings=settings,
        phone_number_id=settings.kapso_phone_number_id,
    )

    await runtime.engine.dispose()

    assert gateway is not None
    assert gateway.phone_number_id == settings.kapso_phone_number_id
    assert store_id == settings.default_store_id


async def test_build_store_channel_gateway_from_runtime_config_ignores_unknown_provider(tmp_path: Path):
    """Only the Kapso WhatsApp provider is supported by the current gateway builder."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        await repository.update_store_channel_connection(
            store_id=settings.default_store_id,
            channel=Channel.WHATSAPP,
            provider=ChannelProvider.KAPSO,
            payload=StoreChannelConnectionUpdateRequest(
                phone_number_id="store-phone-id",
                api_key="store-kapso-key",
                webhook_secret=None,
                is_active=True,
            ),
        )
        runtime_config = await repository.get_store_channel_runtime_config(
            store_id=settings.default_store_id,
            channel=Channel.WHATSAPP,
            provider=ChannelProvider.KAPSO,
        )
        assert runtime_config is not None

    unsupported = runtime_config.model_copy(update={"provider": "other-provider"})

    await runtime.engine.dispose()

    assert build_store_channel_gateway_from_runtime_config(channel_config=unsupported) is None
