"""Bootstrap data for local development and the first MVP demos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True, slots=True)
class MenuSeed:
    """Static menu data used to bootstrap the local catalog."""

    sku: str
    name: str
    description: str
    category: str
    price_cents: int
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class BusinessHoursSeed:
    """Static business-hours data used to bootstrap one weekly schedule."""

    weekday: int
    opens_at: time | None
    closes_at: time | None
    closed: bool = False


DEMO_MENU_ITEMS: tuple[MenuSeed, ...] = (
    MenuSeed(
        sku="empanadas-carne",
        name="Empanadas de carne",
        description="Empanadas caseras al horno, ideales para compartir.",
        category="Empanadas",
        price_cents=180000,
    ),
    MenuSeed(
        sku="hamburguesa-completa",
        name="Hamburguesa completa",
        description="Hamburguesa con queso, lechuga, tomate y papas fritas.",
        category="Hamburguesas",
        price_cents=950000,
    ),
    MenuSeed(
        sku="milanesa-napolitana",
        name="Milanesa napolitana",
        description="Milanesa con salsa, jamón, queso y guarnición.",
        category="Platos",
        price_cents=1250000,
    ),
    MenuSeed(
        sku="pizza-muzzarella",
        name="Pizza muzzarella",
        description="Pizza clásica con salsa de tomate y muzzarella.",
        category="Pizzas",
        price_cents=1100000,
    ),
    MenuSeed(
        sku="sanguche-milanesa",
        name="Sanguche de milanesa",
        description="Pan fresco, milanesa, lechuga, tomate y mayonesa.",
        category="Sandwiches",
        price_cents=890000,
    ),
)


DEFAULT_BUSINESS_HOURS: tuple[BusinessHoursSeed, ...] = (
    BusinessHoursSeed(weekday=0, opens_at=time(hour=11), closes_at=time(hour=23)),
    BusinessHoursSeed(weekday=1, opens_at=time(hour=11), closes_at=time(hour=23)),
    BusinessHoursSeed(weekday=2, opens_at=time(hour=11), closes_at=time(hour=23)),
    BusinessHoursSeed(weekday=3, opens_at=time(hour=11), closes_at=time(hour=23)),
    BusinessHoursSeed(weekday=4, opens_at=time(hour=11), closes_at=time(hour=23)),
    BusinessHoursSeed(weekday=5, opens_at=time(hour=11), closes_at=time(hour=23)),
    BusinessHoursSeed(weekday=6, opens_at=time(hour=19), closes_at=time(hour=23)),
)
