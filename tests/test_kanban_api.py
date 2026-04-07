"""Tests for the kanban API endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ruperto.api.kanban import router
from ruperto.app import create_app
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import MunicipalCaseStatus, StoreVertical
from ruperto.repository import BusinessRepository
from ruperto.schemas import MunicipalCaseCreateRequest, MunicipalCaseStatusUpdateRequest

pytestmark = pytest.mark.anyio

HTTP_OK = 200
HTTP_FOUND = 303
HTTP_UNAUTHORIZED = 401
ROUTE_COUNT = 2


def build_kanban_settings(tmp_path: Path, database_name: str) -> Settings:
    """Build isolated settings for kanban API tests."""
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / database_name}",
        store_vertical=StoreVertical.MUNICIPAL,
        store_name="Municipio Test",
        dashboard_session_secret="test-session-secret",
        dashboard_admin_email="staff@example.com",
        dashboard_admin_password=SecretStr("super-secret"),
        dashboard_admin_name="Staff User",
    )


async def test_kanban_api_list_cases_requires_authentication(tmp_path):
    """The kanban API requires authentication."""
    settings = build_kanban_settings(tmp_path, "kanban-api-test.db")
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/kanban/municipal/cases")

    assert response.status_code == HTTP_UNAUTHORIZED
    assert "Unauthorized" in response.text

    await runtime.engine.dispose()


async def test_kanban_api_update_case_status_requires_authentication(tmp_path):
    """The kanban API update endpoint requires authentication."""
    settings = build_kanban_settings(tmp_path, "kanban-update-test.db")
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.patch("/api/kanban/municipal/cases/999/status", json={"status": "in_progress"})

    assert response.status_code == HTTP_UNAUTHORIZED

    await runtime.engine.dispose()


async def test_kanban_api_endpoints_have_correct_prefix():
    """The kanban API endpoints are properly configured."""
    assert router.prefix == "/api/kanban"
    assert router.tags == ["kanban"]

    routes = list(router.routes)
    assert len(routes) == ROUTE_COUNT

    get_route = None
    patch_route = None
    for route in routes:
        if "GET" in route.methods and route.path == "/api/kanban/municipal/cases":
            get_route = route
        if "PATCH" in route.methods and "{case_id}" in route.path:
            patch_route = route

    assert get_route is not None
    assert patch_route is not None
    assert "/api/kanban/municipal/cases" in get_route.path
    assert "/api/kanban/municipal/cases/{case_id}/status" in patch_route.path


async def test_kanban_api_update_case_status_persists_for_authenticated_dashboard_user(tmp_path):
    """Authenticated kanban updates must persist the new municipal status."""
    settings = build_kanban_settings(tmp_path, "kanban-update-persist-test.db")
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile()
        areas = await repository.list_municipal_areas(store_id=store.id)
        water_area = next(area for area in areas if area.name == "Solicitud de agua")
        created_case = await repository.create_municipal_case(
            store_id=store.id,
            payload=MunicipalCaseCreateRequest(
                area_id=water_area.id,
                title="Falta de agua en manzana 4",
                description="No hay presión desde anoche.",
                reporter_name="Nora",
            ),
        )
        await session.commit()

    app = create_app(settings)
    with TestClient(app) as client:
        login_response = client.post(
            "/dashboard/login",
            data={"email": "staff@example.com", "password": "super-secret", "next": "/dashboard/kanban"},
            follow_redirects=False,
        )
        assert login_response.status_code == HTTP_FOUND

        response = client.patch(
            f"/api/kanban/municipal/cases/{created_case.id}/status",
            json=MunicipalCaseStatusUpdateRequest(status=MunicipalCaseStatus.IN_PROGRESS).model_dump(mode="json"),
        )

    assert response.status_code == HTTP_OK
    assert response.json()["status"] == "in_progress"

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        stored_case = await repository.get_municipal_case(created_case.id)

    assert stored_case.status == MunicipalCaseStatus.IN_PROGRESS
    await runtime.engine.dispose()
