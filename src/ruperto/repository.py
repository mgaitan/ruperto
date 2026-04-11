"""Persistence helpers shared by the platform and its vertical domains."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from pydantic_ai import ModelMessage
from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ruperto.auth import hash_password, normalize_email, verify_password
from ruperto.models import (
    Channel,
    ChannelProvider,
    Conversation,
    ConversationMessage,
    ConversationState,
    Customer,
    CustomerIdentity,
    DeliveryType,
    MenuItem,
    MunicipalArea,
    MunicipalCase,
    MunicipalCaseDraft,
    MunicipalCaseStatus,
    MunicipalCategory,
    MunicipalRequestKind,
    Order,
    OrderItem,
    OrderStatus,
    OutboundNotification,
    PaymentMethod,
    StaffRole,
    StaffUser,
    StoreBusinessHours,
    StoreChannelConnection,
    StoreMembership,
    StoreProfile,
    StoreVertical,
    utc_now,
)
from ruperto.schemas import (
    ConversationHandoffSnapshot,
    ConversationTargetSnapshot,
    CustomerMemorySnapshot,
    CustomerSnapshot,
    DelayEstimateSnapshot,
    MenuItemSnapshot,
    MunicipalAreaCreateRequest,
    MunicipalAreaSnapshot,
    MunicipalCaseCreateRequest,
    MunicipalCaseDraftSnapshot,
    MunicipalCaseSnapshot,
    MunicipalCaseStatusUpdateRequest,
    MunicipalCategoryCreateRequest,
    MunicipalCategorySnapshot,
    OrderItemSnapshot,
    OrderSnapshot,
    OutboundNotificationSnapshot,
    StaffUserSnapshot,
    StoreAvailabilitySnapshot,
    StoreBusinessHoursSnapshot,
    StoreChannelConnectionRuntimeConfig,
    StoreChannelConnectionSnapshot,
    StoreChannelConnectionUpdateRequest,
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
MUNICIPAL_NOTIFICATION_EVENT_BY_STATUS = {
    MunicipalCaseStatus.TRIAGED: "municipal_case_triaged",
    MunicipalCaseStatus.IN_PROGRESS: "municipal_case_in_progress",
    MunicipalCaseStatus.BLOCKED: "municipal_case_blocked",
    MunicipalCaseStatus.RESOLVED: "municipal_case_resolved",
    MunicipalCaseStatus.CLOSED: "municipal_case_closed",
    MunicipalCaseStatus.CANCELLED: "municipal_case_cancelled",
}
STORE_MEMBERSHIP_NOT_FOUND_MESSAGE = "Store membership not found."
STORE_HOURS_SLOT_REQUIRES_BOTH_TIMES_MESSAGE = "Each business-hours slot requires both open and close times."
STORE_HOURS_SLOT_ORDER_MESSAGE = "Business-hours slots must close after they open."
STORE_HOURS_SLOT_OVERLAP_MESSAGE = "Business-hours slots cannot overlap on the same day."
UNSET = object()
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


class MunicipalAreaNotFoundError(ValueError):
    """Raised when the requested municipal area does not exist."""

    def __init__(self) -> None:
        super().__init__("No se encontró el área municipal.")


class MunicipalCategoryNotFoundError(ValueError):
    """Raised when the requested municipal category does not exist."""

    def __init__(self) -> None:
        super().__init__("No se encontró la categoría municipal.")


class MunicipalCaseNotFoundError(ValueError):
    """Raised when the requested municipal case does not exist."""

    def __init__(self) -> None:
        super().__init__("No se encontró el caso municipal.")


class MunicipalCategoryMismatchError(ValueError):
    """Raised when a category does not belong to the selected area."""

    def __init__(self) -> None:
        super().__init__("La categoría no pertenece al área municipal indicada.")


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

    async def get_store_profile_by_slug(self, slug: str) -> StoreProfileSnapshot | None:
        """Return one store profile by public slug when it exists."""
        row = await self.session.scalar(select(StoreProfile).where(StoreProfile.slug == slug.strip().lower()))
        if row is None:
            return None
        return StoreProfileSnapshot.model_validate(row)

    async def create_store_profile(  # noqa: PLR0913
        self,
        *,
        store_name: str,
        bot_name: str,
        store_description: str,
        assistant_personality: str,
        vertical: StoreVertical = StoreVertical.ORDERING,
        slug: str | None = None,
        store_location: str | None = None,
        locale: str = "es-AR",
        transfer_alias: str | None = None,
    ) -> StoreProfileSnapshot:
        """Create a new store profile for staff membership tests and future tenancy."""
        resolved_slug = await self._build_unique_store_slug(slug or store_name)
        row = StoreProfile(
            slug=resolved_slug,
            store_name=store_name.strip(),
            bot_name=bot_name.strip(),
            store_location=self._normalize_optional_text(store_location),
            store_description=store_description.strip(),
            assistant_personality=assistant_personality.strip(),
            vertical=vertical,
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

    async def get_store_channel_connection(
        self,
        *,
        store_id: int,
        channel: Channel,
        provider: ChannelProvider = ChannelProvider.KAPSO,
    ) -> StoreChannelConnectionSnapshot:
        """Return the safe channel-connection snapshot for one store."""
        row = await self.session.scalar(
            select(StoreChannelConnection).where(
                StoreChannelConnection.store_id == store_id,
                StoreChannelConnection.channel == channel,
                StoreChannelConnection.provider == provider,
            )
        )
        if row is None:
            return StoreChannelConnectionSnapshot(
                store_id=store_id,
                channel=channel,
                provider=provider,
            )
        return StoreChannelConnectionSnapshot(
            id=row.id,
            store_id=row.store_id,
            channel=row.channel,
            provider=row.provider,
            phone_number_id=row.phone_number_id,
            is_active=row.is_active,
            api_key_configured=bool(row.api_key),
            webhook_secret_configured=bool(row.webhook_secret),
        )

    async def get_store_channel_runtime_config(
        self,
        *,
        store_id: int,
        channel: Channel,
        provider: ChannelProvider = ChannelProvider.KAPSO,
    ) -> StoreChannelConnectionRuntimeConfig | None:
        """Return one active store-scoped channel connection with runtime credentials."""
        row = await self.session.scalar(
            select(StoreChannelConnection).where(
                StoreChannelConnection.store_id == store_id,
                StoreChannelConnection.channel == channel,
                StoreChannelConnection.provider == provider,
                StoreChannelConnection.is_active.is_(True),
            )
        )
        if row is None or not row.phone_number_id or not row.api_key:
            return None
        return StoreChannelConnectionRuntimeConfig(
            id=row.id,
            store_id=row.store_id,
            channel=row.channel,
            provider=row.provider,
            phone_number_id=row.phone_number_id,
            api_key=row.api_key,
            webhook_secret=row.webhook_secret,
            is_active=row.is_active,
        )

    async def get_channel_runtime_config_by_phone_number(
        self,
        *,
        channel: Channel,
        provider: ChannelProvider,
        phone_number_id: str,
    ) -> StoreChannelConnectionRuntimeConfig | None:
        """Resolve one active connection by the provider phone-number identifier."""
        row = await self.session.scalar(
            select(StoreChannelConnection).where(
                StoreChannelConnection.channel == channel,
                StoreChannelConnection.provider == provider,
                StoreChannelConnection.phone_number_id == phone_number_id,
                StoreChannelConnection.is_active.is_(True),
            )
        )
        if row is None or not row.phone_number_id or not row.api_key:
            return None
        return StoreChannelConnectionRuntimeConfig(
            id=row.id,
            store_id=row.store_id,
            channel=row.channel,
            provider=row.provider,
            phone_number_id=row.phone_number_id,
            api_key=row.api_key,
            webhook_secret=row.webhook_secret,
            is_active=row.is_active,
        )

    async def update_store_channel_connection(
        self,
        *,
        store_id: int,
        channel: Channel,
        provider: ChannelProvider = ChannelProvider.KAPSO,
        payload: StoreChannelConnectionUpdateRequest,
    ) -> StoreChannelConnectionSnapshot:
        """Create or update one store-scoped channel connection."""
        row = await self.session.scalar(
            select(StoreChannelConnection).where(
                StoreChannelConnection.store_id == store_id,
                StoreChannelConnection.channel == channel,
                StoreChannelConnection.provider == provider,
            )
        )
        if row is None:
            row = StoreChannelConnection(store_id=store_id, channel=channel, provider=provider)
            self.session.add(row)
            await self.session.flush()

        normalized_phone_number_id = self._normalize_optional_text(payload.phone_number_id)
        row.phone_number_id = normalized_phone_number_id
        if payload.api_key is not None and payload.api_key.strip():
            row.api_key = payload.api_key.strip()
        if payload.webhook_secret is not None and payload.webhook_secret.strip():
            row.webhook_secret = payload.webhook_secret.strip()
        row.is_active = payload.is_active and bool(row.phone_number_id and row.api_key)
        row.updated_at = utc_now()
        await self.session.flush()
        return await self.get_store_channel_connection(store_id=store_id, channel=channel, provider=provider)

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
        store_id: int = 1,
        phone_number: str | None = None,
    ) -> CustomerSnapshot:
        """Resolve an existing customer or create one for the given identity."""
        normalized_phone = normalize_phone_number(phone_number)
        identity = await self.session.scalar(
            select(CustomerIdentity).where(
                CustomerIdentity.store_id == store_id,
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
            customer = await self.session.scalar(
                select(Customer).where(Customer.store_id == store_id, Customer.phone_number == normalized_phone)
            )

        if customer is None:
            customer = Customer(store_id=store_id, phone_number=normalized_phone)
            self.session.add(customer)
            await self.session.flush()

        self.session.add(
            CustomerIdentity(
                store_id=store_id,
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

    async def list_customers(self, *, limit: int = 50, store_id: int = 1) -> list[CustomerSnapshot]:
        """List customers ordered by recent activity."""
        rows = (
            await self.session.scalars(
                select(Customer)
                .where(Customer.store_id == store_id)
                .order_by(Customer.updated_at.desc(), Customer.id.desc())
                .limit(limit)
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
        store_id: int = 1,
    ) -> Conversation:
        """Return the persisted conversation for one external identity."""
        conversation = await self.session.scalar(
            select(Conversation).where(
                Conversation.store_id == store_id,
                Conversation.channel == channel,
                Conversation.external_id == external_id,
            )
        )
        if conversation is None:
            conversation = Conversation(
                channel=channel,
                external_id=external_id,
                store_id=store_id,
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

    async def conversation_is_awaiting_human(self, conversation_id: int) -> bool:
        """Return whether one conversation is currently paused for human handoff."""
        state = await self._get_or_create_conversation_state(conversation_id)
        return state.awaiting_human

    async def activate_conversation_handoff(
        self,
        *,
        conversation_id: int,
        reason: str | None,
        latest_customer_message: str | None,
    ) -> bool:
        """Mark one conversation as waiting for a human operator."""
        state = await self._get_or_create_conversation_state(conversation_id)
        now = utc_now()
        was_inactive = not state.awaiting_human
        state.awaiting_human = True
        state.handoff_reason = reason.strip() if reason else None
        if state.handoff_requested_at is None:
            state.handoff_requested_at = now
        if latest_customer_message:
            state.handoff_latest_customer_message = latest_customer_message.strip()
            state.handoff_last_customer_message_at = now
        state.updated_at = now
        await self.session.flush()
        return was_inactive

    async def record_handoff_customer_message(self, conversation_id: int, message_text: str) -> None:
        """Update the latest customer message while a conversation waits for a human."""
        state = await self._get_or_create_conversation_state(conversation_id)
        now = utc_now()
        state.handoff_latest_customer_message = message_text.strip()
        state.handoff_last_customer_message_at = now
        state.updated_at = now
        await self.session.flush()

    async def mark_handoff_operator_reply(self, conversation_id: int) -> None:
        """Record that a human operator sent one reply during the handoff."""
        state = await self._get_or_create_conversation_state(conversation_id)
        now = utc_now()
        state.handoff_last_operator_reply_at = now
        state.updated_at = now
        await self.session.flush()

    async def release_conversation_handoff(self, conversation_id: int) -> bool:
        """Return one conversation from human handoff mode back to the bot."""
        state = await self._get_or_create_conversation_state(conversation_id)
        if not state.awaiting_human:
            return False
        state.awaiting_human = False
        state.handoff_reason = None
        state.handoff_requested_at = None
        state.handoff_latest_customer_message = None
        state.handoff_last_customer_message_at = None
        state.handoff_last_operator_reply_at = None
        state.updated_at = utc_now()
        await self.session.flush()
        return True

    async def list_active_conversation_handoffs(self, *, store_id: int) -> list[ConversationHandoffSnapshot]:
        """List conversations currently waiting for a human in one store."""
        rows = (
            await self.session.execute(
                select(Conversation, ConversationState, Customer)
                .join(ConversationState, ConversationState.conversation_id == Conversation.id)
                .join(Customer, Customer.id == Conversation.customer_id)
                .where(
                    Conversation.store_id == store_id,
                    Conversation.channel == Channel.WHATSAPP,
                    ConversationState.awaiting_human.is_(True),
                )
                .order_by(
                    ConversationState.handoff_requested_at.desc().nullslast(),
                    Conversation.updated_at.desc(),
                    Conversation.id.desc(),
                )
            )
        ).all()
        return [
            ConversationHandoffSnapshot(
                conversation_id=conversation.id,
                store_id=conversation.store_id,
                channel=conversation.channel,
                external_id=conversation.external_id,
                customer_id=customer.id,
                customer_name=customer.name,
                customer_phone_number=customer.phone_number,
                handoff_reason=state.handoff_reason,
                latest_customer_message=state.handoff_latest_customer_message,
                requested_at=state.handoff_requested_at,
                last_customer_message_at=state.handoff_last_customer_message_at,
                last_operator_reply_at=state.handoff_last_operator_reply_at,
            )
            for conversation, state, customer in rows
        ]

    async def get_conversation_target(
        self,
        *,
        conversation_id: int,
        store_id: int,
    ) -> ConversationTargetSnapshot | None:
        """Return the outbound target for one conversation inside the given store."""
        target = await self.session.execute(
            select(Conversation.id, Conversation.channel, Conversation.store_id, Conversation.external_id).where(
                Conversation.id == conversation_id,
                Conversation.store_id == store_id,
            )
        )
        row = target.one_or_none()
        if row is None:
            return None
        resolved_conversation_id, channel, resolved_store_id, external_id = row
        return ConversationTargetSnapshot(
            conversation_id=resolved_conversation_id,
            channel=channel,
            store_id=resolved_store_id,
            external_id=external_id,
        )

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

    async def create_municipal_area(
        self,
        *,
        store_id: int,
        payload: MunicipalAreaCreateRequest,
    ) -> MunicipalAreaSnapshot:
        """Create one municipal area for a tenant."""
        row = MunicipalArea(
            store_id=store_id,
            name=payload.name.strip(),
            description=self._normalize_optional_text(payload.description),
            manager_staff_user_id=payload.manager_staff_user_id,
            display_order=payload.display_order,
            is_active=payload.is_active,
        )
        self.session.add(row)
        await self.session.flush()
        return self._municipal_area_snapshot(row)

    async def list_municipal_areas(
        self,
        *,
        store_id: int,
        only_active: bool = False,
    ) -> list[MunicipalAreaSnapshot]:
        """List municipal areas configured for one tenant."""
        statement = (
            select(MunicipalArea)
            .where(MunicipalArea.store_id == store_id)
            .order_by(MunicipalArea.display_order.asc(), MunicipalArea.name.asc(), MunicipalArea.id.asc())
        )
        if only_active:
            statement = statement.where(MunicipalArea.is_active.is_(True))
        rows = (await self.session.scalars(statement)).all()
        return [self._municipal_area_snapshot(row) for row in rows]

    async def create_municipal_category(
        self,
        *,
        area_id: int,
        payload: MunicipalCategoryCreateRequest,
    ) -> MunicipalCategorySnapshot:
        """Create one municipal category under a concrete area."""
        area = await self.session.get(MunicipalArea, area_id)
        if area is None:
            raise MunicipalAreaNotFoundError
        row = MunicipalCategory(
            area_id=area_id,
            name=payload.name.strip(),
            description=self._normalize_optional_text(payload.description),
            request_kind=payload.request_kind,
            requires_precise_location=payload.requires_precise_location,
            is_fallback=payload.is_fallback,
            display_order=payload.display_order,
            is_active=payload.is_active,
        )
        self.session.add(row)
        await self.session.flush()
        return self._municipal_category_snapshot(row)

    async def list_municipal_categories(
        self,
        *,
        store_id: int,
        area_id: int | None = None,
        only_active: bool = False,
    ) -> list[MunicipalCategorySnapshot]:
        """List municipal categories for one tenant, optionally scoped to one area."""
        statement = (
            select(MunicipalCategory)
            .join(MunicipalArea, MunicipalArea.id == MunicipalCategory.area_id)
            .where(MunicipalArea.store_id == store_id)
            .order_by(
                MunicipalArea.display_order.asc(),
                MunicipalCategory.display_order.asc(),
                MunicipalCategory.name.asc(),
                MunicipalCategory.id.asc(),
            )
        )
        if area_id is not None:
            statement = statement.where(MunicipalCategory.area_id == area_id)
        if only_active:
            statement = statement.where(MunicipalCategory.is_active.is_(True), MunicipalArea.is_active.is_(True))
        rows = (await self.session.scalars(statement)).all()
        return [self._municipal_category_snapshot(row) for row in rows]

    async def get_municipal_case_draft(
        self,
        *,
        conversation_id: int,
        create_if_missing: bool = False,
        store_id: int | None = None,
        customer_id: int | None = None,
    ) -> MunicipalCaseDraftSnapshot | None:
        """Return the intake draft for one municipal conversation."""
        row = await self.session.scalar(
            select(MunicipalCaseDraft).where(MunicipalCaseDraft.conversation_id == conversation_id)
        )
        if row is None and create_if_missing:
            assert store_id is not None
            assert customer_id is not None
            row = MunicipalCaseDraft(
                conversation_id=conversation_id,
                store_id=store_id,
                customer_id=customer_id,
            )
            self.session.add(row)
            await self.session.flush()
        if row is None:
            return None
        return self._municipal_case_draft_snapshot(row)

    async def update_municipal_case_draft(  # noqa: PLR0913
        self,
        *,
        conversation_id: int,
        store_id: int,
        customer_id: int,
        area_id: object = UNSET,
        category_id: object = UNSET,
        request_summary: object = UNSET,
        location_text: object = UNSET,
        location_reference: object = UNSET,
        latitude: object = UNSET,
        longitude: object = UNSET,
        awaiting_confirmation: bool | None = None,
    ) -> MunicipalCaseDraftSnapshot:
        """Persist mutable municipal intake state for one conversation."""
        row = await self.session.scalar(
            select(MunicipalCaseDraft).where(MunicipalCaseDraft.conversation_id == conversation_id)
        )
        if row is None:
            row = MunicipalCaseDraft(
                conversation_id=conversation_id,
                store_id=store_id,
                customer_id=customer_id,
            )
            self.session.add(row)
            await self.session.flush()

        row.store_id = store_id
        row.customer_id = customer_id
        if area_id is not UNSET:
            row.area_id = cast("int | None", area_id)
        if category_id is not UNSET:
            row.category_id = cast("int | None", category_id)
        if request_summary is not UNSET:
            row.request_summary = self._normalize_optional_text(cast("str | None", request_summary))
        if location_text is not UNSET:
            row.location_text = self._normalize_optional_text(cast("str | None", location_text))
        if location_reference is not UNSET:
            row.location_reference = self._normalize_optional_text(cast("str | None", location_reference))
        if latitude is not UNSET:
            row.latitude = cast("float | None", latitude)
        if longitude is not UNSET:
            row.longitude = cast("float | None", longitude)
        if awaiting_confirmation is not None:
            row.awaiting_confirmation = awaiting_confirmation
        row.updated_at = utc_now()
        await self.session.flush()
        return self._municipal_case_draft_snapshot(row)

    async def clear_municipal_case_draft(self, *, conversation_id: int) -> bool:
        """Delete the municipal intake draft for one conversation when it exists."""
        row = await self.session.scalar(
            select(MunicipalCaseDraft).where(MunicipalCaseDraft.conversation_id == conversation_id)
        )
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True

    async def create_municipal_case(
        self,
        *,
        store_id: int,
        payload: MunicipalCaseCreateRequest,
    ) -> MunicipalCaseSnapshot:
        """Create one municipal service request tied to a tenant area."""
        area = await self.session.get(MunicipalArea, payload.area_id)
        if area is None or area.store_id != store_id:
            raise MunicipalAreaNotFoundError

        category_id = payload.category_id
        if category_id is not None:
            category = await self.session.get(MunicipalCategory, category_id)
            if category is None:
                raise MunicipalCategoryNotFoundError
            if category.area_id != area.id:
                raise MunicipalCategoryMismatchError
        else:
            category = await self.session.scalar(
                select(MunicipalCategory)
                .where(
                    MunicipalCategory.area_id == area.id,
                    MunicipalCategory.is_fallback.is_(True),
                )
                .order_by(MunicipalCategory.display_order.asc(), MunicipalCategory.id.asc())
            )
            category_id = category.id if category is not None else None

        row = MunicipalCase(
            store_id=store_id,
            area_id=area.id,
            category_id=category_id,
            customer_id=payload.customer_id,
            conversation_id=payload.conversation_id,
            assignee_staff_user_id=payload.assignee_staff_user_id,
            title=payload.title.strip(),
            description=payload.description.strip(),
            reporter_name=self._normalize_optional_text(payload.reporter_name),
            reporter_phone_number=normalize_phone_number(payload.reporter_phone_number),
            location_text=self._normalize_optional_text(payload.location_text),
            location_reference=self._normalize_optional_text(payload.location_reference),
            latitude=payload.latitude,
            longitude=payload.longitude,
        )
        self.session.add(row)
        await self.session.flush()
        return self._municipal_case_snapshot(row)

    async def create_municipal_case_from_draft(self, *, conversation_id: int) -> MunicipalCaseSnapshot:
        """Promote one municipal intake draft into a persisted case."""
        draft = await self.session.scalar(
            select(MunicipalCaseDraft).where(MunicipalCaseDraft.conversation_id == conversation_id)
        )
        if draft is None:
            raise MunicipalCaseNotFoundError
        customer = await self.session.get(Customer, draft.customer_id)
        assert draft.area_id is not None
        assert draft.request_summary is not None
        case = await self.create_municipal_case(
            store_id=draft.store_id,
            payload=MunicipalCaseCreateRequest(
                area_id=draft.area_id,
                category_id=draft.category_id,
                customer_id=draft.customer_id,
                conversation_id=draft.conversation_id,
                title=self._build_municipal_case_title(draft.request_summary),
                description=draft.request_summary,
                reporter_name=customer.name if customer is not None else None,
                reporter_phone_number=customer.phone_number if customer is not None else None,
                location_text=draft.location_text,
                location_reference=draft.location_reference,
                latitude=draft.latitude,
                longitude=draft.longitude,
            ),
        )
        await self.session.delete(draft)
        await self.session.flush()
        return case

    async def get_municipal_case(self, case_id: int) -> MunicipalCaseSnapshot:
        """Return one municipal case snapshot by identifier."""
        row = await self.session.get(MunicipalCase, case_id)
        if row is None:
            raise MunicipalCaseNotFoundError
        return self._municipal_case_snapshot(row)

    async def list_municipal_cases(
        self,
        *,
        store_id: int,
        area_id: int | None = None,
        status: MunicipalCaseStatus | None = None,
        assignee_staff_user_id: int | None = None,
        limit: int = 50,
    ) -> list[MunicipalCaseSnapshot]:
        """List municipal cases filtered by tenant, area, status, or assignee."""
        statement = (
            select(MunicipalCase)
            .where(MunicipalCase.store_id == store_id)
            .order_by(MunicipalCase.updated_at.desc(), MunicipalCase.id.desc())
            .limit(limit)
        )
        if area_id is not None:
            statement = statement.where(MunicipalCase.area_id == area_id)
        if status is not None:
            statement = statement.where(MunicipalCase.status == status)
        if assignee_staff_user_id is not None:
            statement = statement.where(MunicipalCase.assignee_staff_user_id == assignee_staff_user_id)
        rows = (await self.session.scalars(statement)).all()
        return [self._municipal_case_snapshot(row) for row in rows]

    async def update_municipal_case_status(
        self,
        case_id: int,
        payload: MunicipalCaseStatusUpdateRequest,
    ) -> MunicipalCaseSnapshot:
        """Update the lifecycle status of one municipal case."""
        row = await self.session.get(MunicipalCase, case_id)
        if row is None:
            raise MunicipalCaseNotFoundError
        previous_status = row.status
        row.status = payload.status
        row.updated_at = utc_now()
        await self.session.flush()
        await self._queue_municipal_status_notification_if_needed(row, previous_status=previous_status)
        return self._municipal_case_snapshot(row)

    async def assign_municipal_case(
        self,
        case_id: int,
        *,
        staff_user_id: int | None,
    ) -> MunicipalCaseSnapshot:
        """Assign or clear the current assignee of one municipal case."""
        row = await self.session.get(MunicipalCase, case_id)
        if row is None:
            raise MunicipalCaseNotFoundError
        row.assignee_staff_user_id = staff_user_id
        row.updated_at = utc_now()
        await self.session.flush()
        return self._municipal_case_snapshot(row)

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

    async def discard_empty_draft_order(self, customer_id: int, conversation_id: int) -> bool:
        """Delete the current draft if it exists but has no line items."""
        order = await self._get_draft_order(customer_id, conversation_id)
        if order is None:
            return False
        item_count = await self.session.scalar(select(func.count(OrderItem.id)).where(OrderItem.order_id == order.id))
        if item_count:
            return False
        await self.session.delete(order)
        await self.session.flush()
        return True

    async def list_orders(
        self,
        *,
        limit: int = 50,
        status: OrderStatus | None = None,
        store_id: int | None = None,
    ) -> list[OrderSnapshot]:
        """List orders ordered by recent activity."""
        statement = select(Order).order_by(Order.updated_at.desc(), Order.id.desc()).limit(limit)
        if status is not None:
            statement = statement.where(Order.status == status)
        if store_id is not None:
            statement = statement.join(Conversation, Conversation.id == Order.conversation_id).where(
                Conversation.store_id == store_id
            )
        orders = (await self.session.scalars(statement)).all()
        return [await self._build_order_snapshot(order) for order in orders]

    async def get_order(self, order_id: int) -> OrderSnapshot:
        """Return one order snapshot by identifier."""
        order = await self.session.get(Order, order_id)
        if order is None:
            raise OrderNotFoundError
        return await self._build_order_snapshot(order)

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
        snapshots = await self.peek_pending_notifications(channel=channel, external_id=external_id)
        await self.mark_notifications_delivered([snapshot.id for snapshot in snapshots])
        return snapshots

    async def peek_pending_notifications(
        self,
        *,
        channel: Channel,
        external_id: str,
    ) -> list[OutboundNotificationSnapshot]:
        """Return undelivered notifications for one conversation without consuming them."""
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
        return [
            OutboundNotificationSnapshot(
                id=row.id,
                order_id=row.order_id,
                municipal_case_id=row.municipal_case_id,
                conversation_id=row.conversation_id,
                event_type=row.event_type,
                message_text=row.message_text,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def mark_notifications_delivered(self, notification_ids: list[int]) -> None:
        """Mark the given outbound notifications as delivered."""
        if not notification_ids:
            return
        rows = (
            await self.session.scalars(
                select(OutboundNotification).where(OutboundNotification.id.in_(notification_ids))
            )
        ).all()
        delivered_at = utc_now()
        for row in rows:
            row.delivered_at = delivered_at
        await self.session.flush()

    async def get_order_conversation_target(self, order_id: int) -> ConversationTargetSnapshot | None:
        """Return the outbound conversation target for one order when available."""
        row = await self.session.execute(
            select(Conversation.id, Conversation.channel, Conversation.store_id, Conversation.external_id)
            .join(Order, Order.conversation_id == Conversation.id)
            .where(Order.id == order_id)
        )
        target = row.one_or_none()
        if target is None:
            return None
        conversation_id, channel, store_id, external_id = target
        return ConversationTargetSnapshot(
            conversation_id=conversation_id,
            channel=channel,
            store_id=store_id,
            external_id=external_id,
        )

    async def get_municipal_case_conversation_target(self, case_id: int) -> ConversationTargetSnapshot | None:
        """Return the outbound conversation target for one municipal case when available."""
        row = await self.session.execute(
            select(Conversation.id, Conversation.channel, Conversation.store_id, Conversation.external_id)
            .join(MunicipalCase, MunicipalCase.conversation_id == Conversation.id)
            .where(MunicipalCase.id == case_id)
        )
        target = row.one_or_none()
        if target is None:
            return None
        conversation_id, channel, store_id, external_id = target
        return ConversationTargetSnapshot(
            conversation_id=conversation_id,
            channel=channel,
            store_id=store_id,
            external_id=external_id,
        )

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

    def _municipal_area_snapshot(self, area: MunicipalArea) -> MunicipalAreaSnapshot:
        """Build a serializable municipal-area snapshot."""
        return MunicipalAreaSnapshot(
            id=area.id,
            store_id=area.store_id,
            name=area.name,
            description=area.description,
            manager_staff_user_id=area.manager_staff_user_id,
            display_order=area.display_order,
            is_active=area.is_active,
        )

    def _municipal_category_snapshot(self, category: MunicipalCategory) -> MunicipalCategorySnapshot:
        """Build a serializable municipal-category snapshot."""
        return MunicipalCategorySnapshot(
            id=category.id,
            area_id=category.area_id,
            name=category.name,
            description=category.description,
            request_kind=category.request_kind,
            requires_precise_location=category.requires_precise_location,
            is_fallback=category.is_fallback,
            display_order=category.display_order,
            is_active=category.is_active,
        )

    def _municipal_case_snapshot(self, case: MunicipalCase) -> MunicipalCaseSnapshot:
        """Build a serializable municipal-case snapshot."""
        return MunicipalCaseSnapshot(
            id=case.id,
            store_id=case.store_id,
            area_id=case.area_id,
            category_id=case.category_id,
            customer_id=case.customer_id,
            conversation_id=case.conversation_id,
            assignee_staff_user_id=case.assignee_staff_user_id,
            title=case.title,
            description=case.description,
            reporter_name=case.reporter_name,
            reporter_phone_number=case.reporter_phone_number,
            location_text=case.location_text,
            location_reference=case.location_reference,
            latitude=case.latitude,
            longitude=case.longitude,
            status=case.status,
            created_at=self._as_utc_datetime(case.created_at) or case.created_at,
            updated_at=self._as_utc_datetime(case.updated_at) or case.updated_at,
        )

    def _municipal_case_draft_snapshot(self, draft: MunicipalCaseDraft) -> MunicipalCaseDraftSnapshot:
        """Build a serializable municipal-case draft snapshot."""
        return MunicipalCaseDraftSnapshot(
            id=draft.id,
            conversation_id=draft.conversation_id,
            store_id=draft.store_id,
            customer_id=draft.customer_id,
            area_id=draft.area_id,
            category_id=draft.category_id,
            request_summary=draft.request_summary,
            location_text=draft.location_text,
            location_reference=draft.location_reference,
            latitude=draft.latitude,
            longitude=draft.longitude,
            awaiting_confirmation=draft.awaiting_confirmation,
            created_at=self._as_utc_datetime(draft.created_at) or draft.created_at,
            updated_at=self._as_utc_datetime(draft.updated_at) or draft.updated_at,
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
        requested_ready_at = self._as_utc_datetime(order.requested_ready_at)
        assert requested_ready_at is not None
        order.requested_ready_at = requested_ready_at

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
        order.preparation_starts_at = requested_ready_at - timedelta(minutes=preparation_minutes)

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
        requested_ready_at = self._as_utc_datetime(order.requested_ready_at)
        assert requested_ready_at is not None
        order.requested_ready_at = requested_ready_at

        preparation_starts_at = self._as_utc_datetime(order.preparation_starts_at)
        order.preparation_starts_at = preparation_starts_at

        zone = ZoneInfo(timezone_name)
        local_now = utc_now().astimezone(zone)
        local_ready = requested_ready_at.astimezone(zone)
        if local_ready <= local_now:
            raise RequestedReadyTimeError.past_time()

        schedule = await self.list_store_business_hours(store_id=store_id)
        schedule_row = self._find_matching_schedule_row(schedule, local_ready)
        if schedule_row is None:
            next_open_text = self._find_next_opening_text(schedule, local_now)
            raise RequestedReadyTimeError.outside_business_hours(next_open_text)

        if preparation_starts_at is None:
            return

        local_open = self._opening_datetime_for_row(schedule_row, local_ready)
        local_preparation_start = preparation_starts_at.astimezone(zone)
        earliest_start = max(local_now, local_open)
        if local_preparation_start < earliest_start:
            preparation_minutes = int((requested_ready_at - preparation_starts_at).total_seconds() // 60)
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
            requested_ready_at=self._as_utc_datetime(order.requested_ready_at),
            preparation_starts_at=self._as_utc_datetime(order.preparation_starts_at),
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

    async def _queue_municipal_status_notification_if_needed(
        self,
        case: MunicipalCase,
        *,
        previous_status: MunicipalCaseStatus,
    ) -> None:
        """Queue one outbound notification when a municipal case reaches a citizen-facing status."""
        if previous_status == case.status:
            return
        event_type = MUNICIPAL_NOTIFICATION_EVENT_BY_STATUS.get(case.status)
        if event_type is None:
            return
        existing = await self.session.scalar(
            select(OutboundNotification).where(
                OutboundNotification.municipal_case_id == case.id,
                OutboundNotification.event_type == event_type,
            )
        )
        if existing is not None:
            return
        if case.conversation_id is None:
            return
        category = None if case.category_id is None else await self.session.get(MunicipalCategory, case.category_id)
        self.session.add(
            OutboundNotification(
                order_id=None,
                municipal_case_id=case.id,
                conversation_id=case.conversation_id,
                event_type=event_type,
                message_text=self._build_municipal_status_notification_text(case, category=category),
            )
        )
        await self.session.flush()

    def _as_utc_datetime(self, value: datetime | None) -> datetime | None:
        """Normalize SQLite-loaded timestamps into aware UTC datetimes."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _build_status_notification_text(self, order: Order) -> str:
        """Build the outbound message shown when an order reaches a relevant status."""
        if order.status == OrderStatus.ALMOST_READY:
            return "Tu pedido ya casi está 👀"
        if order.status == OrderStatus.OUT_FOR_DELIVERY:
            return "Tu pedido ya salió y va en camino 🚚"
        return "Tu pedido ya está listo para retirar 🙌"

    def _build_municipal_status_notification_text(
        self,
        case: MunicipalCase,
        *,
        category: MunicipalCategory | None,
    ) -> str:
        """Build the outbound message shown when a municipal case reaches a citizen-facing status."""
        noun = "solicitud" if category and category.request_kind == MunicipalRequestKind.REQUEST else "reclamo"
        if case.status == MunicipalCaseStatus.TRIAGED:
            return f"Tu {noun} #{case.id} ya está en revisión 👀"
        if case.status == MunicipalCaseStatus.IN_PROGRESS:
            return f"Tu {noun} #{case.id} ya está en gestión 🛠️"
        if case.status == MunicipalCaseStatus.BLOCKED:
            return f"Tu {noun} #{case.id} quedó bloqueado por el momento."
        if case.status == MunicipalCaseStatus.RESOLVED:
            return f"Tu {noun} #{case.id} fue resuelto ✅"
        if case.status == MunicipalCaseStatus.CLOSED:
            return f"Tu {noun} #{case.id} fue cerrado."
        return f"Tu {noun} #{case.id} fue cancelado."

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

    def _build_municipal_case_title(self, request_summary: str) -> str:
        """Build a short municipal-case title from the stored summary."""
        return request_summary.strip()[:160]

    async def _build_unique_store_slug(self, source_text: str) -> str:
        """Generate a unique public slug for a tenant."""
        base_slug = self._slugify_text(source_text)
        candidate = base_slug
        suffix = 2
        while await self.session.scalar(select(StoreProfile.id).where(StoreProfile.slug == candidate)) is not None:
            candidate = f"{base_slug}-{suffix}"
            suffix += 1
        return candidate

    def _slugify_text(self, value: str) -> str:
        """Convert arbitrary text into a stable lowercase slug."""
        lowered = unicodedata.normalize("NFKD", value.strip()).encode("ascii", "ignore").decode("ascii").lower()
        slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
        return slug or "tenant"
