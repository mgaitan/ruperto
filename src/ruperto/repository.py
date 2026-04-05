"""Persistence helpers for the ordering MVP."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic_ai import ModelMessage
from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ruperto.auth import hash_password, normalize_email, verify_password
from ruperto.models import (
    Channel,
    Conversation,
    ConversationMessage,
    ConversationState,
    Customer,
    CustomerIdentity,
    DeliveryType,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
    OutboundNotification,
    PaymentMethod,
    StaffRole,
    StaffUser,
    StoreBusinessHours,
    StoreMembership,
    StoreProfile,
    utc_now,
)
from ruperto.schemas import (
    CustomerMemorySnapshot,
    CustomerSnapshot,
    DelayEstimateSnapshot,
    MenuItemSnapshot,
    OrderItemSnapshot,
    OrderSnapshot,
    OutboundNotificationSnapshot,
    StaffUserSnapshot,
    StoreAvailabilitySnapshot,
    StoreBusinessHoursSnapshot,
    StoreMembershipSnapshot,
    StoreProfileSnapshot,
    StoreProfileUpdateRequest,
    StoreStaffMembershipSnapshot,
    format_price_ars,
)

LARGE_ORDER_THRESHOLD_CENTS = 3000000
DEFAULT_DELAY_MINUTES = 25
KITCHEN_LOAD_COEFFICIENT_MINUTES = 3
DEFAULT_PREPARATION_MINUTES = 15
DELIVERY_EXTRA_MINUTES = 5
ITEM_PREPARATION_MINUTES_BY_SKU = {
    "empanadas de carne": 12,
    "hamburguesa completa": 15,
    "hamburguesa doble cheddar": 17,
    "hamburguesa bbq": 16,
    "milanesa napolitana": 18,
    "pizza muzzarella": 20,
    "pizza napolitana": 20,
    "pizza fugazzeta": 21,
    "pizza especial": 22,
    "sanguche de milanesa": 14,
    "gaseosa cola 1.5l": 2,
    "agua sin gas 500ml": 1,
    "cerveza rubia lata": 2,
    "flan casero": 4,
    "helado 1/4 kg": 3,
    "brownie con nuez": 4,
}
ACTIVE_ORDER_STATUSES = {
    OrderStatus.CONFIRMED,
    OrderStatus.IN_PREPARATION,
    OrderStatus.ALMOST_READY,
    OrderStatus.READY_FOR_PICKUP,
    OrderStatus.OUT_FOR_DELIVERY,
}
READY_NOTIFICATION_EVENT_BY_STATUS = {
    OrderStatus.ALMOST_READY: "order_almost_ready",
    OrderStatus.READY_FOR_PICKUP: "order_ready",
    OrderStatus.OUT_FOR_DELIVERY: "order_out_for_delivery",
}
STORE_MEMBERSHIP_NOT_FOUND_MESSAGE = "Store membership not found."
STORE_HOURS_SLOT_REQUIRES_BOTH_TIMES_MESSAGE = "Each business-hours slot requires both open and close times."
STORE_HOURS_SLOT_ORDER_MESSAGE = "Business-hours slots must close after they open."
STORE_HOURS_SLOT_OVERLAP_MESSAGE = "Business-hours slots cannot overlap on the same day."
WEEKDAY_LABELS_ES = {
    0: "lunes",
    1: "martes",
    2: "miércoles",
    3: "jueves",
    4: "viernes",
    5: "sábado",
    6: "domingo",
}


def round_delay_minutes(value: int) -> int:
    """Round one delay estimate up to the next 5-minute mark."""
    if value <= 0:
        return 5
    return ((value + 4) // 5) * 5


def normalize_phone_number(value: str | None) -> str | None:
    """Normalize phone numbers to a compact identity-friendly representation."""
    if value is None:
        return None

    digits = re.sub(r"\D+", "", value)
    if not digits:
        return None

    if value.strip().startswith("+"):
        return f"+{digits}"
    return digits


class ProductUnavailableError(ValueError):
    """Raised when a requested product is not available in the menu."""

    def __init__(self) -> None:
        super().__init__("El producto pedido no existe o no está disponible.")


class NoOpenOrderError(ValueError):
    """Raised when the customer has no open order to operate on."""

    def __init__(self) -> None:
        super().__init__("No hay un pedido abierto para confirmar.")


class EmptyOrderError(ValueError):
    """Raised when a draft order has no items."""

    def __init__(self) -> None:
        super().__init__("No se puede confirmar un pedido vacío.")


class OrderNotFoundError(ValueError):
    """Raised when the requested order does not exist."""

    def __init__(self) -> None:
        super().__init__("No se encontró el pedido solicitado.")


class IncompleteOrderError(ValueError):
    """Raised when trying to confirm a draft missing required checkout data."""

    def __init__(self, message: str) -> None:
        super().__init__(message)

    @classmethod
    def missing_delivery_type(cls) -> IncompleteOrderError:
        """Return the error raised when delivery mode is still unknown."""
        return cls("Necesito saber si es envío o retiro antes de confirmar.")

    @classmethod
    def missing_delivery_address(cls) -> IncompleteOrderError:
        """Return the error raised when the draft still lacks a delivery address."""
        return cls("Necesito la dirección de entrega antes de confirmar.")

    @classmethod
    def missing_payment_method(cls) -> IncompleteOrderError:
        """Return the error raised when the draft still lacks a payment method."""
        return cls("Necesito definir el medio de pago antes de confirmar.")


class RequestedReadyTimeError(ValueError):
    """Raised when a scheduled ready time cannot be fulfilled."""

    def __init__(self, message: str) -> None:
        super().__init__(message)

    @classmethod
    def past_time(cls) -> RequestedReadyTimeError:
        """Return the error raised when the requested ready time already passed."""
        return cls("Ese horario ya pasó. Decime otra hora y lo programo.")

    @classmethod
    def outside_business_hours(cls, next_open_text: str) -> RequestedReadyTimeError:
        """Return the error raised when the requested slot is outside business hours."""
        return cls(
            f"Puedo programarlo solo dentro del horario del local. El próximo horario disponible es {next_open_text}."
        )

    @classmethod
    def insufficient_lead_time(cls, earliest_ready_text: str) -> RequestedReadyTimeError:
        """Return the error raised when there is not enough preparation lead time."""
        return cls(
            "Para tenerlo listo a esa hora necesito un poco más de tiempo. "
            f"El primer horario que puedo prometerte es {earliest_ready_text}."
        )


class BusinessRepository:
    """Repository facade covering the core MVP entities."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_store_profile(self, *, store_id: int = 1) -> StoreProfileSnapshot:
        """Return the single store profile used by the MVP."""
        row = await self.session.scalar(select(StoreProfile).where(StoreProfile.id == store_id))
        assert row is not None
        return StoreProfileSnapshot.model_validate(row)

    async def create_store_profile(  # noqa: PLR0913
        self,
        *,
        store_name: str,
        bot_name: str,
        store_description: str,
        assistant_personality: str,
        store_location: str | None = None,
        locale: str = "es-AR",
        transfer_alias: str | None = None,
    ) -> StoreProfileSnapshot:
        """Create a new store profile for staff membership tests and future tenancy."""
        row = StoreProfile(
            store_name=store_name.strip(),
            bot_name=bot_name.strip(),
            store_location=self._normalize_optional_text(store_location),
            store_description=store_description.strip(),
            assistant_personality=assistant_personality.strip(),
            locale=locale,
            transfer_alias=self._normalize_optional_text(transfer_alias),
        )
        self.session.add(row)
        await self.session.flush()
        return StoreProfileSnapshot.model_validate(row)

    async def update_store_profile(
        self,
        payload: StoreProfileUpdateRequest,
        *,
        store_id: int = 1,
    ) -> StoreProfileSnapshot:
        """Persist the editable store profile used by staff and the assistant."""
        row = await self.session.scalar(select(StoreProfile).where(StoreProfile.id == store_id))
        assert row is not None
        row.store_name = payload.store_name.strip()
        row.bot_name = payload.bot_name.strip()
        row.store_location = self._normalize_optional_text(payload.store_location)
        row.store_description = payload.store_description.strip()
        row.assistant_personality = payload.assistant_personality.strip()
        row.transfer_alias = self._normalize_optional_text(payload.transfer_alias)
        row.updated_at = utc_now()
        await self.session.flush()
        return StoreProfileSnapshot.model_validate(row)

    async def get_staff_user_by_id(self, staff_user_id: int) -> StaffUserSnapshot | None:
        """Return one dashboard user by primary key."""
        row = await self.session.get(StaffUser, staff_user_id)
        if row is None:
            return None
        return StaffUserSnapshot.model_validate(row)

    async def get_staff_user_by_email(self, email: str) -> StaffUserSnapshot | None:
        """Return one dashboard user by normalized email."""
        row = await self.session.scalar(select(StaffUser).where(StaffUser.email == normalize_email(email)))
        if row is None:
            return None
        return StaffUserSnapshot.model_validate(row)

    async def ensure_staff_user(
        self,
        *,
        email: str,
        full_name: str,
        password: str,
        store_id: int,
        role: StaffRole = StaffRole.OWNER,
    ) -> StaffUserSnapshot:
        """Create or refresh one dashboard user and membership."""
        normalized_email = normalize_email(email)
        row = await self.session.scalar(select(StaffUser).where(StaffUser.email == normalized_email))
        if row is None:
            row = StaffUser(
                email=normalized_email,
                full_name=full_name.strip(),
                password_hash=hash_password(password),
                is_active=True,
            )
            self.session.add(row)
            await self.session.flush()
        else:
            row.full_name = full_name.strip()
            row.password_hash = hash_password(password)
            row.is_active = True
            row.updated_at = utc_now()
            await self.session.flush()

        membership = await self.session.scalar(
            select(StoreMembership).where(
                StoreMembership.staff_user_id == row.id,
                StoreMembership.store_id == store_id,
            )
        )
        if membership is None:
            self.session.add(StoreMembership(staff_user_id=row.id, store_id=store_id, role=role))
        else:
            membership.role = role
        await self.session.flush()
        return StaffUserSnapshot.model_validate(row)

    async def authenticate_staff_user(self, *, email: str, password: str) -> StaffUserSnapshot | None:
        """Return the dashboard user when the credentials are valid."""
        row = await self.session.scalar(select(StaffUser).where(StaffUser.email == normalize_email(email)))
        if row is None or not row.is_active:
            return None
        if not verify_password(password, row.password_hash):
            return None
        return StaffUserSnapshot.model_validate(row)

    async def list_store_memberships_for_staff_user(self, staff_user_id: int) -> list[StoreMembershipSnapshot]:
        """List the stores one dashboard user can operate."""
        rows = (
            await self.session.execute(
                select(StoreMembership, StoreProfile.store_name)
                .join(StoreProfile, StoreProfile.id == StoreMembership.store_id)
                .where(StoreMembership.staff_user_id == staff_user_id)
                .order_by(StoreProfile.id.asc())
            )
        ).all()
        return [
            StoreMembershipSnapshot(
                store_id=membership.store_id,
                store_name=store_name,
                role=membership.role,
            )
            for membership, store_name in rows
        ]

    async def user_can_access_store(self, *, staff_user_id: int, store_id: int) -> bool:
        """Return whether the given dashboard user has membership in the store."""
        membership = await self.session.scalar(
            select(StoreMembership).where(
                StoreMembership.staff_user_id == staff_user_id,
                StoreMembership.store_id == store_id,
            )
        )
        return membership is not None

    async def list_staff_memberships_for_store(self, *, store_id: int) -> list[StoreStaffMembershipSnapshot]:
        """List staff memberships operating the given store."""
        rows = (
            await self.session.execute(
                select(StoreMembership, StaffUser, StoreProfile.store_name)
                .join(StaffUser, StaffUser.id == StoreMembership.staff_user_id)
                .join(StoreProfile, StoreProfile.id == StoreMembership.store_id)
                .where(StoreMembership.store_id == store_id)
                .order_by(StaffUser.full_name.asc(), StaffUser.email.asc())
            )
        ).all()
        return [
            StoreStaffMembershipSnapshot(
                membership_id=membership.id,
                staff_user_id=membership.staff_user_id,
                store_id=membership.store_id,
                store_name=store_name,
                role=membership.role,
                email=staff_user.email,
                full_name=staff_user.full_name,
                is_active=staff_user.is_active,
            )
            for membership, staff_user, store_name in rows
        ]

    async def update_store_membership_role(
        self,
        *,
        membership_id: int,
        store_id: int,
        role: StaffRole,
    ) -> StoreStaffMembershipSnapshot:
        """Update one membership role inside the given store."""
        row = await self.session.scalar(
            select(StoreMembership).where(StoreMembership.id == membership_id, StoreMembership.store_id == store_id)
        )
        if row is None:
            raise ValueError(STORE_MEMBERSHIP_NOT_FOUND_MESSAGE)
        row.role = role
        await self.session.flush()
        memberships = await self.list_staff_memberships_for_store(store_id=store_id)
        return next(membership for membership in memberships if membership.membership_id == membership_id)

    async def list_store_business_hours(self, *, store_id: int = 1) -> list[StoreBusinessHoursSnapshot]:
        """Return the weekly opening-hours schedule for the store."""
        rows = (
            await self.session.scalars(
                select(StoreBusinessHours)
                .where(StoreBusinessHours.store_id == store_id)
                .order_by(StoreBusinessHours.weekday.asc(), StoreBusinessHours.slot_index.asc())
            )
        ).all()
        return [self._store_business_hours_snapshot(row) for row in rows]

    async def replace_store_business_hours(
        self,
        *,
        hours: list[StoreBusinessHoursSnapshot],
        store_id: int = 1,
    ) -> list[StoreBusinessHoursSnapshot]:
        """Replace the weekly opening-hours schedule for the store."""
        normalized_hours = self._normalize_store_business_hours(hours, store_id=store_id)
        existing_rows = (
            await self.session.scalars(select(StoreBusinessHours).where(StoreBusinessHours.store_id == store_id))
        ).all()
        for row in existing_rows:
            await self.session.delete(row)
        await self.session.flush()

        self.session.add_all(
            [
                StoreBusinessHours(
                    store_id=store_id,
                    weekday=row.weekday,
                    slot_index=row.slot_index,
                    opens_at=self._parse_hour_text(row.opens_at),
                    closes_at=self._parse_hour_text(row.closes_at),
                    closed=row.closed,
                )
                for row in normalized_hours
            ]
        )
        await self.session.flush()
        return await self.list_store_business_hours(store_id=store_id)

    async def get_store_availability(
        self,
        *,
        timezone_name: str,
        now: datetime | None = None,
        store_id: int = 1,
    ) -> StoreAvailabilitySnapshot:
        """Return whether the store is currently open and when it opens next."""
        zone = ZoneInfo(timezone_name)
        local_now = now.astimezone(zone) if now is not None else datetime.now(zone)
        schedule = await self.list_store_business_hours(store_id=store_id)
        current_time = local_now.time().replace(second=0, microsecond=0)
        current_slot = self._find_matching_schedule_row(schedule, local_now)

        if self._is_open_at(current_slot, current_time):
            close_text = (
                f" hasta las {current_slot.closes_at}"
                if current_slot is not None and current_slot.closes_at is not None
                else ""
            )
            return StoreAvailabilitySnapshot(
                is_open=True,
                message_text=f"Estamos abiertos ahora 🍽️{close_text}.",
                next_open_text=None,
            )

        next_open_text = self._find_next_opening_text(schedule, local_now)
        return StoreAvailabilitySnapshot(
            is_open=False,
            message_text=f"Ahora estamos cerrados 😴 Abrimos {next_open_text}.",
            next_open_text=next_open_text,
        )

    async def list_menu_items(self, *, only_available: bool = True) -> list[MenuItemSnapshot]:
        """List menu items visible to customers."""
        statement = select(MenuItem).order_by(MenuItem.category, MenuItem.name)
        if only_available:
            statement = statement.where(MenuItem.available.is_(True))
        rows = (await self.session.scalars(statement)).all()
        return [self._menu_item_snapshot(row) for row in rows]

    async def search_menu_items(self, query: str) -> list[MenuItemSnapshot]:
        """Search menu items by a case-insensitive text fragment."""
        pattern = f"%{query.lower()}%"
        rows = (
            await self.session.scalars(
                select(MenuItem)
                .where(func.lower(MenuItem.name).like(pattern))
                .where(MenuItem.available.is_(True))
                .order_by(MenuItem.name)
            )
        ).all()
        return [self._menu_item_snapshot(row) for row in rows]

    async def get_or_create_customer(
        self,
        *,
        channel: Channel,
        external_id: str,
        phone_number: str | None = None,
    ) -> CustomerSnapshot:
        """Resolve an existing customer or create one for the given identity."""
        normalized_phone = normalize_phone_number(phone_number)
        identity = await self.session.scalar(
            select(CustomerIdentity).where(
                CustomerIdentity.channel == channel,
                CustomerIdentity.external_id == external_id,
            )
        )
        if identity is not None:
            customer = await self.session.get(Customer, identity.customer_id)
            assert customer is not None
            if normalized_phone is not None and customer.phone_number is None:
                customer.phone_number = normalized_phone
                await self.session.flush()
            return CustomerSnapshot.model_validate(customer)

        customer = None
        if normalized_phone is not None:
            customer = await self.session.scalar(select(Customer).where(Customer.phone_number == normalized_phone))

        if customer is None:
            customer = Customer(phone_number=normalized_phone)
            self.session.add(customer)
            await self.session.flush()

        self.session.add(
            CustomerIdentity(
                customer_id=customer.id,
                channel=channel,
                external_id=external_id,
            )
        )
        await self.session.flush()
        return CustomerSnapshot.model_validate(customer)

    async def get_customer(self, customer_id: int) -> CustomerSnapshot:
        """Load a customer by identifier."""
        customer = await self.session.get(Customer, customer_id)
        assert customer is not None
        return CustomerSnapshot.model_validate(customer)

    async def list_customers(self, *, limit: int = 50) -> list[CustomerSnapshot]:
        """List customers ordered by recent activity."""
        rows = (
            await self.session.scalars(
                select(Customer).order_by(Customer.updated_at.desc(), Customer.id.desc()).limit(limit)
            )
        ).all()
        return [CustomerSnapshot.model_validate(row) for row in rows]

    async def update_customer_name(self, customer_id: int, name: str) -> CustomerSnapshot:
        """Persist the customer name."""
        customer = await self.session.get(Customer, customer_id)
        assert customer is not None
        customer.name = name.strip()
        customer.updated_at = utc_now()
        await self.session.flush()
        return CustomerSnapshot.model_validate(customer)

    async def update_customer_default_address(self, customer_id: int, address: str) -> CustomerSnapshot:
        """Persist the customer's preferred delivery address."""
        customer = await self.session.get(Customer, customer_id)
        assert customer is not None
        customer.default_address = address.strip()
        customer.updated_at = utc_now()
        await self.session.flush()
        return CustomerSnapshot.model_validate(customer)

    async def get_or_create_conversation(
        self,
        *,
        channel: Channel,
        external_id: str,
        customer_id: int,
    ) -> Conversation:
        """Return the persisted conversation for one external identity."""
        conversation = await self.session.scalar(
            select(Conversation).where(
                Conversation.channel == channel,
                Conversation.external_id == external_id,
            )
        )
        if conversation is None:
            conversation = Conversation(
                channel=channel,
                external_id=external_id,
                customer_id=customer_id,
            )
            self.session.add(conversation)
            await self.session.flush()
            return conversation

        if conversation.customer_id != customer_id:
            conversation.customer_id = customer_id
        conversation.updated_at = utc_now()
        await self.session.flush()
        return conversation

    async def load_conversation_messages(self, conversation_id: int) -> list[ModelMessage]:
        """Load serialized PydanticAI messages for a conversation."""
        rows = (
            await self.session.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.id)
            )
        ).all()
        messages: list[ModelMessage] = []
        for row in rows:
            payload = json.loads(row.payload_json)
            restored = ModelMessagesTypeAdapter.validate_python([payload])
            messages.extend(restored)
        return messages

    async def get_pending_customer_message(self, conversation_id: int) -> str | None:
        """Return the deferred customer message captured before onboarding completed."""
        state = await self._get_or_create_conversation_state(conversation_id)
        return state.pending_customer_message

    async def set_pending_customer_message(self, conversation_id: int, message_text: str | None) -> str | None:
        """Persist or clear the deferred customer message for one conversation."""
        state = await self._get_or_create_conversation_state(conversation_id)
        state.pending_customer_message = message_text.strip() if message_text else None
        state.updated_at = utc_now()
        await self.session.flush()
        return state.pending_customer_message

    async def append_conversation_messages(self, conversation_id: int, messages: list[ModelMessage]) -> None:
        """Persist the newly generated PydanticAI messages for a conversation."""
        for message in messages:
            payload = ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]
            self.session.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    kind=payload["kind"],
                    payload_json=json.dumps(payload, ensure_ascii=False),
                )
            )

        conversation = await self.session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.updated_at = utc_now()
        await self.session.flush()

    async def get_current_order(
        self,
        customer_id: int,
        conversation_id: int,
        *,
        create_if_missing: bool = True,
    ) -> OrderSnapshot | None:
        """Return the current draft order for a customer and conversation."""
        order = await self._get_draft_order(customer_id, conversation_id)
        if order is None and create_if_missing:
            order = Order(customer_id=customer_id, conversation_id=conversation_id)
            self.session.add(order)
            await self.session.flush()
        if order is None:
            return None
        return await self._build_order_snapshot(order)

    async def get_latest_order(
        self,
        customer_id: int,
        conversation_id: int,
    ) -> OrderSnapshot | None:
        """Return the most recent order for the current conversation."""
        order = await self.session.scalar(
            select(Order)
            .where(
                Order.customer_id == customer_id,
                Order.conversation_id == conversation_id,
            )
            .order_by(Order.updated_at.desc(), Order.id.desc())
        )
        if order is None:
            return None
        return await self._build_order_snapshot(order)

    async def list_orders(
        self,
        *,
        limit: int = 50,
        status: OrderStatus | None = None,
    ) -> list[OrderSnapshot]:
        """List orders ordered by recent activity."""
        statement = select(Order).order_by(Order.updated_at.desc(), Order.id.desc()).limit(limit)
        if status is not None:
            statement = statement.where(Order.status == status)
        orders = (await self.session.scalars(statement)).all()
        return [await self._build_order_snapshot(order) for order in orders]

    async def update_order_status(self, order_id: int, status: OrderStatus) -> OrderSnapshot:
        """Update the operational status for one order."""
        order = await self.session.get(Order, order_id)
        if order is None:
            raise OrderNotFoundError
        previous_status = order.status
        order.status = status
        order.updated_at = utc_now()
        await self.session.flush()
        await self._queue_status_notification_if_needed(order, previous_status=previous_status)
        return await self._build_order_snapshot(order)

    async def set_order_notify_when_ready(
        self,
        customer_id: int,
        conversation_id: int,
        *,
        enabled: bool = True,
    ) -> OrderSnapshot:
        """Persist whether the customer wants a proactive ready notification."""
        order = await self._get_draft_order(customer_id, conversation_id)
        if order is None:
            order = await self.session.scalar(
                select(Order)
                .where(
                    Order.customer_id == customer_id,
                    Order.conversation_id == conversation_id,
                )
                .order_by(Order.updated_at.desc(), Order.id.desc())
            )
        if order is None:
            order = Order(customer_id=customer_id, conversation_id=conversation_id, notify_when_ready=enabled)
            self.session.add(order)
            await self.session.flush()
        order.notify_when_ready = enabled
        order.updated_at = utc_now()
        await self.session.flush()
        return await self._build_order_snapshot(order)

    async def list_pending_notifications(
        self,
        *,
        channel: Channel,
        external_id: str,
    ) -> list[OutboundNotificationSnapshot]:
        """Return undelivered notifications for one conversation and mark them as delivered."""
        rows = (
            await self.session.scalars(
                select(OutboundNotification)
                .join(Conversation, Conversation.id == OutboundNotification.conversation_id)
                .where(
                    Conversation.channel == channel,
                    Conversation.external_id == external_id,
                    OutboundNotification.delivered_at.is_(None),
                )
                .order_by(OutboundNotification.id.asc())
            )
        ).all()
        delivered_at = utc_now()
        snapshots: list[OutboundNotificationSnapshot] = []
        for row in rows:
            row.delivered_at = delivered_at
            snapshots.append(
                OutboundNotificationSnapshot(
                    id=row.id,
                    order_id=row.order_id,
                    conversation_id=row.conversation_id,
                    event_type=row.event_type,
                    message_text=row.message_text,
                    created_at=row.created_at,
                )
            )
        await self.session.flush()
        return snapshots

    async def add_item_to_current_order(
        self,
        customer_id: int,
        conversation_id: int,
        *,
        sku: str,
        quantity: int,
        notes: str | None = None,
    ) -> OrderSnapshot:
        """Append a menu item to the current draft order."""
        menu_item = await self.session.scalar(select(MenuItem).where(MenuItem.sku == sku, MenuItem.available.is_(True)))
        if menu_item is None:
            raise ProductUnavailableError

        order = await self._get_draft_order(customer_id, conversation_id)
        if order is None:
            order = Order(customer_id=customer_id, conversation_id=conversation_id)
            self.session.add(order)
            await self.session.flush()

        self.session.add(
            OrderItem(
                order_id=order.id,
                menu_item_id=menu_item.id,
                name_snapshot=menu_item.name,
                quantity=quantity,
                unit_price_cents=menu_item.price_cents,
                notes=notes,
            )
        )
        await self.session.flush()
        await self._refresh_order_total(order)
        return await self._build_order_snapshot(order)

    async def set_order_requested_ready_at(
        self,
        customer_id: int,
        conversation_id: int,
        requested_ready_at: datetime,
        *,
        timezone_name: str,
        store_id: int = 1,
    ) -> OrderSnapshot:
        """Schedule the active order to be ready at a concrete local time."""
        order = await self._get_draft_order(customer_id, conversation_id)
        if order is None:
            order = await self.session.scalar(
                select(Order)
                .where(
                    Order.customer_id == customer_id,
                    Order.conversation_id == conversation_id,
                )
                .order_by(Order.updated_at.desc(), Order.id.desc())
            )
        if order is None:
            order = Order(customer_id=customer_id, conversation_id=conversation_id)
            self.session.add(order)
            await self.session.flush()
        previous_requested_ready_at = order.requested_ready_at
        previous_preparation_starts_at = order.preparation_starts_at
        order.requested_ready_at = requested_ready_at.astimezone(UTC)
        order.updated_at = utc_now()
        try:
            await self._sync_requested_ready_metadata(order)
            await self._validate_requested_ready_at(
                order,
                timezone_name=timezone_name,
                store_id=store_id,
            )
        except RequestedReadyTimeError:
            order.requested_ready_at = previous_requested_ready_at
            order.preparation_starts_at = previous_preparation_starts_at
            await self.session.flush()
            raise

        await self.session.flush()
        return await self._build_order_snapshot(order)

    async def set_order_delivery_type(
        self,
        customer_id: int,
        conversation_id: int,
        delivery_type: DeliveryType,
    ) -> OrderSnapshot:
        """Set the delivery mode on the current draft order."""
        order = await self._require_current_order(customer_id, conversation_id)
        order.delivery_type = delivery_type
        order.updated_at = utc_now()
        await self._sync_requested_ready_metadata(order)
        await self.session.flush()
        return await self._build_order_snapshot(order)

    async def set_order_delivery_address(
        self,
        customer_id: int,
        conversation_id: int,
        address: str,
    ) -> OrderSnapshot:
        """Set the delivery address on the current draft order and customer profile."""
        order = await self._require_current_order(customer_id, conversation_id)
        order.delivery_address = address.strip()
        order.updated_at = utc_now()
        await self.update_customer_default_address(customer_id, address)
        await self.session.flush()
        return await self._build_order_snapshot(order)

    async def set_order_payment_method(
        self,
        customer_id: int,
        conversation_id: int,
        payment_method: PaymentMethod,
    ) -> OrderSnapshot:
        """Set the payment method on the current draft order."""
        order = await self._require_current_order(customer_id, conversation_id)
        order.payment_method = payment_method
        order.updated_at = utc_now()
        await self.session.flush()
        return await self._build_order_snapshot(order)

    async def reset_current_order(
        self,
        customer_id: int,
        conversation_id: int,
    ) -> OrderSnapshot:
        """Clear all current draft items so the order can be rebuilt after a correction."""
        order = await self._require_current_order(customer_id, conversation_id)
        items = await self._load_order_items(order.id)
        for item in items:
            await self.session.delete(item)
        await self.session.flush()
        order.updated_at = utc_now()
        await self._refresh_order_total(order)
        await self.session.flush()
        return await self._build_order_snapshot(order)

    async def confirm_current_order(
        self,
        customer_id: int,
        conversation_id: int,
        *,
        timezone_name: str = "America/Argentina/Cordoba",
        store_id: int = 1,
    ) -> OrderSnapshot:
        """Confirm the active order if it already contains items."""
        order = await self._get_draft_order(customer_id, conversation_id)
        if order is None:
            raise NoOpenOrderError

        items = await self._load_order_items(order.id)
        if not items:
            raise EmptyOrderError
        if order.delivery_type is None:
            raise IncompleteOrderError.missing_delivery_type()
        if order.delivery_type == DeliveryType.DELIVERY and not order.delivery_address:
            raise IncompleteOrderError.missing_delivery_address()
        if order.payment_method is None:
            raise IncompleteOrderError.missing_payment_method()

        await self._sync_requested_ready_metadata(order)
        await self._validate_requested_ready_at(order, timezone_name=timezone_name, store_id=store_id)
        order.status = OrderStatus.CONFIRMED
        order.updated_at = utc_now()
        await self._refresh_order_total(order)
        return await self._build_order_snapshot(order)

    async def get_customer_memory(self, customer_id: int) -> CustomerMemorySnapshot:
        """Compute a tiny memory summary from confirmed orders."""
        orders = (
            await self.session.scalars(
                select(Order)
                .where(Order.customer_id == customer_id, Order.status == OrderStatus.CONFIRMED)
                .order_by(Order.updated_at.desc())
            )
        ).all()
        if not orders:
            return CustomerMemorySnapshot()

        item_names = (
            await self.session.scalars(
                select(OrderItem.name_snapshot)
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.customer_id == customer_id, Order.status == OrderStatus.CONFIRMED)
            )
        ).all()
        favorite_item_name = Counter(item_names).most_common(1)[0][0]
        recent_items = [item.name_snapshot for item in await self._load_order_items(orders[0].id)]
        return CustomerMemorySnapshot(
            favorite_item_name=favorite_item_name,
            recent_items=recent_items,
        )

    async def get_estimated_delay(self, customer_id: int, conversation_id: int) -> DelayEstimateSnapshot:
        """Estimate the current preparation delay for the active or latest order."""
        order = await self._get_draft_order(customer_id, conversation_id)
        if order is None:
            latest_order = await self.get_latest_order(customer_id, conversation_id)
            if latest_order is None:
                active_orders_ahead = await self.count_active_orders()
                estimated_minutes = DEFAULT_DELAY_MINUTES + (active_orders_ahead * KITCHEN_LOAD_COEFFICIENT_MINUTES)
                return DelayEstimateSnapshot(
                    active_orders_ahead=active_orders_ahead,
                    base_minutes=DEFAULT_DELAY_MINUTES,
                    estimated_minutes=round_delay_minutes(estimated_minutes),
                    display_text=f"{round_delay_minutes(estimated_minutes)} minutos aproximadamente",
                )
            active_orders_ahead = await self.count_active_orders_ahead_by_order_id(latest_order.id)
            return self._estimate_delay_from_snapshot(latest_order, active_orders_ahead=active_orders_ahead)

        snapshot = await self._build_order_snapshot(order)
        active_orders_ahead = await self.count_active_orders_ahead_by_order_id(order.id)
        return self._estimate_delay_from_snapshot(snapshot, active_orders_ahead=active_orders_ahead)

    async def count_active_orders(self) -> int:
        """Count currently active non-draft orders in the kitchen pipeline."""
        result = await self.session.scalar(
            select(func.count(Order.id)).where(
                Order.status.in_(ACTIVE_ORDER_STATUSES),
                self._active_order_pipeline_clause(reference_time=utc_now()),
            )
        )
        return int(result or 0)

    async def count_active_orders_ahead_by_order_id(self, order_id: int) -> int:
        """Count active orders created before the given order."""
        current_order = await self.session.get(Order, order_id)
        assert current_order is not None
        reference_time = utc_now()
        result = await self.session.scalar(
            select(func.count(Order.id)).where(
                Order.id != current_order.id,
                Order.status.in_(ACTIVE_ORDER_STATUSES),
                self._active_order_pipeline_clause(reference_time=reference_time),
                Order.created_at <= current_order.created_at,
            )
        )
        return int(result or 0)

    def _menu_item_snapshot(self, item: MenuItem) -> MenuItemSnapshot:
        """Build a public menu snapshot."""
        return MenuItemSnapshot(
            id=item.id,
            sku=item.sku,
            name=item.name,
            description=item.description,
            category=item.category,
            available=item.available,
            price_cents=item.price_cents,
            image_url=item.image_url,
            price_display=format_price_ars(item.price_cents),
        )

    def _store_business_hours_snapshot(self, row: StoreBusinessHours) -> StoreBusinessHoursSnapshot:
        """Build a serializable business-hours row."""
        return StoreBusinessHoursSnapshot(
            id=row.id,
            store_id=row.store_id,
            weekday=row.weekday,
            slot_index=row.slot_index,
            opens_at=row.opens_at.strftime("%H:%M") if row.opens_at is not None else None,
            closes_at=row.closes_at.strftime("%H:%M") if row.closes_at is not None else None,
            closed=row.closed,
        )

    async def _get_draft_order(self, customer_id: int, conversation_id: int) -> Order | None:
        """Load the active draft order for the customer."""
        return await self.session.scalar(
            select(Order)
            .where(
                Order.customer_id == customer_id,
                Order.conversation_id == conversation_id,
                Order.status == OrderStatus.DRAFT,
            )
            .order_by(Order.updated_at.desc())
        )

    async def _get_or_create_conversation_state(self, conversation_id: int) -> ConversationState:
        """Return mutable conversation state, creating it on first use."""
        state = await self.session.scalar(
            select(ConversationState).where(ConversationState.conversation_id == conversation_id)
        )
        if state is None:
            state = ConversationState(conversation_id=conversation_id)
            self.session.add(state)
            await self.session.flush()
        return state

    async def _require_current_order(self, customer_id: int, conversation_id: int) -> Order:
        """Return the draft order, creating one only if it already exists."""
        order = await self._get_draft_order(customer_id, conversation_id)
        if order is None:
            order = Order(customer_id=customer_id, conversation_id=conversation_id)
            self.session.add(order)
            await self.session.flush()
        return order

    async def _load_order_items(self, order_id: int) -> list[OrderItem]:
        """Load persisted order items."""
        rows = (
            await self.session.scalars(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id))
        ).all()
        return list(rows)

    async def _refresh_order_total(self, order: Order) -> None:
        """Recalculate the current order total from its line items."""
        items = await self._load_order_items(order.id)
        order.total_amount_cents = sum(item.quantity * item.unit_price_cents for item in items)
        await self._sync_requested_ready_metadata(order)
        order.updated_at = utc_now()
        await self.session.flush()

    async def _sync_requested_ready_metadata(self, order: Order) -> None:
        """Keep derived scheduling timestamps aligned with the current order contents."""
        if order.requested_ready_at is None:
            order.preparation_starts_at = None
            return

        items = await self._load_order_items(order.id)
        order_snapshot = OrderSnapshot(
            id=order.id,
            customer_id=order.customer_id,
            conversation_id=order.conversation_id,
            status=order.status,
            delivery_type=order.delivery_type,
            delivery_address=order.delivery_address,
            payment_method=order.payment_method,
            notify_when_ready=order.notify_when_ready,
            requested_ready_at=order.requested_ready_at,
            preparation_starts_at=order.preparation_starts_at,
            total_amount_cents=order.total_amount_cents,
            total_amount_display=format_price_ars(order.total_amount_cents),
            items=[
                OrderItemSnapshot(
                    menu_item_id=item.menu_item_id,
                    name=item.name_snapshot,
                    quantity=item.quantity,
                    unit_price_cents=item.unit_price_cents,
                    unit_price_display=format_price_ars(item.unit_price_cents),
                    notes=item.notes,
                )
                for item in items
            ],
        )
        preparation_minutes = self._estimate_preparation_minutes(order_snapshot)
        order.preparation_starts_at = order.requested_ready_at - timedelta(minutes=preparation_minutes)

    async def _validate_requested_ready_at(
        self,
        order: Order,
        *,
        timezone_name: str,
        store_id: int,
    ) -> None:
        """Ensure a scheduled order can be fulfilled during business hours."""
        if order.requested_ready_at is None:
            return

        zone = ZoneInfo(timezone_name)
        local_now = utc_now().astimezone(zone)
        local_ready = order.requested_ready_at.astimezone(zone)
        if local_ready <= local_now:
            raise RequestedReadyTimeError.past_time()

        schedule = await self.list_store_business_hours(store_id=store_id)
        schedule_row = self._find_matching_schedule_row(schedule, local_ready)
        if schedule_row is None:
            next_open_text = self._find_next_opening_text(schedule, local_now)
            raise RequestedReadyTimeError.outside_business_hours(next_open_text)

        if order.preparation_starts_at is None:
            return

        local_open = self._opening_datetime_for_row(schedule_row, local_ready)
        local_preparation_start = order.preparation_starts_at.astimezone(zone)
        earliest_start = max(local_now, local_open)
        if local_preparation_start < earliest_start:
            preparation_minutes = int((order.requested_ready_at - order.preparation_starts_at).total_seconds() // 60)
            earliest_ready = earliest_start + timedelta(minutes=preparation_minutes)
            raise RequestedReadyTimeError.insufficient_lead_time(
                self._describe_local_datetime(earliest_ready, local_now)
            )

    async def _build_order_snapshot(self, order: Order) -> OrderSnapshot:
        """Build a serializable snapshot for the current order."""
        items = await self._load_order_items(order.id)
        return OrderSnapshot(
            id=order.id,
            customer_id=order.customer_id,
            conversation_id=order.conversation_id,
            status=order.status,
            delivery_type=order.delivery_type,
            delivery_address=order.delivery_address,
            payment_method=order.payment_method,
            notify_when_ready=order.notify_when_ready,
            requested_ready_at=order.requested_ready_at,
            preparation_starts_at=order.preparation_starts_at,
            total_amount_cents=order.total_amount_cents,
            total_amount_display=format_price_ars(order.total_amount_cents),
            items=[
                OrderItemSnapshot(
                    menu_item_id=item.menu_item_id,
                    name=item.name_snapshot,
                    quantity=item.quantity,
                    unit_price_cents=item.unit_price_cents,
                    unit_price_display=format_price_ars(item.unit_price_cents),
                    notes=item.notes,
                )
                for item in items
            ],
        )

    async def _queue_status_notification_if_needed(self, order: Order, *, previous_status: OrderStatus) -> None:
        """Queue one outbound notification when an order reaches a ready state."""
        if not order.notify_when_ready:
            return
        if previous_status == order.status:
            return
        event_type = READY_NOTIFICATION_EVENT_BY_STATUS.get(order.status)
        if event_type is None:
            return
        existing = await self.session.scalar(
            select(OutboundNotification).where(
                OutboundNotification.order_id == order.id,
                OutboundNotification.event_type == event_type,
            )
        )
        if existing is not None:
            return
        conversation = await self.session.get(Conversation, order.conversation_id)
        if conversation is None:
            return
        self.session.add(
            OutboundNotification(
                order_id=order.id,
                conversation_id=conversation.id,
                event_type=event_type,
                message_text=self._build_status_notification_text(order),
            )
        )
        await self.session.flush()

    def _build_status_notification_text(self, order: Order) -> str:
        """Build the outbound message shown when an order reaches a relevant status."""
        if order.status == OrderStatus.ALMOST_READY:
            return "Tu pedido ya casi está 👀"
        if order.status == OrderStatus.OUT_FOR_DELIVERY:
            return "Tu pedido ya salió y va en camino 🚚"
        return "Tu pedido ya está listo para retirar 🙌"

    def _estimate_delay_from_snapshot(
        self,
        order: OrderSnapshot,
        *,
        active_orders_ahead: int,
    ) -> DelayEstimateSnapshot:
        """Compute an MVP delay estimate based on preparation time and kitchen load."""
        base_minutes = self._estimate_preparation_minutes(order)
        estimated_minutes = round_delay_minutes(base_minutes + (active_orders_ahead * KITCHEN_LOAD_COEFFICIENT_MINUTES))

        return DelayEstimateSnapshot(
            active_orders_ahead=active_orders_ahead,
            base_minutes=base_minutes,
            estimated_minutes=estimated_minutes,
            display_text=f"{estimated_minutes} minutos aproximadamente",
        )

    def _estimate_preparation_minutes(self, order: OrderSnapshot) -> int:
        """Estimate one order's base preparation time without kitchen load."""
        if not order.items:
            return DEFAULT_PREPARATION_MINUTES

        line_minutes = [
            ITEM_PREPARATION_MINUTES_BY_SKU.get(item.name.strip().lower(), DEFAULT_PREPARATION_MINUTES)
            for item in order.items
        ]
        base_minutes = max(line_minutes)
        total_quantity = sum(item.quantity for item in order.items)
        distinct_items = len(order.items)

        base_minutes += max(total_quantity - 1, 0) * 2
        base_minutes += max(distinct_items - 1, 0) * 2
        if order.delivery_type == DeliveryType.DELIVERY:
            base_minutes += DELIVERY_EXTRA_MINUTES
        if order.total_amount_cents >= LARGE_ORDER_THRESHOLD_CENTS:
            base_minutes += 4
        return base_minutes

    def _active_order_pipeline_clause(self, *, reference_time: datetime) -> ColumnElement[bool]:
        """Exclude scheduled orders until their preparation window actually starts."""
        return or_(
            Order.preparation_starts_at.is_(None),
            Order.preparation_starts_at <= reference_time,
        )

    def _is_open_at(self, row: StoreBusinessHoursSnapshot | None, current_time: time) -> bool:
        """Return whether one schedule row covers the current time."""
        if row is None or row.closed or row.opens_at is None or row.closes_at is None:
            return False
        opens_at = self._parse_hour_text(row.opens_at)
        closes_at = self._parse_hour_text(row.closes_at)
        assert opens_at is not None
        assert closes_at is not None
        return opens_at <= current_time < closes_at

    def _schedule_rows_for_weekday(
        self,
        schedule: list[StoreBusinessHoursSnapshot],
        weekday: int,
    ) -> list[StoreBusinessHoursSnapshot]:
        """Return the schedule rows configured for one weekday."""
        return [
            row
            for row in schedule
            if row.weekday == weekday and not row.closed and row.opens_at is not None and row.closes_at is not None
        ]

    def _find_matching_schedule_row(
        self,
        schedule: list[StoreBusinessHoursSnapshot],
        local_dt: datetime,
    ) -> StoreBusinessHoursSnapshot | None:
        """Return the schedule row that covers the given local datetime."""
        current_time = local_dt.time().replace(second=0, microsecond=0)
        return next(
            (
                row
                for row in self._schedule_rows_for_weekday(schedule, local_dt.weekday())
                if self._is_open_at(row, current_time)
            ),
            None,
        )

    def _opening_datetime_for_row(self, row: StoreBusinessHoursSnapshot, local_dt: datetime) -> datetime:
        """Return the opening datetime for the schedule row on the given local date."""
        opens_at = self._parse_hour_text(row.opens_at)
        assert opens_at is not None
        return datetime.combine(local_dt.date(), opens_at, tzinfo=local_dt.tzinfo)

    def _describe_local_datetime(self, local_dt: datetime, local_now: datetime) -> str:
        """Describe a local datetime as today, tomorrow, or weekday plus hour."""
        if local_dt.date() == local_now.date():
            day_text = "hoy"
        elif local_dt.date() == (local_now + timedelta(days=1)).date():
            day_text = "mañana"
        else:
            day_text = f"el {WEEKDAY_LABELS_ES[local_dt.weekday()]}"
        return f"{day_text} a las {local_dt.strftime('%H:%M')}"

    def _find_next_opening_text(self, schedule: list[StoreBusinessHoursSnapshot], local_now: datetime) -> str:
        """Find the next opening slot as a short Spanish phrase."""
        for offset in range(8):
            candidate_day = local_now + timedelta(days=offset)
            day_rows = self._schedule_rows_for_weekday(schedule, candidate_day.weekday())
            for row in day_rows:
                if offset == 0:
                    opens_at = self._parse_hour_text(row.opens_at)
                    assert opens_at is not None
                    if opens_at <= local_now.time():
                        continue
                    return f"hoy a las {row.opens_at}"
                if offset == 1:
                    return f"mañana a las {row.opens_at}"
                weekday_name = WEEKDAY_LABELS_ES[candidate_day.weekday()]
                return f"el {weekday_name} a las {row.opens_at}"

        return "pronto"

    def _normalize_store_business_hours(
        self,
        hours: list[StoreBusinessHoursSnapshot],
        *,
        store_id: int,
    ) -> list[StoreBusinessHoursSnapshot]:
        """Normalize and validate staff-defined business hours before persisting them."""
        rows_by_day: dict[int, list[StoreBusinessHoursSnapshot]] = {weekday: [] for weekday in range(7)}
        for row in sorted(hours, key=lambda item: (item.weekday, item.slot_index)):
            if row.closed:
                continue
            if row.opens_at is None and row.closes_at is None:
                continue
            if row.opens_at is None or row.closes_at is None:
                raise ValueError(STORE_HOURS_SLOT_REQUIRES_BOTH_TIMES_MESSAGE)

            opens_at = self._parse_hour_text(row.opens_at)
            closes_at = self._parse_hour_text(row.closes_at)
            assert opens_at is not None
            assert closes_at is not None
            if opens_at >= closes_at:
                raise ValueError(STORE_HOURS_SLOT_ORDER_MESSAGE)

            rows_by_day[row.weekday].append(
                StoreBusinessHoursSnapshot(
                    id=0,
                    store_id=store_id,
                    weekday=row.weekday,
                    slot_index=row.slot_index,
                    opens_at=row.opens_at,
                    closes_at=row.closes_at,
                    closed=False,
                )
            )

        normalized_hours: list[StoreBusinessHoursSnapshot] = []
        for weekday in range(7):
            day_rows = sorted(
                rows_by_day[weekday],
                key=lambda item: (
                    self._parse_hour_text(item.opens_at) or time.min,
                    self._parse_hour_text(item.closes_at) or time.min,
                ),
            )
            if not day_rows:
                normalized_hours.append(
                    StoreBusinessHoursSnapshot(
                        id=0,
                        store_id=store_id,
                        weekday=weekday,
                        slot_index=0,
                        opens_at=None,
                        closes_at=None,
                        closed=True,
                    )
                )
                continue

            previous_close: time | None = None
            for slot_index, row in enumerate(day_rows):
                opens_at = self._parse_hour_text(row.opens_at)
                closes_at = self._parse_hour_text(row.closes_at)
                assert opens_at is not None
                assert closes_at is not None
                if previous_close is not None and opens_at < previous_close:
                    raise ValueError(STORE_HOURS_SLOT_OVERLAP_MESSAGE)
                previous_close = closes_at
                normalized_hours.append(
                    StoreBusinessHoursSnapshot(
                        id=0,
                        store_id=store_id,
                        weekday=weekday,
                        slot_index=slot_index,
                        opens_at=row.opens_at,
                        closes_at=row.closes_at,
                        closed=False,
                    )
                )

        return normalized_hours

    def _parse_hour_text(self, value: str | None) -> time | None:
        """Parse a `HH:MM` string into a time object."""
        if value is None:
            return None
        hour_text, minute_text = value.split(":")
        return time(hour=int(hour_text), minute=int(minute_text))

    def _normalize_optional_text(self, value: str | None) -> str | None:
        """Normalize optional text fields by trimming and collapsing empties."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None
