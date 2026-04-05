"""Tests for the Kapso-backed WhatsApp adapter."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from pydantic import SecretStr

from ruperto.channels.base import ChannelDeliveryError, OutboundCustomerMessage
from ruperto.channels.kapso_whatsapp import KapsoWhatsAppGateway, verify_kapso_webhook_signature
from ruperto.config import Settings
from ruperto.models import Channel


def test_verify_kapso_webhook_signature_accepts_matching_payload():
    """Kapso webhook verification uses the raw payload bytes."""
    payload = b'{"event":"whatsapp.message.received","message":{"text":{"body":"Hola"}}}'
    secret = "kapso-secret"

    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    assert verify_kapso_webhook_signature(raw_payload=payload, signature=signature, secret=secret) is True


def test_verify_kapso_webhook_signature_allows_unsigned_payloads_when_secret_missing():
    """Unsigned payloads are allowed only when no webhook secret is configured."""
    assert verify_kapso_webhook_signature(raw_payload=b"{}", signature=None, secret=None) is True


def test_verify_kapso_webhook_signature_rejects_missing_signature_when_secret_exists():
    """A configured secret requires the Kapso signature header."""
    assert verify_kapso_webhook_signature(raw_payload=b"{}", signature=None, secret="kapso-secret") is False


def test_kapso_gateway_parses_unbatched_text_message():
    """Kapso webhook payloads are normalized into inbound customer messages."""
    gateway = KapsoWhatsAppGateway(kapso_api_key="kapso-key", phone_number_id="597907523413541")
    payload = {
        "event": "whatsapp.message.received",
        "message": {
            "id": "wamid.123",
            "from": "5493513308454",
            "timestamp": "1730092800",
            "type": "text",
            "text": {"body": "Hola, ¿qué hay para comer?"},
        },
        "conversation": {
            "id": "conv_123",
            "phone_number": "+5493513308454",
            "phone_number_id": "597907523413541",
            "kapso": {"contact_name": "Pedro"},
        },
        "phone_number_id": "597907523413541",
    }

    messages = gateway.parse_inbound_messages(payload)

    assert len(messages) == 1
    assert messages[0].channel == Channel.WHATSAPP
    assert messages[0].external_user_id == "+5493513308454"
    assert messages[0].sender_name == "Pedro"
    assert messages[0].message_text == "Hola, ¿qué hay para comer?"


def test_kapso_gateway_parses_batched_text_messages():
    """Kapso buffered webhook batches are flattened into message turns."""
    gateway = KapsoWhatsAppGateway(kapso_api_key="kapso-key", phone_number_id="597907523413541")
    payload = {
        "type": "whatsapp.message.received",
        "batch": True,
        "data": [
            {
                "message": {
                    "id": "wamid.111",
                    "from": "5493513308454",
                    "timestamp": "1730092801",
                    "type": "text",
                    "text": {"body": "Primero"},
                },
                "conversation": {
                    "id": "conv_123",
                    "phone_number": "+5493513308454",
                    "phone_number_id": "597907523413541",
                    "kapso": {"contact_name": "Pedro"},
                },
                "phone_number_id": "597907523413541",
            },
            {
                "message": {
                    "id": "wamid.112",
                    "from": "5493513308454",
                    "timestamp": "1730092802",
                    "type": "text",
                    "text": {"body": "Segundo"},
                },
                "conversation": {
                    "id": "conv_123",
                    "phone_number": "+5493513308454",
                    "phone_number_id": "597907523413541",
                    "kapso": {"contact_name": "Pedro"},
                },
                "phone_number_id": "597907523413541",
            },
        ],
    }

    messages = gateway.parse_inbound_messages(payload)

    assert [message.message_text for message in messages] == ["Primero", "Segundo"]


def test_kapso_gateway_ignores_non_text_and_outbound_events():
    """Only inbound text messages are turned into assistant turns."""
    gateway = KapsoWhatsAppGateway(kapso_api_key="kapso-key", phone_number_id="597907523413541")
    payload = {
        "type": "whatsapp.message.received",
        "batch": True,
        "data": [
            {
                "message": {
                    "id": "wamid.111",
                    "from": "5493513308454",
                    "timestamp": "1730092801",
                    "type": "image",
                    "image": {"id": "media-1"},
                },
                "conversation": {"id": "conv_123", "phone_number": "+5493513308454"},
            },
            {
                "message": {
                    "id": "wamid.112",
                    "from": "5493513308454",
                    "timestamp": "1730092802",
                    "type": "text",
                    "text": {"body": "No me proceses"},
                    "direction": "outbound",
                },
                "conversation": {"id": "conv_123", "phone_number": "+5493513308454"},
            },
        ],
    }

    assert gateway.parse_inbound_messages(payload) == []


def test_kapso_gateway_ignores_non_received_events():
    """Only received-message events enter the assistant conversation loop."""
    gateway = KapsoWhatsAppGateway(kapso_api_key="kapso-key", phone_number_id="597907523413541")
    payload = {
        "event": "whatsapp.message.delivered",
        "message": {
            "id": "wamid.123",
            "from": "5493513308454",
            "timestamp": "1730092800",
            "type": "text",
            "text": {"body": "Hola"},
        },
        "conversation": {"id": "conv_123", "phone_number": "+5493513308454"},
    }

    assert gateway.parse_inbound_messages(payload) == []


def test_kapso_gateway_builds_from_settings():
    """Settings produce a ready-to-use Kapso adapter when configured."""
    settings = Settings(
        kapso_api_key=SecretStr("kapso-key"),
        kapso_phone_number_id="597907523413541",
        kapso_webhook_secret=SecretStr("kapso-secret"),
    )

    gateway = KapsoWhatsAppGateway.from_settings(settings)

    assert gateway is not None
    assert gateway.phone_number_id == "597907523413541"
    assert gateway.webhook_secret == "kapso-secret"


def test_kapso_gateway_from_settings_returns_none_without_runtime_credentials():
    """The adapter stays disabled until the Kapso runtime credentials exist."""
    settings = Settings()

    assert KapsoWhatsAppGateway.from_settings(settings) is None


@pytest.mark.anyio
async def test_kapso_gateway_send_text_returns_message_id(mocker):
    """The Kapso adapter unwraps the provider response into a small send result."""

    class FakeMessages:
        async def send_text(self, **kwargs):
            class FakeResponse:
                def model_dump_json(self):
                    return '{"contacts":[{"input":"+5493513308454"}],"messages":[{"id":"wamid.123"}]}'

            return FakeResponse()

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    mocker.patch("ruperto.channels.kapso_whatsapp.WhatsAppClient", new=FakeClient)
    gateway = KapsoWhatsAppGateway(kapso_api_key="kapso-key", phone_number_id="597907523413541")

    result = await gateway.send_text(
        OutboundCustomerMessage(
            channel=Channel.WHATSAPP,
            external_user_id="+5493513308454",
            message_text="Hola",
        )
    )

    assert result.message_id == "wamid.123"


@pytest.mark.anyio
async def test_kapso_gateway_send_text_raises_on_empty_provider_response(mocker):
    """The adapter rejects empty Kapso responses instead of pretending delivery worked."""

    class FakeMessages:
        async def send_text(self, **kwargs):
            class FakeResponse:
                def model_dump_json(self):
                    return '{"contacts":[],"messages":[]}'

            return FakeResponse()

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    mocker.patch("ruperto.channels.kapso_whatsapp.WhatsAppClient", new=FakeClient)
    gateway = KapsoWhatsAppGateway(kapso_api_key="kapso-key", phone_number_id="597907523413541")

    with pytest.raises(ChannelDeliveryError):
        await gateway.send_text(
            OutboundCustomerMessage(
                channel=Channel.WHATSAPP,
                external_user_id="+5493513308454",
                message_text="Hola",
            )
        )


@pytest.mark.anyio
async def test_kapso_gateway_send_text_wraps_provider_errors(mocker):
    """Provider exceptions are normalized into channel delivery failures."""

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("boom")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    mocker.patch("ruperto.channels.kapso_whatsapp.WhatsAppClient", new=FakeClient)
    gateway = KapsoWhatsAppGateway(kapso_api_key="kapso-key", phone_number_id="597907523413541")

    with pytest.raises(ChannelDeliveryError):
        await gateway.send_text(
            OutboundCustomerMessage(
                channel=Channel.WHATSAPP,
                external_user_id="+5493513308454",
                message_text="Hola",
            )
        )
