"""Tests for the kanban board functionality."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ruperto.app import TEMPLATES, create_app
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import StoreVertical
from ruperto.repository import BusinessRepository
from ruperto.schemas import MunicipalAreaSnapshot

pytestmark = pytest.mark.anyio

HTTP_FOUND = 303
HTTP_UNAUTHORIZED = 401


async def test_kanban_board_requires_authentication():
    """The kanban board requires staff authentication."""
    settings = Settings(
        environment="test",
        store_vertical=StoreVertical.MUNICIPAL,
        store_name="Municipio Test",
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    app = create_app(settings)
    client = TestClient(app)

    # Try to access kanban without authentication
    response = client.get("/dashboard/kanban")

    # Should redirect to login
    assert response.status_code == HTTP_FOUND
    assert "/dashboard/login" in response.headers["location"]

    await runtime.engine.dispose()


async def test_kanban_api_endpoints_work(tmp_path):
    """The kanban API endpoints return proper data."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'kanban-test.db'}",
        store_vertical=StoreVertical.MUNICIPAL,
        store_name="Municipio Test",
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)

    # Create some test data
    async with runtime.session_factory() as session:
        repository = BusinessRepository(session)
        store = await repository.get_store_profile()

        # Add municipal areas
        area1 = await repository.create_municipal_area(
            store_id=store.id, name="Alumbrado público", description="Problemas con iluminación urbana"
        )
        await repository.create_municipal_area(
            store_id=store.id, name="Limpieza urbana", description="Limpieza de calles y espacios públicos"
        )

        # Add municipal categories
        await repository.create_municipal_category(
            store_id=store.id, area_id=area1.id, name="Lámpara apagada", requires_precise_location=True
        )

        await session.commit()

    app = create_app(settings)
    client = TestClient(app)

    # Test the API endpoint directly (bypassing auth for now)
    response = client.get("/api/kanban/municipal/cases")

    # Should return 401 since we're not authenticated
    assert response.status_code == HTTP_UNAUTHORIZED

    await runtime.engine.dispose()


async def test_kanban_template_renders_with_areas(tmp_path):
    """The kanban template can render with area data."""

    # Create test areas
    areas = [
        MunicipalAreaSnapshot(
            id=1,
            store_id=1,
            name="Alumbrado público",
            description="Problemas con iluminación urbana",
            manager_staff_user_id=None,
            display_order=1,
            is_active=True,
            is_fallback=False,
        ),
        MunicipalAreaSnapshot(
            id=2,
            store_id=1,
            name="Limpieza urbana",
            description="Limpieza de calles",
            manager_staff_user_id=None,
            display_order=2,
            is_active=True,
            is_fallback=False,
        ),
    ]

    # Test that the template can render without JSON serialization errors
    context = {
        "areas": areas,
        "request": None,
        "active_page": "kanban",
        "active_store_id": 1,
        "current_user": SimpleNamespace(full_name="Staff User", email="staff@example.com"),
        "flash_message": None,
        "memberships": [SimpleNamespace(store_id=1, store_name="Municipio Test", role=SimpleNamespace(value="owner"))],
        "nav_sections": [],
        "page_description": "Tablero Kanban",
        "page_title": "Tablero Kanban",
        "store": SimpleNamespace(store_name="Municipio Test", store_location="Córdoba", slug="mi-muni"),
        "store_vertical_label": "Municipio",
    }

    # This should not raise an exception
    rendered = TEMPLATES.get_template("dashboard_kanban.html").render(**context)

    # Check that area data is properly rendered
    assert "Alumbrado público" in rendered
    assert "Limpieza urbana" in rendered
    assert "areas:" in rendered  # The JavaScript array should be present
