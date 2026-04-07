"""Tests for the municipal intake service."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

from ruperto.config import Settings
from ruperto.db import DatabaseRuntime, create_database_runtime, init_database
from ruperto.models import (
    Channel,
    MunicipalArea,
    MunicipalCaseStatus,
    MunicipalCategory,
    MunicipalRequestKind,
    StoreVertical,
)
from ruperto.municipal import (
    MUNICIPAL_HUMAN_HANDOFF_REPLY,
    MUNICIPAL_MODEL_UNAVAILABLE_REPLY,
    MunicipalAssistantDeps,
    MunicipalAssistantService,
    MunicipalTurnIntent,
    municipal_business_context,
)
from ruperto.repository import BusinessRepository, MunicipalCaseNotFoundError
from ruperto.schemas import AssistantNextStep, CustomerSnapshot, MunicipalCaseDraftSnapshot, MunicipalCaseSnapshot

pytestmark = pytest.mark.anyio
MAX_CASE_TITLE_LENGTH = 160
EXPECTED_SERVICE_TURNS = 7


class StubMunicipalAgent:
    """Small async agent stub that returns queued municipal intents."""

    def __init__(self, *outcomes: MunicipalTurnIntent | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, MunicipalAssistantDeps, object]] = []

    async def run(self, message_text: str, *, deps: MunicipalAssistantDeps, model: object):
        """Return the next queued outcome or raise the queued error."""
        self.calls.append((message_text, deps, model))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(output=outcome)


async def build_municipal_runtime(tmp_path: Path) -> tuple[Settings, DatabaseRuntime]:
    """Create an initialized municipal runtime backed by a temporary database."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'municipal-service.db'}",
        store_name="Municipio Test",
        bot_name="Moony Test",
        store_vertical=StoreVertical.MUNICIPAL,
        assistant_model_retry_attempts=0,
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    return settings, runtime


async def load_municipal_catalog(
    runtime: DatabaseRuntime,
) -> tuple[int, list, list]:
    """Load the seeded municipal store and its active catalog snapshots."""
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile()
        areas = await repository.list_municipal_areas(store_id=store.id, only_active=True)
        categories = await repository.list_municipal_categories(store_id=store.id, only_active=True)
    return store.id, areas, categories


async def test_municipal_business_context_describes_catalog_and_draft(tmp_path: Path):
    """The municipal prompt context exposes the catalog and any current draft."""
    _settings, runtime = await build_municipal_runtime(tmp_path)
    store_id, areas, categories = await load_municipal_catalog(runtime)

    no_draft_text = await municipal_business_context(
        cast(
            Any,
            SimpleNamespace(
                deps=MunicipalAssistantDeps(
                    store_name="Municipio Test",
                    customer_name=None,
                    areas=areas,
                    categories=categories,
                    current_draft=None,
                )
            ),
        )
    )
    assert no_draft_text is not None
    assert "Atendés al municipio Municipio Test." in no_draft_text
    assert "No hay borrador cargado todavía." in no_draft_text
    assert f"area_id={areas[0].id}: {areas[0].name}" in no_draft_text
    assert f"category_id={categories[0].id}: {categories[0].name}" in no_draft_text

    now = datetime.now(UTC)
    draft_text = await municipal_business_context(
        cast(
            Any,
            SimpleNamespace(
                deps=MunicipalAssistantDeps(
                    store_name="Municipio Test",
                    customer_name="María",
                    areas=areas,
                    categories=categories,
                    current_draft=MunicipalCaseDraftSnapshot(
                        id=1,
                        conversation_id=7,
                        store_id=store_id,
                        customer_id=3,
                        area_id=areas[0].id,
                        category_id=categories[0].id,
                        request_summary="Lámpara apagada frente a la plaza",
                        location_text="San Martín 100",
                        location_reference=None,
                        latitude=-31.4,
                        longitude=-64.2,
                        awaiting_confirmation=True,
                        created_at=now,
                        updated_at=now,
                    ),
                )
            ),
        )
    )
    assert draft_text is not None
    assert "Nombre conocido del vecino: María." in draft_text
    assert "summary='Lámpara apagada frente a la plaza'" in draft_text
    assert "awaiting_confirmation=True" in draft_text

    await runtime.engine.dispose()


