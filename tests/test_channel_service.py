"""Tests for channel orchestration helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from ruperto.channels.base import InboundCustomerMessage
from ruperto.channels.service import (
    _notify_store_staff_about_handoff,
    build_channel_gateway,
    build_store_channel_gateway_from_runtime_config,
    build_whatsapp_gateway_for_phone_number,
    deliver_order_notifications,
    deliver_pending_notifications,
    handle_inbound_customer_message,
    seed_customer_name_from_inbound_message,
)
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.mail import HandoffEmailDeliveryError
from ruperto.models import Channel, ChannelProvider, DeliveryType, OrderStatus, PaymentMethod
from ruperto.repository import BusinessRepository
from ruperto.schemas import AssistantNextStep, AssistantReply, AssistantTurnResult, StoreChannelConnectionUpdateRequest

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
        kapso_api_key=None,
        kapso_phone_number_id=None,
        kapso_webhook_secret=None,
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


async def test_build_whatsapp_gateway_for_phone_number_uses_matching_fallback_runtime_settings(tmp_path: Path):
    """Fallback runtime settings still resolve one tenant when the phone id matches."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    gateway, store_id = await build_whatsapp_gateway_for_phone_number(
        session_factory=runtime.session_factory,
        settings=settings,
        phone_number_id=settings.kapso_phone_number_id,
    )

    await runtime.engine.dispose()

    assert gateway is not None
    assert store_id == settings.default_store_id


async def test_build_whatsapp_gateway_for_phone_number_uses_fallback_without_inbound_number(tmp_path: Path):
    """Fallback runtime settings also resolve when the webhook omits the phone id."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    gateway, store_id = await build_whatsapp_gateway_for_phone_number(
        session_factory=runtime.session_factory,
        settings=settings,
        phone_number_id=None,
    )

    await runtime.engine.dispose()

    assert gateway is not None
    assert gateway.phone_number_id == settings.kapso_phone_number_id
    assert store_id == settings.default_store_id


async def test_deliver_pending_notifications_marks_sent_rows_as_delivered(tmp_path: Path):
    """Successful delivery commits the delivered marker for pending notifications."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    sent_payloads: list[str] = []
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            phone_number="+5493513308454",
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            customer_id=customer.id,
        )
        await repository.add_item_to_current_order(
            customer_id=customer.id,
            conversation_id=conversation.id,
            sku="hamburguesa-doble",
            quantity=1,
        )
        await repository.set_order_delivery_type(customer.id, conversation.id, DeliveryType.PICKUP)
        await repository.set_order_payment_method(customer.id, conversation.id, PaymentMethod.CASH)
        order = await repository.confirm_current_order(customer.id, conversation.id)
        await repository.update_order_status(order.id, OrderStatus.READY_FOR_PICKUP)
        await session.commit()

    async def fake_send_text(self, message):
        sent_payloads.append(message.message_text)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("ruperto.channels.kapso_whatsapp.KapsoWhatsAppGateway.send_text", fake_send_text)
        delivered = await deliver_pending_notifications(
            session_factory=runtime.session_factory,
            settings=settings,
            store_id=settings.default_store_id,
            channel=Channel.WHATSAPP,
            external_user_id="+5493513308454",
        )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        pending = await repository.peek_pending_notifications(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
        )

    await runtime.engine.dispose()

    assert delivered == 1
    assert pending == []
    assert sent_payloads == ["Tu pedido ya está listo para retirar 🙌"]


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


