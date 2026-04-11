"""Tests for dashboard utility functions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from pydantic import SecretStr
from sqlalchemy import select

from ruperto.config import Settings
from ruperto.dashboard_utils import load_dashboard_identity, parse_session_int
from ruperto.db import create_database_runtime, init_database
from ruperto.models import StaffUser, StoreMembership

pytestmark = pytest.mark.anyio
ACTIVE_STORE_ID = 1
INVALID_STORE_ID = 999
SESSION_INT_FIVE = 5
SESSION_INT_SEVEN = 7


async def build_runtime(tmp_path: Path):
    """Create a dashboard runtime backed by a temporary database."""
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'dashboard-utils.db'}",
        dashboard_admin_email="staff@example.com",
        dashboard_admin_password=SecretStr("super-secret"),
        dashboard_admin_name="Staff User",
        kapso_api_key=None,
        kapso_phone_number_id=None,
        kapso_webhook_secret=None,
    )
    runtime = create_database_runtime(settings)
    await init_database(settings=settings, runtime=runtime)
    return settings, runtime


async def test_parse_session_int_accepts_strings_and_rejects_other_values():
    """Session parsing accepts integer-like strings only."""
    assert parse_session_int(SESSION_INT_FIVE) == SESSION_INT_FIVE
    assert parse_session_int(str(SESSION_INT_SEVEN)) == SESSION_INT_SEVEN
    assert parse_session_int("x7") is None
    assert parse_session_int(None) is None


async def test_load_dashboard_identity_clears_session_for_inactive_user(tmp_path: Path):
    """Inactive staff users lose their dashboard session."""
    settings, runtime = await build_runtime(tmp_path)
    async with runtime.session_factory() as session:
        staff_user = await session.scalar(
            select(StaffUser).where(StaffUser.email == (settings.dashboard_admin_email or ""))
        )
        assert staff_user is not None
        staff_user.is_active = False
        await session.commit()

    request = cast(
        Request,
        SimpleNamespace(
            session={"dashboard_staff_user_id": 1},
            app=SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(database=runtime))),
        ),
    )
    identity = await load_dashboard_identity(request)

    assert identity is None
    assert request.session == {}
    await runtime.engine.dispose()


async def test_load_dashboard_identity_resets_invalid_store_scope(tmp_path: Path):
    """Invalid active store ids fall back to one available membership."""
    _settings, runtime = await build_runtime(tmp_path)
    request = cast(
        Request,
        SimpleNamespace(
            session={"dashboard_staff_user_id": ACTIVE_STORE_ID, "dashboard_store_id": INVALID_STORE_ID},
            app=SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(database=runtime))),
        ),
    )

    identity = await load_dashboard_identity(request)

    assert identity is not None
    assert request.session["dashboard_store_id"] == ACTIVE_STORE_ID
    assert identity["store_id"] == ACTIVE_STORE_ID
    await runtime.engine.dispose()


async def test_load_dashboard_identity_clears_session_without_memberships(tmp_path: Path):
    """Staff sessions are dropped when the user no longer belongs to any store."""
    settings, runtime = await build_runtime(tmp_path)
    async with runtime.session_factory() as session:
        staff_user = await session.scalar(
            select(StaffUser).where(StaffUser.email == (settings.dashboard_admin_email or ""))
        )
        assert staff_user is not None
        memberships = (
            await session.scalars(select(StoreMembership).where(StoreMembership.staff_user_id == staff_user.id))
        ).all()
        for membership in memberships:
            await session.delete(membership)
        await session.commit()

    request = cast(
        Request,
        SimpleNamespace(
            session={"dashboard_staff_user_id": staff_user.id},
            app=SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(database=runtime))),
        ),
    )

    identity = await load_dashboard_identity(request)

    assert identity is None
    assert request.session == {}
    await runtime.engine.dispose()