async def test_repository_can_store_clear_and_promote_municipal_case_draft(tmp_path: Path):
    """Municipal drafts can be mutated safely and promoted into persisted cases."""
    _settings, runtime = await build_municipal_runtime(tmp_path)
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile()
        customer = await repository.get_or_create_customer(
            channel=Channel.WHATSAPP,
            external_id="3515551000",
            phone_number="3515551000",
        )
        customer = await repository.update_customer_name(customer.id, "María")
        conversation = await repository.get_or_create_conversation(
            channel=Channel.WHATSAPP,
            external_id="3515551000",
            customer_id=customer.id,
            store_id=store.id,
        )
        areas = await repository.list_municipal_areas(store_id=store.id, only_active=True)
        area = next(area for area in areas if area.name == "Alumbrado público")
        categories = await repository.list_municipal_categories(store_id=store.id, area_id=area.id, only_active=True)
        category = next(category for category in categories if category.name == "Lámpara apagada")

        assert await repository.get_municipal_case_draft(conversation_id=conversation.id) is None
        assert await repository.clear_municipal_case_draft(conversation_id=conversation.id) is False
        with pytest.raises(MunicipalCaseNotFoundError):
            await repository.create_municipal_case_from_draft(conversation_id=conversation.id + 999)

        created = await repository.get_municipal_case_draft(
            conversation_id=conversation.id,
            create_if_missing=True,
            store_id=store.id,
            customer_id=customer.id,
        )
        assert created is not None
        assert await repository.clear_municipal_case_draft(conversation_id=conversation.id) is True
        recreated = await repository.get_municipal_case_draft(
            conversation_id=conversation.id,
            create_if_missing=True,
            store_id=store.id,
            customer_id=customer.id,
        )
        assert recreated is not None

        long_summary = "Lámpara apagada " * 20
        updated = await repository.update_municipal_case_draft(
            conversation_id=conversation.id,
            store_id=store.id,
            customer_id=customer.id,
            area_id=area.id,
            category_id=category.id,
            request_summary=f"  {long_summary}  ",
            location_text="  San Martín 100  ",
            location_reference="  Frente a la plaza  ",
            latitude=-31.41,
            longitude=-64.19,
            awaiting_confirmation=True,
        )
        assert updated.request_summary == long_summary.strip()
        assert updated.location_text == "San Martín 100"
        assert updated.location_reference == "Frente a la plaza"
        assert updated.awaiting_confirmation is True

        cleared = await repository.update_municipal_case_draft(
            conversation_id=conversation.id,
            store_id=store.id,
            customer_id=customer.id,
            request_summary=None,
            location_text=None,
            location_reference=None,
            latitude=None,
            longitude=None,
            awaiting_confirmation=False,
        )
        assert cleared.request_summary is None
        assert cleared.location_text is None
        assert cleared.location_reference is None
        assert cleared.latitude is None
        assert cleared.longitude is None
        assert cleared.awaiting_confirmation is False

        restored = await repository.update_municipal_case_draft(
            conversation_id=conversation.id,
            store_id=store.id,
            customer_id=customer.id,
            area_id=area.id,
            category_id=category.id,
            request_summary=f"  {long_summary}  ",
            location_text="  San Martín 100  ",
            awaiting_confirmation=True,
        )
        created_case = await repository.create_municipal_case_from_draft(conversation_id=conversation.id)
        await session.commit()

    assert restored.request_summary == long_summary.strip()
    assert created_case.reporter_name == "María"
    assert created_case.reporter_phone_number == "3515551000"
    assert created_case.location_text == "San Martín 100"
    assert created_case.title == long_summary.strip()[:MAX_CASE_TITLE_LENGTH].rstrip()

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        assert await repository.get_municipal_case_draft(conversation_id=conversation.id) is None
        stored_case = await repository.get_municipal_case(created_case.id)

    assert stored_case.description == long_summary.strip()
    await runtime.engine.dispose()


