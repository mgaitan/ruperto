"""Municipal service-request intake built on top of the shared platform core."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ruperto.assistant import build_google_model
from ruperto.config import Settings
from ruperto.models import Channel, MunicipalCaseStatus
from ruperto.repository import UNSET, BusinessRepository
from ruperto.schemas import (
    AssistantNextStep,
    AssistantReply,
    AssistantTurnResult,
    CustomerSnapshot,
    MunicipalAreaSnapshot,
    MunicipalCaseDraftSnapshot,
    MunicipalCaseSnapshot,
    MunicipalCategorySnapshot,
)

logger = logging.getLogger(__name__)

MUNICIPAL_CONFIRMATION_HINTS = (
    "confirmo",
    "confirmá",
    "confirma",
    "confirmalo",
    "confirmámelo",
    "está bien",
    "esta bien",
    "dale",
    "ok",
)

MUNICIPAL_AGGRESSIVE_LANGUAGE_HINTS = (
    "mierda",
    "pelotudo",
    "pelotuda",
    "boludo",
    "boluda",
    "idiota",
    "forro",
    "forra",
    "hijo de puta",
    "hdp",
)
MUNICIPAL_CASE_TRACKING_HINTS = (
    "estado",
    "seguimiento",
    "cómo va",
    "como va",
    "qué pasó",
    "que paso",
    "qué onda",
    "que onda",
    "mi caso",
    "mi reclamo",
    "mi solicitud",
)

MUNICIPAL_VAGUE_LOCATION_HINTS = {
    "aca",
    "acá",
    "ahi",
    "ahí",
    "alla",
    "allá",
    "la calle",
    "el barrio",
    "mi barrio",
    "por aca",
    "por acá",
    "por ahi",
    "por ahí",
}
MUNICIPAL_CASE_NUMBER_PATTERN = re.compile(r"\bcaso\s*#?\s*(?P<case_id>\d+)\b", re.IGNORECASE)
MIN_LOCATION_TEXT_LENGTH = 8

MUNICIPAL_MODEL_UNAVAILABLE_REPLY = (
    "Se me complicó tomar el reclamo justo ahora. Si querés, probá de nuevo en unos segundos o te derivo con un "
    "equipo del municipio."
)
MUNICIPAL_HUMAN_HANDOFF_REPLY = "Te derivo con una persona del municipio para que siga el caso con vos."

MUNICIPAL_BASE_INSTRUCTIONS = """
Sos un asistente virtual municipal de Argentina que toma reclamos y solicitudes vecinales por chat.

Tu trabajo en este paso no es redactar la respuesta final:
solo interpretás el turno del vecino y devolvés datos estructurados.

