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
    slot_index: int = 0
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
        sku="empanadas-pollo",
        name="Empanadas de pollo",
        description="Empanadas caseras de pollo condimentado, suaves y jugosas.",
        category="Comidas",
        price_cents=180000,
    ),
    MenuSeed(
        sku="empanadas-jamon-queso",
        name="Empanadas de jamón y queso",
        description="Empanadas horneadas con jamón cocido y mucho queso fundido.",
        category="Comidas",
        price_cents=180000,
    ),
    MenuSeed(
        sku="empanadas-verdura",
        name="Empanadas de verdura",
        description="Empanadas de acelga, salsa blanca y queso, bien caseras.",
        category="Comidas",
        price_cents=175000,
    ),
    MenuSeed(
        sku="docena-empanadas-clasicas",
        name="Docena de empanadas clásicas",
        description="Docena surtida de carne, pollo, jamón y queso y verdura.",
        category="Comidas",
        price_cents=1980000,
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
        sku="hamburguesa-clasica",
        name="Hamburguesa clásica",
        description="Hamburguesa simple con queso, kétchup y mostaza.",
        category="Comidas",
        price_cents=790000,
    ),
    MenuSeed(
        sku="hamburguesa-veg",
        name="Hamburguesa veggie",
        description="Medallón de garbanzos, queso, rúcula, tomate y papas rústicas.",
        category="Comidas",
        price_cents=1030000,
    ),
    MenuSeed(
        sku="hamburguesa-picante",
        name="Hamburguesa picante",
        description="Doble queso, jalapeños, cebolla crispy y salsa picante.",
        category="Comidas",
        price_cents=1210000,
    ),
    MenuSeed(
        sku="combo-burger-completa",
        name="Combo hamburguesa completa",
        description="Hamburguesa completa con papas fritas grandes y gaseosa lata.",
        category="Comidas",
        price_cents=1380000,
    ),
    MenuSeed(
        sku="milanesa-napolitana",
        name="Milanesa napolitana",
        description="Milanesa con salsa, jamón, queso y guarnición.",
        category="Comidas",
        price_cents=1250000,
    ),
    MenuSeed(
        sku="milanesa-completa",
        name="Milanesa completa",
        description="Milanesa con lechuga, tomate, huevo y papas fritas.",
        category="Comidas",
        price_cents=1190000,
    ),
    MenuSeed(
        sku="milanesa-a-caballo",
        name="Milanesa a caballo",
        description="Milanesa con dos huevos fritos y papas.",
        category="Comidas",
        price_cents=1280000,
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
        sku="pizza-calabresa",
        name="Pizza calabresa",
        description="Pizza con muzzarella, longaniza calabresa y aceitunas verdes.",
        category="Comidas",
        price_cents=1480000,
    ),
    MenuSeed(
        sku="pizza-cuatro-quesos",
        name="Pizza cuatro quesos",
        description="Pizza con muzzarella, provolone, roquefort y parmesano.",
        category="Comidas",
        price_cents=1540000,
    ),
    MenuSeed(
        sku="pizza-rucula-crudo",
        name="Pizza rúcula y crudo",
        description="Pizza con muzzarella, jamón crudo, rúcula fresca y oliva.",
        category="Comidas",
        price_cents=1680000,
    ),
    MenuSeed(
        sku="pizza-jamon-morron",
        name="Pizza jamón y morrón",
        description="Pizza con muzzarella, jamón cocido y tiras de morrón asado.",
        category="Comidas",
        price_cents=1440000,
    ),
    MenuSeed(
        sku="sanguche-milanesa",
        name="Sanguche de milanesa",
        description="Pan fresco, milanesa, lechuga, tomate y mayonesa.",
        category="Comidas",
        price_cents=890000,
    ),
    MenuSeed(
        sku="lomito-completo",
        name="Lomito completo",
        description="Lomito con jamón, queso, lechuga, tomate, huevo y mayonesa.",
        category="Comidas",
        price_cents=1320000,
    ),
    MenuSeed(
        sku="lomito-especial",
        name="Lomito especial",
        description="Lomito con cheddar, panceta, cebolla caramelizada y papas.",
        category="Comidas",
        price_cents=1420000,
    ),
    MenuSeed(
        sku="wrap-pollo",
        name="Wrap de pollo",
        description="Wrap tibio de pollo grillado, queso crema y vegetales frescos.",
        category="Comidas",
        price_cents=920000,
    ),
    MenuSeed(
        sku="wrap-veggie",
        name="Wrap veggie",
        description="Wrap con vegetales salteados, hummus y hojas verdes.",
        category="Comidas",
        price_cents=880000,
    ),
    MenuSeed(
        sku="papas-clasicas",
        name="Papas fritas clásicas",
        description="Porción de papas fritas crocantes.",
        category="Comidas",
        price_cents=430000,
    ),
    MenuSeed(
        sku="papas-cheddar-bacon",
        name="Papas cheddar y bacon",
        description="Papas fritas con cheddar fundido y panceta crocante.",
        category="Comidas",
        price_cents=690000,
    ),
    MenuSeed(
        sku="ensalada-cesar",
        name="Ensalada César",
        description="Lechuga, pollo grillado, croutons, queso y aderezo César.",
        category="Comidas",
        price_cents=870000,
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
        sku="agua-con-gas-500",
        name="Agua con gas 500ml",
        description="Agua mineral con gas, bien fría.",
        category="Bebidas",
        price_cents=190000,
    ),
    MenuSeed(
        sku="gaseosa-limon-lata",
        name="Gaseosa lima-limón lata",
        description="Lata de gaseosa lima-limón de 354ml.",
        category="Bebidas",
        price_cents=220000,
    ),
    MenuSeed(
        sku="gaseosa-naranja-lata",
        name="Gaseosa naranja lata",
        description="Lata de gaseosa sabor naranja de 354ml.",
        category="Bebidas",
        price_cents=220000,
    ),
    MenuSeed(
        sku="cola-cero-1-5l",
        name="Gaseosa cola cero 1.5L",
        description="Gaseosa cola sin azúcar para compartir.",
        category="Bebidas",
        price_cents=330000,
    ),
    MenuSeed(
        sku="cerveza-ipa-lata",
        name="Cerveza IPA lata",
        description="Lata de IPA artesanal de 473ml.",
        category="Bebidas",
        price_cents=340000,
    ),
    MenuSeed(
        sku="cerveza-rubia-lata",
        name="Cerveza rubia lata",
        description="Lata de cerveza rubia de 473ml.",
        category="Bebidas",
        price_cents=290000,
    ),
    MenuSeed(
        sku="cerveza-roja-lata",
        name="Cerveza roja lata",
        description="Lata de cerveza roja suave de 473ml.",
        category="Bebidas",
        price_cents=310000,
    ),
    MenuSeed(
        sku="agua-saborizada-pomelo",
        name="Agua saborizada pomelo 1.5L",
        description="Bebida sin gas sabor pomelo, ideal para compartir.",
        category="Bebidas",
        price_cents=280000,
    ),
    MenuSeed(
        sku="flan-casero",
        name="Flan casero",
        description="Flan casero con dulce de leche y crema.",
        category="Postres",
        price_cents=350000,
    ),
    MenuSeed(
        sku="tiramisu",
        name="Tiramisú",
        description="Porción de tiramisú casero con cacao y crema suave.",
        category="Postres",
        price_cents=520000,
    ),
    MenuSeed(
        sku="helado-1-4",
        name="Helado 1/4 kg",
        description="Helado artesanal, un cuarto kilo a elección.",
        category="Postres",
        price_cents=420000,
    ),
    MenuSeed(
        sku="helado-1-2",
        name="Helado 1/2 kg",
        description="Helado artesanal de medio kilo con sabores a elección.",
        category="Postres",
        price_cents=760000,
    ),
    MenuSeed(
        sku="brownie-nuez",
        name="Brownie con nuez",
        description="Porción de brownie húmedo con nuez.",
        category="Postres",
        price_cents=310000,
    ),
    MenuSeed(
        sku="cheesecake-frutos-rojos",
        name="Cheesecake de frutos rojos",
        description="Porción de cheesecake cremoso con salsa de frutos rojos.",
        category="Postres",
        price_cents=540000,
    ),
    MenuSeed(
        sku="budin-pan",
        name="Budín de pan",
        description="Porción de budín de pan casero con caramelo.",
        category="Postres",
        price_cents=290000,
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
