"""Tests for channel orchestration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from ruperto.channels.base import InboundCustomerMessage
from ruperto.channels.service import (
    build_channel_gateway,
    deliver_order_notifications,
    deliver_pending_notifications,
    seed_customer_name_from_inbound_message,
)
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import Channel
from ruperto.repository import BusinessRepository

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
