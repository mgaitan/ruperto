"""ORM models shared by the platform and its vertical domains."""

from __future__ import annotations

from datetime import UTC, datetime, time
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, Time, UniqueConstraint
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


class ChannelProvider(StrEnum):
    """Providers that can back one concrete channel connection."""

    KAPSO = "kapso"


class StoreVertical(StrEnum):
    """Business verticals supported by the platform."""

    ORDERING = "ordering"
    MUNICIPAL = "municipal"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    """Return the persisted values for one string enum."""
    return [item.value for item in enum_type]


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


class StaffRole(StrEnum):
    """Roles available to staff users within one store."""

    OWNER = "owner"
    MANAGER = "manager"
    STAFF = "staff"


class MunicipalCaseStatus(StrEnum):
    """Lifecycle states for municipal service requests."""

    NEW = "new"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class MunicipalRequestKind(StrEnum):
    """Semantic kind of one municipal category."""

    COMPLAINT = "complaint"
    REQUEST = "request"


class StoreProfile(Base):
    """Persist the configurable public profile of the business."""

    __tablename__ = "store_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(length=120), unique=True)
    store_name: Mapped[str] = mapped_column(String(length=120))
    bot_name: Mapped[str] = mapped_column(String(length=120))
    store_location: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    store_description: Mapped[str] = mapped_column(String(length=500))
    assistant_personality: Mapped[str] = mapped_column(String(length=255))
    vertical: Mapped[StoreVertical] = mapped_column(
        SqlEnum(StoreVertical, native_enum=False, length=32, values_callable=enum_values),
        default=StoreVertical.ORDERING,
    )
    locale: Mapped[str] = mapped_column(String(length=32), default="es-AR")
    currency_code: Mapped[str] = mapped_column(String(length=8), default="ARS")
    transfer_alias: Mapped[str | None] = mapped_column(String(length=120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class StaffUser(Base):
    """One authenticated dashboard user."""

    __tablename__ = "staff_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(length=255), unique=True)
    full_name: Mapped[str] = mapped_column(String(length=120))
    password_hash: Mapped[str] = mapped_column(String(length=255))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class PasswordResetToken(Base):
    """One one-time password-reset token issued to a dashboard user."""

    __tablename__ = "password_reset_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_user_id: Mapped[int] = mapped_column(ForeignKey("staff_user.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(length=64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class StoreMembership(Base):
    """Map one dashboard user to one store and role."""

    __tablename__ = "store_membership"
    __table_args__ = (UniqueConstraint("staff_user_id", "store_id", name="uq_store_membership"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_user_id: Mapped[int] = mapped_column(ForeignKey("staff_user.id", ondelete="CASCADE"))
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"))
    role: Mapped[StaffRole] = mapped_column(SqlEnum(StaffRole, native_enum=False, length=32), default=StaffRole.OWNER)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class StoreBusinessHours(Base):
    """One weekly opening-hours window for the store."""

    __tablename__ = "store_business_hours"
    __table_args__ = (UniqueConstraint("store_id", "weekday", "slot_index", name="uq_store_business_hours"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"), default=1)
    weekday: Mapped[int]
    slot_index: Mapped[int] = mapped_column(default=0)
    opens_at: Mapped[time | None] = mapped_column(Time(), nullable=True)
    closes_at: Mapped[time | None] = mapped_column(Time(), nullable=True)
    closed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class Customer(Base):
    """A customer known by the ordering system."""

    __tablename__ = "customer"
    __table_args__ = (UniqueConstraint("store_id", "phone_number", name="uq_customer_store_phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"), default=1)
    name: Mapped[str | None] = mapped_column(String(length=120), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(length=32), nullable=True)
    default_address: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class CustomerIdentity(Base):
    """External identities that point to a known customer."""

    __tablename__ = "customer_identity"
    __table_args__ = (UniqueConstraint("store_id", "channel", "external_id", name="uq_customer_identity_store"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"), default=1)
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
    __table_args__ = (UniqueConstraint("store_id", "channel", "external_id", name="uq_conversation_identity_store"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[Channel] = mapped_column(SqlEnum(Channel, native_enum=False, length=32))
    external_id: Mapped[str] = mapped_column(String(length=120))
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"), default=1)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class StoreChannelConnection(Base):
    """One provider-backed channel connection owned by one store."""

    __tablename__ = "store_channel_connection"
    __table_args__ = (
        UniqueConstraint("store_id", "channel", "provider", name="uq_store_channel_connection_store"),
        UniqueConstraint("channel", "provider", "phone_number_id", name="uq_store_channel_connection_phone"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"))
    channel: Mapped[Channel] = mapped_column(SqlEnum(Channel, native_enum=False, length=32))
    provider: Mapped[ChannelProvider] = mapped_column(
        SqlEnum(ChannelProvider, native_enum=False, length=32),
        default=ChannelProvider.KAPSO,
    )
    phone_number_id: Mapped[str | None] = mapped_column(String(length=120), nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(Text(), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=False)
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
    awaiting_human: Mapped[bool] = mapped_column(default=False)
    handoff_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    handoff_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    handoff_latest_customer_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    handoff_last_customer_message_at: Mapped[datetime | None] = mapped_column(nullable=True)
    handoff_last_operator_reply_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class MunicipalArea(Base):
    """One configurable municipal service area owned by a tenant."""

    __tablename__ = "municipal_area"
    __table_args__ = (UniqueConstraint("store_id", "name", name="uq_municipal_area_store_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(length=120))
    description: Mapped[str | None] = mapped_column(String(length=300), nullable=True)
    manager_staff_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    display_order: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class MunicipalCategory(Base):
    """One optional subcategory within a municipal area."""

    __tablename__ = "municipal_category"
    __table_args__ = (UniqueConstraint("area_id", "name", name="uq_municipal_category_area_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    area_id: Mapped[int] = mapped_column(ForeignKey("municipal_area.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(length=120))
    description: Mapped[str | None] = mapped_column(String(length=300), nullable=True)
    request_kind: Mapped[MunicipalRequestKind] = mapped_column(
        SqlEnum(MunicipalRequestKind, native_enum=False, length=32, values_callable=enum_values),
        default=MunicipalRequestKind.COMPLAINT,
    )
    requires_precise_location: Mapped[bool] = mapped_column(default=False)
    is_fallback: Mapped[bool] = mapped_column(default=False)
    display_order: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class MunicipalCase(Base):
    """One municipal service request tracked from intake to closure."""

    __tablename__ = "municipal_case"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"))
    area_id: Mapped[int] = mapped_column(ForeignKey("municipal_area.id", ondelete="RESTRICT"))
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("municipal_category.id", ondelete="SET NULL"),
        nullable=True,
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer.id", ondelete="SET NULL"),
        nullable=True,
    )
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation.id", ondelete="SET NULL"),
        nullable=True,
    )
    assignee_staff_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(length=160))
    description: Mapped[str] = mapped_column(Text())
    reporter_name: Mapped[str | None] = mapped_column(String(length=120), nullable=True)
    reporter_phone_number: Mapped[str | None] = mapped_column(String(length=32), nullable=True)
    location_text: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    location_reference: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float(), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float(), nullable=True)
    status: Mapped[MunicipalCaseStatus] = mapped_column(
        SqlEnum(MunicipalCaseStatus, native_enum=False, length=32, values_callable=enum_values),
        default=MunicipalCaseStatus.NEW,
    )
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class MunicipalCaseDraft(Base):
    """Mutable intake draft for one municipal conversation before submission."""

    __tablename__ = "municipal_case_draft"
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_municipal_case_draft_conversation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id", ondelete="CASCADE"))
    store_id: Mapped[int] = mapped_column(ForeignKey("store_profile.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id", ondelete="CASCADE"))
    area_id: Mapped[int | None] = mapped_column(
        ForeignKey("municipal_area.id", ondelete="SET NULL"),
        nullable=True,
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("municipal_category.id", ondelete="SET NULL"),
        nullable=True,
    )
    request_summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    location_text: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    location_reference: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float(), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float(), nullable=True)
    awaiting_confirmation: Mapped[bool] = mapped_column(default=False)
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
    notify_when_ready: Mapped[bool] = mapped_column(default=True)
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


class OutboundNotification(Base):
    """One outbound notification queued for delivery on a conversation channel."""

    __tablename__ = "outbound_notification"
    __table_args__ = (
        UniqueConstraint("order_id", "event_type", name="uq_outbound_notification_order_event"),
        UniqueConstraint("municipal_case_id", "event_type", name="uq_outbound_notification_case_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("customer_order.id", ondelete="CASCADE"), nullable=True)
    municipal_case_id: Mapped[int | None] = mapped_column(
        ForeignKey("municipal_case.id", ondelete="CASCADE"),
        nullable=True,
    )
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(length=64))
    message_text: Mapped[str] = mapped_column(Text())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
