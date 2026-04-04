"""ORM models for the food-ordering MVP."""

from __future__ import annotations

from datetime import UTC, datetime, time
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, Time, UniqueConstraint
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class Channel(StrEnum):
    """Supported conversation channels."""

    WHATSAPP = "whatsapp"
    DEV = "dev"


class OrderStatus(StrEnum):
    """Lifecycle states for customer orders."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    IN_PREPARATION = "in_preparation"
    ALMOST_READY = "almost_ready"
    READY_FOR_PICKUP = "ready_for_pickup"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class DeliveryType(StrEnum):
    """Delivery modes supported by the ordering flow."""

    DELIVERY = "delivery"
    PICKUP = "pickup"


class PaymentMethod(StrEnum):
    """Payment methods currently supported by the ordering flow."""

    CASH = "cash"
    CARD_LINK = "card_link"
    TRANSFER = "transfer"


class StoreProfile(Base):
    """Persist the configurable public profile of the business."""

    __tablename__ = "store_profile"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    store_name: Mapped[str] = mapped_column(String(length=120))
    bot_name: Mapped[str] = mapped_column(String(length=120))
    store_location: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    store_description: Mapped[str] = mapped_column(String(length=500))
    assistant_personality: Mapped[str] = mapped_column(String(length=255))
    locale: Mapped[str] = mapped_column(String(length=32), default="es-AR")
    currency_code: Mapped[str] = mapped_column(String(length=8), default="ARS")
    transfer_alias: Mapped[str | None] = mapped_column(String(length=120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class StoreBusinessHours(Base):
    """One weekly opening-hours window for the store."""

    __tablename__ = "store_business_hours"
    __table_args__ = (UniqueConstraint("store_id", "weekday", name="uq_store_business_hours"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"), default=1)
    weekday: Mapped[int]
    opens_at: Mapped[time | None] = mapped_column(Time(), nullable=True)
    closes_at: Mapped[time | None] = mapped_column(Time(), nullable=True)
    closed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class Customer(Base):
    """A customer known by the ordering system."""

    __tablename__ = "customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(length=120), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(length=32), nullable=True, unique=True)
    default_address: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class CustomerIdentity(Base):
    """External identities that point to a known customer."""

    __tablename__ = "customer_identity"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_customer_identity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id", ondelete="CASCADE"))
    channel: Mapped[Channel] = mapped_column(SqlEnum(Channel, native_enum=False, length=32))
    external_id: Mapped[str] = mapped_column(String(length=120))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class MenuItem(Base):
    """A product available to customers."""

    __tablename__ = "menu_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(length=80), unique=True)
    name: Mapped[str] = mapped_column(String(length=120))
    description: Mapped[str] = mapped_column(String(length=300))
    category: Mapped[str] = mapped_column(String(length=80))
    price_cents: Mapped[int]
    available: Mapped[bool] = mapped_column(default=True)
    image_url: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class Conversation(Base):
    """A persisted conversation stream for one external identity."""

    __tablename__ = "conversation"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_conversation_identity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[Channel] = mapped_column(SqlEnum(Channel, native_enum=False, length=32))
    external_id: Mapped[str] = mapped_column(String(length=120))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class ConversationMessage(Base):
    """A single serialized PydanticAI message within a conversation."""

    __tablename__ = "conversation_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(length=32))
    payload_json: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class ConversationState(Base):
    """Mutable per-conversation state that does not belong in the message log."""

    __tablename__ = "conversation_state"
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_conversation_state_conversation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id", ondelete="CASCADE"))
    pending_customer_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class Order(Base):
    """A customer order, typically starting as a draft."""

    __tablename__ = "customer_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[OrderStatus] = mapped_column(
        SqlEnum(OrderStatus, native_enum=False, length=32), default=OrderStatus.DRAFT
    )
    delivery_type: Mapped[DeliveryType | None] = mapped_column(
        SqlEnum(DeliveryType, native_enum=False, length=32),
        nullable=True,
    )
    delivery_address: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    payment_method: Mapped[PaymentMethod | None] = mapped_column(
        SqlEnum(PaymentMethod, native_enum=False, length=32),
        nullable=True,
    )
    requested_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preparation_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_amount_cents: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class OrderItem(Base):
    """A single product line within an order."""

    __tablename__ = "order_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("customer_order.id", ondelete="CASCADE"))
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_item.id", ondelete="RESTRICT"))
    name_snapshot: Mapped[str] = mapped_column(String(length=120))
    quantity: Mapped[int]
    unit_price_cents: Mapped[int]
    notes: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
