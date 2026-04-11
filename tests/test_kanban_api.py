"""Tests for the kanban API endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.requests import Request as StarletteRequest

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
HTTP_NOT_FOUND = 404
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


def build_request(app, *, path: str, session: dict[str, int] | None = None) -> StarletteRequest:
    """Build a direct request object for route endpoint coverage."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "PATCH",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
        "session": session or {},
    }
    return StarletteRequest(scope)


def get_route_endpoint(path: str, method: str):
    """Return the route endpoint registered for one kanban path and method."""
    for route in router.routes:
        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", None)
        if route_path == path and route_methods is not None and method in route_methods:
            return cast(Any, route).endpoint
    msg = f"Could not find route {method} {path!r}"
    raise AssertionError(msg)


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


async def test_kanban_api_lists_cases_for_authenticated_dashboard_user(tmp_path):
    """Authenticated staff can list kanban cases scoped to the active store."""
    settings = build_kanban_settings(tmp_path, "kanban-list-auth-test.db")
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile()
        area = next(
            area
            for area in await repository.list_municipal_areas(store_id=store.id)
            if area.name == "Solicitud de agua"
        )
        await repository.create_municipal_case(
            store_id=store.id,
            payload=MunicipalCaseCreateRequest(
                area_id=area.id,
                title="Falta de agua en lote 8",
                description="La presión bajó por completo.",
            ),
        )
        await session.commit()

    app = create_app(settings)
    with TestClient(app) as client:
        client.post(
            "/dashboard/login",
            data={"email": "staff@example.com", "password": "super-secret", "next": "/dashboard/kanban"},
            follow_redirects=False,
        )
        response = client.get("/api/kanban/municipal/cases")

    assert response.status_code == HTTP_OK
    assert len(response.json()) == 1
    await runtime.engine.dispose()


async def test_kanban_api_returns_not_found_when_updating_unknown_case(tmp_path):
    """Unknown municipal cases return a 404 from the kanban status endpoint."""
    settings = build_kanban_settings(tmp_path, "kanban-missing-case-test.db")
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    app = create_app(settings)
    with TestClient(app) as client:
        client.post(
            "/dashboard/login",
            data={"email": "staff@example.com", "password": "super-secret", "next": "/dashboard/kanban"},
            follow_redirects=False,
        )
        response = client.patch("/api/kanban/municipal/cases/999/status", json={"status": "triaged"})

    assert response.status_code == HTTP_NOT_FOUND
    assert response.json()["detail"] == "Municipal case not found."
    await runtime.engine.dispose()


async def test_kanban_route_endpoint_commits_success_and_not_found_paths(tmp_path):
    """Direct route calls cover the kanban commit and not-found branches under Python 3.13."""
    settings = build_kanban_settings(tmp_path, "kanban-direct-coverage.db")
    app = create_app(settings)

    with TestClient(app):
        runtime = app.state.runtime
        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            staff_user = await repository.get_staff_user_by_email("staff@example.com")
            assert staff_user is not None
            session_data = {"dashboard_staff_user_id": staff_user.id, "dashboard_store_id": 1}
            store = await repository.get_store_profile()
            area = next(
                area
                for area in await repository.list_municipal_areas(store_id=store.id)
                if area.name == "Solicitud de agua"
            )
            created_case = await repository.create_municipal_case(
                store_id=store.id,
                payload=MunicipalCaseCreateRequest(
                    area_id=area.id,
                    title="Cobertura directa",
                    description="Cubrir commit del endpoint",
                ),
            )
            await session.commit()

        update_case_status = get_route_endpoint("/api/kanban/municipal/cases/{case_id}/status", "PATCH")
        updated_case = await update_case_status(
            request=build_request(
                app,
                path=f"/api/kanban/municipal/cases/{created_case.id}/status",
                session=session_data,
            ),
            case_id=created_case.id,
            payload=MunicipalCaseStatusUpdateRequest(status=MunicipalCaseStatus.TRIAGED),
            identity={"store_id": 1},
        )

        assert updated_case.status == MunicipalCaseStatus.TRIAGED

        async with runtime.database.session_factory() as session:
            repository = BusinessRepository(session)
            stored_case = await repository.get_municipal_case(created_case.id)
            assert stored_case.status == MunicipalCaseStatus.TRIAGED

        with pytest.raises(Exception) as error_info:
            await update_case_status(
                request=build_request(app, path="/api/kanban/municipal/cases/999/status", session=session_data),
                case_id=999,
                payload=MunicipalCaseStatusUpdateRequest(status=MunicipalCaseStatus.TRIAGED),
                identity={"store_id": 1},
            )

    assert "Municipal case not found." in str(error_info.value)
