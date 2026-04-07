"""Dashboard utility functions."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request

from ruperto.repository import BusinessRepository

SESSION_STAFF_USER_ID_KEY = "dashboard_staff_user_id"
SESSION_STORE_ID_KEY = "dashboard_store_id"


def parse_session_int(value: object) -> int | None:
    """Parse one integer-like value stored in the signed session cookie."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


async def load_dashboard_identity(request: Request) -> Mapping[str, object] | None:
    """Resolve the current signed-in dashboard user from the session."""
    runtime = request.app.state.runtime
    staff_user_id = parse_session_int(request.session.get(SESSION_STAFF_USER_ID_KEY))
    if staff_user_id is None:
        return None

    async with runtime.database.session_factory() as session:
        repository = BusinessRepository(session)
        staff_user = await repository.get_staff_user_by_id(staff_user_id)
        if staff_user is None or not staff_user.is_active:
            request.session.clear()
            return None
        memberships = await repository.list_store_memberships_for_staff_user(staff_user_id)
        if not memberships:
            request.session.clear()
            return None

    requested_store_id = parse_session_int(request.session.get(SESSION_STORE_ID_KEY))
    available_store_ids = {membership.store_id for membership in memberships}

    if requested_store_id is not None and requested_store_id in available_store_ids:
        active_store_id = requested_store_id
    else:
        active_store_id = next(iter(available_store_ids))
        request.session[SESSION_STORE_ID_KEY] = active_store_id

    return {
        "staff_user_id": staff_user_id,
        "active_store_id": active_store_id,
        "store_id": active_store_id,
    }