async def test_municipal_service_creates_a_case_through_multiple_turns(tmp_path: Path):
    """The municipal assistant collects catalog, description, location, and identity before submission."""
    settings, runtime = await build_municipal_runtime(tmp_path)
    store_id, areas, categories = await load_municipal_catalog(runtime)
    streets_area = next(area for area in areas if area.name == "Mantenimiento de calles")
    pothole_category = next(category for category in categories if category.name == "Bache")
    agent = StubMunicipalAgent(
        MunicipalTurnIntent(asks_for_catalog=True),
        MunicipalTurnIntent(area_id=streets_area.id),
        MunicipalTurnIntent(category_id=pothole_category.id),
        MunicipalTurnIntent(request_summary="Hay un bache grande que rompe las cubiertas"),
        MunicipalTurnIntent(location_text="San Martín 123 esquina Belgrano"),
        MunicipalTurnIntent(citizen_name="María"),
        MunicipalTurnIntent(confirms_submission=True),
    )
    service = MunicipalAssistantService(session_factory=runtime.session_factory, settings=settings, agent=agent)

    first = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="3515550000",
        message_text="Hola, quiero hacer un reclamo",
        model="stub",
        store_id=store_id,
    )
    second = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="3515550000",
        message_text="Es por mantenimiento de calles",
        model="stub",
        store_id=store_id,
    )
    third = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="3515550000",
        message_text="Es un bache",
        model="stub",
        store_id=store_id,
    )
    fourth = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="3515550000",
        message_text="Hay un bache grande que rompe las cubiertas",
        model="stub",
        store_id=store_id,
    )
    fifth = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="3515550000",
        message_text="San Martín 123 esquina Belgrano",
        model="stub",
        store_id=store_id,
    )
    sixth = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="3515550000",
        message_text="Soy María",
        model="stub",
        store_id=store_id,
    )
    seventh = await service.handle_customer_message(
        channel=Channel.WHATSAPP,
        external_user_id="3515550000",
        message_text="Confirmalo",
        model="stub",
        store_id=store_id,
    )

    assert first.reply.next_step == AssistantNextStep.CHOOSE_AREA
    assert second.reply.next_step == AssistantNextStep.CHOOSE_CATEGORY
    assert third.reply.next_step == AssistantNextStep.DESCRIBE_REQUEST
    assert fourth.reply.next_step == AssistantNextStep.SHARE_LOCATION
    assert "ubicación lo más precisa posible" in fourth.reply.reply_text
    assert fifth.reply.next_step == AssistantNextStep.ASK_NAME
    assert sixth.reply.next_step == AssistantNextStep.CONFIRM_CASE
    assert "**Área**" in sixth.reply.reply_text
    assert "**Categoría**" in sixth.reply.reply_text
    assert seventh.reply.next_step == AssistantNextStep.COMPLETE
    assert "Tu reclamo quedó registrado como caso #" in seventh.reply.reply_text
    assert len(agent.calls) == EXPECTED_SERVICE_TURNS

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        cases = await repository.list_municipal_cases(store_id=store_id)
        customer = await repository.get_customer(first.customer.id)
        conversation = await repository.get_or_create_conversation(
            channel=Channel.WHATSAPP,
            external_id="3515550000",
            customer_id=customer.id,
            store_id=store_id,
        )
        draft = await repository.get_municipal_case_draft(conversation_id=conversation.id)

    assert len(cases) == 1
    assert cases[0].area_id == streets_area.id
    assert cases[0].category_id == pothole_category.id
    assert cases[0].description == "Hay un bache grande que rompe las cubiertas"
    assert cases[0].location_text == "San Martín 123 esquina Belgrano"
    assert cases[0].reporter_name == "María"
    assert cases[0].reporter_phone_number == "3515550000"
    assert draft is None

    await runtime.engine.dispose()


