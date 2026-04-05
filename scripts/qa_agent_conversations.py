"""Run conversational QA scenarios against the real ordering assistant."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from ruperto.assistant import OrderingAssistantService
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import Channel


@dataclass(frozen=True, slots=True)
class Scenario:
    """One conversational QA scenario."""

    slug: str
    persona: str
    external_user_id: str
    description: str
    turns: list[str]


SCENARIOS: list[Scenario] = [
    Scenario(
        slug="direct_pickup_burger",
        persona="Cliente pragmático",
        external_user_id="qa-directo-1",
        description="Sabe exactamente lo que quiere y busca cerrar rápido.",
        turns=[
            "Hola, soy Martín.",
            "Quiero una hamburguesa doble cheddar para retirar.",
            "Pago en efectivo.",
            "Confirmá nomás.",
        ],
    ),
    Scenario(
        slug="indecisive_veggie",
        persona="Cliente indeciso",
        external_user_id="qa-indeciso-1",
        description="Duda entre varias opciones veggie y pide recomendación.",
        turns=[
            "Buenas noches",
            "¿Algo veggie?",
            "Las dos, ¿cuál me recomendás? Tengo hambre.",
            "Dale, la burger.",
            "¿Qué le puedo sumar?",
        ],
    ),
    Scenario(
        slug="shipping_question_only",
        persona="Consulta informativa",
        external_user_id="qa-informativo-1",
        description="Todavía no quiere pedir; solo pregunta por envío.",
        turns=[
            "Hola, ¿tenés para enviar?",
            "¿Y más o menos por qué zona llegan?",
        ],
    ),
    Scenario(
        slug="soda_category_followup",
        persona="Cliente curioso",
        external_user_id="qa-bebidas-1",
        description="Pregunta por categoría y después por precio.",
        turns=[
            "No tenés gaseosas?",
            "¿Cuánto sale?",
        ],
    ),
    Scenario(
        slug="scheduled_lunch_order",
        persona="Cliente organizado",
        external_user_id="qa-programado-1",
        description="Quiere un pedido programado para una hora concreta.",
        turns=[
            "Soy Pedro",
            "Una hamburguesa doble cheddar y una cerveza rubia. ¿Me lo preparás para las 12?",
            "Lo retiro.",
            "Transferencia.",
            "Confirmá el pedido.",
        ],
    ),
    Scenario(
        slug="lomito_correction",
        persona="Cliente que corrige",
        external_user_id="qa-correccion-1",
        description="Hace un pedido ambiguo y luego lo corrige.",
        turns=[
            "Hola, soy Ana.",
            "2 lomos y unas papas.",
            "Uno y uno.",
            "Dije uno de cada uno.",
        ],
    ),
    Scenario(
        slug="unsupported_customization",
        persona="Cliente especial",
        external_user_id="qa-especial-1",
        description="Pide una personalización inexistente para ver cómo responde el bot.",
        turns=[
            "Hola, soy Nico.",
            "Quiero la hamburguesa picante pero con doble picante y triple cheddar.",
            "¿Eso se puede?",
        ],
    ),
    Scenario(
        slug="off_topic_request",
        persona="Fuera de dominio",
        external_user_id="qa-fuera-dominio-1",
        description="Pregunta algo que no tiene nada que ver con la rotisería.",
        turns=[
            "Hola, ¿me podés decir quién ganó el mundial 2022?",
            "¿Y me hacés un resumen del partido?",
        ],
    ),
    Scenario(
        slug="main_plus_side_upsell",
        persona="Cliente con hambre",
        external_user_id="qa-upsell-1",
        description="Pide principal y guarnición para ver si todavía propone bebida o postre.",
        turns=[
            "Hola, soy Juli.",
            "1 sanguche de mila y unas papas clásicas.",
        ],
    ),
    Scenario(
        slug="delivery_cost_question",
        persona="Cliente cuidadoso",
        external_user_id="qa-envio-costo-1",
        description="Pregunta por costo de envío después de armar pedido parcial.",
        turns=[
            "Hola, soy Lucas.",
            "Quiero una hamburguesa completa.",
            "Envío.",
            "¿El envío tiene costo?",
        ],
    ),
    Scenario(
        slug="known_customer_repeat",
        persona="Cliente recurrente",
        external_user_id="qa-recurrente-1",
        description="Hace un pedido y luego vuelve a escribir para ver si conserva contexto.",
        turns=[
            "Hola, soy Elena.",
            "Quiero una pizza napolitana.",
            "Retiro.",
            "Pago efectivo.",
            "Confirmá.",
            "Che, ¿te acordás qué pedí recién?",
        ],
    ),
    Scenario(
        slug="price_before_name",
        persona="Cliente directo",
        external_user_id="qa-precio-antes-nombre-1",
        description="Pregunta precios sin presentarse.",
        turns=[
            "¿Cuánto sale una fugazzeta?",
            "Y una cerveza IPA, ¿cuánto?",
        ],
    ),
    Scenario(
        slug="post_order_notification_expectation",
        persona="Cliente prevenido",
        external_user_id="qa-notificacion-1",
        description="Quiere saber si le avisan cuando esté listo.",
        turns=[
            "Hola, soy Carla.",
            "Quiero un lomito especial.",
            "Retiro.",
            "Pago con transferencia.",
            "¿Me avisás cuando esté listo?",
        ],
    ),
    Scenario(
        slug="large_family_order",
        persona="Pedido grande",
        external_user_id="qa-grande-1",
        description="Hace un pedido largo con varias líneas y cantidades.",
        turns=[
            "Hola, soy Diego.",
            "Quiero 2 pizzas muzza, 1 docena de empanadas clásicas y 2 gaseosas cola 1.5L.",
            "Envío.",
            "A 9 de Julio 1302, Anisacate.",
            "Link de pago.",
        ],
    ),
    Scenario(
        slug="dessert_only",
        persona="Antojo nocturno",
        external_user_id="qa-postre-1",
        description="Pide solo postres y bebidas, sin plato principal.",
        turns=[
            "Hola, soy Belu.",
            "¿Qué postres tienen?",
            "Dame un tiramisú y un cheesecake.",
            "¿Tenés algo para tomar sin alcohol?",
        ],
    ),
    Scenario(
        slug="address_reuse",
        persona="Cliente repetido",
        external_user_id="qa-direccion-1",
        description="Primero deja dirección, luego vuelve a pedir para ver si se reutiliza.",
        turns=[
            "Hola, soy Pablo.",
            "Quiero una milanesa napolitana.",
            "Envío.",
            "Lavalle 12333.",
            "Efectivo.",
            "Confirmá.",
            "Ahora quiero una hamburguesa BBQ.",
            "Envío.",
        ],
    ),
    Scenario(
        slug="ambiguous_beer_choice",
        persona="Cliente coloquial",
        external_user_id="qa-cerveza-1",
        description="Usa expresiones ambiguas al sumar bebida.",
        turns=[
            "Soy Fer.",
            "Quiero una burger veggie.",
            "Sumame una cervecita.",
        ],
    ),
    Scenario(
        slug="menu_browse_then_order",
        persona="Cliente explorador",
        external_user_id="qa-carta-1",
        description="Pide la carta, recorre y recién después decide.",
        turns=[
            "Hola, ¿me pasás la carta?",
            "¿Qué wraps hay?",
            "Bueno, dame el wrap de pollo.",
            "Retiro.",
        ],
    ),
    Scenario(
        slug="wrong_channel_expectation",
        persona="Cliente techie",
        external_user_id="qa-canal-1",
        description="Pregunta por canales y cosas fuera de operación.",
        turns=[
            "¿Tenés Instagram o solo WhatsApp?",
            "¿Puedo pagar con crypto?",
        ],
    ),
    Scenario(
        slug="allergic_customer",
        persona="Cliente con restricción",
        external_user_id="qa-alergia-1",
        description="Consulta por ingredientes y modificaciones.",
        turns=[
            "Hola, soy Mica.",
            "¿La ensalada César trae pollo sí o sí?",
            "¿Y la hamburguesa veggie tiene queso?",
        ],
    ),
    Scenario(
        slug="natural_language_combo",
        persona="Cliente apurado",
        external_user_id="qa-natural-1",
        description="Mete todo junto en una sola frase.",
        turns=[
            "Hola soy Tomi quiero una pizza especial para enviar a Olegario Andrade 330 y pago con transferencia",
        ],
    ),
    Scenario(
        slug="cash_mixed_with_english",
        persona="Cliente code-switch",
        external_user_id="qa-cash-english-1",
        description="Mezcla español e inglés en el pago.",
        turns=[
            "Hola, soy Juani.",
            "Una hamburguesa BBQ.",
            "Retiro.",
            "Pago en el local, cash.",
        ],
    ),
    Scenario(
        slug="rejection_of_upsell",
        persona="Cliente seco",
        external_user_id="qa-no-upsell-1",
        description="Rechaza explícitamente cualquier agregado.",
        turns=[
            "Hola, soy Agus.",
            "Quiero una pizza rúcula y crudo.",
            "Nada más.",
            "Retiro.",
        ],
    ),
    Scenario(
        slug="exact_price_comparison",
        persona="Cliente comparador",
        external_user_id="qa-comparador-1",
        description="Quiere comparar dos productos antes de decidir.",
        turns=[
            "¿Qué sale más, el lomito completo o la milanesa completa?",
            "¿Y cuál te parece más llenador?",
        ],
    ),
    Scenario(
        slug="abandoned_then_resume",
        persona="Cliente intermitente",
        external_user_id="qa-retoma-1",
        description="Deja la charla a medias y vuelve después dentro de la misma conversación.",
        turns=[
            "Hola, soy Emi.",
            "Quiero una hamburguesa completa.",
            "Pará, todavía no sé si la voy a retirar.",
            "Bueno, sí, retiro.",
            "Transferencia.",
        ],
    ),
]


def format_order_summary(payload: dict[str, object] | None) -> str:
    """Render a compact order summary line for the report."""
    if payload is None:
        return "Sin pedido activo."
    status = payload.get("status", "—")
    total = payload.get("total_amount_display", "—")
    items = payload.get("items", [])
    if not isinstance(items, list):
        return f"Estado: {status}. Total: {total}."
    item_summary = ", ".join(_format_order_item(item) for item in items)
    notify = payload.get("notify_when_ready", False)
    return f"Estado: {status}. Total: {total}. Ítems: {item_summary or '—'}. Avisos automáticos: {notify}."


def _format_order_item(item: object) -> str:
    """Render one order line item for the summary."""
    if not isinstance(item, dict):
        return "ítem inválido"
    item_map = cast(dict[str, object], item)
    quantity = item_map.get("quantity")
    name = item_map.get("name")
    return f"{quantity if quantity is not None else '—'} x {name if name is not None else '—'}"


def build_report_header(settings: Settings) -> list[str]:
    """Return the markdown header for one QA report."""
    started_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return [
        "# Conversational QA Report",
        "",
        f"- Generated: {started_at}",
        f"- Model: `{settings.gemini_model}`",
        f"- Scenarios: {len(SCENARIOS)}",
        "",
    ]


async def render_scenario(
    service: OrderingAssistantService,
    scenario: Scenario,
    index: int,
) -> list[str]:
    """Run one scenario and return its markdown section."""
    sections = [
        f"## {index}. {scenario.slug}",
        "",
        f"- Persona: {scenario.persona}",
        f"- External user id: `{scenario.external_user_id}`",
        f"- Goal: {scenario.description}",
        "",
    ]
    for turn_number, user_message in enumerate(scenario.turns, start=1):
        sections.extend(await render_turn(service, scenario, turn_number, user_message))
        if sections[-1] == "":  # keep shape stable for both success and failure
            pass
        if any(line == "**Error**" for line in sections[-8:]):
            break
    sections.extend(["---", ""])
    return sections


async def render_turn(
    service: OrderingAssistantService,
    scenario: Scenario,
    turn_number: int,
    user_message: str,
) -> list[str]:
    """Run one turn and return its markdown fragment."""
    base = [
        f"### Turn {turn_number}",
        "",
        "**Customer**",
        "",
        user_message,
        "",
    ]
    try:
        result = await service.handle_customer_message(
            channel=Channel.DEV,
            external_user_id=scenario.external_user_id,
            message_text=user_message,
        )
    except Exception as error:  # noqa: BLE001
        return [
            *base,
            "**Error**",
            "",
            f"`{type(error).__name__}: {error}`",
            "",
        ]
    order_summary = format_order_summary(
        result.current_order.model_dump() if result.current_order is not None else None
    )
    return [
        *base,
        "**Ruperto**",
        "",
        result.reply.reply_text,
        "",
        f"- Next step: `{result.reply.next_step}`",
        f"- Order: {order_summary}",
        "",
    ]


async def write_report(report_path: Path, sections: list[str]) -> None:
    """Persist the generated markdown report."""
    await asyncio.to_thread(report_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(report_path.write_text, "\n".join(sections), encoding="utf-8")


async def run_scenarios(report_path: Path) -> None:
    """Execute all scenarios against a fresh temporary database and write a markdown report."""
    settings = Settings()
    with TemporaryDirectory(prefix="ruperto-qa-") as tmp_dir:
        db_path = Path(tmp_dir) / "qa.db"
        qa_settings = settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{db_path}"})
        runtime = create_database_runtime(qa_settings)
        await init_database(settings=qa_settings, runtime=runtime)
        service = OrderingAssistantService(session_factory=runtime.session_factory, settings=qa_settings)
        sections = build_report_header(qa_settings)
        for index, scenario in enumerate(SCENARIOS, start=1):
            sections.extend(await render_scenario(service, scenario, index))
        await write_report(report_path, sections)
        await runtime.engine.dispose()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run conversational QA scenarios against the real assistant.")
    parser.add_argument("report_path", type=Path, help="Where to write the markdown report.")
    args = parser.parse_args()
    asyncio.run(run_scenarios(args.report_path))
