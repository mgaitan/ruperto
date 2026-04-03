"""Persistence helpers for the ordering MVP."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic_ai import ModelMessage
from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ruperto.models import (
    Channel,
    Conversation,
    ConversationMessage,
    Customer,
    CustomerIdentity,
    DeliveryType,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    StoreBusinessHours,
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
    StoreAvailabilitySnapshot,
    StoreBusinessHoursSnapshot,
    StoreProfileSnapshot,
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
WEEKDAY_LABELS_ES = {
    0: "lunes",
    1: "martes",
    2: "miércoles",
    3: "jueves",
    4: "viernes",
    5: "sábado",
    6: "domingo",
}
WEEKDAY_LABELS_ES = {
    0: "lunes",
    1: "martes",
    2: "miércoles",
    3: "jueves",
    4: "viernes",
    5: "sábado",
    6: "domingo",
}


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


class BusinessRepository:
    """Repository facade covering the core MVP entities."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_store_profile(self) -> StoreProfileSnapshot:
        """Return the single store profile used by the MVP."""
        row = await self.session.scalar(select(StoreProfile).where(StoreProfile.id == 1))
        assert row is not None
        return StoreProfileSnapshot.model_validate(row)

    async def list_store_business_hours(self, *, store_id: int = 1) -> list[StoreBusinessHoursSnapshot]:
        """Return the weekly opening-hours schedule for the store."""
        rows = (
            await self.session.scalars(
                select(StoreBusinessHours)
                .where(StoreBusinessHours.store_id == store_id)
                .order_by(StoreBusinessHours.weekday.asc())
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
                    opens_at=self._parse_hour_text(row.opens_at),
                    closes_at=self._parse_hour_text(row.closes_at),
                    closed=row.closed,
                )
                for row in hours
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
        today = next((row for row in schedule if row.weekday == local_now.weekday()), None)
        current_time = local_now.time().replace(second=0, microsecond=0)

        if self._is_open_at(today, current_time):
            close_text = f" hasta las {today.closes_at}" if today is not None and today.closes_at is not None else ""
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
        order.status = status
        order.updated_at = utc_now()
        await self.session.flush()
        return await self._build_order_snapshot(order)

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

    async def confirm_current_order(self, customer_id: int, conversation_id: int) -> OrderSnapshot:
        """Confirm the active order if it already contains items."""
        order = await self._get_draft_order(customer_id, conversation_id)
        if order is None:
            raise NoOpenOrderError

        items = await self._load_order_items(order.id)
        if not items:
            raise EmptyOrderError

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
                    estimated_minutes=estimated_minutes,
                    display_text=f"{estimated_minutes} minutos aproximadamente",
                )
            active_orders_ahead = await self.count_active_orders_ahead_by_order_id(latest_order.id)
            return self._estimate_delay_from_snapshot(latest_order, active_orders_ahead=active_orders_ahead)

        snapshot = await self._build_order_snapshot(order)
        active_orders_ahead = await self.count_active_orders_ahead_by_order_id(order.id)
        return self._estimate_delay_from_snapshot(snapshot, active_orders_ahead=active_orders_ahead)

    async def count_active_orders(self) -> int:
        """Count currently active non-draft orders in the kitchen pipeline."""
        result = await self.session.scalar(select(func.count(Order.id)).where(Order.status.in_(ACTIVE_ORDER_STATUSES)))
        return int(result or 0)

    async def count_active_orders_ahead_by_order_id(self, order_id: int) -> int:
        """Count active orders created before the given order."""
        current_order = await self.session.get(Order, order_id)
        assert current_order is not None
        result = await self.session.scalar(
            select(func.count(Order.id)).where(
                Order.id != current_order.id,
                Order.status.in_(ACTIVE_ORDER_STATUSES),
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
        order.updated_at = utc_now()
        await self.session.flush()

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

    def _estimate_delay_from_snapshot(
        self,
        order: OrderSnapshot,
        *,
        active_orders_ahead: int,
    ) -> DelayEstimateSnapshot:
        """Compute an MVP delay estimate based on preparation time and kitchen load."""
        base_minutes = self._estimate_preparation_minutes(order)
        estimated_minutes = base_minutes + (active_orders_ahead * KITCHEN_LOAD_COEFFICIENT_MINUTES)

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

    def _is_open_at(self, row: StoreBusinessHoursSnapshot | None, current_time: time) -> bool:
        """Return whether one schedule row covers the current time."""
        if row is None or row.closed or row.opens_at is None or row.closes_at is None:
            return False
        opens_at = self._parse_hour_text(row.opens_at)
        closes_at = self._parse_hour_text(row.closes_at)
        assert opens_at is not None
        assert closes_at is not None
        return opens_at <= current_time < closes_at

    def _find_next_opening_text(self, schedule: list[StoreBusinessHoursSnapshot], local_now: datetime) -> str:
        """Find the next opening slot as a short Spanish phrase."""
        for offset in range(8):
            candidate_day = local_now + timedelta(days=offset)
            row = next((item for item in schedule if item.weekday == candidate_day.weekday()), None)
            if row is None or row.closed or row.opens_at is None:
                continue

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

    def _parse_hour_text(self, value: str | None) -> time | None:
        """Parse a `HH:MM` string into a time object."""
        if value is None:
            return None
        hour_text, minute_text = value.split(":")
        return time(hour=int(hour_text), minute=int(minute_text))