async def test_municipal_service_supports_handoff_and_safe_fallbacks(tmp_path: Path):
    """The municipal assistant hands off when requested, unavailable, or unconfigured."""
    settings, runtime = await build_municipal_runtime(tmp_path)
    store_id, areas, categories = await load_municipal_catalog(runtime)
    water_area = next(area for area in areas if area.name == "Solicitud de agua")
    water_category = next(category for category in categories if category.name == "Falta de agua")

    human_service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(MunicipalTurnIntent(requests_human=True)),
    )
    human_result = await human_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-humano",
        message_text="Quiero hablar con una persona",
        model="stub",
        store_id=store_id,
    )
    assert human_result.reply.next_step == AssistantNextStep.HANDOFF
    assert human_result.reply.reply_text == MUNICIPAL_HUMAN_HANDOFF_REPLY

    fallback_service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(TimeoutError("sin respuesta")),
    )
    fallback_result = await fallback_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-timeout",
        message_text="Hola",
        model="stub",
        store_id=store_id,
    )
    assert fallback_result.reply.next_step == AssistantNextStep.HANDOFF
    assert fallback_result.reply.reply_text == MUNICIPAL_MODEL_UNAVAILABLE_REPLY

    generic_location_service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(
            MunicipalTurnIntent(area_id=water_area.id),
            MunicipalTurnIntent(category_id=water_category.id),
            MunicipalTurnIntent(request_summary="No tenemos agua desde anoche"),
        ),
    )
    await generic_location_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-agua",
        message_text="Es por agua",
        model="stub",
        store_id=store_id,
    )
    await generic_location_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-agua",
        message_text="Falta de agua",
        model="stub",
        store_id=store_id,
    )
    generic_location = await generic_location_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-agua",
        message_text="No tenemos agua desde anoche",
        model="stub",
        store_id=store_id,
    )
    assert generic_location.reply.next_step == AssistantNextStep.SHARE_LOCATION
    assert generic_location.reply.reply_text == "¿En qué dirección, barrio o punto de referencia pasa?"

    async with runtime.session_factory() as session:
        areas_rows = (await session.scalars(select(MunicipalArea))).all()
        categories_rows = (await session.scalars(select(MunicipalCategory))).all()
        for category in categories_rows:
            await session.delete(category)
        for area in areas_rows:
            await session.delete(area)
        await session.commit()

    no_catalog_service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(MunicipalTurnIntent(asks_for_catalog=True)),
    )
    no_catalog_result = await no_catalog_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-sin-catalogo",
        message_text="Hola",
        model="stub",
        store_id=store_id,
    )
    assert no_catalog_result.reply.next_step == AssistantNextStep.HANDOFF
    assert no_catalog_result.reply.reply_text == MUNICIPAL_HUMAN_HANDOFF_REPLY

    await runtime.engine.dispose()


