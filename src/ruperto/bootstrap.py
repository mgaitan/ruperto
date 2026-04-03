"""Bootstrap data for local development and the first MVP demos."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MenuSeed:
    """Static menu data used to bootstrap the local catalog."""

    sku: str
    name: str
    description: str
    category: str
    price_cents: int
    image_url: str | None = None


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