async def test_handle_inbound_customer_message_activates_human_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Assistant handoff replies persist conversation state for operator takeover."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    fake_result = AssistantTurnResult.model_validate(
        {
            "conversation_id": 1,
            "customer": {"id": 1, "name": "Pedro", "phone_number": "+5493513308454", "default_address": None},
            "reply": {"reply_text": "Te paso con una persona del equipo.", "next_step": "handoff", "handoff": True},
            "current_order": None,
        }
    )
    mocked_router = AsyncMock(return_value=fake_result)
    monkeypatch.setattr("ruperto.channels.service.handle_customer_message_for_store", mocked_router)

    result = await handle_inbound_customer_message(
        session_factory=runtime.session_factory,
        settings=settings,
        inbound_message=InboundCustomerMessage(
            channel=Channel.WHATSAPP,
            external_user_id="+5493513308454",
            message_text="Necesito hablar con una persona.",
        ),
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        customer = await repository.get_or_create_customer(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            phone_number="+5493513308454",
        )
        conversation = await repository.get_or_create_conversation(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            customer_id=customer.id,
        )
        handoffs = await repository.list_active_conversation_handoffs(store_id=settings.default_store_id)

    await runtime.engine.dispose()

    assert result.reply.handoff is True
    assert mocked_router.await_count == 1
    assert handoffs[0].conversation_id == conversation.id
    assert handoffs[0].latest_customer_message == "Necesito hablar con una persona."


async def test_handle_inbound_customer_message_skips_bot_while_waiting_for_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Customer follow-ups stay silent until an operator releases the handoff."""
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
        conversation = await repository.get_or_create_conversation(
            channel=Channel.WHATSAPP,
            external_id="+5493513308454",
            customer_id=customer.id,
        )
        await repository.activate_conversation_handoff(
            conversation_id=conversation.id,
            reason="Te paso con una persona del equipo.",
            latest_customer_message="Necesito ayuda urgente.",
        )
        await session.commit()

    mocked_router = AsyncMock()
    monkeypatch.setattr("ruperto.channels.service.handle_customer_message_for_store", mocked_router)

    result = await handle_inbound_customer_message(
        session_factory=runtime.session_factory,
        settings=settings,
        inbound_message=InboundCustomerMessage(
            channel=Channel.WHATSAPP,
            external_user_id="+5493513308454",
            message_text="¿Me pueden responder?",
        ),
    )

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        handoffs = await repository.list_active_conversation_handoffs(store_id=settings.default_store_id)

    await runtime.engine.dispose()

    assert result.reply == AssistantReply(reply_text="", next_step=AssistantNextStep.HANDOFF, handoff=True)
    assert mocked_router.await_count == 0
    assert handoffs[0].latest_customer_message == "¿Me pueden responder?"


async def test_handle_inbound_customer_message_suppresses_handoff_alert_email_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """SMTP delivery failures do not break the handoff activation path."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "dashboard_admin_email": "owner@example.com",
            "dashboard_admin_password": SecretStr("super-secret"),
            "dashboard_admin_name": "Owner Demo",
            "smtp_server": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "mailer@example.com",
            "smtp_password": SecretStr("smtp-secret"),
        }
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    fake_result = AssistantTurnResult.model_validate(
        {
            "conversation_id": 1,
            "customer": {"id": 1, "name": "Pedro", "phone_number": "+5493513308454", "default_address": None},
            "reply": {"reply_text": "Te paso con una persona del equipo.", "next_step": "handoff", "handoff": True},
            "current_order": None,
        }
    )
    mocked_router = AsyncMock(return_value=fake_result)
    monkeypatch.setattr("ruperto.channels.service.handle_customer_message_for_store", mocked_router)

    def fail_email(**kwargs):
        raise HandoffEmailDeliveryError()

    monkeypatch.setattr("ruperto.channels.service.send_handoff_alert_email", fail_email)

    result = await handle_inbound_customer_message(
        session_factory=runtime.session_factory,
        settings=settings,
        inbound_message=InboundCustomerMessage(
            channel=Channel.WHATSAPP,
            external_user_id="+5493513308454",
            message_text="Necesito hablar con una persona.",
        ),
    )

    await runtime.engine.dispose()

    assert result.reply.handoff is True
    assert mocked_router.await_count == 1


async def test_notify_store_staff_about_handoff_returns_early_without_smtp(tmp_path: Path):
    """The handoff notifier exits quietly when SMTP is not configured."""
    settings = build_settings(tmp_path).model_copy(
        update={
            "dashboard_admin_email": "owner@example.com",
            "dashboard_admin_password": SecretStr("super-secret"),
            "dashboard_admin_name": "Owner Demo",
            "smtp_server": None,
            "smtp_port": None,
            "smtp_user": None,
            "smtp_password": None,
        }
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    await _notify_store_staff_about_handoff(
        session_factory=runtime.session_factory,
        settings=settings,
        store_id=settings.default_store_id,
        inbound_message=InboundCustomerMessage(
            channel=Channel.WHATSAPP,
            external_user_id="+5493513308454",
            message_text="Necesito una persona.",
        ),
        reply_text="Te paso con una persona del equipo.",
    )

    await runtime.engine.dispose()
