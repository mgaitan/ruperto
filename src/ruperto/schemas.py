"""Typed schemas shared by the repositories, API, and assistant."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ruperto.models import DeliveryType, OrderStatus, PaymentMethod


def format_price_ars(amount_cents: int) -> str:
    """Format integer cent amounts as an Argentine peso display string."""
    pesos = amount_cents / 100
    integer = f"{pesos:,.0f}".replace(",", ".")
    return f"$ {integer}"


class BaseSchema(BaseModel):
    """Base schema with attribute parsing enabled."""

    model_config = ConfigDict(from_attributes=True)


class StoreProfileSnapshot(BaseSchema):
    """Public store information useful to the assistant and admin surfaces."""

    id: int
    store_name: str
    bot_name: str
    store_location: str | None
    store_description: str
    assistant_personality: str
    locale: str
    currency_code: str


class StoreBusinessHoursSnapshot(BaseSchema):
    """One opening-hours row for the weekly store schedule."""

    id: int
    store_id: int
    weekday: int
    opens_at: str | None
    closes_at: str | None
    closed: bool


class StoreAvailabilitySnapshot(BaseModel):
    """Current store availability for conversational messaging."""

    is_open: bool
    message_text: str
    next_open_text: str | None = None


class CustomerSnapshot(BaseSchema):
    """Current customer data known by the system."""

    id: int
    name: str | None
    phone_number: str | None
    default_address: str | None


class MenuItemSnapshot(BaseSchema):
    """Customer-facing catalog item."""

    id: int
    sku: str
    name: str
    description: str
    category: str
    available: bool
    price_cents: int
    image_url: str | None
    price_display: str


class OrderItemSnapshot(BaseModel):
    """Snapshot of a line item within an order."""

    menu_item_id: int
    name: str
    quantity: int
    unit_price_cents: int
    unit_price_display: str
    notes: str | None


class OrderSnapshot(BaseModel):
    """Current view of an order."""

    id: int
    customer_id: int
    conversation_id: int | None
    status: OrderStatus
    delivery_type: DeliveryType | None
    delivery_address: str | None
    payment_method: PaymentMethod | None
    total_amount_cents: int
    total_amount_display: str
    items: list[OrderItemSnapshot] = Field(default_factory=list)


class CustomerMemorySnapshot(BaseModel):
    """Small memory fragment derived from historical orders."""

    favorite_item_name: str | None = None
    recent_items: list[str] = Field(default_factory=list)


class DelayEstimateSnapshot(BaseModel):
    """Operational delay estimate exposed to the assistant."""

    active_orders_ahead: int
    base_minutes: int
    estimated_minutes: int
    display_text: str


class AssistantNextStep(StrEnum):
    """High-level next steps for the ordering conversation."""

    ASK_NAME = "ask_name"
    CHOOSE_ITEMS = "choose_items"
    CHOOSE_DELIVERY = "choose_delivery"
    ASK_ADDRESS = "ask_address"
    CHOOSE_PAYMENT = "choose_payment"
    CONFIRM_ORDER = "confirm_order"
    COMPLETE = "complete"
    HANDOFF = "handoff"


class AssistantReply(BaseModel):
    """Structured output returned by the assistant model."""

    reply_text: str
    next_step: AssistantNextStep
    handoff: bool = False


class AssistantTurnResult(BaseModel):
    """Result returned by the service after handling one incoming message."""

    conversation_id: int
    customer: CustomerSnapshot
    reply: AssistantReply
    current_order: OrderSnapshot | None = None


class DevMessageRequest(BaseModel):
    """Payload accepted by the development chat endpoint."""

    external_user_id: str = Field(min_length=1)
    message_text: str = Field(min_length=1)


class OrderStatusUpdateRequest(BaseModel):
    """Payload accepted by staff endpoints to update one order status."""

    status: OrderStatus


class StoreBusinessHoursUpdateEntry(BaseModel):
    """One staff-supplied business-hours row."""

    weekday: int = Field(ge=0, le=6)
    opens_at: str | None = None
    closes_at: str | None = None
    closed: bool = False


class StoreBusinessHoursUpdateRequest(BaseModel):
    """Payload accepted by staff endpoints to replace the weekly schedule."""

    hours: list[StoreBusinessHoursUpdateEntry]
