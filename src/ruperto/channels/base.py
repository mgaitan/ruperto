"""Shared channel contracts used by Ruperto integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ruperto.models import Channel


class ChannelDeliveryError(RuntimeError):
    """Raised when one channel adapter cannot deliver a message."""


@dataclass(slots=True)
class InboundCustomerMessage:
    """A normalized inbound customer text message."""

    channel: Channel
    external_user_id: str
    message_text: str
    sender_name: str | None = None
    message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutboundCustomerMessage:
    """A normalized outbound customer text message."""

    channel: Channel
    external_user_id: str
    message_text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class TextChannelGateway(Protocol):
    """Protocol implemented by outbound text-capable channel adapters."""

    channel: Channel

    async def send_text(self, message: OutboundCustomerMessage) -> None:
        """Deliver one outbound text message to the target channel."""