async def test_municipal_service_retries_once_and_handles_generic_errors(tmp_path: Path, monkeypatch):
    """The municipal interpreter retries transient errors and falls back on generic failures."""
    base_settings, runtime = await build_municipal_runtime(tmp_path)
    settings = base_settings.model_copy(update={"assistant_model_retry_attempts": 1})
    store_id, areas, categories = await load_municipal_catalog(runtime)
    sleeps: list[float] = []

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr("ruperto.municipal.asyncio.sleep", fake_sleep)

    retry_service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(RuntimeError("transient"), MunicipalTurnIntent(asks_for_catalog=True)),
    )
    retried = await retry_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-retry",
        message_text="Hola",
        model="stub",
        store_id=store_id,
    )
    assert retried.reply.next_step == AssistantNextStep.CHOOSE_AREA
    assert sleeps == [0.25]

    generic_failure_service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=base_settings,
        agent=StubMunicipalAgent(RuntimeError("boom")),
    )
    generic_failure = await generic_failure_service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-error",
        message_text="Hola",
        model="stub",
        store_id=store_id,
    )
    assert generic_failure.reply.next_step == AssistantNextStep.HANDOFF
    assert generic_failure.reply.reply_text == MUNICIPAL_MODEL_UNAVAILABLE_REPLY

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        invalid_area_reply, _ = await retry_service._build_reply(
            repository=repository,
            store_name="Municipio Test",
            customer=CustomerSnapshot(id=1, name="María", phone_number=None, default_address=None),
            draft=MunicipalCaseDraftSnapshot(
                id=9,
                conversation_id=3,
                store_id=store_id,
                customer_id=1,
                area_id=999,
                category_id=None,
                request_summary="Hay un poste roto",
                location_text="Belgrano 200",
                location_reference="Frente a la plaza",
                latitude=None,
                longitude=None,
                awaiting_confirmation=True,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
            areas=areas,
            categories=categories,
            intent=MunicipalTurnIntent(),
            message_text="confirmo",
            conversation_id=3,
        )
    assert invalid_area_reply.next_step == AssistantNextStep.CHOOSE_AREA
    review_text = retry_service._build_case_review_reply(
        customer_name="María",
        draft=MunicipalCaseDraftSnapshot(
            id=10,
            conversation_id=4,
            store_id=store_id,
            customer_id=1,
            area_id=areas[0].id,
            category_id=categories[0].id,
            request_summary="Hay un poste roto",
            location_text="Belgrano 200",
            location_reference="Frente a la plaza",
            latitude=None,
            longitude=None,
            awaiting_confirmation=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        area=areas[0],
        category=categories[0],
    )
    assert "**Referencia**\nFrente a la plaza" in review_text

    await runtime.engine.dispose()


async def test_municipal_service_closes_submission_on_explicit_confirmation_even_if_model_misses_it(tmp_path: Path):
    """A customer confirmation should close the draft even when the model misses the flag."""
    settings, runtime = await build_municipal_runtime(tmp_path)
    store_id, areas, categories = await load_municipal_catalog(runtime)
    water_area = next(area for area in areas if area.name == "Solicitud de agua")
    water_category = next(category for category in categories if category.name == "Falta de agua")

    service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(
            MunicipalTurnIntent(area_id=water_area.id),
            MunicipalTurnIntent(category_id=water_category.id),
            MunicipalTurnIntent(request_summary="Necesito acarreo de agua"),
            MunicipalTurnIntent(location_text="9 de Julio 1302 - Anisacate"),
            MunicipalTurnIntent(citizen_name="Martín Gaitán"),
            MunicipalTurnIntent(),
        ),
    )

    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-agua-confirm",
        message_text="Necesito agua",
        model="stub",
        store_id=store_id,
    )
    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-agua-confirm",
        message_text="Falta de agua",
        model="stub",
        store_id=store_id,
    )
    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-agua-confirm",
        message_text="Necesito acarreo de agua",
        model="stub",
        store_id=store_id,
    )
    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-agua-confirm",
        message_text="9 de Julio 1302 - Anisacate",
        model="stub",
        store_id=store_id,
    )
    review = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-agua-confirm",
        message_text="Martín Gaitán",
        model="stub",
        store_id=store_id,
    )
    confirmed = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-agua-confirm",
        message_text="confirmo",
        model="stub",
        store_id=store_id,
    )

    assert review.reply.next_step == AssistantNextStep.CONFIRM_CASE
    assert "ingreso la solicitud" in review.reply.reply_text.lower()
    assert confirmed.reply.next_step == AssistantNextStep.COMPLETE
    assert "tu solicitud quedó registrada como caso #" in confirmed.reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_municipal_request_kind_changes_review_and_completion_copy(tmp_path: Path):
    """Request-like categories should avoid complaint wording."""
    settings, runtime = await build_municipal_runtime(tmp_path)
    store_id, areas, categories = await load_municipal_catalog(runtime)
    water_area = next(area for area in areas if area.name == "Solicitud de agua")
    water_category = next(category for category in categories if category.name == "Falta de agua")

    assert water_category.request_kind == MunicipalRequestKind.REQUEST

    service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(),
    )
    review_text = service._build_case_review_reply(
        customer_name="Martín",
        draft=MunicipalCaseDraftSnapshot(
            id=1,
            conversation_id=1,
            store_id=store_id,
            customer_id=1,
            area_id=water_area.id,
            category_id=water_category.id,
            request_summary="Necesito agua",
            location_text="9 de Julio 1302",
            location_reference=None,
            latitude=None,
            longitude=None,
            awaiting_confirmation=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        area=water_area,
        category=water_category,
    )

    assert "ingreso la solicitud" in review_text.lower()
    assert "reclamo" not in review_text.lower()

    await runtime.engine.dispose()


async def test_municipal_service_rejects_aggressive_turns_and_requests_a_clean_rephrase(tmp_path: Path):
    """Aggressive wording should trigger a respectful rephrase prompt instead of continuing blindly."""
    settings, runtime = await build_municipal_runtime(tmp_path)
    store_id, areas, _categories = await load_municipal_catalog(runtime)

    service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(MunicipalTurnIntent(asks_for_catalog=True), MunicipalTurnIntent()),
    )

    await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-enojado",
        message_text="quiero hacer un reclamo",
        model="stub",
        store_id=store_id,
    )
    reply = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id="vecino-enojado",
        message_text="la intendenta es una mierda",
        model="stub",
        store_id=store_id,
    )

    assert reply.reply.next_step == AssistantNextStep.DESCRIBE_REQUEST
    assert "sin insultos" in reply.reply.reply_text.lower()
    assert areas[0].name not in reply.reply.reply_text

    await runtime.engine.dispose()


