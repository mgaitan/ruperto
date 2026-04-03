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
        category="Comidas",
        price_cents=180000,
    ),
    MenuSeed(
        sku="hamburguesa-completa",
        name="Hamburguesa completa",
        description="Hamburguesa con queso, lechuga, tomate y papas fritas.",
        category="Comidas",
        price_cents=950000,
    ),
    MenuSeed(
        sku="hamburguesa-doble",
        name="Hamburguesa doble cheddar",
        description="Doble medallón, cheddar, cebolla caramelizada y papas fritas.",
        category="Comidas",
        price_cents=1190000,
    ),
    MenuSeed(
        sku="hamburguesa-bbq",
        name="Hamburguesa BBQ",
        description="Hamburguesa con queso, panceta, salsa barbacoa y papas fritas.",
        category="Comidas",
        price_cents=1150000,
    ),
    MenuSeed(
        sku="milanesa-napolitana",
        name="Milanesa napolitana",
        description="Milanesa con salsa, jamón, queso y guarnición.",
        category="Comidas",
        price_cents=1250000,
    ),
    MenuSeed(
        sku="pizza-muzzarella",
        name="Pizza muzzarella",
        description="Pizza clásica con salsa de tomate y muzzarella.",
        category="Comidas",
        price_cents=1100000,
    ),
    MenuSeed(
        sku="pizza-napolitana",
        name="Pizza napolitana",
        description="Pizza con muzzarella, rodajas de tomate fresco, ajo y orégano.",
        category="Comidas",
        price_cents=1240000,
    ),
    MenuSeed(
        sku="pizza-fugazzeta",
        name="Pizza fugazzeta",
        description="Pizza con abundante muzzarella, cebolla y orégano.",
        category="Comidas",
        price_cents=1280000,
    ),
    MenuSeed(
        sku="pizza-especial",
        name="Pizza especial",
        description="Pizza con muzzarella, jamón, morrón y aceitunas.",
        category="Comidas",
        price_cents=1390000,
    ),
    MenuSeed(
        sku="sanguche-milanesa",
        name="Sanguche de milanesa",
        description="Pan fresco, milanesa, lechuga, tomate y mayonesa.",
        category="Comidas",
        price_cents=890000,
    ),
    MenuSeed(
        sku="gaseosa-cola-1l",
        name="Gaseosa cola 1.5L",
        description="Gaseosa cola bien fría, ideal para compartir.",
        category="Bebidas",
        price_cents=320000,
    ),
    MenuSeed(
        sku="agua-sin-gas-500",
        name="Agua sin gas 500ml",
        description="Agua mineral sin gas.",
        category="Bebidas",
        price_cents=180000,
    ),
    MenuSeed(
        sku="cerveza-rubia-lata",
        name="Cerveza rubia lata",
        description="Lata de cerveza rubia de 473ml.",
        category="Bebidas",
        price_cents=290000,
    ),
    MenuSeed(
        sku="flan-casero",
        name="Flan casero",
        description="Flan casero con dulce de leche y crema.",
        category="Postres",
        price_cents=350000,
    ),
    MenuSeed(
        sku="helado-1-4",
        name="Helado 1/4 kg",
        description="Helado artesanal, un cuarto kilo a elección.",
        category="Postres",
        price_cents=420000,
    ),
    MenuSeed(
        sku="brownie-nuez",
        name="Brownie con nuez",
        description="Porción de brownie húmedo con nuez.",
        category="Postres",
        price_cents=310000,
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
