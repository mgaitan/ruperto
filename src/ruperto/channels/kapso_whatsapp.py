"""Kapso-backed WhatsApp channel adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kapso_whatsapp import MessageDirection, MessageType, WebhookMessage, WhatsAppClient
from pydantic import BaseModel, ConfigDict, Field

from ruperto.channels.base import ChannelDeliveryError, InboundCustomerMessage, OutboundCustomerMessage
from ruperto.config import Settings
from ruperto.models import Channel
from ruperto.repository import normalize_phone_number

KAPSO_MESSAGE_RECEIVED_EVENT = "whatsapp.message.received"


def verify_kapso_webhook_signature(*, raw_payload: bytes, signature: str | None, secret: str | None) -> bool:
    """Return whether the raw webhook payload matches the Kapso signature."""
    if not secret:
        return True
    if signature is None:
        return False
    expected_signature = hmac.new(secret.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected_signature)


class KapsoConversationKapso(BaseModel):
    """Kapso-specific conversation metadata included in webhook payloads."""

    contact_name: str | None = Field(default=None, alias="contact_name")


class KapsoConversationPayload(BaseModel):
    """Conversation context attached to one Kapso WhatsApp webhook."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    phone_number: str | None = Field(default=None, alias="phone_number")
    status: str | None = None
    phone_number_id: str | None = Field(default=None, alias="phone_number_id")
    kapso: KapsoConversationKapso | None = None


class KapsoMessageEvent(BaseModel):
    """One normalized Kapso WhatsApp event payload."""

    model_config = ConfigDict(populate_by_name=True)

    event: str | None = None
    type: str | None = None
    batch: bool = False
    message: WebhookMessage | None = None
    conversation: KapsoConversationPayload | None = None
    phone_number_id: str | None = Field(default=None, alias="phone_number_id")
    data: list[KapsoMessageEvent] = Field(default_factory=list)


@dataclass(slots=True)
class KapsoSendResult:
    """Small send result returned by the Kapso adapter."""

    message_id: str | None


def _iter_kapso_message_events(payload: Mapping[str, Any]) -> list[KapsoMessageEvent]:
    """Expand a Kapso webhook payload into one flat event list."""
    envelope = KapsoMessageEvent.model_validate(payload)
    if envelope.batch:
        return [
            event.model_copy(update={"type": event.type or envelope.type, "event": event.event or envelope.event})
            for event in envelope.data
        ]
    return [envelope]


def _extract_inbound_text_message(event: KapsoMessageEvent) -> InboundCustomerMessage | None:
    """Return one normalized inbound text message when the event contains one."""
    event_name = event.event or event.type
    if event_name != KAPSO_MESSAGE_RECEIVED_EVENT:
        return None
    if event.message is None or event.message.type != MessageType.TEXT or event.message.text is None:
        return None
    if event.message.direction == MessageDirection.OUTBOUND:
        return None

    external_user_id = (
        normalize_phone_number(
            event.conversation.phone_number if event.conversation is not None else event.message.from_
        )
        or event.message.from_
    )
    sender_name = event.conversation.kapso.contact_name if event.conversation and event.conversation.kapso else None
    return InboundCustomerMessage(
        channel=Channel.WHATSAPP,
        external_user_id=external_user_id,
        message_text=event.message.text.body,
        sender_name=sender_name,
        message_id=event.message.id,
        metadata={
            "conversation_id": event.conversation.id if event.conversation is not None else None,
            "phone_number_id": event.phone_number_id
            or (event.conversation.phone_number_id if event.conversation else None),
        },
    )


class KapsoWhatsAppGateway:
    """Send and parse WhatsApp messages through the Kapso proxy."""

    channel = Channel.WHATSAPP

    def __init__(
        self,
        *,
        kapso_api_key: str,
        phone_number_id: str,
        webhook_secret: str | None = None,
    ) -> None:
        self.kapso_api_key = kapso_api_key
        self.phone_number_id = phone_number_id
        self.webhook_secret = webhook_secret

    @classmethod
    def from_settings(cls, settings: Settings) -> KapsoWhatsAppGateway | None:
        """Build the adapter from settings when Kapso is configured."""
        if settings.kapso_api_key is None or settings.kapso_phone_number_id is None:
            return None
        return cls(
            kapso_api_key=settings.kapso_api_key.get_secret_value(),
            phone_number_id=settings.kapso_phone_number_id,
            webhook_secret=(
                settings.kapso_webhook_secret.get_secret_value() if settings.kapso_webhook_secret is not None else None
            ),
        )

    def verify_webhook(self, *, raw_payload: bytes, signature: str | None) -> bool:
        """Return whether the incoming webhook signature is valid."""
        return verify_kapso_webhook_signature(
            raw_payload=raw_payload,
            signature=signature,
            secret=self.webhook_secret,
        )

    def parse_inbound_messages(self, payload: Mapping[str, Any]) -> list[InboundCustomerMessage]:
        """Normalize one Kapso webhook payload into inbound customer messages."""
        return [
            inbound_message
            for event in _iter_kapso_message_events(payload)
            if (inbound_message := _extract_inbound_text_message(event)) is not None
        ]

    async def send_text(self, message: OutboundCustomerMessage) -> KapsoSendResult:
        """Send one outbound text message through Kapso's WhatsApp proxy."""
        try:
            async with WhatsAppClient(kapso_api_key=self.kapso_api_key) as client:
                response = await client.messages.send_text(
                    phone_number_id=self.phone_number_id,
                    to=message.external_user_id,
                    body=message.message_text,
                )
        except Exception as error:
            raise ChannelDeliveryError from error

        response_payload = json.loads(response.model_dump_json())
        contacts = response_payload.get("contacts", [])
        messages = response_payload.get("messages", [])
        message_id = messages[0].get("id") if messages else None
        if not contacts and not messages:
            raise ChannelDeliveryError
        return KapsoSendResult(message_id=message_id)