async def test_vague_location_does_not_count_as_valid_location(tmp_path: Path):
    """Generic phrases such as 'la calle' should not close the location step."""
    settings, runtime = await build_municipal_runtime(tmp_path)
    store_id, areas, categories = await load_municipal_catalog(runtime)
    hygiene_area = next(area for area in areas if area.name == "Higiene urbana")
    fallback_category = next(
        category for category in categories if category.area_id == hygiene_area.id and category.is_fallback
    )

    service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(),
    )
    async with runtime.session_factory() as session:
        reply, _created = await service._build_reply(
            repository=BusinessRepository(session),
            store_name="Municipio Test",
            customer=CustomerSnapshot(id=1, name=None, phone_number=None, default_address=None),
            draft=MunicipalCaseDraftSnapshot(
                id=1,
                conversation_id=1,
                store_id=store_id,
                customer_id=1,
                area_id=hygiene_area.id,
                category_id=fallback_category.id,
                request_summary="La calle es un chiquero",
                location_text="la calle",
                location_reference=None,
                latitude=None,
                longitude=None,
                awaiting_confirmation=False,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
            areas=areas,
            categories=categories,
            intent=MunicipalTurnIntent(),
            message_text="la calle es un chiquero",
            conversation_id=1,
        )

    assert reply.next_step == AssistantNextStep.SHARE_LOCATION
    assert "dirección, barrio o punto de referencia" in reply.reply_text.lower()

    await runtime.engine.dispose()


async def test_municipal_completion_uses_first_name_only(tmp_path: Path):
    """Customer-facing confirmation should prefer the first name instead of the full legal name."""
    settings, runtime = await build_municipal_runtime(tmp_path)
    store_id, areas, categories = await load_municipal_catalog(runtime)
    water_area = next(area for area in areas if area.name == "Solicitud de agua")
    water_category = next(category for category in categories if category.name == "Falta de agua")

    service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(),
    )
    completed_text = service._build_case_created_reply(
        customer_name="Tomás Aranda",
        created_case=MunicipalCaseSnapshot(
            id=4,
            store_id=store_id,
            area_id=water_area.id,
            category_id=water_category.id,
            customer_id=1,
            conversation_id=1,
            assignee_staff_user_id=None,
            title="Necesito agua",
            description="Necesito agua",
            reporter_name="Tomás Aranda",
            reporter_phone_number=None,
            location_text="9 de Julio 1302",
            location_reference=None,
            latitude=None,
            longitude=None,
            status=MunicipalCaseStatus.NEW,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        area=water_area,
        category=water_category,
    )

    assert completed_text.startswith("Listo, Tomás.")
    assert "Tomás Aranda" not in completed_text

    await runtime.engine.dispose()


async def test_municipal_helper_methods_cover_location_and_rephrase_edges(tmp_path: Path):
    """Helper methods keep municipal copy and location heuristics grounded."""
    settings, runtime = await build_municipal_runtime(tmp_path)
    store_id, areas, categories = await load_municipal_catalog(runtime)
    service = MunicipalAssistantService(
        session_factory=runtime.session_factory,
        settings=settings,
        agent=StubMunicipalAgent(),
    )
    water_category = next(category for category in categories if category.name == "Falta de agua")
    now = datetime.now(UTC)

    coordinate_draft = MunicipalCaseDraftSnapshot(
        id=1,
        conversation_id=1,
        store_id=store_id,
        customer_id=1,
        area_id=areas[0].id,
        category_id=water_category.id,
        request_summary="Necesito agua",
        location_text=None,
        location_reference=None,
        latitude=-31.4,
        longitude=-64.2,
        awaiting_confirmation=False,
        created_at=now,
        updated_at=now,
    )
    assert service._draft_has_location(coordinate_draft) is True
    assert service._display_name(None) is None
    assert service._display_name("   ") is None
    assert service._location_text_is_specific_enough("abc") is False
    assert service._location_text_is_specific_enough("ruta provincial 5") is True
    assert service._location_text_is_specific_enough("pasaje central - barrio norte") is True
    assert service._location_text_is_specific_enough("frente a la plaza") is True
    assert "sin insultos" in service._build_respectful_rephrase_reply(draft=coordinate_draft).lower()
    assert "sin insultos" in service._build_respectful_rephrase_reply(draft=None).lower()

    await runtime.engine.dispose()
