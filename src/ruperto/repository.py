"""Persistence helpers for the ordering MVP."""

from __future__ import annotations

import json
import re
from collections import Counter

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
    StoreProfile,
    utc_now,
)
from ruperto.schemas import (
    CustomerMemorySnapshot,
    CustomerSnapshot,
    MenuItemSnapshot,
    OrderItemSnapshot,
    OrderSnapshot,
    StoreProfileSnapshot,
    format_price_ars,
)


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


class BusinessRepository:
    """Repository facade covering the core MVP entities."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_store_profile(self) -> StoreProfileSnapshot:
        """Return the single store profile used by the MVP."""
        row = await self.session.scalar(select(StoreProfile).where(StoreProfile.id == 1))
        assert row is not None
        return StoreProfileSnapshot.model_validate(row)

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