Reglas:
- Respondé siempre pensando en español de Argentina.
- No inventes áreas, categorías, personas, ubicaciones ni detalles del reclamo.
- Si identificás un área o categoría, usá solamente los IDs disponibles en el contexto.
- Cada categoría indica si corresponde a un reclamo o a una solicitud. Respetá esa semántica.
- Si el mensaje es solo saludo o pide opciones, marcá `asks_for_catalog=true`.
- Si la persona quiere hablar con alguien del municipio, marcá `requests_human=true`.
- Si el mensaje confirma un resumen previo del reclamo, marcá `confirms_submission=true`.
- Si detectás nombre, descripción del problema o ubicación, extraelos sin reformular de más.
- `request_summary` debe ser una descripción breve y útil del problema, en una sola frase.
- Si no hay dato explícito, dejá el campo como `null`.
""".strip()


class MunicipalTurnIntent(BaseModel):
    """Structured interpretation of one municipal customer turn."""

    asks_for_catalog: bool = False
    requests_human: bool = False
    confirms_submission: bool = False
    citizen_name: str | None = None
    area_id: int | None = None
    category_id: int | None = None
    request_summary: str | None = None
    location_text: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class MunicipalAgentRunResult(Protocol):
    """Minimal protocol required from one municipal interpreter run."""

    output: MunicipalTurnIntent


class MunicipalTurnInterpreter(Protocol):
    """Protocol for models that interpret one municipal customer turn."""

    async def run(
        self,
        message_text: str,
        *,
        deps: MunicipalAssistantDeps,
        model: Model | str,
    ) -> MunicipalAgentRunResult:
        """Interpret one municipal turn and return structured output."""


@dataclass(slots=True)
class MunicipalAssistantDeps:
    """Runtime context injected into the municipal interpretation agent."""

    store_name: str
    customer_name: str | None
    areas: list[MunicipalAreaSnapshot]
    categories: list[MunicipalCategorySnapshot]
    current_draft: MunicipalCaseDraftSnapshot | None


municipal_intake_agent = cast(
    Agent[MunicipalAssistantDeps, MunicipalTurnIntent],
    Agent(
        None,
        deps_type=MunicipalAssistantDeps,
        output_type=MunicipalTurnIntent,
        instructions=MUNICIPAL_BASE_INSTRUCTIONS,
        defer_model_check=True,
        max_concurrency=1,
    ),
)
default_municipal_turn_interpreter = cast(MunicipalTurnInterpreter, municipal_intake_agent)


@municipal_intake_agent.instructions
async def municipal_business_context(ctx: RunContext[MunicipalAssistantDeps]) -> str:
    """Provide municipal catalog and draft context to the interpretation model."""
    areas_text = "\n".join(
        f"- area_id={area.id}: {area.name}. {area.description or 'Sin descripción.'}" for area in ctx.deps.areas
    )
    categories_text = "\n".join(
        (
            f"- category_id={category.id}: {category.name} "
            f"(area_id={category.area_id}, request_kind={category.request_kind}, "
            f"precise_location={category.requires_precise_location}, "
            f"fallback={category.is_fallback})"
        )
        for category in ctx.deps.categories
    )
    if ctx.deps.current_draft is None:
        draft_text = "No hay borrador cargado todavía."
    else:
        draft_text = (
            f"Borrador actual: area_id={ctx.deps.current_draft.area_id}, "
            f"category_id={ctx.deps.current_draft.category_id}, "
            f"summary={ctx.deps.current_draft.request_summary!r}, "
            f"location={ctx.deps.current_draft.location_text!r}, "
            f"latitude={ctx.deps.current_draft.latitude!r}, "
            f"longitude={ctx.deps.current_draft.longitude!r}, "
            f"awaiting_confirmation={ctx.deps.current_draft.awaiting_confirmation}."
        )
    return (
        f"Atendés al municipio {ctx.deps.store_name}. "
        f"Nombre conocido del vecino: {ctx.deps.customer_name or 'desconocido'}.\n"
        f"Áreas disponibles:\n{areas_text}\n"
        f"Categorías disponibles:\n{categories_text}\n"
        f"{draft_text}"
    )


class MunicipalAssistantService:
    """Application service that orchestrates one municipal intake turn."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        agent: MunicipalTurnInterpreter = default_municipal_turn_interpreter,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.agent = agent

    async def handle_customer_message(
        self,
        *,
        channel: Channel,
        external_user_id: str,
        message_text: str,
        model: Model | str | None = None,
        store_id: int | None = None,
    ) -> AssistantTurnResult:
        """Capture or continue one municipal service request through chat."""
        resolved_store_id = store_id or self.settings.default_store_id
        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            store = await repository.get_store_profile(store_id=resolved_store_id)
            customer = await repository.get_or_create_customer(
                channel=channel,
                external_id=external_user_id,
                store_id=resolved_store_id,
                phone_number=external_user_id if channel == Channel.WHATSAPP else None,
            )
            conversation = await repository.get_or_create_conversation(
                channel=channel,
                external_id=external_user_id,
                customer_id=customer.id,
                store_id=resolved_store_id,
            )
            draft = await repository.get_municipal_case_draft(conversation_id=conversation.id)
            areas = await repository.list_municipal_areas(store_id=resolved_store_id, only_active=True)
            categories = await repository.list_municipal_categories(store_id=resolved_store_id, only_active=True)
            tracked_case = await self._maybe_get_tracked_case(
                repository=repository,
                customer=customer,
                store_id=resolved_store_id,
                draft=draft,
                message_text=message_text,
            )
            await session.commit()

        if not areas:
            return AssistantTurnResult(
                conversation_id=conversation.id,
                customer=customer,
                reply=AssistantReply(
                    reply_text=MUNICIPAL_HUMAN_HANDOFF_REPLY,
                    next_step=AssistantNextStep.HANDOFF,
                    handoff=True,
                ),
                current_order=None,
            )
        if tracked_case is not None:
            tracked_snapshot, requested_case_id = tracked_case
            if tracked_snapshot is None:
                reply = AssistantReply(
                    reply_text=self._build_missing_case_follow_up_reply(case_id=requested_case_id),
                    next_step=AssistantNextStep.CHOOSE_AREA,
                )
            else:
                reply = AssistantReply(
                    reply_text=self._build_case_tracking_reply(
                        case_snapshot=tracked_snapshot,
                        categories=categories,
                    ),
                    next_step=AssistantNextStep.COMPLETE,
                )
            return AssistantTurnResult(
                conversation_id=conversation.id,
                customer=customer,
                reply=reply,
                current_order=None,
            )

        active_model = model if model is not None else build_google_model(self.settings)
        try:
            intent = await self._interpret_turn(
                message_text=message_text,
                model=active_model,
                deps=MunicipalAssistantDeps(
                    store_name=store.store_name,
                    customer_name=customer.name,
                    areas=areas,
                    categories=categories,
                    current_draft=draft,
                ),
            )
        except TimeoutError:
            return self._build_model_unavailable_result(conversation_id=conversation.id, customer=customer)
        except Exception:  # noqa: BLE001
            return self._build_model_unavailable_result(conversation_id=conversation.id, customer=customer)

        async with self.session_factory() as session:
            repository = BusinessRepository(session)
            customer = await repository.get_customer(customer.id)
            if intent.citizen_name:
                customer = await repository.update_customer_name(customer.id, intent.citizen_name)

            draft = await repository.get_municipal_case_draft(
                conversation_id=conversation.id,
                create_if_missing=False,
            )
            updated_draft = await self._apply_intent_to_draft(
                repository=repository,
                customer=customer,
                conversation_id=conversation.id,
                store_id=resolved_store_id,
                current_draft=draft,
                intent=intent,
                areas=areas,
                categories=categories,
            )
            reply, _created_case = await self._build_reply(
                repository=repository,
                store_name=store.store_name,
                customer=customer,
                draft=updated_draft,
                areas=areas,
                categories=categories,
                intent=intent,
                message_text=message_text,
                conversation_id=conversation.id,
            )
            await session.commit()

        return AssistantTurnResult(
            conversation_id=conversation.id,
            customer=customer,
            reply=reply,
            current_order=None,
        )

    async def _maybe_get_tracked_case(
        self,
        *,
        repository: BusinessRepository,
        customer: CustomerSnapshot,
        store_id: int,
        draft: MunicipalCaseDraftSnapshot | None,
        message_text: str,
    ) -> tuple[MunicipalCaseSnapshot | None, int | None] | None:
        """Return the requested municipal case when the citizen is following up on an existing ticket."""
        case_id = self._extract_case_number(message_text)
        if case_id is None and draft is not None and self._draft_is_active(draft):
            return None
        if case_id is None and not self._message_requests_case_follow_up(message_text):
            return None

        return (
            await repository.get_customer_municipal_case(case_id, customer_id=customer.id, store_id=store_id)
            if case_id is not None
            else await repository.get_latest_customer_municipal_case(customer_id=customer.id, store_id=store_id)
        ), case_id

    async def _interpret_turn(
        self,
        *,
        message_text: str,
        model: Model | str,
        deps: MunicipalAssistantDeps,
    ) -> MunicipalTurnIntent:
        """Run the municipal interpretation agent with lightweight retries."""
        attempts = self.settings.assistant_model_retry_attempts + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(self.settings.assistant_model_timeout_seconds):
                    result = await self.agent.run(message_text, deps=deps, model=model)
                return cast(MunicipalTurnIntent, result.output)
            except Exception as error:  # noqa: BLE001
                logger.warning("Municipal assistant model failed", exc_info=error, extra={"attempt": attempt})
                last_error = error
                if attempt < attempts:
                    await asyncio.sleep(min(0.25 * attempt, 1.0))

        assert last_error is not None
        raise last_error

    async def _apply_intent_to_draft(  # noqa: PLR0913
        self,
        *,
        repository: BusinessRepository,
        customer: CustomerSnapshot,
        conversation_id: int,
        store_id: int,
        current_draft: MunicipalCaseDraftSnapshot | None,
        intent: MunicipalTurnIntent,
        areas: list[MunicipalAreaSnapshot],
        categories: list[MunicipalCategorySnapshot],
    ) -> MunicipalCaseDraftSnapshot | None:
        """Merge the interpreted data into the municipal draft state."""
        valid_area_ids = {area.id for area in areas}
        valid_categories = {category.id: category for category in categories}
        next_area_id = current_draft.area_id if current_draft is not None else None
        next_category_id = current_draft.category_id if current_draft is not None else None
        draft_changed = False

        if intent.area_id in valid_area_ids and intent.area_id != next_area_id:
            next_area_id = intent.area_id
            next_category_id = None
            draft_changed = True
        if intent.category_id in valid_categories:
            next_category = valid_categories[intent.category_id]
            if next_category_id != next_category.id or next_area_id != next_category.area_id:
                draft_changed = True
            next_category_id = next_category.id
            next_area_id = next_category.area_id

        if (
            next_area_id is None
            and next_category_id is None
            and intent.request_summary is None
            and intent.location_text is None
            and intent.latitude is None
            and intent.longitude is None
            and current_draft is None
        ):
            return None
        if (
            intent.request_summary is not None
            or intent.location_text is not None
            or intent.latitude is not None
            or intent.longitude is not None
        ):
            draft_changed = True

        return await repository.update_municipal_case_draft(
            conversation_id=conversation_id,
            store_id=store_id,
            customer_id=customer.id,
            area_id=next_area_id,
            category_id=next_category_id,
            request_summary=UNSET if intent.request_summary is None else intent.request_summary,
            location_text=UNSET if intent.location_text is None else intent.location_text,
            latitude=UNSET if intent.latitude is None else intent.latitude,
            longitude=UNSET if intent.longitude is None else intent.longitude,
            awaiting_confirmation=(
                False if draft_changed or current_draft is None else current_draft.awaiting_confirmation
            ),
        )

    async def _build_reply(  # noqa: C901, PLR0911, PLR0913
        self,
        *,
        repository: BusinessRepository,
        store_name: str,
        customer: CustomerSnapshot,
        draft: MunicipalCaseDraftSnapshot | None,
        areas: list[MunicipalAreaSnapshot],
        categories: list[MunicipalCategorySnapshot],
        intent: MunicipalTurnIntent,
        message_text: str,
        conversation_id: int,
    ) -> tuple[AssistantReply, MunicipalCaseSnapshot | None]:
        """Build the next municipal reply and optionally create the case."""
        if intent.requests_human:
            return (
                AssistantReply(
                    reply_text=MUNICIPAL_HUMAN_HANDOFF_REPLY,
                    next_step=AssistantNextStep.HANDOFF,
                    handoff=True,
                ),
                None,
            )

        if self._message_contains_aggressive_language(message_text):
            return (
                AssistantReply(
                    reply_text=self._build_respectful_rephrase_reply(draft=draft),
                    next_step=AssistantNextStep.DESCRIBE_REQUEST,
                ),
                None,
            )

        if draft is None or (intent.asks_for_catalog and draft.area_id is None):
            return (
                AssistantReply(
                    reply_text=self._build_area_prompt(store_name=store_name, areas=areas),
                    next_step=AssistantNextStep.CHOOSE_AREA,
                ),
                None,
            )

        selected_area = next((area for area in areas if area.id == draft.area_id), None)
        if selected_area is None:
            return (
                AssistantReply(
                    reply_text=self._build_area_prompt(store_name=store_name, areas=areas),
                    next_step=AssistantNextStep.CHOOSE_AREA,
                ),
                None,
            )

        area_categories = [category for category in categories if category.area_id == selected_area.id]
        selected_category = next((category for category in area_categories if category.id == draft.category_id), None)
        if selected_category is None:
            return (
                AssistantReply(
                    reply_text=self._build_category_prompt(area=selected_area, categories=area_categories),
                    next_step=AssistantNextStep.CHOOSE_CATEGORY,
                ),
                None,
            )

        if draft.request_summary is None:
            return (
                AssistantReply(
                    reply_text="Contame brevemente qué necesitás o qué pasó, así cargo bien la gestión.",
                    next_step=AssistantNextStep.DESCRIBE_REQUEST,
                ),
                None,
            )

        if not self._draft_has_location(draft):
            if selected_category.requires_precise_location:
                reply_text = (
                    "Necesito la ubicación lo más precisa posible. "
                    "Puede ser calle y altura, esquina, punto de referencia o la ubicación compartida."
                )
            else:
                reply_text = "¿En qué dirección, barrio o punto de referencia pasa?"
            return (
                AssistantReply(
                    reply_text=reply_text,
                    next_step=AssistantNextStep.SHARE_LOCATION,
                ),
                None,
            )

        if customer.name is None:
            return (
                AssistantReply(
                    reply_text=f"¿A nombre de quién cargamos {self._definite_article(selected_category)}?",
                    next_step=AssistantNextStep.ASK_NAME,
                ),
                None,
            )

        if draft.awaiting_confirmation and (
            intent.confirms_submission or self._message_explicitly_confirms_submission(message_text)
        ):
            created_case = await repository.create_municipal_case_from_draft(conversation_id=conversation_id)
            return (
                AssistantReply(
                    reply_text=self._build_case_created_reply(
                        customer_name=customer.name,
                        created_case=created_case,
                        area=selected_area,
                        category=selected_category,
                    ),
                    next_step=AssistantNextStep.COMPLETE,
                ),
                created_case,
            )

        updated_draft = await repository.update_municipal_case_draft(
            conversation_id=conversation_id,
            store_id=draft.store_id,
            customer_id=draft.customer_id,
            awaiting_confirmation=True,
        )
        return (
            AssistantReply(
                reply_text=self._build_case_review_reply(
                    customer_name=customer.name,
                    draft=updated_draft,
                    area=selected_area,
                    category=selected_category,
                ),
                next_step=AssistantNextStep.CONFIRM_CASE,
            ),
            None,
        )

    def _build_area_prompt(self, *, store_name: str, areas: list[MunicipalAreaSnapshot]) -> str:
        """Render the opening municipal area chooser."""
        lines = [
            f"Hola, soy el asistente de {store_name}.",
            "Puedo ayudarte a cargar un reclamo o una solicitud en estas áreas:",
            "",
        ]
        lines.extend(f"- {area.name}" for area in areas)
        lines.extend(["", "Decime cuál corresponde o contame el problema y te ayudo a ubicarlo."])
        return "\n".join(lines)

    def _build_category_prompt(
        self,
        *,
        area: MunicipalAreaSnapshot,
        categories: list[MunicipalCategorySnapshot],
    ) -> str:
        """Render the category chooser for one municipal area."""
        lines = [f"Buenísimo. Dentro de {area.name} puedo cargar:", ""]
        for category in categories:
            suffix = " (ubicación exacta)" if category.requires_precise_location else ""
            lines.append(f"- {category.name}{suffix}")
        lines.extend(["", "Decime cuál corresponde. Si no encaja exacto, podés elegir “Otro”."])
        return "\n".join(lines)

    def _build_case_review_reply(
        self,
        *,
        customer_name: str | None,
        draft: MunicipalCaseDraftSnapshot,
        area: MunicipalAreaSnapshot,
        category: MunicipalCategorySnapshot,
    ) -> str:
        """Render the municipal case review shown before submission."""
        display_name = self._display_name(customer_name)
        greeting = f"Así lo cargaría, {display_name}:" if display_name is not None else "Así lo cargaría:"
        lines = [
            greeting,
            "",
            f"**Área**\n{area.name}",
            "",
            f"**Categoría**\n{category.name}",
            "",
            f"**Detalle**\n{draft.request_summary}",
            "",
            f"**Ubicación**\n{draft.location_text}",
        ]
        if draft.location_reference:
            lines.extend(["", f"**Referencia**\n{draft.location_reference}"])
        lines.extend(["", f"Si está bien así, confirmamelo y ingreso {self._definite_article(category)}."])
        return "\n".join(lines)

    def _build_case_created_reply(
        self,
        *,
        customer_name: str | None,
        created_case: MunicipalCaseSnapshot,
        area: MunicipalAreaSnapshot,
        category: MunicipalCategorySnapshot,
    ) -> str:
        """Render the successful municipal case confirmation."""
        display_name = self._display_name(customer_name)
        greeting = f"Listo, {display_name}." if display_name is not None else "Listo."
        return "\n".join(
            [
                greeting,
                "",
                (
                    f"Tu {self._noun(category)} quedó "
                    f"{self._registered_participle(category)} como caso #{created_case.id}."
                ),
                "",
                f"**Área**\n{area.name}",
                "",
                f"**Categoría**\n{category.name}",
                "",
                "Si hace falta, el equipo municipal te contacta por este mismo medio.",
            ]
        )

    def _build_case_tracking_reply(
        self,
        *,
        case_snapshot: MunicipalCaseSnapshot,
        categories: list[MunicipalCategorySnapshot],
    ) -> str:
        """Render a proactive follow-up answer about one already created municipal case."""
        noun = self._noun_for_case(case_snapshot=case_snapshot, categories=categories)
        status_text = self._municipal_case_status_text(case_snapshot.status)
        lines = [f"Tu {noun} #{case_snapshot.id} {status_text}."]
        if case_snapshot.title:
            lines.extend(["", f"**Detalle**\n{case_snapshot.title}"])
        if case_snapshot.location_text:
            lines.extend(["", f"**Ubicación**\n{case_snapshot.location_text}"])
        lines.extend(["", "Si querés, también puedo ayudarte a cargar otro caso nuevo por acá."])
        return "\n".join(lines)

    def _build_missing_case_follow_up_reply(self, *, case_id: int | None) -> str:
        """Render a safe fallback when the requested municipal case cannot be found."""
        if case_id is not None:
            return (
                f"No encuentro un caso tuyo con el número #{case_id}. "
                "Si querés, probá con otro número o contame qué pasó y te ayudo a cargarlo."
            )
        return "Todavía no encuentro un caso tuyo para seguir por acá. Si querés, contame qué pasó y lo cargamos."

    def _draft_has_location(self, draft: MunicipalCaseDraftSnapshot) -> bool:
        """Return whether the municipal draft already has enough location context."""
        if draft.latitude is not None and draft.longitude is not None:
            return True
        if draft.location_text is None:
            return False
        return self._location_text_is_specific_enough(draft.location_text)

    def _draft_is_active(self, draft: MunicipalCaseDraftSnapshot) -> bool:
        """Return whether the draft already has enough data to keep intake in the foreground."""
        return any(
            value is not None
            for value in (
                draft.area_id,
                draft.category_id,
                draft.request_summary,
                draft.location_text,
            )
        )

    def _display_name(self, customer_name: str | None) -> str | None:
        """Return a short customer-facing name when one is available."""
        if customer_name is None:
            return None
        normalized = " ".join(customer_name.strip().split())
        if not normalized:
            return None
        return normalized.split(" ", maxsplit=1)[0]

    def _noun(self, category: MunicipalCategorySnapshot) -> str:
        """Return the noun that matches the selected category."""
        return "solicitud" if category.request_kind.value == "request" else "reclamo"

    def _definite_article(self, category: MunicipalCategorySnapshot) -> str:
        """Return one short article+noun phrase suitable for prompts."""
        return "la solicitud" if category.request_kind.value == "request" else "el reclamo"

    def _registered_participle(self, category: MunicipalCategorySnapshot) -> str:
        """Return the participle that agrees with the category noun."""
        return "registrada" if category.request_kind.value == "request" else "registrado"

    def _message_explicitly_confirms_submission(self, message_text: str) -> bool:
        """Detect an explicit approval from the latest customer turn."""
        lowered = " ".join(message_text.lower().split())
        return any(hint in lowered for hint in MUNICIPAL_CONFIRMATION_HINTS)

    def _message_requests_case_follow_up(self, message_text: str) -> bool:
        """Return whether the citizen is asking about an already created municipal case."""
        lowered = " ".join(message_text.casefold().split())
        return any(hint in lowered for hint in MUNICIPAL_CASE_TRACKING_HINTS)

    def _message_contains_aggressive_language(self, message_text: str) -> bool:
        """Return whether the turn contains insulting or aggressive phrasing."""
        lowered = " ".join(message_text.casefold().split())
        return any(hint in lowered for hint in MUNICIPAL_AGGRESSIVE_LANGUAGE_HINTS)

    def _extract_case_number(self, message_text: str) -> int | None:
        """Extract one explicit municipal case identifier from the latest message."""
        match = MUNICIPAL_CASE_NUMBER_PATTERN.search(message_text)
        if match is None:
            return None
        return int(match.group("case_id"))

    def _location_text_is_specific_enough(self, location_text: str) -> bool:
        """Return whether the saved location looks usable for staff follow-up."""
        lowered = " ".join(location_text.casefold().split())
        if lowered in MUNICIPAL_VAGUE_LOCATION_HINTS:
            return False
        if len(lowered) < MIN_LOCATION_TEXT_LENGTH:
            return False
        if any(char.isdigit() for char in lowered):
            return True
        if any(separator in lowered for separator in ("/", "-", ",")):
            return True
        specific_place_words = (
            "esquina",
            "frente",
            "altura",
            "barrio",
            "plaza",
            "calle",
            "avenida",
            "av.",
            "ruta",
            "pasaje",
        )
        return any(word in lowered for word in specific_place_words if lowered != word)

    def _build_respectful_rephrase_reply(self, *, draft: MunicipalCaseDraftSnapshot | None) -> str:
        """Ask the citizen to restate the issue without insults while keeping the flow open."""
        if draft is not None and draft.area_id is not None:
            return "Te puedo ayudar con eso, pero necesito que me expliques qué pasa sin insultos así lo cargo bien."
        return "Te puedo ayudar con eso. Contame qué está pasando, sin insultos, y te ayudo a ubicarlo."

    def _noun_for_case(
        self,
        *,
        case_snapshot: MunicipalCaseSnapshot,
        categories: list[MunicipalCategorySnapshot],
    ) -> str:
        """Return the citizen-facing noun that matches one stored municipal case."""
        category = next((item for item in categories if item.id == case_snapshot.category_id), None)
        if category is None:
            return "caso"
        return self._noun(category)

    def _municipal_case_status_text(self, status: MunicipalCaseStatus) -> str:
        """Return short status copy for one municipal case follow-up reply."""
        return {
            MunicipalCaseStatus.NEW: "ya quedó registrado y está esperando revisión",
            MunicipalCaseStatus.TRIAGED: "ya está en revisión",
            MunicipalCaseStatus.IN_PROGRESS: "ya está en gestión",
            MunicipalCaseStatus.BLOCKED: "está bloqueado por el momento",
            MunicipalCaseStatus.RESOLVED: "ya fue resuelto",
            MunicipalCaseStatus.CLOSED: "ya fue cerrado",
            MunicipalCaseStatus.CANCELLED: "fue cancelado",
        }[status]

    def _build_model_unavailable_result(
        self,
        *,
        conversation_id: int,
        customer: CustomerSnapshot,
    ) -> AssistantTurnResult:
        """Return a safe municipal fallback when the model cannot interpret a turn."""
        return AssistantTurnResult(
            conversation_id=conversation_id,
            customer=customer,
            reply=AssistantReply(
                reply_text=MUNICIPAL_MODEL_UNAVAILABLE_REPLY,
                next_step=AssistantNextStep.HANDOFF,
                handoff=True,
            ),
            current_order=None,
        )
